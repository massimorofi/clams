"""Outbound projection (§8): sub-issues, labels, board, comments — all via the
outbox with idempotency key ``gh:<kind>:<task_id>:<attempt>:<state>``.

Sub-issue lifecycle keys are the exception to the ``<attempt>:<state>`` shape
and are fixed, because replan/plan passes re-run them and must dedupe:
``gh:gh_subissue:<task_id>:created`` (provision) and
``gh:gh_subissue:<task_id>:closed`` (close after a replan superseded the task).
"""
from __future__ import annotations

import json

from ..db import repo_outbox, repo_tasks, repo_workflows
from .client import (CLAMS_TASK_LABEL, STATUS_BLOCKED, STATUS_DONE,
                     STATUS_IN_PROGRESS, STATUS_REVIEW)


def task_body(task) -> str:
    brief = json.loads(task["brief_json"])
    return (
        f"Clams task `{task['task_id']}` (workflow `{task['workflow_id']}`, "
        f"#{task['sequence_index']})\n\n"
        f"**Objective:** {task['objective']}\n\n"
        f"**Acceptance:** {task['acceptance']}\n\n"
        f"**Verify:**\n```\n" + "\n".join(brief.get("run", [])) + "\n```\n\n"
        f"**Writable:** {', '.join(brief.get('writable', []))}\n")


def resolve_repo(conn, workflow_id: str, repo: str = "") -> str:
    """Repo slug for a workflow, falling back to its project row.

    Callers pass ``repo`` when they already know it (planner, commands). When
    they do not - or the workflow has no project row / the row has no repo -
    this returns ``""`` instead of raising, so projection stays best-effort.
    """
    if repo:
        return repo
    wf = repo_workflows.get_workflow(conn, workflow_id)
    if wf is None:
        return ""
    row = conn.execute("SELECT repo FROM projects WHERE project_id=?",
                       (wf["project_id"],)).fetchone()
    return (row["repo"] if row else "") or ""


def project_tasks_created(conn, workflow_id: str, repo: str) -> int:
    """Create one sub-issue per task (via outbox). Returns enqueued count.

    Strictly idempotent: a task that already carries a ``gh_issue_number`` is
    skipped (its sub-issue exists), and a task whose ``gh_subissue`` row is
    already in the outbox is skipped too - ``enqueue`` dedupes on
    ``gh:gh_subissue:<task_id>:created`` and returns ``None``, which is what
    keeps a retried replan/plan pass from double-counting (and from GitHub
    seeing the same key twice). So calling this twice for the same workflow
    enqueues exactly one row per task and the second call returns 0.
    """
    wf = repo_workflows.get_workflow(conn, workflow_id)
    if wf is None or not wf["gh_issue_number"]:
        return 0
    repo = resolve_repo(conn, workflow_id, repo)
    n = 0
    for t in repo_tasks.list_tasks(conn, workflow_id):
        if t["gh_issue_number"]:
            continue
        key = f"gh:gh_subissue:{t['task_id']}:created"
        payload = {"repo": repo, "parent": wf["gh_issue_number"],
                   "task_id": t["task_id"], "title": t["title"],
                   "body": task_body(t),
                   "labels": [CLAMS_TASK_LABEL, STATUS_IN_PROGRESS],
                   "idempotency_key": key}
        if repo_outbox.enqueue(conn, "gh_subissue", payload,
                               idempotency_key=key) is None:
            continue  # already queued on an earlier pass: dedup, do not count
        n += 1
    return n


SUPERSEDED_COMMENT = (
    "Superseded by a replan.\n\n"
    "Task `{task_id}` (workflow `{workflow_id}`) is no longer part of the "
    "current plan, so this sub-issue is being closed. The work it described "
    "was replaced by the replanned task set - nothing was executed from it "
    "after the replan.\n")


