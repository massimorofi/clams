"""`clams` command-line interface (spec §10 operational surface)."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Optional

from .config import Settings
from .db import open_store


def _setup_logging(level: str = "INFO") -> None:
    """Send logs to stderr.

    stdout is block-buffered when redirected, so ``print`` output is lost if the
    process is killed before the buffer flushes. stderr is line-buffered, so logs
    survive a SIGTERM and are visible under ``docker logs`` / ``>> file``.
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )


def _settings() -> Settings:
    return Settings.load()


def cmd_init_db(args) -> int:
    s = _settings()
    store = open_store(s.paths.db_path)
    print(f"initialised database at {s.paths.db_path}")
    store.db.close()
    return 0


def cmd_intake(args) -> int:
    s = _settings()
    store = open_store(s.paths.db_path)
    from .gh import GitHubClient
    from .intake import IntakeService, IntakeResult
    from .project_setup import ensure_default_project, parse_owner_repo

    # The configured repo is the default project; every other registered project
    # is polled too (issue #18), so `clams intake` matches what `clams watch`
    # feeds the pipeline.
    default_project_id = ensure_default_project(store, s)
    gh = GitHubClient(s.github.owner, s.github.repo, s.github.token,
                     s.github.api_base)
    services = [IntakeService(store, gh, s, project_id=default_project_id)]
    for row in store.projects.all():
        pid = int(row["id"])
        if pid == default_project_id:
            continue
        try:
            owner, repo = parse_owner_repo(row["url"])
        except ValueError:
            continue
        services.append(IntakeService(
            store,
            GitHubClient(owner, repo, s.github.token, s.github.api_base),
            s, project_id=pid))

    total = IntakeResult()
    for svc in services:
        result = asyncio.run(svc.sweep())
        print(f"  project {svc.project_id}: {result.summary()}")
        total.scanned += result.scanned
        total.claimed.extend(result.claimed)
        total.skipped.extend(result.skipped)
        total.failed.extend(result.failed)
    print(total.summary())
    store.db.close()
    return 0 if not total.failed else 1


def cmd_plan(args) -> int:
    s = _settings()
    store = open_store(s.paths.db_path)
    from .gh import GitHubClient
    from .planner import PlannerService
    from .claudio_client import ClaudioClient

    gh = GitHubClient(s.github.owner, s.github.repo, s.github.token, s.github.api_base)
    # Planning and review judgement are single, structured completions. Use the
    # direct OpenAI-compatible LLM (fast, deterministic) rather than the agentic
    # Claudio chat, which spins up tool use over multiple turns and times out on
    # a plain decomposition request (issue #18 planning step).
    from .llm import make_planner_llm
    llm = make_planner_llm(s)
    from .project_setup import ensure_default_project
    default_project_id = ensure_default_project(store, s)

    svc = PlannerService(store, gh, s, llm_call=llm,
                        project_id=default_project_id)
    plan, tasks = asyncio.run(svc.plan_requirement(args.requirement))
    print(f"requirement #{args.requirement} planned into {len(tasks)} task(s): {tasks}")
    store.db.close()
    return 0


def cmd_dispatch(args) -> int:
    s = _settings()
    store = open_store(s.paths.db_path)
    from .bus import RedisBus

    bus = RedisBus(s.redis.url, s.redis.namespace)
    orch = _orchestrator(s, store, bus)
    report = asyncio.run(orch.step_dispatch(__import__(
        "clams.orchestrator", fromlist=["CycleReport"]).CycleReport()))
    print(f"dispatched {len(report.dispatched)} task(s)")
    store.db.close()
    return 0


