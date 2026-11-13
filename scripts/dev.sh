#!/usr/bin/env bash
#
# Clams dev supervisor
# -------------------
# Start / stop / status for the two local dev processes:
#   * backend  : Clams HTTP + SSE API  (clams serve)   -> 127.0.0.1:8200
#   * frontend : Vite dev server        (npm run dev)   -> 0.0.0.0:5173
#
# Usage:
#   scripts/dev.sh start     Start both (backend first, then frontend)
#   scripts/dev.sh stop      Stop both
#   scripts/dev.sh status    Show what is running
#   scripts/dev.sh restart   Stop then start
#   scripts/dev.sh logs      Follow both logs (Ctrl-C to stop following)
#   scripts/dev.sh logs backend|frontend   Follow one log
#
# Ports are overridable via env:
#   CLAMS_DEV_BACKEND_PORT (8200)  CLAMS_DEV_FRONTEND_PORT (5173)
#   CLAMS_DEV_BACKEND_HOST (127.0.0.1)  CLAMS_DEV_FRONTEND_HOST (0.0.0.0)
#
set -euo pipefail

# --- resolve repo root from this script's location, so it works from anywhere ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

BACKEND_HOST="${CLAMS_DEV_BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${CLAMS_DEV_BACKEND_PORT:-8200}"
FRONTEND_HOST="${CLAMS_DEV_FRONTEND_HOST:-0.0.0.0}"
FRONTEND_PORT="${CLAMS_DEV_FRONTEND_PORT:-5173}"

PY=".venv/bin/python"
FRONTEND_DIR="frontend"

RUN_DIR=".dev"
BACKEND_PID="$RUN_DIR/backend.pid"
FRONTEND_PID="$RUN_DIR/frontend.pid"
BACKEND_LOG="$RUN_DIR/backend.log"
FRONTEND_LOG="$RUN_DIR/frontend.log"

BACKEND_URL="http://${BACKEND_HOST}:${BACKEND_PORT}"
FRONTEND_URL="http://127.0.0.1:${FRONTEND_PORT}"

# --- colors (only when stdout is a tty) ---
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_GREEN=$'\033[32m'; C_RED=$'\033[31m'
  C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_DIM=$'\033[2m'
else
  C_RESET=""; C_GREEN=""; C_RED=""; C_YELLOW=""; C_BLUE=""; C_DIM=""
fi

ok()   { printf '%s\n' "${C_GREEN}✓ $*${C_RESET}"; }
warn() { printf '%s\n' "${C_YELLOW}! $*${C_RESET}"; }
err()  { printf '%s\n' "${C_RED}✗ $*${C_RESET}" >&2; }
info() { printf '%s\n' "${C_BLUE}› $*${C_RESET}"; }

# --- helpers ---------------------------------------------------------------

