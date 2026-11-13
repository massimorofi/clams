#!/usr/bin/env bash
#
# Clams dev supervisor (issue #19)
# -------------------------------
# Starts the five components of a Clams deployment:
#
#   1. backend      Clams HTTP + SSE API   (clams serve)      127.0.0.1:8200
#   2. frontend   Vite dev server        (npm run dev)      0.0.0.0:5173
#   3. planner    Claudio agent, planner role              127.0.0.1:1072
#   4. executor   Claudio agent, executor role             127.0.0.1:1073
#   5. orchestrator  Clams control loop  (clams watch)     (no port)
#
# plus redis, the event bus everything depends on.
#
# The two Claudio agents run as Docker containers because Claudio is not
# installed natively here. Their ports are 1072/1073 by deliberate choice:
# never Claudio's stock 8100, so a local stock instance can never collide
# with the Clams deployment (issue #19).
#
# Usage:
#   scripts/dev.sh start             Start everything (idempotent)
#   scripts/dev.sh stop              Stop everything
#   scripts/dev.sh status            Per-component status table
#   scripts/dev.sh restart           Stop then start
#   scripts/dev.sh logs              Follow all logs (Ctrl-C to stop)
#   scripts/dev.sh logs <component>  Follow one component's log
#   scripts/dev.sh agents            Show the agent registry + probe each
#
# Ports are overridable via env:
#   CLAMS_DEV_BACKEND_PORT (8200)   CLAMS_DEV_BACKEND_HOST (127.0.0.1)
#   CLAMS_DEV_FRONTEND_PORT (5173)  CLAMS_DEV_FRONTEND_HOST (0.0.0.0)
#   CLAMS_PLANNER_AGENT_PORT (1072) CLAMS_EXECUTOR_AGENT_PORT (1073)
#   CLAMS_AGENT_IMAGE (claudio/agent:latest)
#   CLAMS_AGENT_AUTH_TOKEN (dev-token-change-me)
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
PLANNER_PORT="${CLAMS_PLANNER_AGENT_PORT:-1072}"
EXECUTOR_PORT="${CLAMS_EXECUTOR_AGENT_PORT:-1073}"
AGENT_IMAGE="${CLAMS_AGENT_IMAGE:-claudio/agent:latest}"
AGENT_TOKEN="${CLAMS_AGENT_AUTH_TOKEN:-dev-token-change-me}"
REDIS_PORT="${CLAMS_REDIS_PORT:-6379}"

PY=".venv/bin/python"
FRONTEND_DIR="frontend"
COMPOSE_FILE="docker-compose.yml"
RUN_DIR=".dev"

BACKEND_URL="http://${BACKEND_HOST}:${BACKEND_PORT}"
FRONTEND_URL="http://127.0.0.1:${FRONTEND_PORT}"

# The five managed components, in start order.
COMPONENTS="backend frontend planner_agent executor_agent orchestrator"

# Container names for the two Claudio agent instances.
PLANNER_CONTAINER="clams-agent-planner"
EXECUTOR_CONTAINER="clams-agent-executor"

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

pid_file()  { printf '%s/%s.pid'  "$RUN_DIR" "$1"; }
log_file()  { printf '%s/%s.log'  "$RUN_DIR" "$1"; }

