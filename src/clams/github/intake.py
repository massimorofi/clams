"""GitHub intake (§8): poll enabled projects for Ready work.

Board-linked projects (gh_project_node_id set) are kanban-driven — the
Projects V2 Status column is the trigger and the status display:
  * card in "Ready" column    -> enqueue workflow (+ move card to In progress)
  * card dragged to "Backlog" -> cancel command
Projects without a board keep the label gestures:
  * label status:Ready        -> enqueue workflow (+ In Progress label)
  * label status:Cancelled    -> cancel command
Clams' own sub-issues (label `clams:task`) are never decomposed.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from ..db import (repo_commands, repo_events, repo_outbox, repo_projects,
                  repo_workflows)
from ..db.conn import immediate
from .client import CLAMS_TASK_LABEL, STATUS_CANCELLED, STATUS_IN_PROGRESS, \
    STATUS_READY

log = logging.getLogger("clams.intake")

# Projects V2 Status option names (board-driven projects)
BOARD_READY = "Ready"
BOARD_BACKLOG = "Backlog"
BOARD_IN_PROGRESS = "In progress"


def run_intake(conn, gh, settings) -> int:
    """Sweep all enabled projects. Returns number of workflows enqueued."""
    enqueued = 0
    for proj in repo_projects.list_projects(conn, enabled_only=True):
        repo = proj["repo"]
        if proj["gh_project_node_id"]:
            try:
                enqueued += _sweep_board(conn, gh, proj)
            except Exception as exc:  # noqa: BLE001
                log.warning("intake board sweep failed for %s: %s", repo, exc)
            continue
        # label-driven project
        try:
            ready = gh.list_issues_by_label(repo, STATUS_READY)
        except Exception as exc:  # noqa: BLE001
            log.warning("intake list failed for %s: %s", repo, exc)
            continue
        for issue in ready:
            labels = {l["name"] for l in issue.get("labels", [])}
            if CLAMS_TASK_LABEL in labels:
                continue          # never decompose Clams' own sub-issues
            if _enqueue_issue(conn, gh, proj, issue, via_board=False):
                enqueued += 1
        # inbound cancel gesture
        try:
            cancelled = gh.list_issues_by_label(repo, STATUS_CANCELLED)
        except Exception:  # noqa: BLE001
            cancelled = []
        for issue in cancelled:
            _cancel_for_issue(conn, proj, issue["number"])
    return enqueued


def _sweep_board(conn, gh, proj) -> int:
    """Board-driven sweep: Ready column enqueues, Backlog column cancels."""
    board = gh.list_board_items(proj["gh_project_node_id"])
    enqueued = 0
    for number, status in board.items():
        if status == BOARD_READY:
            issue = gh.get_issue(proj["repo"], number)
            labels = {l["name"] for l in issue.get("labels", [])}
            if CLAMS_TASK_LABEL in labels:
                continue
            if _enqueue_issue(conn, gh, proj, issue, via_board=True):
                enqueued += 1
        elif status == BOARD_BACKLOG:
            _cancel_for_issue(conn, proj, number)
    return enqueued


def _cancel_for_issue(conn, proj, number: int) -> None:
    wf = repo_workflows.find_by_issue(conn, proj["project_id"], number)
    if wf and wf["status"] not in ("cancelled", "done"):
        repo_commands.insert_command(
            conn, "cancel", {"source": "github-board"},
            "github", target=wf["workflow_id"])


def _enqueue_issue(conn, gh, proj, issue, *, via_board: bool) -> bool:
    goal = f"{issue['title']}\n\n{issue.get('body') or ''}".strip()
    with immediate(conn):
        existing = repo_workflows.find_by_issue(conn, proj["project_id"],
                                                issue["number"])
        if existing is not None:
            return False          # UNIQUE guard + idempotent re-poll
        wf = repo_workflows.create_workflow(conn, proj["project_id"], "github",
                                            goal, issue["number"])
        if via_board:
            repo_outbox.enqueue(
                conn, "gh_board_move",
                {"project_node_id": proj["gh_project_node_id"],
                 "issue": issue["number"],
                 "option_name": BOARD_IN_PROGRESS,
                 "idempotency_key": f"gh:gh_board_move:{issue['number']}:inprog"},
                idempotency_key=f"gh:gh_board_move:{issue['number']}:inprog")
        else:
            repo_outbox.enqueue(
                conn, "gh_label",
                {"repo": proj["repo"], "issue": issue["number"],
                 "labels": [STATUS_IN_PROGRESS],
                 "idempotency_key": f"gh:gh_label:{issue['number']}:inprogress"},
                idempotency_key=f"gh:gh_label:{issue['number']}:inprogress")
        repo_events.append_event(conn, "workflow", wf, "info",
                                 f"intake #{issue['number']} {issue['title'][:80]}")
    return True