def _orchestrator(s: Settings, store, bus):
    from .gh import GitHubClient
    from .intake import IntakeService
    from .planner import PlannerService
    from .projects import board_from_settings
    from .project_setup import build_project_contexts, ensure_default_project
    from .review import ReviewService
    from .steering import SteeringService
    from .claudio_client import ClaudioClient
    from .orchestrator import Orchestrator

    # Resolve the Projects V2 board before anything else: a board that Clams
    # cannot drive must stop startup, not fail per-item mid-run.
    board = asyncio.run(board_from_settings(s))

    # Multi-project (issue #18): the configured repo is the *default project*.
    # Establish its id first so the intake and steering bind to it, then build
    # a context for every other registered project.
    default_project_id = ensure_default_project(store, s)

    gh = GitHubClient(s.github.owner, s.github.repo, s.github.token,
                     s.github.api_base, board=board)
    # Planning and review judgement are single, structured completions. Use the
    # direct OpenAI-compatible LLM (fast, deterministic) rather than the agentic
    # Claudio chat, which spins up tool use over multiple turns and times out on
    # a plain decomposition request (issue #18 planning step).
    from .llm import make_planner_llm
    llm = make_planner_llm(s)

    planner = PlannerService(store, gh, s, llm_call=llm,
                            project_id=default_project_id)
    intake = IntakeService(store, gh, s, project_id=default_project_id)
    review = ReviewService(store, gh, s,
                          judge=planner.judge_review if planner else None,
                          project_id=default_project_id)
    steering = SteeringService(store, s, gh=gh, planner=planner, intake=intake,
                              project_id=default_project_id)

    contexts = build_project_contexts(store, s, llm)

    return Orchestrator(store, bus, s, gh=gh, planner=planner, intake=intake,
                       steering=steering, review=review,
                       project_contexts=contexts,
                       default_project_id=default_project_id)


def cmd_execute(args) -> int:
    s = _settings()
    store = open_store(s.paths.db_path)
    from .bus import RedisBus
    from .executor import ExecutorService, ClaudioTaskRunner
    from .claudio_client import ClaudioClient

    bus = RedisBus(s.redis.url, s.redis.namespace)
    claudio = ClaudioClient(s.executor.base_url, s.executor.auth_token,
                           s.executor.request_timeout)
    from .gh import GitHubClient
    gh = GitHubClient(s.github.owner, s.github.repo, s.github.token, s.github.api_base)
    svc = ExecutorService(store, bus, s,
                          task_runner=ClaudioTaskRunner(
                              claudio, approval_mode=s.executor.approval_mode),
                          gh=gh,
                          executor_id=s.executor.executor_id)
    result = asyncio.run(svc.run_once())
    print(json.dumps(json.loads(result.to_json()), indent=2) if result else "no task")
    store.db.close()
    return 0 if result and result.status == "completed" else 1


def cmd_worker(args) -> int:
    """Run the executor as a long-lived daemon.

    `clams execute` is a one-shot: it claims a single task and exits non-zero
    when the queue is empty. Using it as a container CMD therefore puts the
    container into a restart loop whenever there is simply no work -- the
    restart looks like a crash but is just an empty queue. The daemon form
    keeps polling and never exits on idleness.
    """
    s = _settings()
    store = open_store(s.paths.db_path)
    from .bus import RedisBus
    from .executor import ExecutorService, ClaudioTaskRunner
    from .claudio_client import ClaudioClient
    from .gh import GitHubClient

    log = logging.getLogger("clams.worker")
    bus = RedisBus(s.redis.url, s.redis.namespace)
    claudio = ClaudioClient(s.executor.base_url, s.executor.auth_token,
                           s.executor.request_timeout)
    gh = GitHubClient(s.github.owner, s.github.repo, s.github.token, s.github.api_base)
    svc = ExecutorService(store, bus, s,
                         task_runner=ClaudioTaskRunner(
                             claudio, approval_mode=s.executor.approval_mode),
                         gh=gh,
                         executor_id=s.executor.executor_id)
    print(f"clams executor daemon running (id={s.executor.executor_id})", flush=True)
    log.info("executor daemon started: agent=%s db=%s",
            s.executor.base_url, s.paths.db_path)
    try:
        asyncio.run(svc.run_forever())
    except KeyboardInterrupt:
        print("stopped", flush=True)
        log.info("executor daemon stopped by user")
    return 0


