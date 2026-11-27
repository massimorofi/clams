"""Worker progress: lease/attempt fencing, monotonic seq, bounded text.

Synchronous, stdlib-only tests on a temp SQLite DB migrated via clams.db.conn.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from clams import handoffs
from clams.db import repo_progress
from clams.db.conn import connect, migrate

LEASE = "lease-aaaa"
LEASE_NEW = "lease-bbbb"


class ProgressDB(unittest.TestCase):
    """One temp DB per test; ``_reopen`` simulates a process restart."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="clams-prog-")
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
            "'running',1,?)",
            (json.dumps({"objective": "orig", "acceptance": ["a1"]}), LEASE))
        self._close = False

    def tearDown(self) -> None:
        if not self._close:
            self.conn.close()

    # ------------------------------------------------------------- helpers
    def _reopen(self) -> None:
        """Simulate a process restart: close everything, open the DB again."""
        self.conn.close()
        self._close = True
        self.conn = connect(self.db)

    def _append(self, seq: int, text: str = "line", *, kind: str = "log",
                attempt: int = 1, lease: str = LEASE) -> dict:
        return repo_progress.append_progress(
            self.conn, task_id="t1", attempt_no=attempt, seq=seq, kind=kind,
            text=text, lease_token=lease)

    def _rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM worker_progress ORDER BY id").fetchall()

    def _task_snapshot(self) -> tuple:
        row = self.conn.execute(
            "SELECT title, objective, acceptance, brief_json, status, attempt, "
            "lease_token FROM tasks WHERE task_id='t1'").fetchone()
        return tuple(row)

    # ------------------------------------------------------------- happy path
    def test_append_accepts_current_attempt_and_lease(self) -> None:
        res = self._append(1, "starting")
        self.assertTrue(res["accepted"], res)
        self.assertEqual(repo_progress.last_seq(self.conn, "t1", 1), 1)
        self.assertEqual(len(self._rows()), 1)

    def test_seq_increases_within_attempt(self) -> None:
        self.assertTrue(self._append(1)["accepted"])
        self.assertTrue(self._append(2, "still going")["accepted"])
        self.assertEqual([r["seq"] for r in self._rows()], [1, 2])
        self.assertEqual(repo_progress.next_seq(self.conn, "t1", 1), 3)

    # ------------------------------------------------------------- fencing
    def test_stale_lease_token_is_rejected_and_writes_nothing(self) -> None:
        self.assertTrue(self._append(1, "live")["accepted"])
        before_rows = len(self._rows())
        before_task = self._task_snapshot()

        res = self._append(2, "from a dead executor", lease="lease-STALE")
        self.assertFalse(res["accepted"])
        self.assertEqual(res["reason"], repo_progress.REJECT_STALE_LEASE)
        self.assertIsNone(res["id"])
        # nothing mutated: no row appended, task row byte-identical
        self.assertEqual(len(self._rows()), before_rows)
        self.assertEqual(self._task_snapshot(), before_task)

    def test_stale_attempt_no_is_rejected_and_writes_nothing(self) -> None:
        self.assertTrue(self._append(1)["accepted"])
        before_rows, before_task = len(self._rows()), self._task_snapshot()

        res = self._append(9, "from an old attempt", attempt=9)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["reason"], repo_progress.REJECT_STALE_ATTEMPT)
        self.assertEqual(len(self._rows()), before_rows)
        self.assertEqual(self._task_snapshot(), before_task)

    def test_non_monotonic_seq_is_rejected(self) -> None:
        self.assertTrue(self._append(5, "five")["accepted"])
        for bad in (5, 4, 1):
            res = self._append(bad, f"seq {bad}")
            self.assertFalse(res["accepted"], res)
            self.assertEqual(res["reason"], repo_progress.REJECT_BAD_SEQ)
        self.assertEqual([r["seq"] for r in self._rows()], [5])

    def test_replayed_message_is_rejected_as_non_monotonic(self) -> None:
        payload = {"task_id": "t1", "attempt_no": 1, "seq": 1,
                   "kind": "log", "text": "once", "lease_token": LEASE}
        first = repo_progress.append_progress_record(
            self.conn, handoffs.WorkerProgress.from_payload(payload))
        self.assertTrue(first["accepted"])
        again = repo_progress.append_progress_record(
            self.conn, handoffs.WorkerProgress.from_payload(payload))
        self.assertFalse(again["accepted"])
        self.assertEqual(again["reason"], repo_progress.REJECT_BAD_SEQ)
        self.assertEqual(len(self._rows()), 1)

    def test_new_attempt_gets_its_own_seq_space(self) -> None:
        self.assertTrue(self._append(1)["accepted"])
        self.assertTrue(self._append(2)["accepted"])
        # retry: the task moves to attempt 2 with a fresh lease
        self.conn.execute(
            "UPDATE tasks SET attempt=2, lease_token=? WHERE task_id='t1'",
            (LEASE_NEW,))
        self.assertFalse(self._append(3, "old lease", lease=LEASE)["accepted"])
        res = self._append(1, "attempt two starts", lease=LEASE_NEW,
                           attempt=2)
        self.assertTrue(res["accepted"], res)
        self.assertEqual(repo_progress.last_seq(self.conn, "t1", 2), 1)
        self.assertEqual(repo_progress.last_seq(self.conn, "t1", 1), 2)

    def test_unknown_task_is_rejected(self) -> None:
        res = repo_progress.append_progress(
            self.conn, task_id="nope", attempt_no=1, seq=1, kind="log",
            text="x", lease_token=LEASE)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["reason"], repo_progress.REJECT_UNKNOWN_TASK)
        self.assertEqual(len(self._rows()), 0)

    def test_invalid_kind_is_rejected(self) -> None:
        res = self._append(1, "x", kind="shakespeare")
        self.assertFalse(res["accepted"])
        self.assertEqual(res["reason"], repo_progress.REJECT_BAD_KIND)
        self.assertEqual(len(self._rows()), 0)

    # ------------------------------------------------------- text bounds
    def test_long_text_is_bounded_and_marked(self) -> None:
        res = self._append(1, "x" * (handoffs.MAX_PROGRESS_TEXT * 3))
        self.assertTrue(res["accepted"])
        stored = self._rows()[0]["text"]
        self.assertLessEqual(len(stored), handoffs.MAX_PROGRESS_TEXT)
        self.assertTrue(handoffs.is_truncated(stored))
        self.assertTrue(stored.endswith("...[truncated]"))

    def test_short_text_is_not_marked(self) -> None:
        self._append(1, "short line")
        self.assertEqual(self._rows()[0]["text"], "short line")
        self.assertFalse(handoffs.is_truncated(self._rows()[0]["text"]))

    def test_exact_limit_is_not_truncated(self) -> None:
        text = "y" * handoffs.MAX_PROGRESS_TEXT
        self._append(1, text)
        stored = self._rows()[0]["text"]
        self.assertEqual(len(stored), handoffs.MAX_PROGRESS_TEXT)
        self.assertFalse(handoffs.is_truncated(stored))

    def test_newlines_and_tabs_are_collapsed(self) -> None:
        self._append(1, "one\ntwo\r\nthree\tfour")
        self.assertEqual(self._rows()[0]["text"], "one two three four")

    def test_contract_enforces_bounds_at_construction(self) -> None:
        rec = handoffs.WorkerProgress.from_payload(
            {"task_id": "t1", "attempt_no": 1, "seq": 1, "kind": "note",
             "text": "z" * 5000, "lease_token": LEASE})
        self.assertLessEqual(len(rec.text), handoffs.MAX_PROGRESS_TEXT)
        self.assertTrue(handoffs.is_truncated(rec.text))
        with self.assertRaises(ValueError):
            handoffs.WorkerProgress.from_payload(
                {"task_id": "t1", "attempt_no": 1, "seq": 1, "kind": "bogus",
                 "text": "x"})

    # ---------------------------------------------------------- durability
    def test_progress_survives_a_restart(self) -> None:
        self._append(1, "before restart")
        self._append(2, "and more")
        self._reopen()
        rows = repo_progress.list_progress(self.conn, "t1", 1)
        self.assertEqual([r["text"] for r in rows],
                         ["before restart", "and more"])
        self.assertEqual(repo_progress.last_seq(self.conn, "t1", 1), 2)

    def test_rejected_writes_stay_rejected_after_restart(self) -> None:
        self._append(1, "kept")
        self._append(2, "dropped", lease="lease-STALE")
        self._reopen()
        self.assertEqual(repo_progress.count_progress(self.conn, "t1"), 1)
        # the stale sender is still fenced after a restart
        res = self._append(2, "dropped again", lease="lease-STALE")
        self.assertFalse(res["accepted"])
        self.assertEqual(repo_progress.count_progress(self.conn, "t1"), 1)

    def test_readable_on_a_read_only_connection(self) -> None:
        self._append(1, "visible")
        self.conn.close()
        self._close = True
        ro = connect(self.db, read_only=True)
        try:
            self.assertEqual(repo_progress.count_progress(ro, "t1"), 1)
            self.assertEqual(ro.execute(
                "SELECT text FROM worker_progress").fetchone()["text"],
                "visible")
        finally:
            ro.close()

    # ------------------------------------------------------ immutability
    def test_progress_never_touches_the_task_brief(self) -> None:
        before = self._task_snapshot()
        self._append(1, "progress line")
        self._append(2, "more", kind="status")
        self._append(3, "stale", lease="nope")   # rejected
        self.assertEqual(self._task_snapshot(), before)

    def test_new_modules_contain_no_task_writes(self) -> None:
        """Static guard: no code path in the new modules writes to tasks."""
        import inspect
        for mod in (repo_progress, handoffs):
            src = inspect.getsource(mod)
            for forbidden in ("UPDATE tasks", "INSERT INTO tasks",
                              "DELETE FROM tasks", "set_brief_field"):
                self.assertNotIn(forbidden, src,
                                 f"{mod.__name__} must not {forbidden}")


class ProgressBatch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="clams-prog2-")
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
            "VALUES('t1','wf1',1,'t','o','a','{}','running',1,?)", (LEASE,))

    def tearDown(self) -> None:
        self.conn.close()

    def test_batch_stops_at_first_rejection(self) -> None:
        recs = [handoffs.WorkerProgress("t1", 1, 1, "log", "a", LEASE),
                handoffs.WorkerProgress("t1", 1, 2, "log", "b", LEASE),
                handoffs.WorkerProgress("t1", 1, 3, "log", "stale", "wrong"),
                handoffs.WorkerProgress("t1", 1, 4, "log", "never", LEASE)]
        out = repo_progress.append_progress_batch(self.conn, recs)
        self.assertEqual([o["accepted"] for o in out],
                         [True, True, False])
        rows = repo_progress.list_progress(self.conn, "t1", 1)
        self.assertEqual([r["seq"] for r in rows], [1, 2])


if __name__ == "__main__":
    unittest.main()
