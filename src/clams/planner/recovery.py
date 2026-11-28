"""Recovery classifier: decide what a ``recover`` command must clean up.

Import discipline (the planner's import-light rule): this module imports
stdlib only at module scope — no ``httpx``, no ``fastapi`` and **no
module-level ``from ..github import ...``** (``clams.github`` pulls in httpx).
The one GitHub constant needed is imported lazily inside the body of the single
function that takes a client, so this module stays importable on its own
merit. Note that ``clams.planner.__init__`` imports the planner loop, which
pulls in pydantic; that is pre-existing and outside this file's control.

Everything here is a pure function of its inputs (or of a read-only DB
connection): nothing writes, nothing calls the network. The recovery command
handler composes these to act; this module only classifies.

Vocabulary
----------
task          a row of ``tasks`` (sqlite3.Row, dict or any object exposing the
              same fields). Relevant columns: ``task_id``, ``gh_issue_number``,
              ``status``, ``title``.
sub-issue     the GitHub issue a task was published as. Represented as a dict
              with at least ``number`` / ``title`` / ``task_id`` and optionally
              ``state`` ('open' | 'closed').
orphan        an OPEN sub-issue that no task of this workflow references and no
              live task elsewhere references -> nothing owns it, sweep it.
stale success an OPEN sub-issue whose task already reached ``success``; created
              before auto-close-on-success existed, so it should have been
              closed. Sweep it (close it), preserving the task row.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Iterable, Optional

__all__ = [
    "referenced_issue_numbers",
    "find_orphans",
    "find_stale_success",
    "collect_open_subissues",
    "plan_recovery",
]

SUCCESS = "success"
CANCELLED = "cancelled"
OPEN_STATE = "open"

#: fields carried through when a sub-issue row is normalised to a plain dict
_SUB_FIELDS = ("number", "title", "task_id", "state", "url", "html_url")


# ----------------------------------------------------------------- row helpers
def _field(row: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a dict, ``sqlite3.Row`` or plain object.

    Tolerant on purpose: classifiers get rows from the DB, dicts from tests and
    issue payloads from the GitHub client, and must never raise on a missing
    column.
    """
    if row is None:
        return default
    if isinstance(row, Mapping):
        value = row.get(name, default)
    else:
        try:
            value = row[name]
        except (KeyError, IndexError, TypeError):
            value = getattr(row, name, default)
    return default if value is None else value