def cmd_review(args) -> int:
    s = _settings()
    store = open_store(s.paths.db_path)
    from .gh import GitHubClient
    from .review import ReviewService

    gh = GitHubClient(s.github.owner, s.github.repo, s.github.token, s.github.api_base)
    from .project_setup import ensure_default_project
    default_project_id = ensure_default_project(store, s)
    svc = ReviewService(store, gh, s, project_id=default_project_id)
    verdict = asyncio.run(svc.review_requirement(args.requirement))
    print(verdict.summary())
    store.db.close()
    return 0 if verdict.passed else 1


def cmd_clear_alerts(args) -> int:
    """Delete alert rows from the Activity feed.

    Acknowledging an alert only marks it read; the row stays in the feed, so a
    resolved incident (a watchdog storm, a burst of misrouted outbox failures)
    clutters Activity indefinitely. This actually removes them.
    """
    s = _settings()
    store = open_store(s.paths.db_path)
    before = store.alerts.count_all()
    removed = store.alerts.purge(acknowledged_only=not args.include_unacked,
                                 severity=args.severity, before=args.before)
    print(f"removed {removed} alert(s) ({before} -> "
          f"{store.alerts.count_all()} remaining)")
    store.db.close()
    return 0


def cmd_watch(args) -> int:
    s = _settings()
    store = open_store(s.paths.db_path)
    from .bus import RedisBus

    log = logging.getLogger("clams.watch")
    bus = RedisBus(s.redis.url, s.redis.namespace)
    orch = _orchestrator(s, store, bus)
    print("clams orchestrator running (Ctrl-C to stop)", flush=True)
    log.info("orchestrator started: poll_interval=%.1fs db=%s",
            s.poll_interval, s.paths.db_path)
    try:
        asyncio.run(orch.run_forever())
    except KeyboardInterrupt:
        print("stopped", flush=True)
        log.info("orchestrator stopped by user")
    return 0


def cmd_status(args) -> int:
    s = _settings()
    store = open_store(s.paths.db_path)
    items = store.work_items.all()
    stats = store.outbox.stats()
    active = store.task_claims.active()
    alerts = store.alerts.list_unacked()
    print(f"requirements tracked : {len(items)}")
    print(f"active task leases   : {len(active)}")
    print(f"outbox               : {stats}")
    print(f"unacked alerts       : {len(alerts)}")
    for it in items:
        print(f"  #{it.requirement_number} [{it.status}] {it.requirement_title}")
    return 0


def cmd_scrub_credentials(args) -> int:
    """Rewrite any clone whose origin still embeds a token (issue #125)."""
    s = _settings()
    from .provision import find_token_bearing_remotes, scrub_remote_tokens
    dirty = find_token_bearing_remotes(s.paths.work_root)
    if dirty:
        print(f"{len(dirty)} clone(s) have credentials in the remote URL:")
        for d in dirty:
            print(f"  {d}")
    fixed = scrub_remote_tokens(s.paths.work_root)
    for f in fixed:
        print(f"  scrubbed {f}")
    if not dirty and not fixed:
        print("no token-bearing remotes found")
    print("NOTE: any token that was written to disk must be rotated in GitHub; "
          "scrubbing the file does not un-leak it.")
    return 0


def cmd_doctor(args) -> int:
    """Run the watchdog check matrix once and print a pass/fail table."""
    s = _settings()
    store = open_store(s.paths.db_path)
    from .bus import RedisBus
    from .gh import GitHubClient
    from .projects import board_from_settings
    from .watchdog import WatchdogService

    bus = RedisBus(s.redis.url, s.redis.namespace)
    board = asyncio.run(board_from_settings(s))
    gh = GitHubClient(s.github.owner, s.github.repo, s.github.token,
                     s.github.api_base, board=board)
    report = asyncio.run(svc_report(store, s, bus, gh))
    print(report.to_text())
    store.db.close()
    return 0 if report.healthy else 1


async def svc_report(store, s, bus, gh):
    from .watchdog import WatchdogService
    svc = WatchdogService(store, s, bus=bus, gh=gh)
    return await svc.run_checks()


