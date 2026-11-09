#!/usr/bin/env python3
"""`clamsctl` — Docker control script for the Clams stack (issue #9).

Subcommands
    build      build the images
    start      start the stack (detached)
    stop       stop the stack (containers kept)
    restart    stop then start
    recreate   tear the containers down and bring them back up fresh
    status     container status table
    health     probe every service and exit non-zero if anything is unhealthy
    cleanup    remove containers, project volumes and the local images
    logs       follow logs for one service

Design notes
    * Every action prints the exact docker command it is about to run before
      running it, so the script is inspectable rather than magic.
    * Destructive commands (``recreate``, ``cleanup``) require ``--yes`` when
      run without a TTY, so they cannot fire by accident from a script.
    * Exit code is 0 on success and non-zero on any failure, so the script is
      usable from CI and from shell ``&&`` chains.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Optional

COMPOSE_FILE = "docker-compose.yml"
# Services in dependency order, used for status/health reporting.
SERVICES = ("redis", "watchdog", "executor", "planner", "gateway")
# HTTP probes for `health`: (service, url, expected status).
HTTP_PROBES = (
    ("planner", "http://127.0.0.1:8100/healthz", 200),
    ("gateway", "http://127.0.0.1:8100/", 200),
)


def _run(cmd: list[str], capture: bool = False, check: bool = True,
         timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    """Run a command, echoing it first so the operator can see what happened."""
    printable = " ".join(cmd)
    print(f"$ {printable}")
    return subprocess.run(cmd, capture_output=capture, text=True,
                         check=check, timeout=timeout)


def _compose(*args: str, capture: bool = False, check: bool = True,
            timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    return _run(["docker", "compose", "-f", COMPOSE_FILE, *args],
                capture=capture, check=check, timeout=timeout)


# ---------------------------------------------------------------- actions ----
def cmd_build(args) -> int:
    """Build the images."""
    print("Building images...")
    _compose("build", *args.services, timeout=args.timeout)
    print("Build complete.")
    return 0


def cmd_start(args) -> int:
    """Start the stack detached."""
    print("Starting stack...")
    _compose("up", "-d", "--remove-orphans", timeout=args.timeout)
    print("Stack started. Check `clamsctl status`.")
    return 0


def cmd_stop(args) -> int:
    """Stop the stack, keeping containers."""
    print("Stopping stack...")
    _compose("stop", timeout=args.timeout)
    print("Stack stopped.")
    return 0


def cmd_restart(args) -> int:
    rc = cmd_stop(args)
    if rc:
        return rc
    return cmd_start(args)


def cmd_recreate(args) -> int:
    """Recreate containers from scratch."""
    if not args.yes and not sys.stdin.isatty():
        print("refusing to recreate without --yes (non-interactive session)",
              file=sys.stderr)
        return 2
    print("Recreating containers...")
    _compose("down", timeout=args.timeout)
    _compose("up", "-d", "--remove-orphans", "--force-recreate",
             timeout=args.timeout)
    print("Containers recreated.")
    return 0


def cmd_status(args) -> int:
    """Show container status."""
    rc = _compose("ps", check=False).returncode
    if rc != 0:
        print("docker compose ps failed; is the stack created?", file=sys.stderr)
    return rc


def _container_health(service: str) -> str:
    """Return the docker health status of a service's container."""
    try:
        out = subprocess.run(
            ["docker", "compose", "-f", COMPOSE_FILE, "ps", "--format", "json",
             service],
            capture_output=True, text=True, check=False, timeout=30,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        return f"unavailable ({exc})"

    lines = [ln for ln in out.splitlines() if ln.strip()]
    if not lines:
        return "not created"
    try:
        data = json.loads(lines[0])
    except json.JSONDecodeError:
        return "unknown"
    return data.get("Health") or data.get("health") or data.get("Status", "unknown")


def cmd_health(args) -> int:
    """Probe docker healthchecks and the HTTP endpoints."""
    failures: list[str] = []

    print("Container healthchecks:")
    for svc in SERVICES:
        state = _container_health(svc)
        ok = state.lower() == "healthy"
        print(f"  [{'OK  ' if ok else 'FAIL'}] {svc}: {state}")
        if not ok:
            failures.append(f"{svc}: {state}")

    print("HTTP probes:")
    for name, url, expected in HTTP_PROBES:
        ok, detail = _probe_http(url, expected, args.probe_timeout)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name} {url} -> {detail}")
        if not ok:
            failures.append(f"{name} {url}: {detail}")

    if failures:
        print(f"\nUNHEALTHY: {len(failures)} problem(s)", file=sys.stderr)
        return 1
    print("\nAll checks passed.")
    return 0


def _probe_http(url: str, expected: int, timeout: float) -> tuple[bool, str]:
    try:
        import httpx
        r = httpx.get(url, timeout=timeout)
        return (r.status_code == expected, f"{r.status_code}")
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


def cmd_cleanup(args) -> int:
    """Remove containers, project volumes and the local images."""
    if not args.yes and not sys.stdin.isatty():
        print("refusing to cleanup without --yes (non-interactive session)",
              file=sys.stderr)
        return 2
    print("Stopping and removing containers...")
    _compose("down", "--volumes", timeout=args.timeout)
    print("Removing dangling images...")
    _run(["docker", "image", "prune", "-f"], check=False)
    print("Cleanup complete.")
    return 0


def cmd_logs(args) -> int:
    """Follow logs for one service."""
    cmd = ["docker", "compose", "-f", COMPOSE_FILE, "logs", "--tail",
           str(args.tail), args.service]
    if not args.no_follow:
        cmd.append("--follow")
    return _run(cmd, check=False).returncode


# ---------------------------------------------------------------- parser ----
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clamsctl",
        description="Docker control script for the Clams stack",
    )
    p.add_argument("--timeout", type=float, default=600.0,
                   help="per-command timeout in seconds (default: 600)")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build the images")
    b.add_argument("services", nargs="*", help="limit to these services")

    sub.add_parser("start", help="start the stack (detached)")
    sub.add_parser("stop", help="stop the stack")
    sub.add_parser("restart", help="stop then start")

    rc = sub.add_parser("recreate", help="te down and bring back up fresh")
    rc.add_argument("--yes", action="store_true", help="skip the confirmation")

    sub.add_parser("status", help="show container status")

    h = sub.add_parser("health", help="probe healthchecks and endpoints")
    h.add_argument("--probe-timeout", type=float, default=8.0,
                   help="HTTP probe timeout (default: 8s)")

    cl = sub.add_parser("cleanup", help="remove containers, volumes, images")
    cl.add_argument("--yes", action="store_true", help="skip the confirmation")

    lg = sub.add_parser("logs", help="follow logs for one service")
    lg.add_argument("service")
    lg.add_argument("--tail", type=int, default=100)
    lg.add_argument("--no-follow", action="store_true")

    return p


HANDLERS = {
    "build": cmd_build,
    "start": cmd_start,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "recreate": cmd_recreate,
    "status": cmd_status,
    "health": cmd_health,
    "cleanup": cmd_cleanup,
    "logs": cmd_logs,
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return HANDLERS[args.cmd](args)
    except subprocess.CalledProcessError as exc:
        print(f"command failed ({exc.returncode}): {' '.join(exc.cmd)}",
              file=sys.stderr)
        if exc.stderr:
            print(str(exc.stderr).strip()[:500], file=sys.stderr)
        return exc.returncode or 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