def _as_int(value: Any) -> Optional[int]:
    """Coerce to int; ``None``/``bool``/junk -> ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_dict(row: Any) -> dict:
    """Normalise a sub-issue (dict / ``sqlite3.Row`` / object) to a plain dict."""
    if isinstance(row, Mapping):
        return dict(row)
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            return {k: row[k] for k in keys()}
        except (KeyError, IndexError, TypeError):
            pass
    out: dict[str, Any] = {}
    for name in _SUB_FIELDS:
        value = _field(row, name)
        if value is not None:
            out[name] = value
    return out


def _is_open(row: Any) -> bool:
    """True unless the payload explicitly says the issue is not open.

    A missing ``state`` counts as open: callers that derive sub-issues from the
    DB have already filtered, and the gh path filters before calling.
    """
    state = _field(row, "state")
    if state is None:
        return True
    return str(state).strip().lower() == OPEN_STATE


def _sub_number(row: Any) -> Optional[int]:
    """The issue number of a sub-issue (``number`` or ``gh_issue_number``)."""
    return _as_int(_field(row, "number")) or _as_int(_field(row, "gh_issue_number"))



# --------------------------------------------------------------- classifiers
def referenced_issue_numbers(tasks: Iterable[Any]) -> set[int]:
    """Every ``gh_issue_number`` a workflow's tasks reference.

    ``None`` and ``0`` are ignored: a task that was never published (or a row
    with a placeholder) must not make a real sub-issue look referenced, and must
    not become an issue number of its own.
    """
    numbers: set[int] = set()
    for task in tasks or ():
        number = _as_int(_field(task, "gh_issue_number"))
        if number:
            numbers.add(number)
    return numbers


def find_orphans(open_subissues: Iterable[Any], tasks: Iterable[Any],
                 live_tasks: Iterable[Any] = ()) -> list[dict]:
    """Open sub-issues that no live task owns.

    A sub-issue is an orphan when **no task of this workflow** references its
    number **and** no live task elsewhere (``live_tasks``) references it either —
    the second check is what stops a task that legitimately moved to another
    workflow (or is mid-replan in a sibling) from being swept out from under it.

    Sub-issues whose payload says they are already closed are never returned:
    there is nothing to clean up.
    """
    owned = referenced_issue_numbers(tasks)
    live = referenced_issue_numbers(live_tasks)
    orphans: list[dict] = []
    for sub in open_subissues or ():
        if not _is_open(sub):
            continue
        number = _sub_number(sub)
        if not number or number in owned or number in live:
            continue
        orphans.append(_as_dict(sub))
    return orphans


def find_stale_success(open_subissues: Iterable[Any],
                       tasks: Iterable[Any]) -> list[dict]:
    """Open sub-issues whose task has already reached ``success``.

    These are leftovers from before auto-close-on-success existed: the task is
    done, the issue was never closed. Matching is by issue number first and falls
    back to ``task_id`` so a sub-issue still resolves when its task row lost (or
    never had) the number. Closed sub-issues are never swept.
    """
    status_by_number: dict[int, str] = {}
    status_by_task: dict[str, str] = {}
    for task in tasks or ():
        status = _field(task, "status")
        number = _as_int(_field(task, "gh_issue_number"))
        task_id = _field(task, "task_id")
        if number:
            status_by_number[number] = str(status)
        if task_id:
            status_by_task[str(task_id)] = str(status)

    stale: list[dict] = []
    for sub in open_subissues or ():
        if not _is_open(sub):
            continue
        number = _sub_number(sub)
        status = status_by_number.get(number) if number else None
        if status is None:
            task_id = _field(sub, "task_id")
            status = status_by_task.get(str(task_id)) if task_id else None
        if status == SUCCESS:
            stale.append(_as_dict(sub))
    return stale



# ------------------------------------------------------------- DB / gh reads
def _tasks_for_workflow(conn, workflow_id: str) -> list[Any]:
    """All task rows of a workflow, ordered by ``sequence_index``."""
    return conn.execute(
        "SELECT * FROM tasks WHERE workflow_id=? ORDER BY sequence_index",
        (workflow_id,)).fetchall()


def _repo_for_workflow(conn, workflow_id: str) -> Optional[str]:
    """``owner/name`` of the project the workflow belongs to (None if absent)."""
    row = conn.execute(
        "SELECT p.repo FROM workflows w "
        "JOIN projects p ON p.project_id = w.project_id "
        "WHERE w.workflow_id=?", (workflow_id,)).fetchone()
    return _field(row, "repo")


def _open_subissues_from_db(conn, workflow_id: str) -> list[dict]:
    """Sub-issue state derived from the DB alone (no network).

    A task row with a non-null ``gh_issue_number`` whose status is not
    ``'cancelled'`` is treated as an open sub-issue. This is the conservative
    offline view: without GitHub we cannot see issues that were closed behind
    our back, only the ones we know were published and not cancelled.
    """
    rows = conn.execute(
        "SELECT task_id, title, gh_issue_number FROM tasks "
        "WHERE workflow_id=? AND gh_issue_number IS NOT NULL AND status<>? "
        "ORDER BY sequence_index", (workflow_id, CANCELLED)).fetchall()
    out: list[dict] = []
    for r in rows:
        number = _as_int(_field(r, "gh_issue_number"))
        if not number:
            continue
        out.append({"number": number,
                    "title": _field(r, "title", ""),
                    "task_id": _field(r, "task_id"),
                    "state": OPEN_STATE})
    return out


def _open_subissues_from_gh(conn, workflow_id: str, gh_client) -> list[dict]:
    """Sub-issue state read from GitHub: ``clams:task`` issues of this workflow.

    ``CLAMS_TASK_LABEL`` is imported here — inside the function body — so that
    importing this module never pulls in ``clams.github`` (and therefore httpx).
    """
    from ..github.client import CLAMS_TASK_LABEL  # lazy: httpx lives behind it

    repo = _repo_for_workflow(conn, workflow_id)
    if not repo:
        return []

    task_id_by_number: dict[int, Any] = {}
    for task in _tasks_for_workflow(conn, workflow_id):
        number = _as_int(_field(task, "gh_issue_number"))
        if number:
            task_id_by_number[number] = _field(task, "task_id")

    issues = gh_client.list_issues_by_label(repo, CLAMS_TASK_LABEL) or []
    out: list[dict] = []
    for issue in issues:
        number = _sub_number(issue)
        if not number or number not in task_id_by_number:
            continue          # not one of this workflow's sub-issues
        if not _is_open(issue):
            continue          # closed -> nothing to sweep
        item = _as_dict(issue)
        item["number"] = number
        item["task_id"] = task_id_by_number[number]
        item["state"] = OPEN_STATE
        out.append(item)
    return out


def collect_open_subissues(conn, workflow_id: str,
                           gh_client=None) -> list[dict]:
    """The workflow's currently-open sub-issues, as ``{number, title, task_id}``.

    ``gh_client=None`` -> derive from the DB (offline, no network). With a
    client -> ask GitHub for ``clams:task`` issues, keep only this workflow's
    sub-issues that are still ``state == 'open'``.
    """
    if gh_client is None:
        return _open_subissues_from_db(conn, workflow_id)
    return _open_subissues_from_gh(conn, workflow_id, gh_client)



# ------------------------------------------------------------------ the plan
def plan_recovery(conn, workflow_id: str, gh_client=None) -> dict:
    """Classify a workflow for recovery. Read-only, idempotent, no side effects.

    Returns::

        {'orphans': [...],          # open sub-issues no live task owns
         'stale_success': [...],    # open sub-issues whose task is 'success'
         'preserved_success': [...],# task_ids already success with no open issue
         'orphan_count': int,
         'stale_success_count': int,
         'noop': bool}              # True exactly when both sweeps are empty

    ``preserved_success`` is the "already correct" set: tasks that reached
    ``success`` whose sub-issue is *not* open (auto-close did its job, or the
    task was never published). Recovery must leave those alone, so listing them
    here is what makes a second run provably a no-op rather than a re-close.

    Running this twice on an unchanged workflow returns the same answer, and
    once the sweeps have been acted on it returns ``noop=True``.
    """
    tasks = _tasks_for_workflow(conn, workflow_id)
    open_subs = collect_open_subissues(conn, workflow_id, gh_client)

    orphans = find_orphans(open_subs, tasks)
    stale_success = find_stale_success(open_subs, tasks)

    open_numbers = {n for n in (_sub_number(s) for s in open_subs) if n}
    open_task_ids = {str(_field(s, "task_id")) for s in open_subs
                     if _field(s, "task_id") is not None}

    preserved_success: list[Any] = []
    for task in tasks:
        if str(_field(task, "status")) != SUCCESS:
            continue
        task_id = _field(task, "task_id")
        number = _as_int(_field(task, "gh_issue_number"))
        still_open = bool(number and number in open_numbers) or \
            bool(task_id is not None and str(task_id) in open_task_ids)
        if not still_open:
            preserved_success.append(task_id)

    return {
        "orphans": orphans,
        "stale_success": stale_success,
        "preserved_success": preserved_success,
        "orphan_count": len(orphans),
        "stale_success_count": len(stale_success),
        "noop": not orphans and not stale_success,
    }
