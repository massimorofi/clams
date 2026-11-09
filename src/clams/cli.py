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
    from .intake import IntakeService

    gh = GitHubClient(s.github.owner, s.github.repo, s.github.token, s.github.api_base)
    svc = IntakeService(store, gh, s)
    result = asyncio.run(svc.sweep())
    print(result.summary())
    store.db.close()
    return 0 if not result.failed else 1


def cmd_plan(args) -> int:
    s = _settings()
    store = open_store(s.paths.db_path)
    from .gh import GitHubClient
    from .planner import PlannerService
    from .claudio_client import ClaudioClient

    gh = GitHubClient(s.github.owner, s.github.repo, s.github.token, s.github.api_base)
    claudio = ClaudioClient(s.planner.base_url, s.planner.auth_token,
                           s.planner.request_timeout)

    def llm(prompt: str) -> str:
        return asyncio.run(claudio.chat(prompt)).final_output

    svc = PlannerService(store, gh, s, llm_call=llm)
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
    from .review import ReviewService
    from .steering import SteeringService
    from .claudio_client import ClaudioClient
    from .orchestrator import Orchestrator

    # Resolve the Projects V2 board before anything else: a board that Clams
    # cannot drive must stop startup, not fail per-item mid-run.
    board = asyncio.run(board_from_settings(s))

    gh = GitHubClient(s.github.owner, s.github.repo, s.github.token,
                     s.github.api_base, board=board)
    claudio = ClaudioClient(s.planner.base_url, s.planner.auth_token,
                           s.planner.request_timeout)

    def llm(prompt: str) -> str:
        return asyncio.run(claudio.chat(prompt)).final_output

    planner = PlannerService(store, gh, s, llm_call=llm)
    intake = IntakeService(store, gh, s)
    review = ReviewService(store, gh, s,
                          judge=planner.judge_review if planner else None)
    steering = SteeringService(store, s, gh=gh, planner=planner, intake=intake)
    return Orchestrator(store, bus, s, gh=gh, planner=planner, intake=intake,
                       steering=steering, review=review)


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
                          task_runner=ClaudioTaskRunner(claudio), gh=gh,
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
                         task_runner=ClaudioTaskRunner(claudio), gh=gh,
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
    svc = ReviewService(store, gh, s)
    verdict = asyncio.run(svc.review_requirement(args.requirement))
    print(verdict.summary())
    store.db.close()
    return 0 if verdict.passed else 1


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
    "healthcheck": cmd_healthcheck,
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "log_level", "INFO"))
    handler = HANDLERS[args.cmd]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
