"""Handoffs: bounded contracts, strict enums, forward-only dispositions,
restart durability, command-inbox write path and task-brief immutability.

Synchronous, stdlib-only tests on a temp SQLite DB migrated via clams.db.conn.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from clams import handoffs
from clams.db import repo_handoffs, repo_progress
from clams.db.conn import connect, migrate
from clams.planner import commands as cmds

BRIEF = json.dumps({"objective": "do the thing", "acceptance": ["a1"],
                    "scope": "fixed"})


class HandoffDB(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="clams-hand-")
        self.db = os.path.join(self.tmp, "clams.db")
        self.conn = connect(self.db)
        migrate(self.conn)
        self.conn.execute(
            "INSERT INTO projects(project_id, repo) VALUES(1,'acme/ops')")
        self.conn.execute(
            "INSERT INTO workflows(workflow_id, project_id, source, goal, "
            "status) VALUES('wf1',1,'adhoc','goal','running')")
        self.conn.execute(
            "INSERT INTO tasks(task_id, workflow_id, sequence_index, title, "
            "objective, acceptance, brief_json, status, attempt, lease_token) "
            "VALUES('t1','wf1',1,'title','objective','acceptance',?,"
            "'running',1,'lease-1')", (BRIEF,))
        self._closed = False

    def tearDown(self) -> None:
        if not self._closed:
            self.conn.close()

    # ------------------------------------------------------------- helpers
    def _reopen(self) -> None:
        """Simulate a process restart: close the connection, open it again."""
        self.conn.close()
        self._closed = True
        self.conn = connect(self.db)

    def _open_ro(self):
        """Read-only reopen, like the API does after a restart."""
        ro = connect(self.db, read_only=True)
        self.addCleanup(ro.close)
        return ro

    def _record(self, **over) -> dict:
        payload = {"workflow_id": "wf1", "task_id": "t1", "attempt_no": 1,
                   "kind": "blocked", "reason": "need credentials",
                   "requested_action": "provide the token"}
        payload.update(over)
        return repo_handoffs.record_handoff(
            self.conn, handoffs.Handoff.from_payload(payload))

    def _brief(self) -> tuple:
        row = self.conn.execute(
            "SELECT title, objective, acceptance, brief_json, status, attempt, "
            "lease_token FROM tasks WHERE task_id='t1'").fetchone()
        return tuple(row)

    # ------------------------------------------------------------- recording
    def test_record_and_read_back(self) -> None:
        res = self._record()
        self.assertTrue(res["created"])
        h = repo_handoffs.get_handoff(self.conn, res["id"])
        self.assertEqual(h.status, handoffs.HandoffStatus.OPEN.value)
        self.assertEqual(h.kind, "blocked")
        self.assertEqual(h.reason, "need credentials")
        self.assertEqual(h.workflow_id, "wf1")

    def test_duplicate_message_appends_nothing(self) -> None:
        first = self._record()
        second = self._record()
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(second["created"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) n FROM handoffs").fetchone()["n"],
            1)

    def test_distinct_reasons_are_distinct_rows(self) -> None:
        a = self._record(reason="reason A")
        b = self._record(reason="reason B")
        self.assertNotEqual(a["id"], b["id"])

    def test_handoff_for_deleted_task_keeps_payload_workflow(self) -> None:
        """History outlives the task row (a replan deletes tasks)."""
        res = self._record()
        self.conn.execute("DELETE FROM tasks WHERE task_id='t1'")
        h = repo_handoffs.get_handoff(self.conn, res["id"])
        self.assertEqual(h.workflow_id, "wf1")

    # ------------------------------------------------------- bounded text
    def test_reason_and_action_are_bounded_and_marked(self) -> None:
        self._record(reason="r" * 5000, requested_action="a" * 5000)
        h = repo_handoffs.get_handoff(self.conn, 1)
        self.assertLessEqual(len(h.reason), handoffs.MAX_HANDOFF_REASON)
        self.assertTrue(handoffs.is_truncated(h.reason))
        self.assertLessEqual(len(h.requested_action),
                             handoffs.MAX_REQUESTED_ACTION)
        self.assertTrue(handoffs.is_truncated(h.requested_action))

    def test_disposed_by_is_bounded(self) -> None:
        self._record()
        res = repo_handoffs.dispose(self.conn, 1, "acknowledged",
                                    disposed_by="o" * 5000)
        self.assertTrue(res["ok"])
        self.assertLessEqual(len(res["disposed_by"]), handoffs.MAX_DISPOSED_BY)
        self.assertTrue(handoffs.is_truncated(res["disposed_by"]))

    # --------------------------------------------------------- strict enums
    def test_unknown_handoff_kind_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            handoffs.Handoff.from_payload(
                {"workflow_id": "wf1", "task_id": "t1", "attempt_no": 1,
                 "kind": "please_help"})

    def test_unknown_progress_kind_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            handoffs.WorkerProgress.from_payload(
                {"task_id": "t1", "attempt_no": 1, "seq": 1, "kind": "vibes",
                 "text": "x"})

    def test_unknown_status_and_disposition_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            handoffs.Handoff.from_payload(
                {"workflow_id": "wf1", "task_id": "t1", "attempt_no": 1,
                 "kind": "blocked", "status": "wontfix"})
        with self.assertRaises(ValueError):
            handoffs.Handoff.from_payload(
                {"workflow_id": "wf1", "task_id": "t1", "attempt_no": 1,
                 "kind": "blocked", "disposition": "maybe"})
        self._record()
        self.assertEqual(
            repo_handoffs.dispose(self.conn, 1, "closed")["reason"],
            repo_handoffs.REJECT_BAD_STATUS)
        self.assertEqual(
            repo_handoffs.dispose(self.conn, 1, "resolved",
                                  disposition="shrug")["reason"],
            repo_handoffs.REJECT_BAD_DISPOSITION)

    # --------------------------------------------------- dispositions
    def test_happy_path_open_ack_resolved(self) -> None:
        hid = self._record()["id"]
        self.assertTrue(
            repo_handoffs.dispose(self.conn, hid, "acknowledged",
                                  disposed_by="ops")["changed"])
        res = repo_handoffs.dispose(self.conn, hid, "resolved",
                                    disposition="accepted", disposed_by="ops")
        self.assertTrue(res["ok"])
        h = repo_handoffs.get_handoff(self.conn, hid)
        self.assertEqual(h.status, "resolved")
        self.assertEqual(h.disposition, "accepted")
        self.assertEqual(h.disposed_by, "ops")

    def test_open_may_be_dismissed_directly(self) -> None:
        hid = self._record()["id"]
        self.assertTrue(repo_handoffs.dispose(
            self.conn, hid, "rejected", disposition="denied")["changed"])
        self.assertEqual(repo_handoffs.get_handoff(self.conn, hid).status,
                         "rejected")

    def test_transitions_never_move_backwards(self) -> None:
        hid = self._record()["id"]
        repo_handoffs.dispose(self.conn, hid, "acknowledged")
        self.assertEqual(
            repo_handoffs.dispose(self.conn, hid, "open")["reason"],
            repo_handoffs.REJECT_BAD_TRANSITION)
        repo_handoffs.dispose(self.conn, hid, "resolved")
        for back in ("open", "acknowledged"):
            self.assertEqual(
                repo_handoffs.dispose(self.conn, hid, back)["reason"],
                repo_handoffs.REJECT_BAD_TRANSITION)
        # a rejected transition changed nothing
        self.assertEqual(repo_handoffs.get_handoff(self.conn, hid).status,
                         "resolved")

    def test_replaying_a_disposition_is_a_noop(self) -> None:
        hid = self._record()["id"]
        repo_handoffs.dispose(self.conn, hid, "acknowledged",
                              disposed_by="ops")
        again = repo_handoffs.dispose(self.conn, hid, "acknowledged",
                                      disposed_by="other")
        self.assertTrue(again["ok"])
        self.assertFalse(again["changed"])
        self.assertEqual(
            repo_handoffs.get_handoff(self.conn, hid).disposed_by, "ops")

    def test_dispose_unknown_handoff(self) -> None:
        self.assertEqual(
            repo_handoffs.dispose(self.conn, 4242, "acknowledged")["reason"],
            repo_handoffs.REJECT_UNKNOWN)

    def test_disposition_is_mirrored_in_append_only_events(self) -> None:
        hid = self._record()["id"]
        before = self.conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
        repo_handoffs.dispose(self.conn, hid, "acknowledged", disposed_by="ops")
        repo_handoffs.dispose(self.conn, hid, "resolved", disposed_by="ops")
        rows = self.conn.execute(
            "SELECT message FROM events WHERE scope='task' ORDER BY event_id"
        ).fetchall()
        self.assertGreaterEqual(len(rows) - before, 2)
        self.assertTrue(any("acknowledged -> resolved" in r["message"]
                            for r in rows))

    # ---------------------------------------------------------- durability
    def test_handoff_history_survives_a_restart(self) -> None:
        hid = self._record(reason="worker is stuck")["id"]
        repo_handoffs.dispose(self.conn, hid, "acknowledged",
                              disposed_by="ops")
        self._reopen()
        h = repo_handoffs.get_handoff(self.conn, hid)
        self.assertEqual(h.status, "acknowledged")
        self.assertEqual(h.reason, "worker is stuck")
        self.assertEqual(h.disposed_by, "ops")
        # and the history is still readable on a read-only connection
        ro = self._open_ro()
        self.assertEqual([x.id for x in repo_handoffs.list_handoffs(ro)],
                         [hid])
        self.assertEqual(ro.execute(
            "SELECT COUNT(*) n FROM handoffs").fetchone()["n"], 1)

    def test_dispositions_continue_after_a_restart(self) -> None:
        hid = self._record()["id"]
        repo_handoffs.dispose(self.conn, hid, "acknowledged")
        self._reopen()
        self.assertTrue(repo_handoffs.dispose(
            self.conn, hid, "resolved", disposition="accepted")["changed"])
        self.assertEqual(repo_handoffs.get_handoff(self.conn, hid).status,
                         "resolved")

    def test_counts_by_status(self) -> None:
        self._record(reason="a")
        b = self._record(reason="b")["id"]
        repo_handoffs.dispose(self.conn, b, "rejected")
        self.assertEqual(repo_handoffs.counts_by_status(self.conn),
                         {"open": 1, "rejected": 1})

    # ------------------------------------------- brief immutability
    def test_scope_change_handoff_does_not_edit_the_brief(self) -> None:
        before = self._brief()
        res = self._record(
            kind="scope_change", reason="acceptance is wrong",
            requested_action="add a rollback task")
        self.assertTrue(res["created"])
        h = repo_handoffs.get_handoff(self.conn, res["id"])
        self.assertTrue(h.asks_scope_change)
        self.assertEqual(h.requested_action, "add a rollback task")
        # the brief is byte-identical: a handoff is a request, not an edit
        self.assertEqual(self._brief(), before)

    def test_disposing_a_scope_change_handoff_leaves_the_brief_alone(self) -> None:
        hid = self._record(kind="scope_change",
                           requested_action="widen the scope")["id"]
        before = self._brief()
        repo_handoffs.dispose(self.conn, hid, "acknowledged")
        repo_handoffs.dispose(self.conn, hid, "rejected",
                              disposition="denied")
        self.assertEqual(self._brief(), before)

    def test_new_modules_never_write_task_briefs(self) -> None:
        import inspect
        for mod in (repo_handoffs, repo_progress, handoffs):
            src = inspect.getsource(mod)
            for forbidden in ("UPDATE tasks", "INSERT INTO tasks",
                              "DELETE FROM tasks", "set_brief_field",
                              "brief_json="):
                self.assertNotIn(forbidden, src,
                                 f"{mod.__name__} must not {forbidden}")

    # ------------------------------------------------- command inbox path
    def test_disposition_goes_through_the_command_inbox(self) -> None:
        """The API path enqueues a command; the planner is the only writer."""
        hid = self._record()["id"]
        before = self._brief()
        cid = repo_handoffs.enqueue_disposition(
            self.conn, hid, "acknowledged", issued_by="ops",
            note="looking now")
        # nothing applied yet: the handoff is still open
        self.assertEqual(repo_handoffs.get_handoff(self.conn, hid).status,
                         "open")
        self.assertEqual(cmds.drain_commands(self.conn), 1)
        self.assertEqual(repo_handoffs.get_handoff(self.conn, hid).status,
                         "acknowledged")
        row = self.conn.execute("SELECT status, result FROM commands "
                                "WHERE command_id=?", (cid,)).fetchone()
        self.assertEqual(row["status"], "applied")
        self.assertEqual(json.loads(row["result"])["status"], "acknowledged")
        self.assertEqual(self._brief(), before)

    def test_bad_disposition_command_is_rejected_not_applied(self) -> None:
        hid = self._record()["id"]
        repo_handoffs.dispose(self.conn, hid, "resolved")
        cid = repo_handoffs.enqueue_disposition(self.conn, hid, "open",
                                                issued_by="ops")
        self.assertEqual(cmds.drain_commands(self.conn), 0)
        self.assertEqual(
            self.conn.execute("SELECT status FROM commands WHERE command_id=?",
                              (cid,)).fetchone()["status"], "rejected")
        self.assertEqual(repo_handoffs.get_handoff(self.conn, hid).status,
                         "resolved")

    def test_enqueue_disposition_validates_before_inserting(self) -> None:
        with self.assertRaises(ValueError):
            repo_handoffs.enqueue_disposition(self.conn, 1, "closed")
        with self.assertRaises(ValueError):
            repo_handoffs.enqueue_disposition(self.conn, 1, "resolved",
                                              disposition="nope")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) n FROM commands").fetchone()["n"],
            0)

    # ------------------------------------------------ ingestion wiring
    def test_ingest_worker_payload_persists_progress_and_handoff(self) -> None:
        before = self._brief()
        prog = cmds.ingest_worker_payload(self.conn, json.dumps({
            "type": "task.progress", "task_id": "t1", "attempt_no": 1,
            "seq": 1, "kind": "tool", "text": "running tests",
            "lease_token": "lease-1"}), msg_id="m1")
        self.assertTrue(prog["accepted"])
        hand = cmds.ingest_worker_payload(self.conn, json.dumps({
            "type": "task.handoff", "task_id": "t1", "attempt_no": 1,
            "kind": "needs_decision", "reason": "two valid paths",
            "requested_action": "pick one"}), msg_id="m2")
        self.assertTrue(hand["created"])
        self.assertEqual(repo_progress.count_progress(self.conn, "t1"), 1)
        self.assertEqual(len(repo_handoffs.list_handoffs(self.conn)), 1)
        # surfaced on the timeline for the operator
        msgs = [r["message"] for r in self.conn.execute(
            "SELECT message FROM events ORDER BY event_id")]
        self.assertTrue(any("handoff (needs_decision)" in m for m in msgs))
        self.assertEqual(self._brief(), before)

    def test_ingest_rejects_stale_progress_without_writing(self) -> None:
        res = cmds.ingest_worker_payload(self.conn, json.dumps({
            "type": "task.progress", "task_id": "t1", "attempt_no": 1,
            "seq": 1, "kind": "log", "text": "stale",
            "lease_token": "old-lease"}))
        self.assertFalse(res["accepted"])
        self.assertEqual(res["reason"], repo_progress.REJECT_STALE_LEASE)
        self.assertEqual(repo_progress.count_progress(self.conn, "t1"), 0)

    def test_ingest_is_idempotent_on_message_id(self) -> None:
        payload = json.dumps({"type": "task.progress", "task_id": "t1",
                              "attempt_no": 1, "seq": 1, "kind": "log",
                              "text": "x", "lease_token": "lease-1"})
        self.assertTrue(cmds.ingest_worker_payload(
            self.conn, payload, msg_id="m1")["accepted"])
        self.assertTrue(cmds.ingest_worker_payload(
            self.conn, payload, msg_id="m1").get("duplicate"))
        self.assertEqual(repo_progress.count_progress(self.conn, "t1"), 1)

    def test_scope_change_ingest_records_request_and_keeps_brief(self) -> None:
        before = self._brief()
        res = cmds.ingest_worker_payload(self.conn, json.dumps({
            "type": "task.handoff", "task_id": "t1", "attempt_no": 1,
            "kind": "scope_change", "reason": "brief is wrong",
            "requested_action": "acceptance should mention rollback"}))
        self.assertTrue(res["created"])
        self.assertEqual(self._brief(), before)
        msgs = [r["message"] for r in self.conn.execute(
            "SELECT message FROM events ORDER BY event_id")]
        self.assertTrue(any("brief left immutable" in m for m in msgs))

    def test_worker_narration_type_detection(self) -> None:
        self.assertEqual(cmds.worker_narration_type(
            b'{"type":"task.progress"}'), "task.progress")
        self.assertEqual(cmds.worker_narration_type(
            {"type": "task.handoff"}), "task.handoff")
        self.assertIsNone(cmds.worker_narration_type('{"type":"task.result"}'))
        self.assertIsNone(cmds.worker_narration_type("not json"))
        self.assertIsNone(cmds.worker_narration_type(None))

    def test_ingest_rejects_garbage(self) -> None:
        with self.assertRaises(ValueError):
            cmds.ingest_worker_payload(self.conn, "[]")
        with self.assertRaises(ValueError):
            cmds.ingest_worker_payload(self.conn, {"type": "task.result"})


if __name__ == "__main__":
    unittest.main()
