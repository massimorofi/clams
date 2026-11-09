"""S4 — intake service tests."""
from __future__ import annotations

import asyncio
import json

import pytest

from clams.config import Settings
from clams.db import open_store
from clams.gh import STATUS_IN_PROGRESS, STATUS_READY
from clams.intake import IntakeService


class FakeGH:
    """In-memory stand-in for GitHubClient."""

    def __init__(self, issues=None, fail=False):
        self._issues = issues or []
        self.fail = fail
        self.label_calls = []
        self.comment_calls = []

    async def list_issues(self, state="open", labels=None, since=None, per_page=100):
        if self.fail:
            from clams.gh import GitHubError
            raise GitHubError("network down")
        return list(self._issues)

    async def create_issue(self, title, body="", labels=None):
        from clams.gh import Issue
        n = 900 + len(self.label_calls)
        return Issue(number=n, title=title, body=body, labels=labels or [])

    async def set_status(self, number, status_label, keep=None):
        self.label_calls.append((number, status_label))
        return [status_label]

    async def add_comment(self, number, body):
        self.comment_calls.append((number, body))
        return {"id": number}


def issue(n, title="Req", labels=None, updated="2026-11-08T00:00:00Z"):
    from clams.gh import Issue
    return Issue(number=n, title=title, body="body", state="open",
                 labels=labels or [STATUS_READY], updated_at=updated)


@pytest.fixture()
def store(tmp_path):
    s = open_store(tmp_path / "intake.db")
    yield s
    s.db.close()


@pytest.fixture()
def settings():
    return Settings()


async def test_sweep_claims_ready_issues(store, settings):
    gh = FakeGH(issues=[issue(11), issue(12)])
    svc = IntakeService(store, gh, settings)
    res = await svc.sweep()
    assert res.scanned == 2
    assert sorted(res.claimed) == [11, 12]
    assert store.work_items.get(11).status == "claimed"
    assert store.work_items.get(11).gh_status == "In Progress"


async def test_sweep_queues_outbox_side_effects(store, settings):
    gh = FakeGH(issues=[issue(21)])
    svc = IntakeService(store, gh, settings)
    await svc.sweep()
    stats = store.outbox.stats()
    assert stats["pending"] == 2  # one status change + one ack comment
    rows = store.db.conn.execute(
        "SELECT kind, payload FROM outbox ORDER BY id").fetchall()
    kinds = [r["kind"] for r in rows]
    assert kinds == ["gh_status", "gh_comment"]
    payload = json.loads(rows[0]["payload"])
    assert payload["status"] == STATUS_IN_PROGRESS
    assert payload["issue"] == 21


async def test_sweep_is_idempotent(store, settings):
    gh = FakeGH(issues=[issue(31)])
    svc = IntakeService(store, gh, settings)
    first = await svc.sweep()
    second = await svc.sweep()
    assert first.claimed == [31]
    assert second.claimed == []
    assert second.skipped == [31]
    # outbox was not duplicated
    assert store.db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 2


async def test_concurrent_sweeps_claim_disjoint_sets(store, settings):
    """Two pollers racing over the same issues must never double-claim."""
    issues = [issue(n) for n in range(50, 60)]
    gh = FakeGH(issues=issues)
    svc = IntakeService(store, gh, settings)

    claimed = []

    async def poller():
        r = await svc.sweep()
        claimed.extend(r.claimed)

    await asyncio.gather(poller(), poller(), poller(), poller())
    assert sorted(claimed) == list(range(50, 60))
    # exactly one claim row per requirement in the DB
    rows = store.db.conn.execute(
        "SELECT requirement_number, COUNT(*) c FROM work_items GROUP BY requirement_number"
    ).fetchall()
    assert all(r["c"] == 1 for r in rows)


async def test_already_tracked_items_are_skipped(store, settings):
    from clams.db import WorkItem
    store.work_items.upsert(WorkItem(requirement_number=41, requirement_title="old",
                                    gh_status="In Progress",
                                    gh_updated_at="2026-01-01T00:00:00Z",
                                    status="planned"))
    gh = FakeGH(issues=[issue(41)])
    svc = IntakeService(store, gh, settings)
    res = await svc.sweep()
    assert res.claimed == []
    assert res.skipped == [41]
    # local row untouched
    assert store.work_items.get(41).status == "planned"


async def test_github_outage_is_tolerated_and_alerted(store, settings):
    gh = FakeGH(fail=True)
    svc = IntakeService(store, gh, settings)
    res = await svc.sweep()
    assert res.scanned == 0
    assert res.claimed == []
    alerts = store.alerts.list_unacked()
    assert len(alerts) == 1
    assert "GitHub sweep failed" in alerts[0]["message"]


async def test_one_bad_item_does_not_kill_the_sweep(store, settings):
    class FlakyGH(FakeGH):
        async def list_issues(self, **kw):
            return [issue(61), issue(62), issue(63)]

        async def create_issue(self, **kw):
            return await super().create_issue(**kw)

    gh = FlakyGH()
    svc = IntakeService(store, gh, settings)

    # make the second claim blow up
    original = svc.claim

    async def claim(number, title, gh_updated_at):
        if number == 62:
            raise RuntimeError("boom")
        return await original(number, title, gh_updated_at)

    svc.claim = claim
    res = await svc.sweep()
    assert sorted(res.claimed) == [61, 63]
    assert res.failed == [(62, "boom")]


async def test_add_requirement_creates_issue_with_ready_label(store, settings):
    gh = FakeGH()
    svc = IntakeService(store, gh, settings)
    num = await svc.add_requirement("Add JWT auth", "body here")
    assert num == 900
    # not tracked yet — intake owns that write
    assert store.work_items.exists(num) is False
    # and the next sweep picks it up
    gh._issues = [issue(num, title="Add JWT auth")]
    res = await svc.sweep()
    assert res.claimed == [num]


async def test_claim_returns_false_when_another_process_won(store, settings):
    gh = FakeGH()
    svc = IntakeService(store, gh, settings)
    assert await svc.claim(71, "req", "2026-11-08T00:00:00Z") is True
    assert await svc.claim(71, "req", "2026-11-08T00:00:00Z") is False