pid_alive() {
  local pid="${1:-}"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

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

http_ok() {
  local url="$1"
  curl -sf -o /dev/null --max-time 3 "$url" 2>/dev/null
}

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
# Starts a command in a new session (process group), appending stdout+stderr to
# <logfile>, and echoes its pid. Killing the negative pid kills the whole group.
launch_detached() {
  local logfile="$1"
  shift
  : > "$logfile"
  setsid "$@" >> "$logfile" 2>&1 < /dev/null &
  local pid=$!
  sleep 0.2
  printf '%s' "$pid"
}

# stop_pid <name> <pidfile>
stop_pid() {
  local name="$1" pidfile="$2"
  local pid
  pid="$(read_pid "$pidfile" || true)"
  if [ -z "$pid" ] || ! pid_alive "$pid"; then
    info "$name was not running."
    rm -f "$pidfile"
    return 0
  fi
  info "Stopping $name (pid $pid)…"
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
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


# --- redis -----------------------------------------------------------------

start_redis() {
  if (echo > "/dev/tcp/127.0.0.1/${REDIS_PORT}") >/dev/null 2>&1; then
    ok "Redis already reachable on :${REDIS_PORT}"
    return 0
  fi
  info "Starting redis via docker compose…"
  docker compose -f "$COMPOSE_FILE" up -d redis >/dev/null 2>&1 || {
    err "Could not start redis. Is docker running?"
    exit 1
  }
  local waited=0
  while [ "$waited" -lt 20 ]; do
    if (echo > "/dev/tcp/127.0.0.1/${REDIS_PORT}") >/dev/null 2>&1; then
      ok "Redis up on :${REDIS_PORT}"
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  err "Redis did not become reachable on :${REDIS_PORT}"
  exit 1
}

# --- claudio agent containers ---------------------------------------------

# start_claudio_agent <name> <port> <role>
#
# Runs a Claudio agent container bound to loopback on <port>. Reuses Claudio's
# own default configuration; only the port, the auth token and the workspace
# are set explicitly, per issue #19.
start_claudio_agent() {
  local name="$1" port="$2" role="$3"
  local url="http://127.0.0.1:${port}"

  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$name"; then
    if http_ok "${url}/healthz"; then
      ok "${role} agent already running (${name}) at ${url}"
      return 0
    fi
    warn "${name} is running but not healthy yet; waiting…"
    if wait_http "${url}/healthz" 60; then
      ok "${role} agent healthy at ${url}"
      return 0
    fi
    err "${role} agent never became healthy. Check: docker logs ${name}"
    exit 1
  fi

  if ! docker image inspect "$AGENT_IMAGE" >/dev/null 2>&1; then
    err "Image ${AGENT_IMAGE} not found. Pull or build it first."
    exit 1
  fi

  info "Starting ${role} agent (${name}) on ${url}…"
  local workdir
  workdir="$(pwd)/.dev/agents/${role}"
  mkdir -p "$workdir"

  docker run -d \
    --name "$name" \
    --restart unless-stopped \
    --init \
    -p "127.0.0.1:${port}:${port}" \
    -e "CLAUDIO__SERVER__HOST=0.0.0.0" \
    -e "CLAUDIO__SERVER__PORT=${port}" \
    -e "CLAUDIO__SERVER__AUTH_TOKEN=${AGENT_TOKEN}" \
    -e "CLAUDIO__AGENT__DEFAULT_WORKSPACE_DIR=/workspace" \
    -e "CLAMS_AGENT_ROLE=${role}" \
    -v "${workdir}:/workspace" \
    "$AGENT_IMAGE" >/dev/null || {
      err "Failed to start ${role} agent container."
      exit 1
    }

  if wait_http "${url}/healthz" 90; then
    ok "${role} agent up at ${url}"
  else
    err "${role} agent did not become healthy in 90s. Check: docker logs ${name}"
    exit 1
  fi
}

stop_container() {
  local name="$1"
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$name"; then
    info "Stopping container ${name}…"
    docker rm -f "$name" >/dev/null 2>&1 || true
    ok "${name} stopped."
  else
    info "${name} was not running."
  fi
}

# --- native clams processes -----------------------------------------------

start_backend() {
  local pidfile; pidfile="$(pid_file backend)"
  local pid
  pid="$(read_pid "$pidfile" || true)"
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
  pid="$(launch_detached "$(log_file backend)" \
        "$PY" -m clams.cli serve --host "$BACKEND_HOST" --port "$BACKEND_PORT")"
  printf '%s' "$pid" > "$pidfile"
  if wait_http "$BACKEND_URL/healthz" 20; then
    ok "Backend up (pid $pid) — $BACKEND_URL"
  else
    err "Backend did not become healthy in time. Check $(log_file backend)"
    exit 1
  fi
}

start_orchestrator() {
  local pidfile; pidfile="$(pid_file orchestrator)"
  local pid
  pid="$(read_pid "$pidfile" || true)"
  if [ -n "$pid" ] && pid_alive "$pid"; then
    ok "Orchestrator already running (pid $pid)"
    return 0
  fi
  if [ ! -x "$PY" ]; then
    err "Python not found at $PY."
    exit 1
  fi
  mkdir -p "$RUN_DIR"
  info "Starting orchestrator (clams watch)…"
  pid="$(launch_detached "$(log_file orchestrator)" \
        "$PY" -m clams.cli watch)"
  printf '%s' "$pid" > "$pidfile"
  sleep 1
  if pid_alive "$pid"; then
    ok "Orchestrator running (pid $pid)"
  else
    err "Orchestrator exited immediately. Check $(log_file orchestrator)"
    exit 1
  fi
}

start_frontend() {
  local pidfile; pidfile="$(pid_file frontend)"
  local pid
  pid="$(read_pid "$pidfile" || true)"
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
  pid="$(launch_detached "$(log_file frontend)" bash -c \
        "cd '$FRONTEND_DIR' && exec npm run dev -- --host '$FRONTEND_HOST' --port '$FRONTEND_PORT'")"
  printf '%s' "$pid" > "$pidfile"
  if wait_http "$FRONTEND_URL" 20; then
    ok "Frontend up (pid $pid) — $FRONTEND_URL"
  else
    err "Frontend did not respond in time. Check $(log_file frontend)"
    exit 1
  fi
}


# --- commands --------------------------------------------------------------

cmd_start() {
  info "Starting the Clams stack (5 components)…"
  echo
  start_redis
  start_claudio_agent "$PLANNER_CONTAINER"   "$PLANNER_PORT"  "planner"
  start_claudio_agent "$EXECUTOR_CONTAINER"  "$EXECUTOR_PORT" "executor"
  start_backend
  start_orchestrator
  start_frontend
  echo
  ok "All five components started."
  echo "  Frontend     ${FRONTEND_URL}"
  echo "  Backend      ${BACKEND_URL}"
  echo "  Planner      http://127.0.0.1:${PLANNER_PORT}/api/v1"
  echo "  Executor     http://127.0.0.1:${EXECUTOR_PORT}/api/v1"
  echo "  Orchestrator clams watch (pid $(read_pid "$(pid_file orchestrator)" || echo '?'))"
  echo "  Logs         ${RUN_DIR}/*.log"
}

cmd_stop() {
  stop_pid "frontend"     "$(pid_file frontend)"
  stop_pid "orchestrator" "$(pid_file orchestrator)"
  stop_pid "backend"      "$(pid_file backend)"
  stop_container "$EXECUTOR_CONTAINER"
  stop_container "$PLANNER_CONTAINER"
}

cmd_restart() {
  cmd_stop
  echo
  cmd_start
}

# component_state <name> -> echoes "up" | "down"
component_state() {
  local name="$1"
  case "$name" in
    planner_agent|executor_agent)
      local cname
      if [ "$name" = "planner_agent" ]; then cname="$PLANNER_CONTAINER"; else cname="$EXECUTOR_CONTAINER"; fi
      local port
      if [ "$name" = "planner_agent" ]; then port="$PLANNER_PORT"; else port="$EXECUTOR_PORT"; fi
      if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$cname" \
         && http_ok "http://127.0.0.1:${port}/healthz"; then
        echo up
      else
        echo down
      fi
      ;;
    *)
      local pid
      pid="$(read_pid "$(pid_file "$name")" || true)"
      if [ -n "$pid" ] && pid_alive "$pid"; then echo up; else echo down; fi
      ;;
  esac
}

