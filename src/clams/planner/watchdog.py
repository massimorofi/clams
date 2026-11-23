"""Zero-LLM watchdog inside the planner process (§9.9).

Probes: Redis ping, Claudio-P/E /health, executor liveness, oldest-ready age,
outbox dead count, DLQ length, disk space, model-server reachability, DB size,
replans-at-cap, daily DB integrity quick-check.

Remediation ladder: alert -> pause dispatch (paused_by=watchdog) -> auto-resume
only if paused_by=watchdog and healthy for N consecutive probes. An operator
pause is never auto-cleared.

Liveness monitor (check_liveness): the watchdog — NOT the executor — owns the
task lease. It renews running tasks' leases from external deterministic
evidence (the Claudio session API: message count / last-ts growth + health).
A frozen session is never punished: the lease keeps renewing and a human gets
an evidence bundle + alert to decide. An unreachable claudio means INFRA
failure: the task is requeued WITHOUT burning its attempt.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from typing import Optional

import httpx

from ..db import (repo_alerts, repo_events, repo_executors, repo_outbox,
                  repo_runtime, repo_stall_evidence, repo_tasks)
from ..db.conn import (db_integrity_ok, get_runtime_state, immediate,
                       iso_to_epoch, set_runtime_state)

log = logging.getLogger("clams.watchdog")

KEY_CONSEC_HEALTHY = "watchdog_consecutive_healthy"
KEY_LAST_INTEGRITY = "watchdog_last_integrity"
KEY_LIVENESS_STATE = "liveness_state"

#: probes whose failure pauses dispatch.
#: NOTE: oldest_ready is deliberately NOT critical — pausing dispatch makes
#: ready tasks age, which would re-trip the probe and deadlock the pause.
#: It alerts only.
CRITICAL = {"redis", "claudio_planner", "claudio_executor", "dlq"}

#: alert prefixes whose subject is a task; hygiene clears them when the task
#: is gone or no longer running.
TASK_ALERT_PREFIXES = ("stall:", "turns:", "attempts:", "infra:", "orphan:")

#: consecutive health failures before an infra requeue (one blip is noise)
DOWN_STREAK_TO_REQUEUE = 2


class Watchdog:
    def __init__(self, bus, claudio_p, claudio_e, settings,
                 disk_path: Optional[str] = None):
        self.bus = bus
        self.cp = claudio_p
        self.ce = claudio_e
        self.s = settings
        self.disk_path = disk_path or settings.data_dir
        self._last_probe = 0.0
        self._last_liveness = 0.0

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
        results["model_server"] = "ok" if self._probe_model_server() else "fail"
        results["db_size"] = (
            "ok" if self._db_size_mb() <= self.s.max_db_size_mb else "fail")
        results["replans"] = "ok" if not self._replans_at_cap(conn) else "fail"
        # daily integrity quick-check
        last = get_runtime_state(conn, KEY_LAST_INTEGRITY)
        if last is None or time.time() - float(last) > 86400:
            ok = db_integrity_ok(conn)
            results["db_integrity"] = "ok" if ok else "fail"
            set_runtime_state(conn, KEY_LAST_INTEGRITY, str(int(time.time())))

        with immediate(conn):
            self._ladder(conn, results)
            self._alert_hygiene(conn)
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

    # ------------------------------------------------------------- new probes
    def _probe_model_server(self) -> bool:
        """OpenAI-compatible model server reachability. Empty URL = disabled."""
        url = (self.s.model_server_url or "").strip()
        if not url:
            return True
        base = url.rstrip("/")
        probe = base + "/models" if base.endswith("/v1") else base + "/health"
        try:
            return httpx.get(probe, timeout=5.0).status_code < 500
        except Exception:
            return False

    def _db_size_mb(self) -> float:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.s.db_path + suffix)
            except OSError:
                pass
        return total / (1024 * 1024)

    def _replans_at_cap(self, conn) -> list[str]:
        """Running workflows that exhausted their replan budget with
        non-terminal tasks left — the next failure will block them."""
        rows = conn.execute(
            "SELECT workflow_id FROM workflows w WHERE w.status='running' "
            "AND w.replans >= ? AND EXISTS (SELECT 1 FROM tasks t WHERE "
            "t.workflow_id = w.workflow_id AND t.status NOT IN "
            "('success','failed','blocked','cancelled'))",
            (self.s.max_replans,)).fetchall()
        return [r["workflow_id"] for r in rows]

    # --------------------------------------------------------- alert hygiene
    def _alert_hygiene(self, conn) -> int:
        """Clear open alerts whose subject is gone (stale stall/turns/attempts
        for a finished or deleted task; workflow alert once unblocked)."""
        cleared = 0
        for a in repo_alerts.list_open(conn):
            probe = a["probe"]
            if probe.startswith(TASK_ALERT_PREFIXES):
                tid = probe.split(":", 1)[1]
                task = repo_tasks.get_task(conn, tid)
                if task is None or task["status"] != "running":
                    cleared += repo_alerts.clear_alert(conn, probe)
            elif probe == "workflow":
                n = conn.execute(
                    "SELECT COUNT(*) n FROM workflows WHERE status='blocked'"
                ).fetchone()["n"]
                if n == 0:
                    cleared += repo_alerts.clear_alert(conn, probe)
        return cleared

    # ------------------------------------------------------ liveness monitor
    def maybe_check_liveness(self, conn) -> Optional[int]:
        now = time.monotonic()
        if now - self._last_liveness < self.s.watchdog_probe_interval_s:
            return None
        self._last_liveness = now
        return self.check_liveness(conn)

    def _load_liveness_state(self, conn) -> dict:
        raw = get_runtime_state(conn, KEY_LIVENESS_STATE)
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _save_liveness_state(self, conn, state: dict) -> None:
        set_runtime_state(conn, KEY_LIVENESS_STATE, json.dumps(state))

    def check_liveness(self, conn) -> int:
        """Evidence-based lease renewal + stall policy. Returns the number of
        tasks needing attention (stall alerts raised/escalated + infra
        requeues). Never kills: a frozen session keeps its lease and waits
        for a human; only a proven-dead claudio requeues (attempt preserved).
        """
        state = self._load_liveness_state(conn)
        running = repo_tasks.by_status(conn, "running")
        running_ids = {r["task_id"] for r in running}
        # drop state for tasks that stopped running
        for tid in [t for t in state if t not in running_ids]:
            state.pop(tid)
        attention = 0
        now = time.time()
        for row in running:
            tid = row["task_id"]
            st = state.get(tid) or {}
            if st.get("attempt") != row["attempt"]:
                st = {"attempt": row["attempt"]}     # new attempt: fresh window
            if self.ce is None or not row["session_ref"]:
                # no evidence source must never burn an attempt: fail open
                repo_tasks.renew_lease(conn, tid, row["attempt"],
                                       row["lease_token"],
                                       self.s.lease_running_extend_s)
                state[tid] = st
                continue
            if not self.ce.health():
                st["down_streak"] = st.get("down_streak", 0) + 1
                if st["down_streak"] >= DOWN_STREAK_TO_REQUEUE:
                    with immediate(conn):
                        requeued = repo_tasks.requeue_infra(conn, tid)
                        if requeued is not None:
                            repo_alerts.raise_alert(
                                conn, f"infra:{tid}", "warn",
                                f"task {tid} requeued without burning attempt "
                                f"{row['attempt']}: claudio unreachable "
                                "(watchdog evidence check)")
                            repo_events.append_event(
                                conn, "task", tid, "warn",
                                f"infra requeue: claudio unreachable "
                                f"(attempt {row['attempt']} preserved)")
                            attention += 1
                    state.pop(tid, None)
                else:
                    state[tid] = st
                continue
            st["down_streak"] = 0
            # evidence: session message count + last-ts (ts primary: count
            # saturates at the fetch limit)
            count, last_ts, err = self._session_evidence(row["session_ref"])
            grew = (err is None and count >= 0
                    and (count != st.get("count")
                         or (last_ts is not None and last_ts != st.get("last_ts"))))
            if st.get("count") is None:
                grew = True                      # first observation: baseline
            st["count"], st["last_ts"] = count, last_ts
            # runner-death signal: the executor's beat thread dies with its
            # process, so beat silence + no session progress = orphaned task
            # (e.g. the runner container was recreated mid-task). Requeue
            # free: nobody is driving this task anymore.
            if self._beats_silent(row):
                if not grew:
                    with immediate(conn):
                        if repo_tasks.requeue_infra(conn, tid) is not None:
                            repo_alerts.raise_alert(
                                conn, f"infra:{tid}", "warn",
                                f"task {tid} requeued without burning attempt "
                                f"{row['attempt']}: orphaned — executor beat "
                                "silent and session evidence not advancing")
                            repo_events.append_event(
                                conn, "task", tid, "warn",
                                f"orphan requeue: no executor beats, no "
                                f"session progress (attempt {row['attempt']} "
                                "preserved)")
                            attention += 1
                    state.pop(tid, None)
                    continue
                repo_alerts.raise_alert(
                    conn, f"orphan:{tid}", "warn",
                    f"task {tid}: executor silent but the claudio run is "
                    "still advancing — no runner will gate its result; "
                    "restart the executor or cancel the task")
            # lease renewed from evidence (or fail-open while frozen: a long
            # silent process is allowed; a human decides via the alert)
            repo_tasks.renew_lease(conn, tid, row["attempt"],
                                   row["lease_token"],
                                   self.s.lease_running_extend_s)
            if grew:
                st["frozen_since"] = None
                if st.get("stall_severity"):
                    repo_alerts.clear_alert(conn, f"stall:{tid}")
                    st.pop("stall_severity", None)
                self._per_task_checks(conn, row, st)
                state[tid] = st
                continue
            # frozen -------------------------------------------------------
            if st.get("frozen_since") is None:
                st["frozen_since"] = now
            frozen_min = (now - st["frozen_since"]) / 60.0
            if frozen_min >= self.s.stall_alert_minutes:
                sev = st.get("stall_severity")
                want = ("error" if frozen_min >= self.s.stall_escalate_minutes
                        else "warn")
                if sev != want:
                    bundle = self._build_bundle(conn, row, st, count, last_ts,
                                                err, frozen_min)
                    with immediate(conn):
                        repo_stall_evidence.upsert(conn, tid, row["attempt"],
                                                   bundle)
                        if sev and sev != want:
                            # raise_alert dedupes while open: clear the old
                            # severity first so the upgrade lands
                            repo_alerts.clear_alert(conn, f"stall:{tid}")
                        repo_alerts.raise_alert(
                            conn, f"stall:{tid}", want,
                            f"task {tid} (attempt {row['attempt']}) session "
                            f"evidence frozen {int(frozen_min)} min "
                            f"(turns={self._turns_of(row)}) — evidence stored; "
                            "decide via UI: cancel or retry")
                        repo_events.append_event(
                            conn, "task", tid,
                            "warn" if want == "warn" else "error",
                            f"stalled: no session progress "
                            f"{int(frozen_min)}m ({want})")
                    st["stall_severity"] = want
                    attention += 1
            self._per_task_checks(conn, row, st)
            state[tid] = st
        self._save_liveness_state(conn, state)
        return attention

    def _session_evidence(self, session_ref: str):
        """(count, last_ts, err) from the Claudio session API. Never raises."""
        try:
            msgs = self.ce.session_messages(session_ref,
                                            limit=self.s.evidence_tail_limit)
        except Exception as e:                    # incl. ClaudioUnavailable
            return -1, None, str(e)[:200]
        count = len(msgs)
        last_ts = None
        if msgs:
            last_ts = str(msgs[-1].get("ts") or msgs[-1].get("created_at")
                          or "") or None
        return count, last_ts, None

    def _beats_silent(self, row) -> bool:
        """True when the executor-runner stopped proving itself: its beat
        thread dies with the process, so heartbeat silence beyond the grace
        means nobody is driving this task anymore."""
        ts = None
        try:
            hb = json.loads(row["heartbeat_json"] or "{}")
            ts = hb.get("ts") or None
        except (ValueError, TypeError, KeyError):
            pass
        ts = ts or row["started_at"]
        if not ts:
            return False
        try:
            return (time.time() - iso_to_epoch(ts)) > self.s.orphan_silence_s
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _turns_of(row) -> int:
        try:
            return json.loads(row["heartbeat_json"] or "{}").get("turns_used", -1)
        except (ValueError, TypeError, KeyError):
            return -1

    def _per_task_checks(self, conn, row, st: dict) -> None:
        """Deterministic per-task alerts (never kill): turn budget, final
        attempt."""
        turns = self._turns_of(row)
        budget = self.s.max_turns
        try:
            budget = json.loads(row["brief_json"] or "{}").get(
                "max_turns") or self.s.max_turns
        except (ValueError, TypeError, KeyError):
            pass
        if turns >= 0 and turns > budget:
            repo_alerts.raise_alert(
                conn, f"turns:{row['task_id']}", "warn",
                f"task {row['task_id']} used {turns} turns, over the "
                f"max_turns budget of {budget} — advisory only, task not "
                "killed; consider cancelling")
        elif turns >= 0:
            repo_alerts.clear_alert(conn, f"turns:{row['task_id']}")
        if row["attempt"] >= row["max_attempts"]:
            repo_alerts.raise_alert(
                conn, f"attempts:{row['task_id']}", "warn",
                f"task {row['task_id']} is on its final attempt "
                f"({row['attempt']}/{row['max_attempts']})")

    def _build_bundle(self, conn, row, st, count, last_ts, err,
                      frozen_min) -> dict:
        tail = []
        try:
            msgs = self.ce.session_messages(row["session_ref"], limit=5)
            tail = [{"role": m.get("role") or m.get("author"),
                     "ts": m.get("ts") or m.get("created_at"),
                     "content": str(m.get("content", ""))[:200]}
                    for m in msgs]
        except Exception as e:
            tail = [{"error": str(e)[:200]}]
        live = [r["executor_id"] for r in repo_executors.live_executors(
            conn, max_idle_s=3 * self.s.executor_alive_interval_s)]
        return {
            "task_id": row["task_id"], "attempt": row["attempt"],
            "status": row["status"], "lease_expires_at": row["lease_expires_at"],
            "heartbeat_json": row["heartbeat_json"],
            "session": {"count": count, "last_ts": last_ts, "error": err,
                        "tail": tail},
            "claudio_executor_health": bool(self.ce and self.ce.health()),
            "claudio_planner_health": bool(self.cp is None or self.cp.health()),
            "executors_live": live,
            "model_server_ok": self._probe_model_server(),
            "frozen_since": st.get("frozen_since"),
            "frozen_minutes": int(frozen_min),
            "settings": {"stall_alert_minutes": self.s.stall_alert_minutes,
                         "max_turns": self.s.max_turns},
        }
