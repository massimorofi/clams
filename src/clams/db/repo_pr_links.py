"""pr_links: the ONLY bridge from a GitHub delivery to a Clams task.

Why a table instead of inference: a webhook tells us ``repo``, a branch, a PR
number and a head SHA. A task tells us a title and a goal. Anything that links
one to the other by *text* similarity (title, branch name, goal wording) is a
guess, and a wrong guess silently drives real work on the wrong task. So the
link is stored explicitly (``link_task_commit``) and correlation is a lookup
with no fallback: no row, no correlation.

The stored row also defines "the currently associated PR head". ``state =
'current'`` is the only thing an event may be compared against; a delivery whose
``head_sha`` differs from it is *stale* (an old commit's check finally finished,
a redelivery, a re-run) and must stay audit-only.

Also home of the per-project follow-up policy (``projects.gh_followup_policy``),
because the policy is meaningless without the mapping it gates.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from .conn import utcnow_s

#: correlation verdicts (stored verbatim on github_events.correlation)
MATCHED = "matched"          # exactly one current link for repo+PR/branch+SHA
UNMATCHED = "unmatched"      # repo/branch/PR/SHA triple was never recorded
STALE_SHA = "stale_sha"      # link exists, but the event SHA is not current
AMBIGUOUS = "ambiguous"      # >1 current task claims this PR/branch: no guess
NO_REPO = "unknown_repo"     # repo is not a configured project at all

#: follow-up policies (projects.gh_followup_policy)
POLICY_HANDOFF = "handoff"               # default: operator handoff, no auto task
POLICY_AUTO_DISPATCH = "auto_dispatch"   # explicit opt-in to automatic dispatch
POLICIES = (POLICY_HANDOFF, POLICY_AUTO_DISPATCH)
DEFAULT_POLICY = POLICY_HANDOFF

#: pr_links.state
STATE_CURRENT = "current"
STATE_SUPERSEDED = "superseded"
STATE_CLOSED = "closed"
STATES = (STATE_CURRENT, STATE_SUPERSEDED, STATE_CLOSED)


def link_task_commit(conn: sqlite3.Connection, *, workflow_id: str, task_id: str,
                     repo: str, head_sha: str, head_ref: Optional[str] = None,
                     pr_number: Optional[int] = None,
                     created_by: str = "planner") -> int:
    """Record "this task's work is at ``head_sha`` of ``repo``".

    Any previous *current* link for the same (repo, task) is superseded, never
    deleted: the old SHA stays queryable, which is exactly what lets a late
    event about it be recognised as stale. Re-linking the same SHA is an
    idempotent no-op that returns the existing ``link_id``.
    """
    if not repo or not head_sha or not task_id:
        raise ValueError("link_task_commit needs repo, task_id and head_sha")
    existing = conn.execute(
        "SELECT link_id, state FROM pr_links WHERE repo=? AND task_id=? "
        "AND head_sha=?", (repo, task_id, head_sha)).fetchone()
    if existing is not None:
        if existing["state"] == STATE_CURRENT:
            return int(existing["link_id"])
        conn.execute("UPDATE pr_links SET state=?, updated_at=? WHERE link_id=?",
                     (STATE_CURRENT, utcnow_s(), existing["link_id"]))
        return int(existing["link_id"])
    conn.execute(
        "UPDATE pr_links SET state=?, updated_at=? WHERE repo=? AND task_id=? "
        "AND state=?", (STATE_SUPERSEDED, utcnow_s(), repo, task_id,
                        STATE_CURRENT))
    with conn.execute(
        "INSERT INTO pr_links(workflow_id, task_id, repo, pr_number, head_ref, "
        "head_sha, state, created_by, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING link_id",
        (workflow_id, task_id, repo, pr_number, head_ref, head_sha,
         STATE_CURRENT, created_by, utcnow_s(), utcnow_s())) as cur:
        return int(cur.fetchone()["link_id"])


def current_links(conn: sqlite3.Connection, *, repo: str,
                  pr_number: Optional[int] = None,
                  head_ref: Optional[str] = None) -> list[sqlite3.Row]:
    """Every current link for a repo, narrowed by PR number and/or branch.

    Narrowing is *by key only*. There is no title/goal parameter on purpose:
    nothing in this module can match on text.
    """
    sql = "SELECT * FROM pr_links WHERE repo=? AND state=?"
    vals: list[Any] = [repo, STATE_CURRENT]
    if pr_number is not None:
        sql += " AND pr_number=?"
        vals.append(pr_number)
    if head_ref:
        sql += " AND head_ref=?"
        vals.append(head_ref)
    return conn.execute(sql + " ORDER BY link_id", vals).fetchall()


def current_link_for_sha(conn: sqlite3.Connection, *, repo: str,
                         head_sha: str) -> Optional[sqlite3.Row]:
    """The current link whose SHA *is* ``head_sha`` (the strong match)."""
    if not head_sha:
        return None
    return conn.execute(
        "SELECT * FROM pr_links WHERE repo=? AND head_sha=? AND state=? "
        "ORDER BY link_id LIMIT 1", (repo, head_sha, STATE_CURRENT)).fetchone()


def get_link(conn: sqlite3.Connection, link_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM pr_links WHERE link_id=?",
                        (link_id,)).fetchone()


def list_links(conn: sqlite3.Connection, *, task_id: Optional[str] = None,
               state: Optional[str] = None, limit: int = 100) -> list[sqlite3.Row]:
    sql, vals = "SELECT * FROM pr_links WHERE 1=1", []
    if task_id:
        sql += " AND task_id=?"
        vals.append(task_id)
    if state:
        sql += " AND state=?"
        vals.append(state)
    return conn.execute(sql + " ORDER BY link_id DESC LIMIT ?",
                        (*vals, max(1, min(int(limit), 500)))).fetchall()


def set_state(conn: sqlite3.Connection, link_id: int, state: str) -> None:
    if state not in STATES:
        raise ValueError(f"bad pr_links state {state!r}")
    conn.execute("UPDATE pr_links SET state=?, updated_at=? WHERE link_id=?",
                 (state, utcnow_s(), link_id))


def correlate(conn: sqlite3.Connection, *, repo: str,
              pr_number: Optional[int] = None, head_ref: Optional[str] = None,
              head_sha: Optional[str] = None) -> dict[str, Any]:
    """Resolve a delivery to at most one current link. Never guesses.

    Returns ``{"status": <verdict>, "link": row|None, "candidates": n}``:

    * ``matched``    - exactly one current link whose SHA equals ``head_sha``.
                       Only this verdict may influence status or dispatch.
    * ``stale_sha``  - the PR/branch is linked, but to a different SHA (or the
                       SHA matches a superseded link). Audit-only.
    * ``ambiguous``  - several current links share this PR/branch: the event
                       cannot be attributed to one task, so nothing is done.
    * ``unmatched``  - nothing stored for this repo+branch/PR+SHA.
    * ``unknown_repo`` - the repo is not a configured project at all.
    """
    if not repo:
        return {"status": UNMATCHED, "link": None, "candidates": 0}
    proj = conn.execute("SELECT 1 FROM projects WHERE repo=?", (repo,)).fetchone()
    if proj is None:
        return {"status": NO_REPO, "link": None, "candidates": 0}
    if pr_number is None and not head_ref:
        # no PR/branch key: the SHA alone may still be an explicit link
        link = current_link_for_sha(conn, repo=repo, head_sha=head_sha or "")
        if link is not None:
            return {"status": MATCHED, "link": link, "candidates": 1}
        return {"status": UNMATCHED, "link": None, "candidates": 0}
    rows = current_links(conn, repo=repo, pr_number=pr_number, head_ref=head_ref)
    if not rows:
        # the branch/PR is unknown here; a SHA-only hit would still be an
        # explicit stored mapping, so check it before calling it unmatched
        link = current_link_for_sha(conn, repo=repo, head_sha=head_sha or "")
        if link is not None:
            return {"status": MATCHED, "link": link, "candidates": 1}
        return {"status": UNMATCHED, "link": None, "candidates": 0}
    if head_sha:
        exact = [r for r in rows if (r["head_sha"] or "") == head_sha]
        if len(exact) == 1:
            return {"status": MATCHED, "link": exact[0], "candidates": 1}
        if len(exact) > 1:
            return {"status": AMBIGUOUS, "link": None,
                    "candidates": len(exact)}
        # PR/branch known, SHA not the current one -> stale, never matched
        return {"status": STALE_SHA, "link": rows[0], "candidates": len(rows)}
    if len(rows) == 1:
        # no SHA on the event at all: cannot assert currency -> audit-only
        return {"status": STALE_SHA, "link": rows[0], "candidates": 1}
    return {"status": AMBIGUOUS, "link": None, "candidates": len(rows)}


# --------------------------------------------------------------- project policy
def followup_policy(conn: sqlite3.Connection, repo: str) -> str:
    """The project's follow-up policy; ``handoff`` when nothing is configured.

    No row, an empty value or an unknown value all collapse to the safe
    default: **no automatic dispatch**. Absence is never read as consent.
    """
    row = conn.execute(
        "SELECT followup_policy FROM gh_project_policy WHERE repo=?",
        (repo,)).fetchone()
    if row is None:
        return DEFAULT_POLICY
    value = (row["followup_policy"] or "").strip()
    return value if value in POLICIES else DEFAULT_POLICY


def set_followup_policy(conn: sqlite3.Connection, repo: str, policy: str) -> None:
    """Store the policy explicitly (raises on anything outside POLICIES).

    The project must exist: a policy for an unknown repo would be silently
    ignored, which is worse than an error.
    """
    if policy not in POLICIES:
        raise ValueError(f"followup policy must be one of {POLICIES}, got {policy!r}")
    if conn.execute("SELECT 1 FROM projects WHERE repo=?", (repo,)).fetchone() is None:
        raise KeyError(f"no project {repo}")
    conn.execute(
        "INSERT INTO gh_project_policy(repo, followup_policy, updated_at) "
        "VALUES(?,?,?) ON CONFLICT(repo) DO UPDATE SET "
        "  followup_policy=excluded.followup_policy, updated_at=excluded.updated_at",
        (repo, policy, utcnow_s()))


def clear_followup_policy(conn: sqlite3.Connection, repo: str) -> None:
    conn.execute("DELETE FROM gh_project_policy WHERE repo=?", (repo,))


def list_followup_policies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM gh_project_policy ORDER BY repo").fetchall()


def auto_dispatch_enabled(conn: sqlite3.Connection, repo: str) -> bool:
    return followup_policy(conn, repo) == POLICY_AUTO_DISPATCH
