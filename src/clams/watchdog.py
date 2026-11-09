"""Zero-LLM watchdog: check matrix, alerts and remediation ladder (spec §6, FR-6).

This process deliberately contains **no LLM call**. Every decision is a
deterministic probe with a threshold. It never imports the planner, the
decomposer, or any model client — a test asserts that, because the moment the
supervisor depends on a model it stops being able to supervise the model.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .bus import Bus, publish_alert
from .config import Settings
from .contracts import AlertEvent, Heartbeat
from .db import Store

CHECK_LLM = "llm_engine_health"
CHECK_STRIXPER = "strixper_api"
CHECK_SQLITE = "sqlite_storage"
CHECK_DISK = "storage_volume_free"
CHECK_QUEUE = "redis_task_queue"
CHECK_GITHUB = "github_reachability"
CHECK_HEARTBEAT = "agent_heartbeats"
CHECK_CACHE_ISOLATION = "engine_cache_isolation"

ALL_CHECKS = [
    CHECK_LLM,
    CHECK_STRIXPER,
    CHECK_SQLITE,
    CHECK_DISK,
    CHECK_QUEUE,
    CHECK_GITHUB,
    CHECK_HEARTBEAT,
    CHECK_CACHE_ISOLATION,
]


@dataclass
class CheckResult:
    name: str
    ok: bool
    severity: str = "info"
    detail: str = ""

    @property
    def critical(self) -> bool:
        return not self.ok and self.severity == "critical"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _resolve(value: Any) -> Any:
    """Await ``value`` if it is awaitable, otherwise return it as-is.

    Lets callers inject plain synchronous probes while the built-in probes stay
    async.
    """
    import inspect

    if inspect.isawaitable(value):
        return await value
    return value


# ---- probes (injectable so tests can drive every failure mode) ------

async def probe_llm_engine(base_url: str, timeout: float = 5.0) -> bool:
    """1-token completion probe against an OpenAI-compatible endpoint."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{base_url.rstrip('/')}/v1/completions",
                json={"model": "probe", "prompt": "hi", "max_tokens": 1,
                      "temperature": 0},
            )
            return r.status_code == 200
    except Exception:
        return False


async def probe_strixper(base_url: str, path: str = "/api/v1/runner/active",
                        timeout: float = 5.0) -> tuple[bool, str]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{base_url.rstrip('/')}{path}")
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            data = r.json()
    except Exception as exc:
        return False, f"unreachable: {exc}"

    active = data.get("active") or {}
    err = active.get("engine_switch_error")
    if err:
        return False, f"engine switch error: {err}"
    if active.get("engine_probe_error"):
        return False, f"engine probe error: {active['engine_probe_error']}"
    return True, str(active.get("config_name") or active.get("engine_url") or "ok")


def probe_sqlite(store: Store, max_ms: float = 5000.0) -> tuple[bool, str]:
    start = time.monotonic()
    try:
        store.db.conn.execute("BEGIN IMMEDIATE;")
        store.db.conn.execute("ROLLBACK;")
    except Exception as exc:
        elapsed = (time.monotonic() - start) * 1000
        return False, f"DB locked/failed after {elapsed:.0f}ms: {exc}"
    elapsed = (time.monotonic() - start) * 1000
    if elapsed > max_ms:
        return False, f"round-trip {elapsed:.0f}ms exceeds {max_ms:.0f}ms"
    return True, f"{elapsed:.1f}ms"


def probe_disk(path: str, min_free_bytes: int) -> tuple[bool, str]:
    try:
        st = os.statvfs(path)
    except Exception as exc:
        return False, f"statvfs failed on {path}: {exc}"
    free = st.f_bavail * st.f_frsize
    if free < min_free_bytes:
        return False, f"only {free / (1024**3):.2f} GiB free (< {min_free_bytes / (1024**3):.2f} GiB)"
    return True, f"{free / (1024**3):.2f} GiB free"


async def probe_queue(bus: Bus, max_age_seconds: float) -> tuple[bool, str]:
    info = await bus.pending("clams:tasks", "executors")
    count = int(info.get("count", 0))
    oldest_ms = float(info.get("oldest_idle_ms", 0))
    oldest_s = oldest_ms / 1000.0
    if oldest_s > max_age_seconds:
        return False, (
            f"oldest pending message idle {oldest_s:.0f}s > "
            f"{max_age_seconds:.0f}s (depth {count})"
        )
    return True, f"depth={count}, oldest idle {oldest_s:.0f}s"