def project_tasks_superseded(conn, workflow_id: str, repo: str,
                             stale_task_ids: list[str]) -> int:
    """Close the sub-issues of tasks a replan superseded (via outbox).

    ``stale_task_ids`` is the ``'stale'`` list from
    ``repo_tasks.reconcile_plan`` (titles that disappeared from the plan; the
    rows are cancelled, never deleted). Only tasks that actually got a GitHub
    sub-issue can be closed, so a task without ``gh_issue_number`` - and an
    unknown ``task_id`` - is skipped silently.

    Idempotent on ``gh:gh_subissue:<task_id>:closed``: re-running with the same
    ids enqueues nothing new. Returns the number of *newly enqueued* rows.
    """
    if not stale_task_ids:
        return 0
    wf = repo_workflows.get_workflow(conn, workflow_id)
    if wf is None or not wf["gh_issue_number"]:
        return 0
    repo = resolve_repo(conn, workflow_id, repo)
    if not repo:
        # No way to address the issue: skip rather than enqueue a row the relay
        # could only fail. A workflow with no project config must not raise.
        return 0
    n = 0
    for task_id in stale_task_ids:
        if not task_id:
            continue
        t = repo_tasks.get_task(conn, task_id)
        if t is None or t["workflow_id"] != workflow_id:
            continue
        if not t["gh_issue_number"]:
            continue  # never provisioned a sub-issue: nothing to close
        key = f"gh:gh_subissue:{task_id}:closed"
        payload = {"repo": repo, "issue": t["gh_issue_number"],
                   "comment": SUPERSEDED_COMMENT.format(
                       task_id=task_id, workflow_id=workflow_id),
                   "idempotency_key": key}
        if repo_outbox.enqueue(conn, "gh_close", payload,
                               idempotency_key=key) is None:
            continue  # already queued: dedup, do not count
        n += 1
    return n


def project_task_state(conn, task_id: str, repo: str, state: str,
                       attempt: int) -> None:
    """Mirror a task's terminal state onto its sub-issue (labels + comment)."""
    t = repo_tasks.get_task(conn, task_id)
    if t is None or not t["gh_issue_number"]:
        return
    label = {"success": STATUS_DONE, "failed": STATUS_BLOCKED,
             "waiting_input": STATUS_REVIEW}.get(state)
    if label:
        key = f"gh:gh_label:{task_id}:{attempt}:{state}"
        repo_outbox.enqueue(
            conn, "gh_label",
            {"repo": repo, "issue": t["gh_issue_number"], "labels": [label],
             "idempotency_key": key}, idempotency_key=key)


# Board-driven projects: workflow state -> Projects V2 Status option name.
# "blocked" lands on In review (board has no Blocked column; user decision).
BOARD_STATE_OPTION = {"running": "In progress", "review": "In review",
                      "done": "Done", "blocked": "In review",
                      "cancelled": "Backlog"}


def project_workflow_state(conn, workflow_id: str, repo: str, state: str) -> None:
    wf = repo_workflows.get_workflow(conn, workflow_id)
    if wf is None or not wf["gh_issue_number"]:
        return
    row = conn.execute(
        "SELECT gh_project_node_id FROM projects WHERE project_id=?",
        (wf["project_id"],)).fetchone()
    node_id = row["gh_project_node_id"] if row else None
    if node_id:
        # board-only: the card column IS the status; no status:* labels
        option = BOARD_STATE_OPTION.get(state)
        if option:
            key = f"gh:gh_board_move:wf:{workflow_id}:{state}"
            repo_outbox.enqueue(
                conn, "gh_board_move",
                {"project_node_id": node_id, "issue": wf["gh_issue_number"],
                 "option_name": option, "idempotency_key": key},
                idempotency_key=key)
        return
    label = {"done": STATUS_DONE, "blocked": STATUS_BLOCKED,
             "cancelled": "status:Cancelled"}.get(state)
    if label:
        key = f"gh:gh_label:wf:{workflow_id}:{state}"
        repo_outbox.enqueue(
            conn, "gh_label",
            {"repo": repo, "issue": wf["gh_issue_number"], "labels": [label],
             "idempotency_key": key}, idempotency_key=key)
