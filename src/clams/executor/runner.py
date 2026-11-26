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

from .. import contracts, prompts
from ..bus import Bus, Delivered
from ..claudio_client import ClaudioClient, ClaudioError, ClaudioUnavailable
from .evidence import EvidenceGate
from .worktree import WorktreeManager

log = logging.getLogger("clams.executor")

NEEDS_INPUT_RE = re.compile(
    r"^NEEDS_INPUT:\s*(?P<q>.+?)(?:\nOPTIONS:\s*(?P<opts>.+))?\s*$",
    re.MULTILINE)


def prompt_vars(brief: contracts.Brief) -> dict:
    """Placeholder values for the executor prompt template (clams.prompts)."""
    forbidden_line = (
        f"Forbidden paths (never touch): {', '.join(brief.forbidden)}\n"
        if brief.forbidden else "")
    answer_block = (
        f"\nThe human answered your earlier question: {brief.answer}\n"
        if brief.answer else "")
    prior_failure_block = (
        "PREVIOUS ATTEMPT FAILED THE EVIDENCE GATE:\n"
        f"{brief.prior_failure}\n"
        "The previous attempt's committed work is already on this branch — "
        "inspect it first and fix exactly these failures instead of redoing "
        "the task from scratch.\n\n"
        if brief.prior_failure else "")
    return {
        "objective": brief.objective,
        "acceptance": brief.acceptance,
        "prior_failure_block": prior_failure_block,
        "writable": ", ".join(brief.writable),
        "forbidden_line": forbidden_line,
        "run": "; ".join(brief.run),
        "max_turns": brief.max_turns,
        "answer_block": answer_block,
    }