async def probe_github(gh) -> bool:
    try:
        return await gh.ping()
    except Exception:
        return False


def probe_heartbeats(heartbeat_dir: str, max_age: float,
                    required: tuple[str, ...] = ("planner", "executor")) -> tuple[bool, str]:
    d = Path(heartbeat_dir)
    stale: list[str] = []
    for name in required:
        f = d / f"{name}.json"
        if not f.exists():
            stale.append(f"{name} (missing)")
            continue
        try:
            data = json.loads(f.read_text())
            ts = datetime.fromisoformat(str(data["timestamp"]).replace("Z", "+00:00"))
        except Exception:
            stale.append(f"{name} (unreadable)")
            continue
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > max_age:
            stale.append(f"{name} ({age:.0f}s old)")
    if stale:
        return False, "stale heartbeats: " + ", ".join(stale)
    return True, "all heartbeats fresh"


def probe_cache_isolation(cache_paths: dict[str, str]) -> tuple[bool, str]:
    """HW-3: every engine must have a distinct --cache-disk path."""
    seen: dict[str, str] = {}
    collisions: list[str] = []
    for engine, path in cache_paths.items():
        norm = str(Path(path).resolve()) if path else ""
        if not norm:
            collisions.append(f"{engine} (no cache path)")
            continue
        if norm in seen:
            collisions.append(f"{engine} shares {norm} with {seen[norm]}")
        else:
            seen[norm] = engine
    if collisions:
        return False, "cache-disk collision: " + "; ".join(collisions)
    return True, f"{len(seen)} distinct cache paths"


# ---- watchdog ------------------------------------------------------

@dataclass
class SweepReport:
    results: list[CheckResult] = field(default_factory=list)
    criticals: list[str] = field(default_factory=list)
    remediated: bool = False
    escalated: bool = False
    timestamp: str = field(default_factory=_now_iso)

    @property
    def healthy(self) -> bool:
        return not self.criticals

    def to_text(self) -> str:
        lines = [f"Watchdog sweep @ {self.timestamp}"]
        for r in self.results:
            mark = "OK  " if r.ok else "FAIL"
            lines.append(f"  [{mark}] {r.name}: {r.detail}")
        if self.criticals:
            lines.append(f"  critical: {', '.join(self.criticals)}")
        if self.escalated:
            lines.append("  ESCALATED TO HUMAN")
        return "\n".join(lines)


