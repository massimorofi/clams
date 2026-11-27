"""`clams` command line: planner | executor | api | migrate | healthcheck |
status | monitor | command | postmortem."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Optional

from . import __version__

try:  # a minimal (stdlib-only) install has no pydantic-settings; only the
      # read-only `postmortem` subcommand needs to work there.
    from .config import ConfigError, Settings
except ModuleNotFoundError:  # pragma: no cover
    ConfigError = None
    Settings = None


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

    st = sub.add_parser("status", help="print a status snapshot")
    st.add_argument("--json", action="store_true", help="machine-readable")

    mon = sub.add_parser("monitor",
                         help="zero-LLM monitoring report (read-only)")
    mon.add_argument("--json", action="store_true", help="machine-readable")
    mon.add_argument("--since-hours", type=int, default=24,
                     help="rolling window for rates/failures (default 24)")

    cmd = sub.add_parser("command", help="issue a control command")
    cmd.add_argument("kind", choices=["pause", "resume"])
    cmd.add_argument("--by", default="operator")

    pm = sub.add_parser("postmortem",
                        help="read-only post-mortem report for terminal "
                             "workflows (JSON written under reports/)")
    pm.add_argument("--status", action="append", dest="statuses",
                    choices=["done", "failed", "cancelled"],
                    help="terminal status to include (repeatable; "
                         "default: all terminal statuses)")
    pm.add_argument("--since", default=None,
                    help="only workflows updated at/after this timestamp "
                         "(YYYY-MM-DD HH:MM:SS or ISO-8601)")
    pm.add_argument("--limit", type=int, default=25,
                    help="max workflows in one report (default 25)")
    pm.add_argument("--out", default=None,
                    help="output directory (default: ./reports)")
    pm.add_argument("--out-file", default=None,
                    help="explicit output file path (overrides --out)")
    pm.add_argument("--db", default=None,
                    help="SQLite path (default: CLAMS_DB_PATH)")
    pm.add_argument("--max-report-bytes", type=int, default=None,
                    help="reject the report if its payload exceeds this size")
    pm.add_argument("--print", dest="print_report", action="store_true",
                    help="also print the report JSON to stdout")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    # `postmortem` is stdlib-only and read-only: it must run even where
    # pydantic-settings is unavailable, so it never touches Settings.
    if args.cmd == "postmortem":
        return _postmortem(args)

    if Settings is None:
        print("config error: pydantic-settings is not installed; only the "
              "'postmortem' subcommand is available in this environment",
              file=sys.stderr)
        return 2
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
        return _status(settings, as_json=args.json)
    if args.cmd == "monitor":
        return _monitor(settings, as_json=args.json,
                        since_hours=args.since_hours)
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


# status line grouping: which statuses count as "still in flight" per table
_ACTIVE_BY_TABLE = {
    "workflows": ("intake", "planning", "running", "review", "blocked",
                  "paused"),
    "tasks": ("pending", "ready", "dispatched", "running", "waiting_input",
              "blocked"),
    "outbox": ("pending", "inflight"),
}


def _fmt_age(seconds) -> str:
    """Human duration: 7s / 4m 03s / 2h 05m."""
    if seconds is None:
        return "never"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _fmt_counts(counts: dict, active: tuple) -> str:
    act = {k: n for k, n in counts.items() if n and k in active}
    term = {k: n for k, n in counts.items() if n and k not in active}
    def grp(d):
        return ", ".join(f"{n} {k}" for k, n in sorted(d.items())) or "none"
    return f"in flight: {grp(act)}  |  terminal: {grp(term)}"


def _status(settings, as_json: bool = False) -> int:
    from .db import (connect, repo_alerts, repo_executors, repo_outbox,
                     repo_runtime, repo_tasks, repo_workflows,
                     schema_version)
    from .db.conn import iso_to_epoch
    import time as _time
    conn = connect(settings.db_path, read_only=True)
    hb_age = repo_runtime.heartbeat_age_s(conn)
    paused = repo_runtime.is_paused(conn)
    paused_by = repo_runtime.paused_by(conn) or ""
    executors = [dict(r) for r in repo_executors.list_executors(conn)]
    alerts = [dict(a) for a in repo_alerts.list_open(conn)]
    out = {
        "schema_version": schema_version(conn),
        "paused": paused,
        "paused_by": paused_by,
        "planner_heartbeat_age_s": hb_age,
        "workflows": repo_workflows.counts_by_status(conn),
        "tasks": repo_tasks.counts_by_status(conn),
        "outbox": repo_outbox.counts(conn),
        "executors": executors,
        "alerts": alerts,
    }
    conn.close()
    if as_json:
        print(json.dumps(out, indent=2))
        return 0

    max_age = settings.planner_heartbeat_max_age_s
    if hb_age is None:
        planner = f"DOWN — no heartbeat recorded (max age {max_age}s)"
    elif hb_age > max_age:
        planner = (f"STALE — last heartbeat {_fmt_age(hb_age)} ago "
                   f"(max {max_age}s)")
    else:
        planner = f"alive — last heartbeat {_fmt_age(hb_age)} ago"
    sched = f"PAUSED (by {paused_by or 'unknown'})" if paused else "active"
    lines = [
        "== clams status ==",
        f"  Planner     {planner}",
        f"  Scheduling  {sched}",
        f"  Schema      v{out['schema_version']}",
        f"  Workflows   {_fmt_counts(out['workflows'], _ACTIVE_BY_TABLE['workflows'])}",
        f"  Tasks       {_fmt_counts(out['tasks'], _ACTIVE_BY_TABLE['tasks'])}",
        f"  Outbox      {_fmt_counts(out['outbox'], _ACTIVE_BY_TABLE['outbox'])}",
    ]
    idle_max = 3 * settings.executor_alive_interval_s
    if executors:
        lines.append(f"  Executors   {len(executors)} registered "
                     f"(offline if silent > {idle_max}s):")
        for e in executors:
            seen = (None if not e.get("last_seen")
                    else _time.time() - iso_to_epoch(e["last_seen"]))
            state = "online " if seen is not None and seen <= idle_max \
                else "offline"
            lines.append(f"                {e['executor_id']}  {state} "
                         f"v{e.get('version')} cap={e.get('capacity')} "
                         f"seen {_fmt_age(seen)} ago")
    else:
        lines.append("  Executors   none registered")
    if alerts:
        lines.append(f"  Alerts      {len(alerts)} open:")
        for a in alerts:
            lines.append(f"                [{a['severity']}] {a['probe']}: "
                         f"{(a['message'] or '')[:110]}")
    else:
        lines.append("  Alerts      none open")
    print("\n".join(lines))
    return 0


def _monitor(settings, as_json: bool = False, since_hours: int = 24) -> int:
    """Zero-LLM monitoring report: gate-failure histogram, rolling success
    rate, turn cost, backlog, open alerts, running-task lease table, recent
    stall evidence. Read-only."""
    import time as _time
    from .db import (connect, repo_alerts, repo_stall_evidence, repo_tasks,
                     repo_workflows)
    from .db.conn import iso_to_epoch
    conn = connect(settings.db_path, read_only=True)
    cutoff = _time.time() - since_hours * 3600

    gate_by_exit: dict[str, int] = {}
    gate_reasons: dict[str, int] = {}
    successes = failures = window_success = window_fail = 0
    turns_ok: list[int] = []
    for r in conn.execute("SELECT * FROM tasks").fetchall():
        in_window = (iso_to_epoch(r["updated_at"]) >= cutoff
                     if r["updated_at"] else False)
        if r["status"] == "success":
            successes += 1
            window_success += int(in_window)
            try:
                t = int(json.loads(
                    r["heartbeat_json"] or "{}").get("turns_used", -1))
                if t >= 0:
                    turns_ok.append(t)
            except ValueError:
                pass
        elif r["status"] == "failed":
            failures += 1
            window_fail += int(in_window)
            try:
                ev = json.loads(
                    r["result_json"] or "{}").get("evidence", {})
            except ValueError:
                ev = {}
            reason = str(ev.get("reason", ""))[:80]
            if reason:
                gate_reasons[reason] = gate_reasons.get(reason, 0) + 1
            for c in ev.get("checks", []):
                if c.get("check") == "run" and c.get("exit"):
                    k = f"exit {c['exit']}"
                    gate_by_exit[k] = gate_by_exit.get(k, 0) + 1
    terminal = successes + failures
    out = {
        "window_hours": since_hours,
        "success_rate": {
            "overall": round(successes / terminal, 3) if terminal else None,
            "window": (round(window_success / (window_success + window_fail), 3)
                       if window_success + window_fail else None),
            "successes": successes, "failures": failures,
        },
        "gate_failures": {"by_exit": gate_by_exit,
                          "top_reasons": dict(
                              sorted(gate_reasons.items(),
                                     key=lambda kv: -kv[1])[:5])},
        "turns_per_success": {
            "avg": round(sum(turns_ok) / len(turns_ok), 1) if turns_ok else None,
            "max": max(turns_ok) if turns_ok else None},
        "ready_backlog": {
            "count": conn.execute("SELECT COUNT(*) n FROM tasks "
                                  "WHERE status='ready'").fetchone()["n"],
            "oldest_age_s": repo_tasks.oldest_ready_age_s(conn)},
        "workflows": repo_workflows.counts_by_status(conn),
        "alerts": [dict(a) for a in repo_alerts.list_open(conn)],
        "running_tasks": [],
        "stall_evidence": [],
    }
    for r in repo_tasks.by_status(conn, "running", "dispatched"):
        hb = {}
        try:
            hb = json.loads(r["heartbeat_json"] or "{}")
        except ValueError:
            pass
        remaining = None
        if r["lease_expires_at"]:
            remaining = int(iso_to_epoch(r["lease_expires_at"]) - _time.time())
        out["running_tasks"].append({
            "task_id": r["task_id"], "status": r["status"],
            "attempt": r["attempt"], "max_attempts": r["max_attempts"],
            "lease_expires_at": r["lease_expires_at"],
            "lease_remaining_s": remaining,
            "turns_used": hb.get("turns_used"),
            "elapsed_s": hb.get("elapsed_s"),
            "session_ref": r["session_ref"]})
    for e in repo_stall_evidence.list_recent(conn, limit=10):
        out["stall_evidence"].append({
            "task_id": e["task_id"], "attempt": e["attempt"],
            "updated_at": e["updated_at"]})
    conn.close()

    if as_json:
        print(json.dumps(out, indent=2))
        return 0
    w = out["window_hours"]
    print(f"== success (overall / last {w}h): "
          f"{out['success_rate']['overall']} / {out['success_rate']['window']}"
          f"  [{successes} ok, {failures} failed]")
    print(f"== gate failures by exit: {out['gate_failures']['by_exit'] or '—'}")
    for reason, n in out["gate_failures"]["top_reasons"].items():
        print(f"   {n}x {reason}")
    print(f"== turns/success: {out['turns_per_success']}")
    print(f"== ready backlog: {out['ready_backlog']}")
    print(f"== workflows: {out['workflows']}")
    print(f"== open alerts ({len(out['alerts'])}):")
    for a in out["alerts"]:
        print(f"   [{a['severity']}] {a['probe']}: {a['message'][:110]}")
    print(f"== running/dispatched tasks:")
    for t in out["running_tasks"]:
        print(f"   {t['task_id'][:8]} {t['status']} a{t['attempt']}/"
              f"{t['max_attempts']} lease_left={t['lease_remaining_s']}s "
              f"turns={t['turns_used']}")
    if out["stall_evidence"]:
        print("== recent stall evidence (clams monitor --json | jq, or "
              "stall_evidence table):")
        for e in out["stall_evidence"]:
            print(f"   {e['task_id'][:8]} a{e['attempt']} @ {e['updated_at']}")
    return 0


def _command(settings, kind: str, by: str) -> int:
    from .db import connect, repo_commands
    conn = connect(settings.db_path)
    cid = repo_commands.insert_command(conn, kind, {}, by)
    conn.close()
    print(f"command {kind} queued as #{cid}")
    return 0


def _pm_db_path(explicit: Optional[str]) -> str:
    """Resolve the SQLite path without requiring pydantic-settings."""
    import os
    if explicit:
        return explicit
    if Settings is not None:
        try:
            return Settings().db_path
        except Exception:  # noqa: BLE001 - bad config must not hide the report
            pass
    return os.environ.get("CLAMS_DB_PATH", "./data/clams.db")


def _postmortem(args) -> int:
    """Read-only post-mortem: select -> snapshot -> report -> validate -> write.

    Exit codes: 0 written, 2 no database / bad arguments, 3 validation failure.
    """
    import sqlite3
    from pathlib import Path

    from .postmortem import (MAX_REPORT_BYTES, ReportValidationError,
                             payload_bytes, report_filename, run_postmortem)

    db_path = _pm_db_path(args.db)
    if not Path(db_path).exists():
        print(f"postmortem: database not found: {db_path}", file=sys.stderr)
        return 2
    try:
        conn = _ro_connect(db_path)
    except sqlite3.Error as exc:
        print(f"postmortem: cannot open {db_path}: {exc}", file=sys.stderr)
        return 2
    try:
        try:
            report = run_postmortem(
                conn, statuses=args.statuses, since=args.since,
                limit=args.limit,
                max_report_bytes=int(args.max_report_bytes
                                     or MAX_REPORT_BYTES))
        except ReportValidationError as exc:
            for e in exc.errors:
                print(f"postmortem invalid: {e}", file=sys.stderr)
            return 3
        except (ValueError, sqlite3.Error) as exc:
            print(f"postmortem failed: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()

    target = (Path(args.out_file) if args.out_file
              else Path(args.out or "reports") / report_filename(report))
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    target.write_text(text + "\n", encoding="utf-8")
    n_find = len(report.get("findings") or [])
    print(f"postmortem report: {target} "
          f"({payload_bytes(report)} bytes, {len(report['workflow_ids'])} "
          f"workflow(s), {n_find} finding(s), "
          f"truncated={report.get('truncated')})")
    if args.print_report:
        print(text)
    return 0


def _ro_connect(db_path: str):
    """Read-only connection; falls back to a plain open only when a read-only
    WAL open is impossible (missing -shm). The post-mortem issues SELECTs
    only either way."""
    from .db import connect
    try:
        return connect(db_path, read_only=True)
    except Exception:  # noqa: BLE001
        return connect(db_path, read_only=False)


def _planner(settings) -> int:
    from .bus import make_bus
    from .claudio_client import ClaudioClient
    from .planner.loop import PlannerLoop
    bus = make_bus(settings.redis_url)
    cp = ClaudioClient(settings.claudio_planner_url,
                       settings.claudio_planner_token,
)
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
)
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