cmd_status() {
  printf '%s\n' "${C_BLUE}Clams stack status${C_RESET}"
  local down=0

  local bstate
  bstate="$(component_state backend)"
  if [ "$bstate" = up ] && http_ok "$BACKEND_URL/healthz"; then
    printf '  %s  %s\n' "${C_GREEN}● backend${C_RESET}       ${BACKEND_URL}"
  elif [ "$bstate" = up ]; then
    printf '  %s  %s\n' "${C_YELLOW}● backend (no /healthz)${C_RESET}  ${BACKEND_URL}"
    down=1
  else
    printf '  %s\n' "${C_DIM}○ backend  stopped${C_RESET}"
    down=1
  fi

  if http_ok "$FRONTEND_URL"; then
    printf '  %s  %s\n' "${C_GREEN}● frontend${C_RESET}      ${FRONTEND_URL}"
  else
    printf '  %s\n' "${C_DIM}○ frontend stopped${C_RESET}"
    down=1
  fi

  printf '  %s  %s\n' \
    "$([ "$(component_state planner_agent)" = up ] && printf '%s' "${C_GREEN}● planner agent${C_RESET}" || { down=1; printf '%s' "${C_RED}○ planner agent${C_RESET}"; })" \
    "http://127.0.0.1:${PLANNER_PORT}/api/v1"
  printf '  %s  %s\n' \
    "$([ "$(component_state executor_agent)" = up ] && printf '%s' "${C_GREEN}● executor agent${C_RESET}" || { down=1; printf '%s' "${C_RED}○ executor agent${C_RESET}"; })" \
    "http://127.0.0.1:${EXECUTOR_PORT}/api/v1"

  if [ "$(component_state orchestrator)" = up ]; then
    printf '  %s\n' "${C_GREEN}● orchestrator${C_RESET}    clams watch"
  else
    printf '  %s\n' "${C_DIM}○ orchestrator stopped${C_RESET}"
    down=1
  fi

  if (echo > "/dev/tcp/127.0.0.1/${REDIS_PORT}") >/dev/null 2>&1; then
    printf '  %s  %s\n' "${C_GREEN}● redis${C_RESET}         127.0.0.1:${REDIS_PORT}"
  else
    printf '  %s\n' "${C_DIM}○ redis stopped${C_RESET}"
    down=1
  fi

  return "$down"
}