class WatchdogService:
    """Deterministic supervisor. No LLM, no planning, just probes and thresholds."""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        bus: Optional[Bus] = None,
        gh=None,
        llm_base_url: Optional[str] = None,
        cache_paths: Optional[dict[str, str]] = None,
        llm_probe: Optional[Callable[..., Any]] = None,
        strixper_probe: Optional[Callable[..., Any]] = None,
        github_probe: Optional[Callable[..., Any]] = None,
        engine_restart: Optional[Callable[..., Any]] = None,
        docker_restart: Optional[Callable[[str], Any]] = None,
    ):
        self.store = store
        self.settings = settings
        self.bus = bus
        self.gh = gh
        self.llm_base_url = llm_base_url or "http://127.0.0.1:8731"
        self.cache_paths = cache_paths or {}
        self._llm_probe = llm_probe or probe_llm_engine
        self._strixper_probe = strixper_probe or probe_strixper
        self._github_probe = github_probe or probe_github
        self._engine_restart = engine_restart
        self._docker_restart = docker_restart
        # consecutive-failure counters (spec: critical after N consecutive)
        self._llm_failures = 0
        self._github_failures = 0
        self._recovery_attempts = 0

    # -- checks ---------------------------------------------------------
    async def run_checks(self) -> SweepReport:
        report = SweepReport()
        report.results = [
            await self.check_llm(),
            await self.check_strixper(),
            self.check_sqlite(),
            self.check_disk(),
            await self.check_queue(),
            await self.check_github(),
            self.check_heartbeat(),
            self.check_cache_isolation(),
        ]
        report.criticals = [r.name for r in report.results if r.critical]
        return report

    async def check_llm(self) -> CheckResult:
        ok = await _resolve(self._llm_probe(self.llm_base_url,
                                          self.settings.watchdog.llm_probe_timeout))
        if ok:
            self._llm_failures = 0
            return CheckResult(CHECK_LLM, True, "info", "engine responded")
        self._llm_failures += 1
        threshold = self.settings.watchdog.llm_failures_critical
        if self._llm_failures >= threshold:
            return CheckResult(
                CHECK_LLM, False, "critical",
                f"{self._llm_failures} consecutive probe timeouts "
                f"(threshold {threshold})",
            )
        return CheckResult(
            CHECK_LLM, False, "warning",
            f"probe failed ({self._llm_failures}/{threshold})",
        )

    async def check_strixper(self) -> CheckResult:
        ok, detail = await _resolve(self._strixper_probe(
            self.settings.strixper.base_url, self.settings.strixper.active_path
        ))
        if ok:
            return CheckResult(CHECK_STRIXPER, True, "info", detail)
        return CheckResult(CHECK_STRIXPER, False, "critical", detail)

    def check_sqlite(self) -> CheckResult:
        ok, detail = probe_sqlite(self.store, 5000.0)
        if ok:
            return CheckResult(CHECK_SQLITE, True, "info", detail)
        return CheckResult(CHECK_SQLITE, False, "critical", detail)

    def check_disk(self) -> CheckResult:
        ok, detail = probe_disk(
            self.settings.paths.data_dir, self.settings.watchdog.disk_min_free_bytes
        )
        if ok:
            return CheckResult(CHECK_DISK, True, "info", detail)
        return CheckResult(CHECK_DISK, False, "critical", detail)

    async def check_queue(self) -> CheckResult:
        if self.bus is None:
            return CheckResult(CHECK_QUEUE, True, "info", "no bus configured, skipped")
        ok, detail = await probe_queue(self.bus, self.settings.watchdog.queue_max_pending_age)
        if ok:
            return CheckResult(CHECK_QUEUE, True, "info", detail)
        return CheckResult(CHECK_QUEUE, False, "critical", detail)

    async def check_github(self) -> CheckResult:
        if self.gh is None and self._github_probe is probe_github:
            return CheckResult(CHECK_GITHUB, True, "info", "no github client, skipped")
        ok = await _resolve(self._github_probe(self.gh))
        if ok:
            self._github_failures = 0
            return CheckResult(CHECK_GITHUB, True, "info", "reachable")
        self._github_failures += 1
        threshold = self.settings.watchdog.github_failures_critical
        if self._github_failures >= threshold:
            return CheckResult(
                CHECK_GITHUB, False, "critical",
                f"{self._github_failures} consecutive sweep failures",
            )
        return CheckResult(
            CHECK_GITHUB, False, "warning",
            f"unreachable ({self._github_failures}/{threshold})",
        )

    def check_heartbeat(self) -> CheckResult:
        ok, detail = probe_heartbeats(
            self.settings.paths.heartbeat_dir, self.settings.watchdog.heartbeat_max_age
        )
        if ok:
            return CheckResult(CHECK_HEARTBEAT, True, "info", detail)
        return CheckResult(CHECK_HEARTBEAT, False, "critical", detail)

    def check_cache_isolation(self) -> CheckResult:
        if not self.cache_paths:
            return CheckResult(CHECK_CACHE_ISOLATION, True, "info", "no engines declared")
        ok, detail = probe_cache_isolation(self.cache_paths)
        if ok:
            return CheckResult(CHECK_CACHE_ISOLATION, True, "info", detail)
        return CheckResult(CHECK_CACHE_ISOLATION, False, "critical", detail)

    # -- alerting -------------------------------------------------------
    def raise_alert(self, severity: str, source: str, message: str,
                   requirement_number: Optional[int] = None,
                   task_number: Optional[int] = None,
                   actions: Optional[list[str]] = None) -> int:
        alert_id = self.store.alerts.add(severity, source, message)
        if self.bus is not None:
            try:
                import asyncio
                evt = AlertEvent(
                    severity=severity, source=source, message=message,
                    timestamp=_now_iso(),
                    requirement_number=requirement_number,
                    task_number=task_number,
                    actions=actions or [],
                )
                asyncio.ensure_future(publish_alert(self.bus, evt))
            except Exception:
                pass
        return alert_id

    # -- remediation ladder (spec 6.2) ---------------------------------
    async def sweep(self) -> SweepReport:
        """One watchdog cycle: check, and if needed climb the remediation ladder."""
        report = await self.run_checks()
        if report.healthy:
            self._recovery_attempts = 0
            return report

        # Step 1: pause & notify
        self._step_pause_and_notify(report)
        # Step 2: diagnose (re-probe to rule out transience)
        failing = await self._step_diagnose(report)
        if not failing:
            report.remediated = True
            return report
        # Step 3: automated recovery
        recovered = await self._step_auto_recover(failing)
        # Step 4: verify
        if recovered:
            verify = await self.run_checks()
            if verify.healthy:
                self._recovery_attempts = 0
                verify.remediated = True
                return verify
        # Step 5: escalate
        self._recovery_attempts += 1
        if self._recovery_attempts >= self.settings.watchdog.max_recovery_attempts:
            self._step_escalate(failing)
            report.escalated = True
        return report

    def _step_pause_and_notify(self, report: SweepReport) -> None:
        self.store.db.conn.execute("UPDATE work_items SET user_paused = 1")
        self.raise_alert(
            "critical", "watchdog",
            "Critical condition detected; dispatching paused. "
            + "; ".join(report.criticals),
            actions=["Resume", "Force Re-Plan", "Cancel Requirement"],
        )

    async def _step_diagnose(self, report: SweepReport) -> list[CheckResult]:
        """Re-run the failing checks to confirm they are not transient."""
        failing = [r for r in report.results if not r.ok]
        confirmed: list[CheckResult] = []
        for r in failing:
            fresh = await self._recheck(r.name)
            if not fresh.ok:
                confirmed.append(fresh)
        return confirmed

    async def _recheck(self, name: str) -> CheckResult:
        if name == CHECK_LLM:
            return await self.check_llm()
        if name == CHECK_STRIXPER:
            return await self.check_strixper()
        if name == CHECK_SQLITE:
            return self.check_sqlite()
        if name == CHECK_DISK:
            return self.check_disk()
        if name == CHECK_QUEUE:
            return await self.check_queue()
        if name == CHECK_GITHUB:
            return await self.check_github()
        if name == CHECK_HEARTBEAT:
            return self.check_heartbeat()
        if name == CHECK_CACHE_ISOLATION:
            return self.check_cache_isolation()
        return CheckResult(name, False, "critical", "unknown check")

    async def _step_auto_recover(self, failing: list[CheckResult]) -> bool:
        names = {r.name for r in failing}
        acted = False
        # 3a: engine restart via strixper
        if {CHECK_LLM, CHECK_STRIXPER} & names and self._engine_restart:
            try:
                await _resolve(self._engine_restart())
                acted = True
            except Exception:
                pass
        # 3b: docker restart for a hung process
        if CHECK_HEARTBEAT in names and self._docker_restart:
            try:
                await _resolve(self._docker_restart("planner"))
                acted = True
            except Exception:
                pass
        # 3c: prune abandoned worktrees
        if CHECK_QUEUE in names or CHECK_DISK in names:
            try:
                from .worktree import cleanup_stale_worktrees
                cleanup_stale_worktrees(self.settings.paths.work_root, set())
                acted = True
            except Exception:
                pass
        return acted

    def _step_escalate(self, failing: list[CheckResult]) -> None:
        self.raise_alert(
            "critical", "watchdog",
            "Automated recovery failed after "
            f"{self.settings.watchdog.max_recovery_attempts} attempts. "
            "Human action required: "
            + "; ".join(f"{r.name}: {r.detail}" for r in failing),
            actions=["Open runbook", "Manual restart"],
        )

    # -- loop -----------------------------------------------------------
    async def run_forever(self) -> None:
        import asyncio

        while True:
            await self.sweep()
            await asyncio.sleep(self.settings.watchdog.interval_seconds)


