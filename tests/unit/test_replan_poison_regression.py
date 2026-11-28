"""cadfa395 / claudio#58 - the "poisoned failure" chain, end to end at the DB level.

The bug, in one sentence: a task that had *already* failed all its attempts kept
its ``failed`` status across a replan, so ``workflow_outcome`` kept reporting
'failed' no matter how well the replacement plan performed, and the workflow
could never close out again.

Reproduced here as the full operator-visible chain:

  1. task #1 fails every attempt -> the workflow blocks with
     'blocked: replans exhausted' (the automated budget was already spent);
  2. an operator issues a ``replan`` command that installs a NEW plan whose
     titles do not match the failed task at all;
  3. every new task transitions to success.

Before the fix the chain dead-ends after step 3: the stale ``failed`` row is
still counted, ``workflow_outcome`` says 'failed', and the close-out routes to
'replan'/'blocked' forever. This test asserts the chain now completes:

  * ``workflow_outcome(wf) == 'done'`` after the last success,
  * the superseded task is ``'cancelled'``,
  * the operator replan reset ``workflows.replans`` to 0,
  * the review/close-out decision is ``'done'`` - not 'replan', not 'blocked'.

Runs in a bare container: stdlib + pytest only. ``<repo>/src`` is bootstrapped
onto sys.path from ``__file__`` and only stdlib-safe modules are imported
(``clams.db.*``, ``clams.planner.commands``, ``clams.planner.reconcile``).
Those two planner modules are loaded through ``_load_planner_module`` because
``clams/planner/__init__.py`` pulls ``loop`` -> ``contracts`` -> pydantic; see
the note above it for how the review routing is asserted with and without
pydantic installed.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clams.db import (connect, migrate, repo_commands,  # noqa: E402
                      repo_events, repo_projects, repo_tasks, repo_workflows)

MAX_REPLANS = 3


# ------------------------------------------------------------------- review
#
# NOTE ON ASSERTING THE ROUTING DECISION
# ``clams/planner/review.py`` is NOT importable with stdlib only, for two
# reasons: its own header does ``from .. import contracts`` (pydantic) and
# ``clams/planner/__init__.py`` does ``from .loop import PlannerLoop`` (which
# also pulls contracts). It has no stdlib-safe decision helper to route through
# either - the close-out decision is inline in ``review_workflow``
# (review.py:139-203), interleaved with the status writes. So this test does
# both halves the objective allows:
#   * when pydantic happens to be installed (dev/CI image) it calls the REAL
#     ``review_workflow`` / ``review._apply`` and asserts the routing directly;
#   * in a bare container it falls back to the outcome + budget assertions -
#     the two inputs ``review_workflow`` routes on - and replays the same three
#     writes it performs, which is what ``_close_out`` below does.
def _load_planner_module(name: str):
    """Import ``clams.planner.<name>`` without running the package header.

    ``clams/planner/__init__.py`` imports ``loop`` -> ``contracts`` -> pydantic,
    which would make even the stdlib-safe planner modules unreachable in a bare
    container. Registering a namespace stub for the package and loading the file
    directly lets ``from ..db import ...`` inside it resolve against the real
    (stdlib-only) ``clams.db``.
    """
    full = f"clams.planner.{name}"
    try:
        return importlib.import_module(full)
    except Exception:                           # noqa: BLE001 - pydantic absent
        pass
    pkg = sys.modules.get("clams.planner")
    if pkg is None:
        pkg = types.ModuleType("clams.planner")
        pkg.__path__ = [str(SRC / "clams" / "planner")]
        sys.modules["clams.planner"] = pkg
    path = SRC / "clams" / "planner" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(full, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


try:                                            # needs pydantic (contracts)
    _review = _load_planner_module("review")
except Exception:                               # noqa: BLE001
    _review = None

# commands.py itself is stdlib-only; only the package header was in the way.
drain_commands = _load_planner_module("commands").drain_commands
# reconcile.py is stdlib-only too (its GitHub projection is lazy + opt-in).
_reconcile = _load_planner_module("reconcile")


class _Settings:
    """Stand-in for clams.config.Settings: review_workflow reads only this."""
    max_replans = MAX_REPLANS


# ------------------------------------------------------------------ db setup
def _db() -> sqlite3.Connection:
    """A migrated throwaway DB, built without fixtures so the file is standalone."""
    tmp = tempfile.mkdtemp(prefix="clams-poison-")
    conn = connect(str(Path(tmp) / "clams.db"))
    migrate(conn)
    return conn


def _entry(seq: int, title: str, **kw) -> dict:
    d = {"sequence_index": seq, "title": title, "objective": f"obj {title}",
         "acceptance": f"acc {title}", "brief": {"steps": [f"do {title}"]},
         "depends_on": [], "max_attempts": 2}
    d.update(kw)
    return d


def _plan(*titles: str) -> list[dict]:
    return [_entry(i, t) for i, t in enumerate(titles, start=1)]


def _install_plan(conn, wf: str, entries: list[dict]) -> dict:
    """Install a (re)plan exactly the way the planner does.

    ``planner.reconcile.apply_replanned_plan`` is the production entry point: it
    wraps ``repo_tasks.reconcile_plan`` (which cancels the superseded
    failed/blocked rows, keeps task identity and records ``plan_json``) and
    writes the supersede audit events. The flip to 'running' is what
    ``repo_workflows.set_plan`` does on the planner's other paths.
    """
    out = _reconcile.apply_replanned_plan(conn, wf, entries)
    repo_workflows.set_status(conn, wf, "running")
    return out


def _task_id(conn, wf: str, title: str) -> str:
    key = repo_tasks.normalize_plan_key(title)
    for r in repo_tasks.list_tasks(conn, wf):
        if repo_tasks.row_key(r) == key:
            return r["task_id"]
    raise AssertionError(f"no task titled {title!r}")


def _run_task(conn, tid: str, *, succeed: bool) -> sqlite3.Row:
    """Drive one task through the real dispatch ladder until it is terminal.

    succeed=False burns every attempt: ``on_result`` requeues while
    ``attempt < max_attempts`` and only then lands on 'failed' (stamping
    error_log). succeed=True lands on 'success' on the first attempt.
    """
    repo_tasks.promote_ready(conn)
    row = repo_tasks.get_task(conn, tid)
    while row["status"] not in ("success", "failed", "cancelled"):
        assert row["status"] == "ready", f"unexpected status {row['status']}"
        claimed = repo_tasks.claim_for_dispatch(conn, tid, 60)
        assert claimed is not None, "claim_for_dispatch found no ready task"
        started = repo_tasks.on_started(conn, tid, claimed["attempt"],
                                        claimed["lease_token"], "exec-1",
                                        f"sess-{claimed['attempt']}")
        assert started is not None, "on_started was fenced"
        row = repo_tasks.on_result(
            conn, tid, claimed["attempt"], claimed["lease_token"],
            "success" if succeed else "failure", succeed,
            {"outcome": "success" if succeed else "failure",
             "passed": succeed,
             "log_tail": "gate green" if succeed else "gate red: boom",
             "session_ref": f"sess-{claimed['attempt']}"})
        assert row is not None, "on_result was fenced"
    return row


def _close_out(conn, wf: str) -> str:
    """Run the review/close-out pass and return the workflow status it left.

    With pydantic installed this is the real ``review.review_workflow``. In a
    bare container it replays the same writes review_workflow performs for each
    routing outcome (see the note above), so the scenario can still be driven
    to its terminal state.
    """
    if _review is not None:
        _review.review_workflow(conn, wf, _Settings())
        return repo_workflows.get_workflow(conn, wf)["status"]
    wfrow = repo_workflows.get_workflow(conn, wf)
    if (wfrow is None or wfrow["status"] in
            ("cancelled", "paused", "done", "blocked", "intake", "planning")
            or not repo_tasks.workflow_tasks_all_terminal(conn, wf)):
        return wfrow["status"] if wfrow else "?"
    repo_workflows.set_status(conn, wf, "review")
    outcome = repo_tasks.workflow_outcome(conn, wf)
    if outcome == "blocked":
        repo_workflows.set_status(conn, wf, "blocked")
        repo_events.append_event(conn, "workflow", wf, "error",
                                 "blocked: task(s) need attention")
    elif outcome == "done":
        repo_workflows.set_status(conn, wf, "done")
        repo_events.append_event(conn, "workflow", wf, "info", "done")
    elif outcome == "cancelled":
        repo_workflows.set_status(conn, wf, "cancelled")
    elif wfrow["replans"] < MAX_REPLANS:
        n = repo_workflows.bump_replans(conn, wf)
        repo_workflows.set_status(conn, wf, "planning")
        repo_events.append_event(conn, "workflow", wf, "warn",
                                 f"replan {n}/{MAX_REPLANS}")
    else:
        repo_workflows.set_status(conn, wf, "blocked")
        repo_events.append_event(conn, "workflow", wf, "error",
                                 "blocked: replans exhausted")
    return repo_workflows.get_workflow(conn, wf)["status"]


# ------------------------------------------------------------------ scenario
FAILED_TITLE = "Legacy: repair the flaky gate"
NEW_TITLES = ("Replan: supersede stale failures",
              "Replan: close out on green",
              "Replan: document the rule")


def _poisoned_workflow(conn) -> tuple[str, str]:
    """Step 1: task #1 fails all attempts with the budget already spent.

    Returns (workflow_id, failed_task_id). The workflow ends up 'blocked' with
    the 'blocked: replans exhausted' event, exactly as cadfa395 saw it.
    """
    pid = repo_projects.upsert_project(conn, "o/r")
    wf = repo_workflows.create_workflow(conn, pid, "adhoc",
                                        "stop the replan poisoning the outcome")
    _install_plan(conn, wf, _plan(FAILED_TITLE))
    tid = _task_id(conn, wf, FAILED_TITLE)
    for _ in range(MAX_REPLANS):          # the automated replans were already used
        repo_workflows.bump_replans(conn, wf)
    row = _run_task(conn, tid, succeed=False)
    assert row["status"] == "failed", "task did not land on failed"
    assert _close_out(conn, wf) == "blocked"
    return wf, tid


def _operator_replan(conn, wf: str, *titles: str) -> dict:
    """Step 2: the operator override, then the fresh plan it implies.

    Returns the command row plus the workflow status the command itself left
    ('planning') - the plan install is what moves it on to 'running'.
    """
    cid = repo_commands.insert_command(conn, "replan", {}, "operator",
                                       target=wf)
    assert drain_commands(conn) == 1, "the replan command was not applied"
    cmd = repo_commands.get(conn, cid)
    assert cmd["status"] == "applied", f"replan rejected: {cmd['result']}"
    after_command = repo_workflows.get_workflow(conn, wf)["status"]
    _install_plan(conn, wf, _plan(*titles))
    return {"cmd": cmd, "status_after_command": after_command}


def _run_new_plan_green(conn, wf: str) -> list[sqlite3.Row]:
    """Step 3: every task of the current plan succeeds (close-out is the
    caller's business, so a test can watch the routing happen)."""
    rows = []
    for r in repo_tasks.list_tasks(conn, wf):
        if r["status"] in ("pending", "ready"):
            rows.append(_run_task(conn, r["task_id"], succeed=True))
    return rows


def _wf_events(conn, wf: str) -> list[str]:
    return [r["message"] for r in conn.execute(
        "SELECT message FROM events WHERE scope='workflow' AND ref=? "
        "ORDER BY event_id", (wf,)).fetchall()]


def _status(conn, tid: str) -> str:
    return repo_tasks.get_task(conn, tid)["status"]


# --------------------------------------------------------------------- tests
def test_poison_before_replan_the_workflow_cannot_be_done():
    """Pre-condition: one failed row holds the outcome at 'failed' + blocked."""
    conn = _db()
    wf, tid = _poisoned_workflow(conn)
    assert _status(conn, tid) == "failed"
    assert repo_tasks.workflow_outcome(conn, wf) == "failed"
    assert repo_workflows.get_workflow(conn, wf)["replans"] == MAX_REPLANS
    assert "blocked: replans exhausted" in _wf_events(conn, wf)
    conn.close()


def test_operator_replan_resets_the_replans_counter():
    """Acceptance: an exhausted workflow gets a FULL budget back (replans == 0)."""
    conn = _db()
    wf, _tid = _poisoned_workflow(conn)
    assert repo_workflows.get_workflow(conn, wf)["replans"] == MAX_REPLANS

    out = _operator_replan(conn, wf, *NEW_TITLES)

    assert out["status_after_command"] == "planning", \
        "the operator replan must move a blocked workflow back to planning"
    row = repo_workflows.get_workflow(conn, wf)
    assert row["replans"] == 0, "operator replan must reset the replan budget"
    conn.close()


def test_superseded_failed_task_becomes_cancelled():
    """The row that is no longer in the plan stops being a 'failed' row."""
    conn = _db()
    wf, tid = _poisoned_workflow(conn)

    _operator_replan(conn, wf, *NEW_TITLES)

    assert _status(conn, tid) == "cancelled", \
        "superseded failed row must be cancelled, not left 'failed'"
    conn.close()


def test_workflow_outcome_is_done_after_every_new_task_succeeds():
    """Acceptance: workflow_outcome == 'done' for the replanned workflow."""
    conn = _db()
    wf, tid = _poisoned_workflow(conn)
    _operator_replan(conn, wf, *NEW_TITLES)

    _run_new_plan_green(conn, wf)

    assert repo_tasks.workflow_outcome(conn, wf) == "done"
    assert repo_tasks.workflow_tasks_all_terminal(conn, wf)
    assert _status(conn, tid) == "cancelled"
    conn.close()


def test_close_out_decision_is_done_not_replan_or_blocked():
    """The close-out after the last success routes to 'done'.

    With pydantic available this asserts ``review.review_workflow``'s own
    routing; in a bare container it asserts the same thing from the two inputs
    it routes on (outcome == 'done', replans < max) plus the audit trail, which
    is the only place the decision is recorded. Either way: no 'replan' and no
    'blocked' may follow the last success.
    """
    conn = _db()
    wf, _tid = _poisoned_workflow(conn)
    _operator_replan(conn, wf, *NEW_TITLES)
    rows = _run_new_plan_green(conn, wf)
    assert rows and all(r["status"] == "success" for r in rows)

    before = len(_wf_events(conn, wf))
    status = _close_out(conn, wf)
    after_close = _wf_events(conn, wf)[before:]

    assert status == "done", f"close-out routed to {status!r}"
    assert "done" in after_close, after_close
    assert not [m for m in after_close
                if m.startswith("replan") or m.startswith("blocked")], after_close
    wfrow = repo_workflows.get_workflow(conn, wf)
    assert wfrow["status"] == "done"
    assert repo_tasks.workflow_outcome(conn, wf) == "done"
    assert wfrow["replans"] < MAX_REPLANS, "budget must not have been spent"
    conn.close()


def test_full_chain_was_impossible_before_the_fix():
    """The whole chain in one test: blocked -> replan -> green -> done."""
    conn = _db()
    wf, failed = _poisoned_workflow(conn)
    assert repo_tasks.workflow_outcome(conn, wf) == "failed"

    _operator_replan(conn, wf, *NEW_TITLES)
    assert repo_workflows.get_workflow(conn, wf)["replans"] == 0
    assert _status(conn, failed) == "cancelled"

    _run_new_plan_green(conn, wf)
    status = _close_out(conn, wf)

    assert repo_tasks.workflow_outcome(conn, wf) == "done"
    assert status == "done"
    assert _status(conn, failed) == "cancelled"
    assert repo_workflows.get_workflow(conn, wf)["replans"] == 0
    conn.close()


def test_failure_record_survives_the_replan():
    """Cancelling the row must not erase the evidence of what went wrong."""
    conn = _db()
    wf, tid = _poisoned_workflow(conn)
    before = repo_tasks.get_task(conn, tid)
    assert before["attempt"] == before["max_attempts"]
    assert "gate red" in (before["error_log"] or "")

    _operator_replan(conn, wf, *NEW_TITLES)
    _run_new_plan_green(conn, wf)
    _close_out(conn, wf)

    row = repo_tasks.get_task(conn, tid)
    assert row["status"] == "cancelled"
    assert row["attempt"] == before["attempt"], "attempt count must survive"
    assert row["error_log"] == before["error_log"], "error_log must survive"
    assert row["result_json"] == before["result_json"], "result_json must survive"
    assert "gate red" in json.loads(row["result_json"])["log_tail"]
    # the workflow timeline keeps the exhaustion that led to the override
    assert any("replans exhausted" in m for m in _wf_events(conn, wf))
    assert any("replan requested by operator" in m for m in _wf_events(conn, wf))
    conn.close()


def test_per_attempt_failure_events_survive_the_replan():
    """The 'attempt N -> failed' rows are written by ``review._apply``.

    That module needs pydantic (``contracts``), so in a bare container this is
    skipped; the surviving-record assertions that ARE stdlib-safe (error_log,
    result_json, tasks.attempt) live in
    ``test_failure_record_survives_the_replan``.
    """
    if _review is None:
        import pytest
        pytest.skip("clams.planner.review needs pydantic; not a bare-container dep")
    from types import SimpleNamespace

    conn = _db()
    pid = repo_projects.upsert_project(conn, "o/r")
    wf = repo_workflows.create_workflow(conn, pid, "adhoc", "goal")
    _install_plan(conn, wf, _plan(FAILED_TITLE))
    tid = _task_id(conn, wf, FAILED_TITLE)
    for _ in range(MAX_REPLANS):
        repo_workflows.bump_replans(conn, wf)

    repo_tasks.promote_ready(conn)
    for _ in range(MAX_REPLANS + 5):
        if repo_tasks.get_task(conn, tid)["status"] != "ready":
            break
        claimed = repo_tasks.claim_for_dispatch(conn, tid, 60)
        repo_tasks.on_started(conn, tid, claimed["attempt"],
                              claimed["lease_token"], "exec-1",
                              f"sess-{claimed['attempt']}")
        _review._apply(conn, SimpleNamespace(
            type="task.result", task_id=tid, attempt=claimed["attempt"],
            lease_token=claimed["lease_token"], outcome="failure",
            evidence={"passed": False}, commit_sha="", branch="",
            changed_files=[], log_tail="gate red: boom",
            session_ref=f"sess-{claimed['attempt']}", error_code=""),
            _Settings(), gh_enabled=False)
        repo_tasks.promote_ready(conn)

    assert _status(conn, tid) == "failed"
    _operator_replan(conn, wf, *NEW_TITLES)
    _run_new_plan_green(conn, wf)
    _close_out(conn, wf)

    msgs = [r["message"] for r in conn.execute(
        "SELECT message FROM events WHERE scope='task' AND ref=? "
        "ORDER BY event_id", (tid,)).fetchall()]
    assert any("-> failed" in m for m in msgs), msgs
    assert _status(conn, tid) == "cancelled"
    assert repo_tasks.workflow_outcome(conn, wf) == "done"
    conn.close()


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except BaseException as exc:  # noqa: BLE001 - runner
                if type(exc).__name__ == "Skipped":
                    print(f"SKIP {name}: {exc}")
                    continue
                failed += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failed else 0)
