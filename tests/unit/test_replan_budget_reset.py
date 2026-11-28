"""The operator `replan` must hand the workflow a FULL replan budget.

Bug: `replan` left ``workflows.replans`` where the automated path had driven it.
A workflow that hit ``max_replans`` and was blocked with 'replans exhausted'
therefore came back with zero budget: the first poisoned outcome after the
override re-entered ``review.review_workflow``'s exhaustion branch and blocked
again instead of replanning.

Stdlib-only (sqlite3 + tempfile); bootstraps ``<repo>/src`` on sys.path so it
runs standalone as well as under pytest.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clams.db import (connect, get_runtime_state, migrate, repo_commands,  # noqa: E402
                      repo_projects, repo_workflows, set_runtime_state)

MAX_REPLANS = 3


def _db() -> sqlite3.Connection:
    """A migrated throwaway DB (no fixture, so the file is standalone)."""
    tmp = tempfile.mkdtemp(prefix="clams-replan-budget-")
    conn = connect(str(Path(tmp) / "clams.db"))
    migrate(conn)
    return conn


def _wf(conn, *, replans: int = 0, status: str = "blocked") -> str:
    pid = repo_projects.upsert_project(conn, "o/r")
    wf = repo_workflows.create_workflow(conn, pid, "adhoc", "goal")
    conn.execute("UPDATE workflows SET replans=? WHERE workflow_id=?",
                 (replans, wf))
    repo_workflows.set_status(conn, wf, status)
    return wf


def _events(conn, wf: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE scope='workflow' AND ref=? "
        "ORDER BY event_id", (wf,)).fetchall()


def _row(conn, wf: str) -> sqlite3.Row:
    return repo_workflows.get_workflow(conn, wf)


# ------------------------------------------------------------------- helper


def test_reset_zeroes_replans_and_returns_previous_count():
    conn = _db()
    wf = _wf(conn, replans=MAX_REPLANS)
    assert _row(conn, wf)["replans"] == MAX_REPLANS

    prev = repo_workflows.reset_replan_budget(conn, wf)

    assert prev == MAX_REPLANS, "must report the count it reset from"
    assert _row(conn, wf)["replans"] == 0, "replans must be zeroed"
    conn.close()


def test_reset_stamps_updated_at():
    conn = _db()
    wf = _wf(conn, replans=2)
    conn.execute("UPDATE workflows SET updated_at='2000-01-01 00:00:00' "
                 "WHERE workflow_id=?", (wf,))
    before = _row(conn, wf)["updated_at"]

    repo_workflows.reset_replan_budget(conn, wf)

    after = _row(conn, wf)["updated_at"]
    assert after != before, "updated_at must be stamped"
    assert after > before
    conn.close()


def test_reset_leaves_max_replans_intact():
    """max_replans is a planner setting, not a workflow column: untouched."""
    conn = _db()
    set_runtime_state(conn, "settings_overrides",
                      json.dumps({"max_replans": MAX_REPLANS}))
    wf = _wf(conn, replans=MAX_REPLANS)
    before = _row(conn, wf)

    repo_workflows.reset_replan_budget(conn, wf)

    assert get_runtime_state(conn, "settings_overrides") == \
        json.dumps({"max_replans": MAX_REPLANS}), "cap must not be rewritten"
    assert "max_replans" not in before.keys(), \
        "workflows has no max_replans column to reset"
    after = _row(conn, wf)
    for col in before.keys():
        if col in ("replans", "updated_at"):
            continue
        assert after[col] == before[col], f"{col} must not change"
    assert after["replans"] == 0
    conn.close()


def test_reset_emits_one_event_with_previous_count_and_reason():
    conn = _db()
    wf = _wf(conn, replans=MAX_REPLANS)
    n_before = len(_events(conn, wf))

    repo_workflows.reset_replan_budget(conn, wf, reason="operator_replan")

    new = _events(conn, wf)[n_before:]
    assert len(new) == 1, f"expected exactly one event, got {len(new)}"
    ev = new[0]
    data = json.loads(ev["data"])
    assert data["previous_replans"] == MAX_REPLANS
    assert data["replans"] == 0
    assert data["reason"] == "operator_replan"
    assert str(MAX_REPLANS) in ev["message"]
    assert "operator_replan" in ev["message"]
    conn.close()


def test_reset_default_reason_is_operator_replan():
    conn = _db()
    wf = _wf(conn, replans=1)
    repo_workflows.reset_replan_budget(conn, wf)
    assert json.loads(_events(conn, wf)[-1]["data"])["reason"] == \
        "operator_replan"
    conn.close()


def test_reset_on_clean_workflow_is_idempotent():
    conn = _db()
    wf = _wf(conn, replans=0)
    assert repo_workflows.reset_replan_budget(conn, wf) == 0
    assert _row(conn, wf)["replans"] == 0
    conn.close()


def test_reset_unknown_workflow_raises():
    conn = _db()
    try:
        repo_workflows.reset_replan_budget(conn, "nope")
    except ValueError as exc:
        assert "unknown workflow" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown workflow")
    conn.close()


def test_automated_bump_still_increments_after_reset():
    """The reset must not disable the automated counter."""
    conn = _db()
    wf = _wf(conn, replans=MAX_REPLANS, status="running")
    repo_workflows.reset_replan_budget(conn, wf)
    assert repo_workflows.bump_replans(conn, wf) == 1
    assert repo_workflows.bump_replans(conn, wf) == 2
    assert _row(conn, wf)["replans"] == 2
    conn.close()


# ------------------------------------------------------- operator replan path


def _drain(conn) -> int:
    from clams.planner.commands import drain_commands
    return drain_commands(conn)


def test_operator_replan_on_exhausted_workflow_zeroes_replans():
    """Acceptance: replans == max_replans -> operator replan -> replans == 0."""
    conn = _db()
    wf = _wf(conn, replans=MAX_REPLANS, status="blocked")
    cid = repo_commands.insert_command(conn, "replan", {}, "operator",
                                       target=wf)

    assert _drain(conn) == 1
    assert repo_commands.get(conn, cid)["status"] == "applied"
    row = _row(conn, wf)
    assert row["replans"] == 0, "operator replan must start with a full budget"
    assert row["status"] == "planning"
    conn.close()


def test_replan_accepted_when_blocked_by_exhausted_replans():
    """The 'not replannable from blocked' guard must not reject this case."""
    conn = _db()
    wf = _wf(conn, replans=MAX_REPLANS, status="blocked")
    repo_commands.insert_command(conn, "replan", {}, "operator", target=wf)

    _drain(conn)

    cmd = conn.execute("SELECT * FROM commands ORDER BY command_id").fetchone()
    assert cmd["status"] == "applied", f"rejected: {cmd['result']}"
    assert _row(conn, wf)["status"] == "planning"
    conn.close()


def test_replan_still_rejected_from_terminal_status():
    conn = _db()
    wf = _wf(conn, replans=MAX_REPLANS, status="cancelled")
    cid = repo_commands.insert_command(conn, "replan", {}, "operator",
                                       target=wf)
    _drain(conn)
    cmd = repo_commands.get(conn, cid)
    assert cmd["status"] == "rejected"
    assert "not replannable from cancelled" in cmd["result"]
    assert _row(conn, wf)["replans"] == MAX_REPLANS, "rejected cmd must not reset"
    conn.close()


def test_operator_replan_records_reset_event():
    conn = _db()
    wf = _wf(conn, replans=MAX_REPLANS, status="blocked")
    repo_commands.insert_command(conn, "replan", {}, "operator", target=wf)
    _drain(conn)
    msgs = [e["message"] for e in _events(conn, wf)]
    assert any("replan budget reset" in m for m in msgs), msgs
    assert any("replan requested by operator" in m for m in msgs), msgs
    conn.close()


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failed else 0)
