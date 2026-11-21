"""`clams` command line: planner | executor | api | migrate | healthcheck |
status | command."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Optional

from . import __version__
from .config import ConfigError, Settings


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="clams",
                                     description="Clams v2 orchestrator")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("planner", help="run the planner loop (single DB writer)")
    sub.add_parser("executor", help="run an executor-runner")
    sub.add_parser("api", help="serve the HTTP API + SPA")
    sub.add_parser("migrate", help="apply pending DB migrations")

    hc = sub.add_parser("healthcheck", help="container healthcheck")
    hc.add_argument("role", choices=["planner", "executor", "api"])

    sub.add_parser("status", help="print a status snapshot")

    cmd = sub.add_parser("command", help="issue a control command")
    cmd.add_argument("kind", choices=["pause", "resume"])
    cmd.add_argument("--by", default="operator")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    settings = Settings()

    try:
        settings.boot_guard()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "migrate":
        return _migrate(settings)
    if args.cmd == "healthcheck":
        return _healthcheck(settings, args.role)
    if args.cmd == "status":
        return _status(settings)
    if args.cmd == "command":
        return _command(settings, args.kind, args.by)
    if args.cmd == "planner":
        return _planner(settings)
    if args.cmd == "executor":
        return _executor(settings)
    if args.cmd == "api":
        return _api(settings)
    return 1


# --------------------------------------------------------------------- impls
def _migrate(settings) -> int:
    from .db import connect, migrate
    conn = connect(settings.db_path)
    applied = migrate(conn)
    conn.close()
    print(f"migrations applied: {applied or 'none (already current)'}")
    return 0


def _healthcheck(settings, role: str) -> int:
    """Container healthcheck: exit 0 iff this role is healthy."""
    from .db import connect, repo_runtime, schema_version
    try:
        if role == "planner":
            conn = connect(settings.db_path, read_only=True)
            age = repo_runtime.heartbeat_age_s(conn)
            ok = (schema_version(conn) >= 1
                  and age is not None
                  and age <= settings.planner_heartbeat_max_age_s)
            conn.close()
            return 0 if ok else 1
        if role == "executor":
            from .bus import make_bus
            from .claudio_client import ClaudioClient
            bus = make_bus(settings.redis_url)
            cl = ClaudioClient(settings.claudio_executor_url,
                               settings.claudio_executor_token)
            return 0 if (bus.ping() and cl.health()) else 1
        if role == "api":
            import httpx
            r = httpx.get(f"http://127.0.0.1:{settings.api_port}/healthz",
                          timeout=3)
            return 0 if r.status_code == 200 else 1
    except Exception as exc:  # noqa: BLE001
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1
    return 1


def _status(settings) -> int:
    from .db import (connect, repo_executors, repo_outbox, repo_runtime,
                     repo_tasks, repo_workflows, schema_version)
    conn = connect(settings.db_path, read_only=True)
    out = {
        "schema_version": schema_version(conn),
        "paused": repo_runtime.is_paused(conn),
        "paused_by": repo_runtime.paused_by(conn),
        "planner_heartbeat_age_s": repo_runtime.heartbeat_age_s(conn),
        "workflows": repo_workflows.counts_by_status(conn),
        "tasks": repo_tasks.counts_by_status(conn),
        "outbox": repo_outbox.counts(conn),
        "executors": [dict(r) for r in repo_executors.list_executors(conn)],
    }
    conn.close()
    print(json.dumps(out, indent=2))
    return 0


def _command(settings, kind: str, by: str) -> int:
    from .db import connect, repo_commands
    conn = connect(settings.db_path)
    cid = repo_commands.insert_command(conn, kind, {}, by)
    conn.close()
    print(f"command {kind} queued as #{cid}")
    return 0


def _planner(settings) -> int:
    from .bus import make_bus
    from .claudio_client import ClaudioClient
    from .planner.loop import PlannerLoop
    bus = make_bus(settings.redis_url)
    cp = ClaudioClient(settings.claudio_planner_url,
                       settings.claudio_planner_token,
                       timeout=settings.claudio_timeout_s)
    ce = ClaudioClient(settings.claudio_executor_url,
                       settings.claudio_executor_token)
    gh = None
    if settings.github_token:
        from .github.client import GitHubClient
        gh = GitHubClient(settings.github_token,
                          api_base=settings.github_api_base,
                          graphql_base=settings.github_graphql_base)
    loop = PlannerLoop(settings, bus, cp, claudio_e=ce, gh_client=gh)
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        loop.stop()
    return 0


def _executor(settings) -> int:
    from .bus import make_bus
    from .claudio_client import ClaudioClient
    from .executor.runner import ExecutorRunner
    from .executor.worktree import WorktreeManager
    bus = make_bus(settings.redis_url)
    cl = ClaudioClient(settings.claudio_executor_url,
                       settings.claudio_executor_token,
                       timeout=settings.claudio_timeout_s)
    wt = WorktreeManager(settings.work_root,
                          chown_uid=settings.worktree_uid or None,
                          chown_gid=settings.worktree_gid or None)
    runner = ExecutorRunner(settings, bus, cl, wt)
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        runner.stop()
    return 0


def _api(settings) -> int:
    import uvicorn
    from .api import create_app
    from .claudio_client import ClaudioClient
    cp = ClaudioClient(settings.claudio_planner_url,
                       settings.claudio_planner_token)
    ce = ClaudioClient(settings.claudio_executor_url,
                       settings.claudio_executor_token)
    app = create_app(settings, claudio_p=cp, claudio_e=ce)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
