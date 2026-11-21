"""Planner main loop (§9). Cycle every ``planner_cycle_s``; every step indexed.

1 drain commands (interrupt priority)
2 if paused -> only steps 6-8
3 intake (every N cycles)
4 plan workflows in intake/planning (bounded LLM budget)
5 promote ready + dispatch
6 consume clams:events (batch, block) -> idempotent fenced apply
7 reap expired leases + question expiry
8 outbox relay
9 watchdog probes (interval)
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Optional

from .. import contracts
from ..bus import Bus
from ..claudio_client import ClaudioClient
from ..db import (repo_events, repo_runtime, repo_tasks, repo_workflows)
from ..db.conn import connect, immediate, migrate
from . import commands as commands_mod
from . import dispatch as dispatch_mod
from . import plan as plan_mod
from . import reaper as reaper_mod
from .review import apply_event, review_workflow, strand_dependents
from .watchdog import Watchdog

log = logging.getLogger("clams.loop")


class PlannerLoop:
    def __init__(self, settings, bus: Bus, claudio_p: Optional[ClaudioClient],
                 *, gh_intake=None, claudio_e: Optional[ClaudioClient] = None,
                 gh_client=None):
        self.s = settings
        self.bus = bus
        self.cp = claudio_p
        self.ce = claudio_e
        self.gh_intake = gh_intake          # callable(conn) -> int
        self.gh_client = gh_client
        if self.gh_intake is None and gh_client is not None:
            from ..github.intake import run_intake
            self.gh_intake = lambda conn: run_intake(conn, gh_client, settings)
        self.conn = connect(settings.db_path)
        migrate(self.conn)
        self.outbox = None                  # set by run_forever / tests
        self.watchdog = Watchdog(bus, claudio_p, claudio_e, settings)
        self._stop = threading.Event()
        self._cycle_n = 0
        if hasattr(bus, "ensure_group"):
            bus.ensure_group(settings.stream_events, settings.group_planner)

    # ------------------------------------------------------------------ cycle
    def cycle(self) -> dict:
        s = self.s
        stats: dict[str, int] = {}
        self._cycle_n += 1
        paused = repo_runtime.is_paused(self.conn)

        # 1. commands (interrupt priority, applied even when paused)
        stats["commands"] = commands_mod.drain_commands(
            self.conn, gh_client=self.gh_client)

        # 2-5. planning work only when not paused
        if not paused:
            # 3. intake every N cycles
            if self.gh_intake and self._cycle_n % s.intake_every_n_cycles == 0:
                try:
                    stats["intake"] = self.gh_intake(self.conn)
                except Exception as exc:  # noqa: BLE001
                    log.warning("intake failed: %s", exc)
            # 4. plan workflows awaiting planning (bounded LLM budget)
            stats["planned"] = self._plan_pending()
            # 5. promote + dispatch
            stats["dispatched"] = len(
                dispatch_mod.dispatch_ready(self.conn, self.bus, s))

        # 6. consume events
        stats["events"] = self._consume_events()

        # 7. reaper
        gh_on = self.gh_client is not None
        stats["reaped"] = len(reaper_mod.reap_expired_leases(
            self.conn, s, gh_enabled=gh_on))
        reaper_mod.expire_questions(self.conn, s, gh_enabled=gh_on)

        # 7b. self-heal workflows stranded by pre-fix failures
        stats["swept"] = self._sweep_deadlocked()

        # 8. outbox relay
        if self.outbox is not None:
            stats["outbox"] = self.outbox.relay_once(self.conn)

        # retention: prune events / sent outbox / processed_events (daily)
        self._maybe_prune()

        # 9. watchdog
        wd = self.watchdog.maybe_probe(self.conn)
        if wd:
            stats["watchdog"] = sum(1 for v in wd.values() if v == "fail")

        # liveness heartbeat
        with immediate(self.conn):
            repo_runtime.heartbeat(self.conn)
        return stats

    def _plan_pending(self) -> int:
        budget = self.s.planning_llm_budget_per_cycle
        n = 0
        if self.cp is None:
            return 0
        for wf in repo_workflows.list_by_status(self.conn, "intake", "planning"):
            if budget <= 0:
                break
            if wf["status"] == "intake":
                repo_workflows.set_status(self.conn, wf["workflow_id"],
                                          "planning")
            repo = self._repo_for(wf)
            base_ref = self._base_ref_for(wf)
            try:
                status = plan_mod.plan_workflow(self.conn, self.cp, self.s,
                                                wf["workflow_id"], repo,
                                                base_ref)
                if status == "running" and self.gh_client is not None:
                    from ..github import projection
                    projection.project_tasks_created(self.conn,
                                                     wf["workflow_id"], repo)
            except Exception as exc:  # noqa: BLE001
                log.exception("planning %s failed: %s", wf["workflow_id"], exc)
            budget -= 1
            n += 1
        return n

    def _sweep_deadlocked(self) -> int:
        """Self-heal running workflows that missed a review.

        Covers two shapes: (a) tasks stranded by a pre-fix failure (pending
        dependents of a failed/blocked/cancelled task) — block them, then
        review; (b) all tasks terminal but no event ever triggered review
        (e.g. a lone task cancelled by command). Idempotent and cheap:
        only running workflows, indexed queries.
        """
        rows = repo_workflows.list_by_status(self.conn, "running")
        n = 0
        for wf in rows:
            wid = wf["workflow_id"]
            with immediate(self.conn):
                strand_dependents(self.conn, wid)
                if repo_tasks.workflow_tasks_all_terminal(self.conn, wid):
                    review_workflow(self.conn, wid, self.s,
                                    gh_enabled=self.gh_client is not None)
            n += 1
        return n

    def _maybe_prune(self) -> None:
        import time
        from ..db.conn import get_runtime_state, set_runtime_state
        from ..db import repo_events as ev
        from ..db import repo_outbox as ob
        last = get_runtime_state(self.conn, "retention_last_prune")
        if last and time.time() - float(last) < 86400:
            return
        with immediate(self.conn):
            ev.prune_events(self.conn, self.s.retention_events_days)
            ev.prune_processed(self.conn, self.s.retention_processed_days)
            ob.prune_sent(self.conn, self.s.retention_sent_outbox_days)
            set_runtime_state(self.conn, "retention_last_prune",
                              str(int(time.time())))

    def _repo_for(self, wf) -> str:
        row = self.conn.execute("SELECT repo FROM projects WHERE project_id=?",
                                (wf["project_id"],)).fetchone()
        return row["repo"] if row else "unknown/repo"

    def _base_ref_for(self, wf) -> str:
        data = self.s.load_projects_yaml()
        for p in data.get("projects", []):
            row = self.conn.execute(
                "SELECT repo FROM projects WHERE project_id=?",
                (wf["project_id"],)).fetchone()
            if row and p.get("repo") == row["repo"]:
                return p.get("base_ref", "main")
        return "main"

    def _consume_events(self) -> int:
        s = self.s
        got = self.bus.read(s.stream_events, s.group_planner, "planner",
                            count=s.events_batch, block_ms=s.events_block_ms)
        n = 0
        for d in got:
            try:
                msg = contracts.parse_message(d.payload)
            except contracts.ContractError as exc:
                log.error("poison event %s: %s", d.id, exc)
                self.bus.push_dlq(s.stream_dlq, d.payload, f"undecodable: {exc}")
                self.bus.ack(s.stream_events, s.group_planner, d.id)
                continue
            if d.deliveries > s.dlq_max_deliveries:
                self.bus.push_dlq(s.stream_dlq, d.payload, "max deliveries")
                self.bus.ack(s.stream_events, s.group_planner, d.id)
                continue
            try:
                apply_event(self.conn, msg, d.id, s,
                            gh_enabled=self.gh_client is not None)
            except Exception as exc:  # noqa: BLE001
                log.exception("event apply failed, leaving unacked: %s", exc)
                continue
            # ack only after the DB transaction committed (apply_event returned)
            self.bus.ack(s.stream_events, s.group_planner, d.id)
            n += 1
        return n

    # ------------------------------------------------------------- lifecycle
    def run_forever(self) -> None:
        from ..outbox import OutboxRelay
        self.outbox = OutboxRelay(self.bus, gh_client=self.gh_client,
                                  backoff_base_s=self.s.outbox_backoff_base_s,
                                  max_attempts=self.s.outbox_max_attempts,
                                  on_subissue_created=self._on_subissue_created)
        log.info("planner loop starting (cycle=%ss)", self.s.planner_cycle_s)
        hb = threading.Thread(target=self._heartbeat_beat, daemon=True,
                              name="clams-heartbeat")
        hb.start()
        try:
            while not self._stop.is_set():
                try:
                    self.cycle()
                except Exception:  # noqa: BLE001
                    log.exception("planner cycle crashed")
                self._stop.wait(self.s.planner_cycle_s)
        finally:
            hb.join(timeout=5)

    def _heartbeat_beat(self) -> None:
        """Keep runtime_state.heartbeat fresh while a cycle is blocked on a
        long planning LLM call (a replan can outlast the 60 s healthcheck
        window). Proves process liveness; the cycle's own write still marks
        loop progress. Own connection: sqlite connections are thread-affine.
        """
        conn = connect(self.s.db_path)
        try:
            while not self._stop.wait(15):
                try:
                    with immediate(conn):
                        repo_runtime.heartbeat(conn)
                except Exception:  # noqa: BLE001
                    log.exception("heartbeat beat failed")
        finally:
            conn.close()

    def _on_subissue_created(self, task_id: str, number: int) -> None:
        with immediate(self.conn):
            self.conn.execute(
                "UPDATE tasks SET gh_issue_number=? WHERE task_id=?",
                (number, task_id))
        # invariant 2: rebuild dispatch from DB after a Redis loss
        dispatch_mod.rebuild_dispatch(self.conn, self.bus, self.s)

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self.conn.close()
