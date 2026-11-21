"""Executor-runner main loop (§7): consumes clams:tasks, drives Claudio-E over
HTTP, runs the evidence gate, emits events. Stateless: no DB access.

Ack rule: ``task.dispatch`` is XACKed only after ``task.result`` (or
``task.question``) has been emitted, or on fence rejection. Start-up replays
own PEL then XAUTOCLAIMs entries idle > 2x heartbeat. Poison / over-limit
messages go to the DLQ.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from typing import Any, Optional

from .. import contracts
from ..bus import Bus, Delivered
from ..claudio_client import ClaudioClient, ClaudioError, ClaudioUnavailable
from .evidence import EvidenceGate
from .worktree import WorktreeManager

log = logging.getLogger("clams.executor")

NEEDS_INPUT_RE = re.compile(
    r"^NEEDS_INPUT:\s*(?P<q>.+?)(?:\nOPTIONS:\s*(?P<opts>.+))?\s*$",
    re.MULTILINE)


def build_prompt(brief: contracts.Brief) -> str:
    parts = [
        f"Objective: {brief.objective}",
        f"Acceptance criteria: {brief.acceptance}",
        "",
        "You work in the current directory (a git worktree). Commit all changes.",
        f"Writable paths: {', '.join(brief.writable)}",
    ]
    if brief.forbidden:
        parts.append(f"Forbidden paths (never touch): {', '.join(brief.forbidden)}")
    parts += [
        f"Verification commands that MUST pass: {'; '.join(brief.run)}",
        f"Max turns: {brief.max_turns}.",
    ]
    if brief.answer:
        parts += ["", f"The human answered your earlier question: {brief.answer}"]
    parts += [
        "",
        "If you cannot proceed without a human decision, reply with a line",
        "'NEEDS_INPUT: <question>' (optionally followed by 'OPTIONS: a | b').",
        "Otherwise do the work and finish with a short summary.",
    ]
    return "\n".join(parts)


def detect_needs_input(text: str) -> Optional[tuple[str, list[str]]]:
    m = NEEDS_INPUT_RE.search(text or "")
    if not m:
        return None
    opts = [o.strip() for o in (m.group("opts") or "").split("|") if o.strip()]
    return m.group("q").strip(), opts


class ExecutorRunner:
    def __init__(self, settings, bus: Bus, claudio: ClaudioClient,
                 wt: WorktreeManager, *, version: str = "2.0.0"):
        self.s = settings
        self.bus = bus
        self.claudio = claudio
        self.wt = wt
        self.version = version
        self.executor_id = settings.executor_id
        self._stop = threading.Event()
        self._last_reap = 0.0

    # ------------------------------------------------------------------ setup
    def startup(self) -> None:
        if hasattr(self.bus, "ensure_group"):
            self.bus.ensure_group(self.s.stream_tasks, self.s.group_executors)
        # replay own PEL, then claim stragglers from dead runners
        for d in self.bus.replay_pending(self.s.stream_tasks,
                                         self.s.group_executors,
                                         self.executor_id):
            self._handle(d)
        idle_ms = 2 * self.s.heartbeat_interval_s * 1000
        for d in self.bus.autoclaim(self.s.stream_tasks, self.s.group_executors,
                                    self.executor_id, idle_ms):
            self._handle(d)

    def run_forever(self) -> None:
        # executor.alive must flow even while a task blocks the loop (no
        # duration timeout anymore) — including startup PEL replay; otherwise
        # the watchdog's executors probe declares the runner dead mid-task
        threading.Thread(target=self._alive_beat, daemon=True,
                         name="clams-alive").start()
        self.startup()
        while not self._stop.is_set():
            self._cycle()

    def _alive_beat(self) -> None:
        while not self._stop.wait(self.s.executor_alive_interval_s):
            try:
                self._emit_alive()
            except Exception:  # noqa: BLE001
                log.exception("alive beat failed")

    def stop(self) -> None:
        self._stop.set()

    def _cycle(self) -> None:
        self._maybe_reap()
        got = self.bus.read(self.s.stream_tasks, self.s.group_executors,
                            self.executor_id, count=self.s.executor_capacity,
                            block_ms=1000)
        for d in got:
            self._handle(d)

    # ---------------------------------------------------------------- handlers
    def _handle(self, d: Delivered) -> None:
        try:
            msg = contracts.parse_message(d.payload)
        except contracts.ContractError as exc:
            log.error("poison message %s: %s", d.id, exc)
            self.bus.push_dlq(self.s.stream_dlq, d.payload, f"undecodable: {exc}")
            self.bus.ack(self.s.stream_tasks, self.s.group_executors, d.id)
            return
        if d.deliveries > self.s.dlq_max_deliveries:
            self.bus.push_dlq(self.s.stream_dlq, d.payload,
                              f"max deliveries {d.deliveries}")
            self.bus.ack(self.s.stream_tasks, self.s.group_executors, d.id)
            return
        if msg.type == "task.cancel":
            self.bus.ack(self.s.stream_tasks, self.s.group_executors, d.id)
            return
        if msg.type != "task.dispatch":
            self.bus.ack(self.s.stream_tasks, self.s.group_executors, d.id)
            return
        self.run_task(msg, d)

    def run_task(self, msg: contracts.TaskDispatch, d: Delivered) -> None:
        brief = msg.brief
        session_ref = str(uuid.uuid4())
        worktree: Optional[Any] = None
        try:
            worktree, branch = self.wt.add(brief.repo, msg.task_id,
                                           msg.attempt, brief.base_ref)
        except Exception as exc:  # noqa: BLE001
            self._emit_result(msg, "failed", session_ref="", branch="",
                              evidence={"reason": f"worktree: {exc}"},
                              log_tail=str(exc), error_code="infra:worktree")
            self.bus.ack(self.s.stream_tasks, self.s.group_executors, d.id)
            return

        self._emit("task.started", msg, session_ref=session_ref,
                   extra={"executor_id": self.executor_id})
        hb = self._start_heartbeat(msg, session_ref)
        try:
            # no duration timeout: a complex task runs as long as it keeps
            # emitting heartbeats; the agent's own max_turns bounds the work.
            # Death is detected planner-side (heartbeat silence), not here.
            resp = self.claudio.chat(
                build_prompt(brief), session_id=session_ref,
                approval_mode="off", workspace_dir=str(worktree),
                timeout=None)
            final = resp.final_output
        except ClaudioUnavailable as exc:      # subclass: must come first
            self._stop_heartbeat(hb)
            self._emit_result(msg, "failed", session_ref=session_ref,
                              branch=str(worktree),
                              evidence={"reason": f"claudio: {exc}"},
                              log_tail=str(exc),
                              error_code="claudio:unavailable")
            self.bus.ack(self.s.stream_tasks, self.s.group_executors, d.id)
            return
        except ClaudioError as exc:
            self._stop_heartbeat(hb)
            code = ("claudio:auth" if getattr(exc, "status", 0) in (401, 403)
                    else "claudio:error")
            self._emit_result(msg, "failed", session_ref=session_ref,
                              branch=str(worktree),
                              evidence={"reason": f"claudio: {exc}"},
                              log_tail=str(exc), error_code=code)
            self.bus.ack(self.s.stream_tasks, self.s.group_executors, d.id)
            return
        self._stop_heartbeat(hb)

        ni = detect_needs_input(final)
        if ni:
            question, options = ni
            self._emit("task.question", msg, session_ref=session_ref,
                       extra={"question": question, "options": options,
                              "idempotency_key":
                                  f"q:{msg.task_id}:{msg.attempt}"})
            self.bus.ack(self.s.stream_tasks, self.s.group_executors, d.id)
            self.wt.remove(brief.repo, msg.task_id, msg.attempt)
            return

        gate = EvidenceGate(self.wt)
        evidence = gate.run(worktree, brief.model_dump(), brief.base_ref)
        outcome = "success" if evidence["passed"] else "failed"
        gate_code = "" if outcome == "success" else \
            f"gate:{str(evidence.get('reason', ''))[:40]}"
        commit_sha = ""
        branch = self.wt.branch_name(msg.task_id, msg.attempt)
        try:
            commit_sha = self.wt.head_commit(worktree)
        except Exception:  # noqa: BLE001
            pass
        if outcome == "success":
            try:
                self.wt.push(worktree, branch)
            except Exception as exc:  # noqa: BLE001
                evidence["push_error"] = str(exc)
        self._emit_result(msg, outcome, session_ref=session_ref, branch=branch,
                          commit_sha=commit_sha, evidence=evidence,
                          changed_files=evidence.get("changed_files", []),
                          log_tail=final[-2000:], error_code=gate_code)
        self.bus.ack(self.s.stream_tasks, self.s.group_executors, d.id)
        if outcome == "success":
            self.wt.remove(brief.repo, msg.task_id, msg.attempt)
        # failed worktree kept for keep_failed_worktree_hours

    # ----------------------------------------------------------------- events
    def _emit(self, mtype: str, msg: contracts.TaskDispatch, *,
              session_ref: str = "", extra: Optional[dict] = None) -> None:
        cls = {
            "task.started": contracts.TaskStarted,
            "task.heartbeat": contracts.TaskHeartbeat,
            "task.question": contracts.TaskQuestion,
            "task.result": contracts.TaskResult,
        }[mtype]
        fields: dict[str, Any] = {
            "task_id": msg.task_id, "attempt": msg.attempt,
            "lease_token": msg.lease_token,
            **(extra or {}),
        }
        if "session_ref" in cls.model_fields:
            fields["session_ref"] = session_ref
        m = cls(**fields)
        self.bus.publish(self.s.stream_events, contracts.dump_message(m))

    def _emit_result(self, msg: contracts.TaskDispatch, outcome: str, *,
                     session_ref: str, branch: str, evidence: dict,
                     commit_sha: str = "", changed_files: Optional[list] = None,
                     log_tail: str = "", error_code: str = "") -> None:
        self._emit("task.result", msg, session_ref=session_ref,
                   extra={"outcome": outcome, "evidence": evidence,
                          "commit_sha": commit_sha, "branch": branch,
                          "changed_files": changed_files or [],
                          "log_tail": log_tail, "error_code": error_code})

    def _session_progress(self, session_ref: str) -> int:
        """Real progress signal: count of session messages (turns/tool calls).
        -1 when unreadable — the planner's stall probe ignores unknown."""
        try:
            return len(self.claudio.session_messages(session_ref, limit=500))
        except Exception:  # noqa: BLE001
            return -1

    def _start_heartbeat(self, msg: contracts.TaskDispatch,
                         session_ref: str) -> tuple:
        done = threading.Event()

        def beat():
            t0 = time.monotonic()
            while not done.wait(self.s.heartbeat_interval_s):
                self._emit("task.heartbeat", msg,
                           extra={"turns_used":
                                      self._session_progress(session_ref),
                                  "elapsed_s": int(time.monotonic() - t0)})
        t = threading.Thread(target=beat, daemon=True)
        t.start()
        return (done, t)

    @staticmethod
    def _stop_heartbeat(hb: tuple) -> None:
        hb[0].set()

    def _emit_alive(self) -> None:
        m = contracts.ExecutorAlive(executor_id=self.executor_id,
                                    capacity=self.s.executor_capacity,
                                    version=self.version)
        self.bus.publish(self.s.stream_events, contracts.dump_message(m))

    def _maybe_reap(self) -> None:
        now = time.monotonic()
        if now - self._last_reap < 300:
            return
        self._last_reap = now
        self.wt.reap_old_failures(self.s.keep_failed_worktree_hours)