def cmd_serve(args) -> int:
    """Run the Clams HTTP + SSE backend for the web frontend."""
    s = _settings()
    import uvicorn

    from .api import create_app

    host = args.host or s.api.host
    port = args.port or s.api.port
    print(f"clams api listening on http://{host}:{port}", flush=True)
    uvicorn.run(create_app(s), host=host, port=port, log_level="info")
    return 0


def cmd_healthcheck(args) -> int:
    """Docker HEALTHCHECK probe for a Clams daemon.

    Each daemon refreshes a heartbeat file while it loops, so file freshness is a
    cheap, dependency-free liveness signal: it needs no Redis, no GitHub and no
    model, and it catches a wedged loop that a process-alive check would miss.
    """
    from datetime import datetime, timezone
    from pathlib import Path

    s = _settings()
    path = Path(s.paths.heartbeat_dir) / f"{args.service}.json"
    max_age = args.max_age if args.max_age is not None else 90.0
    if not path.exists():
        print(f"unhealthy: {path} missing")
        return 1
    try:
        data = json.loads(path.read_text())
        ts = datetime.fromisoformat(str(data["timestamp"]).replace("Z", "+00:00"))
    except Exception as exc:  # noqa: BLE001
        print(f"unhealthy: {path} unreadable: {exc}")
        return 1
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age > max_age:
        print(f"unhealthy: heartbeat {age:.0f}s old (> {max_age:.0f}s)")
        return 1
    print(f"healthy: heartbeat {age:.0f}s old")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clams", description="Clams orchestrator CLI")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="log verbosity (default: INFO)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create/migrate the local SQLite database")
    sub.add_parser("intake", help="sweep GitHub for Ready requirements")
    pl = sub.add_parser("plan", help="decompose a requirement into sub-issues")
    pl.add_argument("requirement", type=int)
    sub.add_parser("dispatch", help="dispatch planned tasks")
    sub.add_parser("execute", help="run one task from the queue")
    sub.add_parser("worker", help="run the executor as a long-lived daemon")
    rv = sub.add_parser("review", help="run the evidence gate on a requirement")
    rv.add_argument("requirement", type=int)
    sub.add_parser("watch", help="run the orchestrator loop")
    sub.add_parser("status", help="show orchestrator status")
    sub.add_parser("doctor", help="run the health check matrix once")
    sub.add_parser("scrub-credentials",
                   help="strip tokens out of provisioned clone remote URLs (#125)")
    ca = sub.add_parser("clear-alerts",
                        help="delete alert rows from the Activity feed")
    ca.add_argument("--severity", default=None,
                    help="only this severity (info | warning | critical)")
    ca.add_argument("--before", default=None,
                    help="only alerts created before this ISO timestamp")
    ca.add_argument("--include-unacked", action="store_true",
                    help="also delete alerts nobody has acknowledged")
    sv = sub.add_parser("serve", help="run the HTTP + SSE backend for the web UI")
    sv.add_argument("--host", default=None, help="bind address (default 0.0.0.0)")
    sv.add_argument("--port", type=int, default=None, help="bind port (default 8200)")
    hc = sub.add_parser("healthcheck", help="liveness probe for a daemon (Docker HEALTHCHECK)")
    hc.add_argument("--service", required=True,
                    help="heartbeat name to probe (orchestrator|executor|watchdog)")
    hc.add_argument("--max-age", type=float, default=None,
                    help="max heartbeat age in seconds (default 90)")
    return p


HANDLERS = {
    "init-db": cmd_init_db,
    "intake": cmd_intake,
    "plan": cmd_plan,
    "dispatch": cmd_dispatch,
    "execute": cmd_execute,
    "worker": cmd_worker,
    "review": cmd_review,
    "watch": cmd_watch,
    "status": cmd_status,
    "doctor": cmd_doctor,
    "scrub-credentials": cmd_scrub_credentials,
    "clear-alerts": cmd_clear_alerts,
    "healthcheck": cmd_healthcheck,
    "serve": cmd_serve,
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "log_level", "INFO"))
    handler = HANDLERS[args.cmd]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
