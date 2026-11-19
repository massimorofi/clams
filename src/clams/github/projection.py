"""Outbound projection (§8): sub-issues, labels, board, comments — all via the
outbox with idempotency key ``gh:<kind>:<task_id>:<attempt>:<state>``.
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


def project_tasks_created(conn, workflow_id: str, repo: str) -> int:
    """Create one sub-issue per task (via outbox). Returns enqueued count."""
    wf = repo_workflows.get_workflow(conn, workflow_id)
    if wf is None or not wf["gh_issue_number"]:
        return 0
    n = 0
    for t in repo_tasks.list_tasks(conn, workflow_id):
        if t["gh_issue_number"]:
            continue
        repo_outbox.enqueue(
            conn, "gh_subissue",
            {"repo": repo, "parent": wf["gh_issue_number"],
             "task_id": t["task_id"], "title": t["title"],
             "body": task_body(t),
             "labels": [CLAMS_TASK_LABEL, STATUS_IN_PROGRESS],
             "idempotency_key": f"gh:gh_subissue:{t['task_id']}:created"},
            idempotency_key=f"gh:gh_subissue:{t['task_id']}:created")
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