# pid_alive <pid>  -> 0 if a live process with that pid exists
pid_alive() {
  local pid="${1:-}"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

# read_pid <pidfile> -> echoes the pid if the file exists and holds a number
read_pid() {
  local f="$1"
  if [ -f "$f" ]; then
    local pid
    pid="$(cat "$f" 2>/dev/null || true)"
    if printf '%s' "$pid" | grep -Eq '^[0-9]+$'; then
      printf '%s' "$pid"
      return 0
    fi
  fi
  return 1
}

# http_ok <url> -> 0 if an HTTP request succeeds (any 2xx/3xx)
http_ok() {
  local url="$1"
  curl -sf -o /dev/null --max-time 3 "$url" 2>/dev/null
}

# wait_http <url> <timeout_secs> -> wait until http_ok or timeout
wait_http() {
  local url="$1" timeout="${2:-20}" waited=0
  while [ "$waited" -lt "$timeout" ]; do
    if http_ok "$url"; then return 0; fi
    sleep 1
    waited=$((waited + 1))
  done
  return 1
}

# launch_detached <logfile> <cmd...>
# Starts a command in a new session (process group), appending its stdout+stderr
# to <logfile>, and echoes its pid. Killing the negative pid kills the whole group.
launch_detached() {
  local logfile="$1"
  shift
  : > "$logfile"
  setsid "$@" >> "$logfile" 2>&1 < /dev/null &
  local pid=$!
  # Give setsid a moment to exec so $! is the real leader.
  sleep 0.2
  printf '%s' "$pid"
}

# --- process control -------------------------------------------------------

stop_one() {
  # stop_one <name> <pidfile>
  local name="$1" pidfile="$2"
  local pid
  pid="$(read_pid "$pidfile" || true)"
  if [ -z "$pid" ] || ! pid_alive "$pid"; then
    info "$name was not running."
    rm -f "$pidfile"
    return 0
  fi
  info "Stopping $name (pid $pid)…"
  # Kill the whole process group first (covers vite's node children).
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  # Wait up to 5s for graceful shutdown.
  local waited=0
  while pid_alive "$pid" && [ "$waited" -lt 5 ]; do
    sleep 1
    waited=$((waited + 1))
  done
  if pid_alive "$pid"; then
    warn "$name did not exit; sending SIGKILL."
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
  ok "$name stopped."
}

start_backend() {
  local pid
  pid="$(read_pid "$BACKEND_PID" || true)"
  if [ -n "$pid" ] && pid_alive "$pid"; then
    ok "Backend already running (pid $pid) at $BACKEND_URL"
    return 0
  fi
  if [ ! -x "$PY" ]; then
    err "Python not found at $PY. Create the venv first: make venv && make install"
    exit 1
  fi
  mkdir -p "$RUN_DIR"
  info "Starting backend (clams serve) on $BACKEND_URL …"
  pid="$(launch_detached "$BACKEND_LOG" "$PY" -m clams.cli serve --host "$BACKEND_HOST" --port "$BACKEND_PORT")"
  printf '%s' "$pid" > "$BACKEND_PID"
  if wait_http "$BACKEND_URL/healthz" 20; then
    ok "Backend up (pid $pid) — $BACKEND_URL"
  else
    err "Backend did not become healthy in time. Check $BACKEND_LOG"
    exit 1
  fi
}

start_frontend() {
  local pid
  pid="$(read_pid "$FRONTEND_PID" || true)"
  if [ -n "$pid" ] && pid_alive "$pid"; then
    ok "Frontend already running (pid $pid) at $FRONTEND_URL"
    return 0
  fi
  if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    err "frontend/node_modules missing. Run: (cd frontend && npm install)"
    exit 1
  fi
  mkdir -p "$RUN_DIR"
  info "Starting frontend (vite) on port $FRONTEND_PORT …"
  pid="$(launch_detached "$FRONTEND_LOG" bash -c "cd '$FRONTEND_DIR' && exec npm run dev -- --host '$FRONTEND_HOST' --port '$FRONTEND_PORT'")"
  printf '%s' "$pid" > "$FRONTEND_PID"
  if wait_http "$FRONTEND_URL" 20; then
    ok "Frontend up (pid $pid) — $FRONTEND_URL"
  else
    err "Frontend did not respond in time. Check $FRONTEND_LOG"
    exit 1
  fi
}

# --- commands --------------------------------------------------------------

cmd_start() {
  info "Starting Clams dev stack…"
  start_backend
  start_frontend
  echo
  ok "Both services started."
  echo "  Frontend: ${FRONTEND_URL}"
  echo "  Backend : ${BACKEND_URL}"
  echo "  Logs    : ${BACKEND_LOG} , ${FRONTEND_LOG}"
}

cmd_stop() {
  stop_one "frontend" "$FRONTEND_PID"
  stop_one "backend"  "$BACKEND_PID"
}

cmd_restart() {
  cmd_stop
  echo
  cmd_start
}

cmd_status() {
  local any_running=0
  local bpid fpid

  printf '%s\n' "${C_BLUE}Clams dev stack${C_RESET}"

  bpid="$(read_pid "$BACKEND_PID" || true)"
  if [ -n "$bpid" ] && pid_alive "$bpid"; then
    if http_ok "$BACKEND_URL/healthz"; then
      printf '  %s %s(pid %s, healthy)%s  %s\n' "${C_GREEN}● backend${C_RESET}" "" "$bpid" "" "$BACKEND_URL"
    else
      printf '  %s %s(pid %s, %snot responding%s)%s  %s\n' "${C_YELLOW}● backend${C_RESET}" "" "$bpid" "$C_RED" "" "$BACKEND_URL"
      any_running=1
    fi
  else
    printf '  %s\n' "${C_DIM}○ backend  stopped${C_RESET}"
  fi

  fpid="$(read_pid "$FRONTEND_PID" || true)"
  if [ -n "$fpid" ] && pid_alive "$fpid"; then
    if http_ok "$FRONTEND_URL"; then
      printf '  %s %s(pid %s, healthy)%s  %s\n' "${C_GREEN}● frontend${C_RESET}" "" "$fpid" "" "$FRONTEND_URL"
    else
      printf '  %s %s(pid %s, %snot responding%s)%s  %s\n' "${C_YELLOW}● frontend${C_RESET}" "" "$fpid" "$C_RED" "" "$FRONTEND_URL"
    fi
  else
    printf '  %s\n' "${C_DIM}○ frontend stopped${C_RESET}"
  fi

  # Redis is needed for full pipeline features (optional for browsing).
  if (exec 3<>"/dev/tcp/127.0.0.1/6379") 2>/dev/null; then
    exec 3>&- 3<&- || true
    printf '  %s\n' "${C_GREEN}● redis    listening (6379)${C_RESET}"
  else
    printf '  %s\n' "${C_YELLOW}○ redis    not running (needed for full pipeline features)${C_RESET}"
  fi
}

cmd_logs() {
  local target="${1:-both}"
  case "$target" in
    backend)  tail -n 50 -f "$BACKEND_LOG" ;;
    frontend) tail -n 50 -f "$FRONTEND_LOG" ;;
    both|"")  tail -n 30 -f "$BACKEND_LOG" "$FRONTEND_LOG" ;;
    *) err "Unknown logs target: $target (use: backend | frontend | both)"; exit 1 ;;
  esac
}

usage() {
  cat <<EOF
Clams dev supervisor

Usage: scripts/dev.sh <command>

Commands:
  start            Start backend then frontend
  stop             Stop both services
  restart          Stop then start
  status           Show running state of backend, frontend and redis
  logs [target]    Follow logs (target: backend | frontend | both; default both)

Environment overrides:
  CLAMS_DEV_BACKEND_HOST   (default 127.0.0.1)
  CLAMS_DEV_BACKEND_PORT   (default 8200)
  CLAMS_DEV_FRONTEND_HOST  (default 0.0.0.0)
  CLAMS_DEV_FRONTEND_PORT  (default 5173)
EOF
}

main() {
  local cmd="${1:-}"
  case "$cmd" in
    start)   shift; cmd_start "$@" ;;
    stop)    shift; cmd_stop "$@" ;;
    restart) shift; cmd_restart "$@" ;;
    status)  shift; cmd_status "$@" ;;
    logs)    shift; cmd_logs "$@" ;;
    ""|-h|--help|help) usage ;;
    *) err "Unknown command: $cmd"; echo; usage; exit 1 ;;
  esac
}

main "$@"