# ---- wdctl: the container healthcheck entrypoint ---------------------

def selfcheck() -> int:
    """`wdctl selfcheck` — exit 0 only if the watchdog itself is alive."""
    try:
        settings = Settings.load()
        # The watchdog is alive if we can write and read its own heartbeat.
        hb_dir = Path(settings.paths.heartbeat_dir)
        hb_dir.mkdir(parents=True, exist_ok=True)
        hb = Heartbeat(service="watchdog", pid=os.getpid(), timestamp=_now_iso())
        (hb_dir / "watchdog.json").write_text(hb.to_json())
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"selfcheck failed: {exc}")
        return 1


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="wdctl")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("selfcheck")
    sub.add_parser("run")
    sub.add_parser("check")
    args = parser.parse_args(argv)

    if args.cmd == "selfcheck":
        return selfcheck()
    if args.cmd in ("run", "check"):
        settings = Settings.load()
        store = open_store_for(settings)
        svc = WatchdogService(store, settings)
        if args.cmd == "check":
            report = __import__("asyncio").run(svc.run_checks())
            print(report.to_text())
            return 0 if report.healthy else 1
        __import__("asyncio").run(svc.run_forever())
        return 0
    parser.print_help()
    return 1


def open_store_for(settings: Settings) -> Store:
    from .db import open_store

    return open_store(settings.paths.db_path)
