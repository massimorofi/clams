"""Human-in-the-loop chat steering (spec §3.2, FR-1, §8.1).

Chat text from the Planner Web UI is parsed into a ``CommandEnvelope`` and applied
to orchestrator state. Parsing is deterministic (regex) first, with an optional
LLM fallback for fuzzy phrasing; an unrecognised utterance never mutates state.

Interrupt priority (spec §8.1): the background loop drains the command stream
**before** processing each queued event, so a pause takes effect immediately.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .config import Settings
from .db import Store

# ---- recognised actions ---------------------------------------------
ACTION_STATUS = "status"
ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"
ACTION_CANCEL_TASK = "cancel_task"
ACTION_ADD_REQUIREMENT = "add_requirement"
ACTION_UPDATE_CRITERIA = "update_criteria"
ACTION_REPLAN = "replan"
ACTION_UNKNOWN = "unknown"


@dataclass
class ParsedCommand:
    action: str
    args: dict[str, Any]
    raw_text: str
    matched: bool = True


# ---- deterministic parser -------------------------------------------
# Patterns are ordered most-specific first: "stop task 3" must be read as a task
# cancellation, not as the global "stop" pause.
_RE_TASK = re.compile(r"^(?:cancel|stop|kill)\s+task\s+#?(\d+)\b\s*(.*)$", re.IGNORECASE)
_RE_ADD_REQ = re.compile(r"^add\s+requirement\s*:\s*(?P<text>.+)$", re.IGNORECASE)
_RE_CRITERIA = re.compile(
    r"^(?:change|update|set)\s+acceptance\s+criteria\s+(?:for|of)\s+#?(\d+)\s+"
    r"(?:to|as)\s+(?P<text>.+)$",
    re.IGNORECASE,
)
_RE_REPLAN = re.compile(
    r"^(?:re-?plan)\s+(?:requirement\s+)?#?(\d+)\b.*$", re.IGNORECASE
)
_RE_PAUSE = re.compile(
    r"^(?:please\s+)?(?:pause|halt|suspend|stop)\b\s*(.*)$", re.IGNORECASE
)
_RE_RESUME = re.compile(
    r"^(?:please\s+)?(?:resume|continue|unpause)\b\s*(.*)$", re.IGNORECASE
)
_RE_STATUS = re.compile(
    r"^(?:(?:what\s+(?:is|'s)|show(?:me)?|check)\s+)?(?:the\s+)?"
    r"(?:overall\s+|global\s+)?(?:status|state)\b\s*(.*)$",
    re.IGNORECASE,
)
_RE_REQ_NUM = re.compile(r"(?:requirement|req|item)\s+#?(\d+)", re.IGNORECASE)


def _req_from(rest: str) -> Optional[int]:
    m = _RE_REQ_NUM.search(rest or "")
    if m:
        return int(m.group(1))
    m = re.search(r"#(\d+)", rest or "")
    if m:
        return int(m.group(1))
    return None


def parse_command(text: str) -> ParsedCommand:
    """Parse a chat utterance into a command. Never raises."""
    if not text or not text.strip():
        return ParsedCommand(ACTION_UNKNOWN, {}, text or "", matched=False)
    t = text.strip()

    m = _RE_TASK.match(t)
    if m:
        return ParsedCommand(
            ACTION_CANCEL_TASK,
            {
                "task_number": int(m.group(1)),
                "replan": bool(re.search(r"re-?plan", m.group(2) or "", re.IGNORECASE)),
            },
            text, matched=True,
        )

    m = _RE_ADD_REQ.match(t)
    if m:
        return ParsedCommand(ACTION_ADD_REQUIREMENT,
                            {"text": m.group("text").strip()}, text, matched=True)

    m = _RE_CRITERIA.match(t)
    if m:
        return ParsedCommand(
            ACTION_UPDATE_CRITERIA,
            {"requirement_number": int(m.group(1)), "text": m.group("text").strip()},
            text, matched=True,
        )

    m = _RE_REPLAN.match(t)
    if m:
        return ParsedCommand(ACTION_REPLAN,
                            {"requirement_number": int(m.group(1))}, text, matched=True)

    m = _RE_PAUSE.match(t)
    if m:
        args: dict[str, Any] = {}
        req = _req_from(m.group(1))
        if req is not None:
            args["requirement_number"] = req
        return ParsedCommand(ACTION_PAUSE, args, text, matched=True)

    m = _RE_RESUME.match(t)
    if m:
        args = {}
        req = _req_from(m.group(1))
        if req is not None:
            args["requirement_number"] = req
        return ParsedCommand(ACTION_RESUME, args, text, matched=True)

    m = _RE_STATUS.match(t)
    if m:
        args = {}
        req = _req_from(m.group(1) or "")
        if req is not None:
            args["requirement_number"] = req
        return ParsedCommand(ACTION_STATUS, args, text, matched=True)

    return ParsedCommand(ACTION_UNKNOWN, {}, text, matched=False)


def llm_parse_command(text: str, llm_call: Callable[[str], str]) -> ParsedCommand:
    """Fallback parse via the LLM, expecting a strict JSON command object."""
    import json

    prompt = (
        "Classify the user's operational request into exactly one JSON object:\n"
        '{"action": "status|pause|resume|cancel_task|add_requirement|'
        'update_criteria|replan", "args": {...}}\n'
        "For status/replan/update_criteria include \"requirement_number\": int.\n"
        "For cancel_task include \"task_number\": int and \"replan\": bool.\n"
        "For add_requirement/update_criteria include \"text\": string.\n"
        "If nothing matches use {\"action\": \"unknown\", \"args\": {}}.\n"
        "Return JSON only.\n\n"
        f"USER: {text}\n"
    )
    try:
        raw = llm_call(prompt)
        from .decompose import extract_json
        obj = extract_json(raw)
        action = str(obj.get("action", ACTION_UNKNOWN))
        args = obj.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        return ParsedCommand(action, args, text, matched=action != ACTION_UNKNOWN)
    except Exception:
        return ParsedCommand(ACTION_UNKNOWN, {}, text, matched=False)


class SteeringService:
    """Applies parsed chat commands to orchestrator state."""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        gh=None,
        planner=None,
        intake=None,
        llm_call: Optional[Callable[[str], str]] = None,
    ):
        self.store = store
        self.settings = settings
        self.gh = gh
        self.planner = planner
        self.intake = intake
        self.llm_call = llm_call

    # -- entry point -----------------------------------------------------
    async def handle(self, text: str) -> str:
        """Parse and apply a chat message; returns the reply shown in the UI."""
        parsed = parse_command(text)
        if not parsed.matched and self.llm_call is not None:
            parsed = llm_parse_command(text, self.llm_call)
        return await self.apply(parsed)

    async def apply(self, cmd: ParsedCommand) -> str:
        handler = {
            ACTION_STATUS: self._do_status,
            ACTION_PAUSE: self._do_pause,
            ACTION_RESUME: self._do_resume,
            ACTION_CANCEL_TASK: self._do_cancel_task,
            ACTION_ADD_REQUIREMENT: self._do_add_requirement,
            ACTION_UPDATE_CRITERIA: self._do_update_criteria,
            ACTION_REPLAN: self._do_replan,
        }.get(cmd.action)

        if handler is None:
            return (
                "I didn't recognise that command. Try: 'status of requirement #12', "
                "'pause execution', 'resume', 'cancel task 3 and re-plan', "
                "'add requirement: ...', or 'change acceptance criteria for #12 to ...'."
            )
        return await handler(cmd)

    # -- actions --------------------------------------------------------
    async def _do_pause(self, cmd: ParsedCommand) -> str:
        req = cmd.args.get("requirement_number")
        if req is None:
            # global pause: every tracked requirement
            self.store.db.conn.execute("UPDATE work_items SET user_paused = 1")
            return "⏸ Execution paused. No new tasks will be dispatched. " \
                   "Already-running executors are untouched."
        self.store.work_items.set_paused(req, True)
        return f"⏸ Requirement #{req} paused. No new tasks will be dispatched for it."

    async def _do_resume(self, cmd: ParsedCommand) -> str:
        req = cmd.args.get("requirement_number")
        if req is None:
            self.store.db.conn.execute(
                "UPDATE work_items SET user_paused = 0")
            return "▶ Execution resumed. Dispatching is enabled again."
        self.store.work_items.set_paused(req, False)
        return f"▶ Requirement #{req} resumed."

    async def _do_status(self, cmd: ParsedCommand) -> str:
        req = cmd.args.get("requirement_number")
        if req is None:
            return self._global_status()
        item = self.store.work_items.get(req)
        if item is None:
            return f"Requirement #{req} is not tracked."
        lines = [
            f"Requirement #{req}: {item.requirement_title}",
            f"  local status : {item.status}",
            f"  github status: {item.gh_status}",
            f"  paused       : {'yes' if item.user_paused else 'no'}",
            f"  re-plans     : {item.replans}",
            f"  sub-tasks    : {len(item.task_numbers)}",
        ]
        for tn in item.task_numbers:
            claim = self.store.task_claims.get(tn)
            if claim:
                lines.append(
                    f"    task #{tn}: claimed by {claim['executor_id']} "
                    f"until {claim['lease_expires_at']} "
                    f"(attempts {claim['attempts']}/{claim['max_attempts']})"
                )
            else:
                lines.append(f"    task #{tn}: unclaimed")
        return "\n".join(lines)

    def _global_status(self) -> str:
        items = self.store.work_items.all()
        active = self.store.task_claims.active()
        alerts = self.store.alerts.list_unacked(severity="critical")
        stats = self.store.outbox.stats()
        return (
            f"Tracked requirements: {len(items)}\n"
            f"Active task leases   : {len(active)}\n"
            f"Outbox               : {stats['pending']} pending, "
            f"{stats['sent']} sent, {stats['failed']} failed\n"
            f"Unacked critical     : {len(alerts)}"
        )

    async def _do_cancel_task(self, cmd: ParsedCommand) -> str:
        task_number = int(cmd.args["task_number"])
        should_replan = bool(cmd.args.get("replan"))

        claim = self.store.task_claims.get(task_number)
        req = claim["requirement_number"] if claim else cmd.args.get("requirement_number")

        # revoke the lease
        self.store.task_claims.release(task_number)
        # mark the child issue Blocked
        if self.gh is not None:
            try:
                from .gh import STATUS_BLOCKED
                await self.gh.set_status(task_number, STATUS_BLOCKED)
            except Exception:  # noqa: BLE001
                pass
        # drop it from the requirement's task list
        if req is not None:
            item = self.store.work_items.get(req)
            if item is not None:
                remaining = [t for t in item.task_numbers if t != task_number]
                self.store.work_items.set_plan(
                    req, item.plan_json or "{}", remaining
                )

        msg = f"⏹ Task #{task_number} cancelled: lease revoked and issue marked Blocked."
        if should_replan and req is not None:
            if self.planner is not None:
                try:
                    plan, new_tasks = await self.planner.replan(
                        req, reason=f"user cancelled task #{task_number}"
                    )
                    msg += f" Re-planned into {len(new_tasks)} task(s)."
                except Exception as exc:  # noqa: BLE001
                    msg += f" Re-plan failed: {exc}"
            else:
                msg += " No planner available to re-plan."
        return msg

    async def _do_add_requirement(self, cmd: ParsedCommand) -> str:
        text = (cmd.args.get("text") or "").strip()
        if not text:
            return "Nothing to add — say 'add requirement: <text>'."
        if self.intake is None:
            return "No intake service available to add requirements."
        number = await self.intake.add_requirement(title=text, body=text)
        return f"➕ Requirement #{number} created and queued for intake."

    async def _do_update_criteria(self, cmd: ParsedCommand) -> str:
        req = cmd.args.get("requirement_number")
        text = (cmd.args.get("text") or "").strip()
        if req is None or not text:
            return "Say 'change acceptance criteria for #12 to <text>'."
        if self.planner is None:
            return "No planner available to update acceptance criteria."
        await self.planner.update_acceptance_criteria(req, text)
        return (
            f"✔ Acceptance criteria for requirement #{req} updated. "
            f"Executor briefs will be refreshed on the next turn."
        )

    async def _do_replan(self, cmd: ParsedCommand) -> str:
        req = cmd.args.get("requirement_number")
        if req is None:
            return "Say 're-plan requirement #12'."
        if self.planner is None:
            return "No planner available to re-plan."
        plan, tasks = await self.planner.replan(req, reason="user requested re-plan")
        return f"🔁 Requirement #{req} re-planned into {len(tasks)} task(s)."