def build_prompt(brief: contracts.Brief) -> str:
    # the planner may ship an operator-edited template on the brief; the
    # built-in default reproduces the historical prompt byte-for-byte
    template = brief.prompt_template or prompts.PROMPTS["executor_task"].default
    return prompts.render_template(template, "executor_task",
                                   **prompt_vars(brief))


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
    def _retry_base(self, brief, task_id: str, attempt: int) -> str:
        """Base ref for a new worktree: attempt N>1 seeds from attempt N-1's
        branch when it exists (work reuse), else falls back to base_ref."""
        if attempt > 1:
            prev = self.wt.branch_name(task_id, attempt - 1)
            if self.wt.ref_exists(brief.repo, prev):
                log.info("task %s attempt %d: seeding from %s",
                         task_id, attempt, prev)
                return prev
        return brief.base_ref

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
        try:
            self.run_task(msg, d)
        except Exception:  # noqa: BLE001 - a runner bug must never kill the
            # process: an unacked message would autoclaim-replay and re-run
            # the whole task on every restart (crash loop, claudio#133)
            log.exception("task %s: unhandled runner error", msg.task_id)
            try:
                self._emit_result(msg, "failed", session_ref="", branch="",
                                  evidence={"reason": "runner: internal "
                                              "error (see runner logs)"},
                                  log_tail="runner internal error",
                                  error_code="infra:runner")
                self.bus.ack(self.s.stream_tasks, self.s.group_executors,
                             d.id)
            except Exception:  # noqa: BLE001 - leave unacked for redelivery
                log.exception("task %s: degraded result emit failed",
                              msg.task_id)

    def run_task(self, msg: contracts.TaskDispatch, d: Delivered) -> None:
        brief = msg.brief
        session_ref = str(uuid.uuid4())
        worktree: Optional[Any] = None
        try:
            # retry work reuse: seed attempt N>1 from attempt N-1's branch so
            # correct work is kept and the agent fixes only what failed
            # (live finding, claudio#58 T2: a 90-min committed attempt was
            # redone from scratch because the retry started from base_ref)
            base = self._retry_base(brief, msg.task_id, msg.attempt)
            worktree, branch = self.wt.add(brief.repo, msg.task_id,
                                           msg.attempt, base)
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
        # scope is measured from the seed ref (`base`), not the workflow base:
        # on retry work reuse the branch already carries the previous attempt's
        # files, and charging them to this attempt is unsatisfiable.
        evidence = gate.run(worktree, brief.model_dump(), brief.base_ref,
                            scope_base=base)
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

    @staticmethod
    def _shrink_evidence(evidence: dict, tail: int) -> dict:
        ev = dict(evidence)
        checks = []
        for c in ev.get("checks", []):
            c = dict(c)
            ot = str(c.get("output_tail") or "")
            if tail <= 0:
                c.pop("output_tail", None)
            elif len(ot) > tail:
                c["output_tail"] = "..." + ot[-tail:]
            # the scope check embeds the whole changed-file list; a task that
            # commits dozens of artifacts made that list alone exceed the cap,
            # so stage 2 still overflowed and fell through to the stage that
            # discards every check — hiding the culprit (claudio#133).
            ch = c.get("changed")
            if isinstance(ch, list) and len(ch) > 20:
                c["changed"] = ch[:20] + [f"(+{len(ch) - 20} more)"]
            checks.append(c)
        ev["checks"] = checks
        return ev

    def _emit_result(self, msg: contracts.TaskDispatch, outcome: str, *,
                     session_ref: str, branch: str, evidence: dict,
                     commit_sha: str = "", changed_files: Optional[list] = None,
                     log_tail: str = "", error_code: str = "") -> None:
        # live finding (claudio#133): a whole-suite pytest gate produced
        # evidence far over the 16 KiB contract cap; dump_message raised and
        # the unhandled error killed the runner mid-task -> autoclaim re-run
        # -> crash loop that burned the same attempt for hours. Trim
        # progressively instead of ever dying.
        base = {"outcome": outcome, "commit_sha": commit_sha, "branch": branch,
                "error_code": error_code}
        # Even the last-resort payload has to name the culprit: stage 3 used to
        # keep only `reason`, so "run: empty output" arrived with every check
        # discarded and no way to tell WHICH command failed (claudio#133).
        reason = str((evidence or {}).get("reason") or outcome)[:300]
        fails = []
        for c in ((evidence or {}).get("checks") or []):
            if c.get("ok"):
                continue
            f = {"check": c.get("check"), "detail": c.get("detail", ""),
                 "cmd": str(c.get("cmd") or "")[:160]}
            # scope failures carry their data in `violations`, not `detail`,
            # so the last-resort payload reported "scope: failed" with nothing
            # naming the offending files (claudio#133 T5).
            v = c.get("violations")
            if v:
                f["violations"] = [str(x)[:80] for x in list(v)[:15]]
                f["violation_count"] = len(v)
            fails.append(f)
            if len(fails) >= 8:
                break
        attempts = [
            {**base, "evidence": evidence,
             "changed_files": (changed_files or [])[:200],
             "log_tail": log_tail[-4000:]},
            {**base, "evidence": {**self._shrink_evidence(evidence, 300),
                                  "trimmed": True},
             "changed_files": (changed_files or [])[:50],
             "log_tail": log_tail[-1000:]},
            {**base, "evidence": {**self._shrink_evidence(evidence, 0),
                                  "trimmed": True},
             "changed_files": [f"{len(changed_files or [])} files "
                               "(list omitted)"],
             "log_tail": ""},
            {**base, "evidence": {"reason": reason, "failed": fails,
                                  "trimmed": True},
             "changed_files": [], "log_tail": ""},
            # guaranteed to fit: reason alone, so the ladder can never exhaust
            {**base, "evidence": {"reason": reason, "trimmed": True},
             "changed_files": [], "log_tail": ""},
        ]
        for i, extra in enumerate(attempts):
            try:
                self._emit("task.result", msg, session_ref=session_ref,
                           extra=extra)
                if i:
                    log.info("task %s result emitted with evidence trimmed "
                             "(stage %d)", msg.task_id, i)
                return
            except contracts.ContractError:
                log.warning("task %s result over payload cap, trimming "
                            "(stage %d)", msg.task_id, i)

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
