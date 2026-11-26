"""T4: superseded sub-issues must be closable.

GithubClient.close_issue + the gh_close outbox kind.

The gate container has no third-party deps, so httpx is stubbed *before*
clams.github.client is imported (the client only touches httpx inside
_request/_graphql, which these tests replace).
"""
import sys, types  # noqa: E401
sys.modules.setdefault('httpx', types.ModuleType('httpx'))

import pytest  # noqa: E402

from clams.db import repo_outbox  # noqa: E402
from clams.github.client import GithubClient  # noqa: E402
from clams.outbox import OutboxRelay  # noqa: E402


class RecordingClient(GithubClient):
    """GithubClient with the transport replaced: every _request is recorded."""

    def __init__(self, response=None):
        super().__init__("tok")
        self.calls = []
        self.response = {"state": "open"} if response is None else response

    def _request(self, method, path, *, json_body=None, params=None):
        self.calls.append({"method": method, "path": path,
                           "json": json_body, "params": params})
        return dict(self.response)


# ------------------------------------------------------------- client method
def test_close_issue_exists():
    assert callable(getattr(GithubClient, "close_issue", None))


def test_close_issue_patches_state_closed():
    gh = RecordingClient()
    gh.close_issue("o/r", 42)
    assert [c["method"] for c in gh.calls] == ["PATCH"]
    call = gh.calls[0]
    assert call["path"] == "/repos/o/r/issues/42"
    assert call["json"] == {"state": "closed"}


def test_close_issue_posts_comment_first_when_given():
    gh = RecordingClient()
    gh.close_issue("o/r", 7, comment="superseded by #8")
    assert [c["method"] for c in gh.calls] == ["POST", "PATCH"]
    assert gh.calls[0]["path"] == "/repos/o/r/issues/7/comments"
    assert gh.calls[0]["json"] == {"body": "superseded by #8"}
    assert gh.calls[1]["path"] == "/repos/o/r/issues/7"
    assert gh.calls[1]["json"] == {"state": "closed"}


def test_close_issue_no_comment_by_default():
    gh = RecordingClient()
    gh.close_issue("o/r", 9)
    assert all("comments" not in c["path"] for c in gh.calls)


def test_close_issue_already_closed_is_success():
    """Re-delivery must not raise: state is read back from the response."""
    gh = RecordingClient(response={"state": "closed", "number": 42})
    out = gh.close_issue("o/r", 42, comment="superseded")
    assert out["state"] == "closed"
    assert out["closed"] is True


# --------------------------------------------------------------- outbox kind
@pytest.fixture()
def gh_close_db(db):
    """Migrated schema with the outbox kind CHECK widened to include gh_close.

    The shipped 001_init constraint predates this kind; widening it is a
    migration (out of scope for this module), so the test recreates the table
    without the enum to exercise the Python-side kind guard for real.
    """
    db.execute("DROP TABLE outbox")
    db.execute(
        "CREATE TABLE outbox ("
        "  outbox_id INTEGER PRIMARY KEY, kind TEXT NOT NULL,"
        "  payload TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,"
        "  status TEXT NOT NULL DEFAULT 'pending',"
        "  attempts INTEGER NOT NULL DEFAULT 0,"
        "  next_attempt_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        "  last_error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    return db


def test_gh_close_is_a_known_kind():
    assert "gh_close" in repo_outbox.OUTBOX_KINDS


def test_enqueue_accepts_gh_close(gh_close_db):
    oid = repo_outbox.enqueue(
        gh_close_db, "gh_close",
        {"repo": "o/r", "issue": 12, "comment": "superseded by #13"},
        "gh:gh_close:12")
    assert oid is not None
    # same idempotency key dedupes rather than raising
    assert repo_outbox.enqueue(gh_close_db, "gh_close",
                                {"repo": "o/r", "issue": 12},
                                "gh:gh_close:12") is None


def test_enqueue_rejects_unknown_kind(gh_close_db):
    with pytest.raises(ValueError):
        repo_outbox.enqueue(gh_close_db, "gh_nonsense", {}, "k")


# ------------------------------------------------------------- relay dispatch
class FakeGH:
    def __init__(self):
        self.closed = []

    def close_issue(self, repo, number, *, comment=None):
        self.closed.append((repo, number, comment))
        return {"state": "closed"}


def test_dispatch_gh_routes_gh_close_once(monkeypatch):
    fake = FakeGH()
    monkeypatch.setattr("clams.outbox._make_gh_client", lambda: fake)
    relay = OutboxRelay(None, gh_client=None)
    relay._dispatch_gh("gh_close", {"repo": "o/r", "issue": 3,
                                    "comment": "superseded by #4"})
    assert fake.closed == [("o/r", 3, "superseded by #4")]
    assert len(fake.closed) == 1


def test_dispatch_gh_uses_injected_client(monkeypatch):
    fake = FakeGH()

    def boom():  # must not be called: an injected client wins
        raise AssertionError("lazy client built despite injected gh client")

    monkeypatch.setattr("clams.outbox._make_gh_client", boom)
    OutboxRelay(None, gh_client=fake)._dispatch_gh(
        "gh_close", {"repo": "a/b", "issue": 5})
    assert fake.closed == [("a/b", 5, None)]


def test_dispatch_gh_close_end_to_end(gh_close_db):
    fake = FakeGH()
    relay = OutboxRelay(None, gh_client=fake)
    repo_outbox.enqueue(gh_close_db, "gh_close",
                        {"repo": "o/r", "issue": 1, "comment": "superseded"},
                        "k-close")
    assert relay.relay_once(gh_close_db) == {"sent": 1, "failed": 0, "dead": 0}
    assert fake.closed == [("o/r", 1, "superseded")]
