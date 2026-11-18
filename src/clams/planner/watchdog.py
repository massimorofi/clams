"""Zero-LLM watchdog inside the planner process (§9.9).

Probes: Redis ping, Claudio-P/E /health, executor liveness, oldest-ready age,
outbox dead count, DLQ length, disk space, daily DB integrity quick-check.

Remediation ladder: alert -> pause dispatch (paused_by=watchdog) -> auto-resume
only if paused_by=watchdog and healthy for N consecutive probes. An operator
pause is never auto-cleared.
"""
from __future__ import annotations

import logging
import shutil
import time
from typing import Optional

from ..db import (repo_alerts, repo_events, repo_executors, repo_outbox,
                  repo_runtime, repo_tasks)
from ..db.conn import db_integrity_ok, get_runtime_state, immediate, set_runtime_state

log = logging.getLogger("clams.watchdog")

KEY_CONSEC_HEALTHY = "watchdog_consecutive_healthy"
KEY_LAST_INTEGRITY = "watchdog_last_integrity"

#: probes whose failure pauses dispatch.
#: NOTE: oldest_ready is deliberately NOT critical — pausing dispatch makes
#: ready tasks age, which would re-trip the probe and deadlock the pause.
#: It alerts only.
CRITICAL = {"redis", "claudio_planner", "claudio_executor", "dlq"}


class Watchdog:
    def __init__(self, bus, claudio_p, claudio_e, settings,
                 disk_path: Optional[str] = None):
        self.bus = bus
        self.cp = claudio_p
        self.ce = claudio_e
        self.s = settings
        self.disk_path = disk_path or settings.data_dir
        self._last_probe = 0.0

    def maybe_probe(self, conn) -> Optional[dict]:
        now = time.monotonic()
        if now - self._last_probe < self.s.watchdog_probe_interval_s:
            return None
        self._last_probe = now
        return self.probe(conn)

    def probe(self, conn) -> dict:
        results: dict[str, str] = {}
        results["redis"] = "ok" if self.bus.ping() else "fail"
        results["claudio_planner"] = "ok" if (self.cp is None or self.cp.health()) else "fail"
        results["claudio_executor"] = "ok" if (self.ce is None or self.ce.health()) else "fail"
        live = repo_executors.live_executors(
            conn, max_idle_s=3 * self.s.executor_alive_interval_s)
        results["executors"] = "ok" if live else "fail"
        age = repo_tasks.oldest_ready_age_s(conn)
        results["oldest_ready"] = (
            "fail" if age is not None and age > self.s.oldest_ready_alert_s
            else "ok")
        results["outbox"] = "fail" if repo_outbox.dead_count(conn) else "ok"
        dlq_len = self.bus.xlen(self.s.stream_dlq)
        results["dlq"] = "fail" if dlq_len else "ok"
        free_mb = shutil.disk_usage(self.disk_path).free // (1024 * 1024)
        results["disk"] = "ok" if free_mb >= self.s.min_free_disk_mb else "fail"
        # daily integrity quick-check
        last = get_runtime_state(conn, KEY_LAST_INTEGRITY)
        if last is None or time.time() - float(last) > 86400:
            ok = db_integrity_ok(conn)
            results["db_integrity"] = "ok" if ok else "fail"
            set_runtime_state(conn, KEY_LAST_INTEGRITY, str(int(time.time())))

        with immediate(conn):
            self._ladder(conn, results)
        return results

    def _ladder(self, conn, results: dict[str, str]) -> None:
        failures = [p for p, st in results.items() if st == "fail"]
        for probe in failures:
            repo_alerts.raise_alert(conn, probe, "error",
                                    f"watchdog probe failed: {probe}")
        critical = [p for p in failures if p in CRITICAL]
        if critical:
            if not repo_runtime.is_paused(conn):
                repo_runtime.pause(conn, "watchdog")
                repo_events.append_event(
                    conn, "system", "watchdog", "error",
                    f"dispatch paused: {', '.join(critical)}")
            set_runtime_state(conn, KEY_CONSEC_HEALTHY, "0")
            return
        # healthy: count consecutive clean probes
        try:
            n = int(get_runtime_state(conn, KEY_CONSEC_HEALTHY, "0")) + 1
        except ValueError:
            n = 1
        set_runtime_state(conn, KEY_CONSEC_HEALTHY, str(n))
        for probe in results:
            repo_alerts.clear_alert(conn, probe)
        if (repo_runtime.is_paused(conn)
                and repo_runtime.paused_by(conn) == "watchdog"
                and n >= self.s.watchdog_healthy_probes_to_resume):
            if repo_runtime.resume(conn, "watchdog"):
                repo_events.append_event(conn, "system", "watchdog", "info",
                                         "dispatch auto-resumed after healthy "
                                         f"probes ({n})")
            set_runtime_state(conn, KEY_CONSEC_HEALTHY, "0")