cmd_logs() {
  local target="${1:-}"
  if [ -z "$target" ]; then
    tail -n 50 -f \
      "$(log_file backend)" "$(log_file frontend)" \
      "$(log_file orchestrator)" 2>/dev/null
    return
  fi
  tail -n 100 -f "$(log_file "$target")"
}

cmd_agents() {
  info "Agent registry"
  curl -s --max-time 5 "http://${BACKEND_HOST}:${BACKEND_PORT}/api/v1/agents" \
    | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
for a in d["agents"]:
    print("  %-14s %-12s %-42s %s" % (a["id"], a["role"], a["effective_base_url"],
          "enabled" if a["enabled"] else "DISABLED"))
' || err "Could not read the agent registry (is the backend up?)"
}

usage() {
  cat <<EOF
Clams dev supervisor

  start             Start all 5 components (redis, 2 Claudio agents, backend,
                    orchestrator, frontend). Idempotent.
  stop              Stop everything.
  status            Per-component status.
  restart           Stop then start.
  logs              Follow backend + frontend + orchestrator logs.
  logs <component>  Follow one log: backend | frontend | orchestrator
  agents            Show the configured agent registry.

Ports (env-overridable):
  backend ${BACKEND_PORT}   frontend ${FRONTEND_PORT}   planner agent ${PLANNER_PORT}   executor agent ${EXECUTOR_PORT}
EOF
}

main() {
  local cmd="${1:-}"
  case "$cmd" in
    start)   shift; cmd_start "$@" ;;
    stop)    shift; cmd_stop "$@" ;;
    status)  shift; cmd_status "$@" ;;
    restart) shift; cmd_restart "$@" ;;
    logs)    shift; cmd_logs "$@" ;;
    agents)  shift; cmd_agents "$@" ;;
    ""|-h|--help|help) usage ;;
    *) err "Unknown command: $cmd"; usage; exit 2 ;;
  esac
}

main "$@"
