"""S2 — GitHub client tests (transport mocked)."""
from __future__ import annotations

import json

import httpx
import pytest

from clams.gh import (
    ALL_LABELS,
    GitHubClient,
    GitHubError,
    GitHubNotFoundError,
    Issue,
    STATUS_BLOCKED,
    STATUS_REVIEW,
    parent_label,
)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr("clams.gh._sleep", lambda s: __import__("asyncio").sleep(0))


def make_client(handler) -> GitHubClient:
    transport = httpx.MockTransport(handler)
    return GitHubClient("acme", "widgets", "tok", transport=transport)


def issue_json(n=1, **kw):
    d = {
        "number": n,
        "title": kw.get("title", f"Issue {n}"),
        "body": kw.get("body", "body"),
        "state": kw.get("state", "open"),
        "updated_at": "2026-11-08T00:00:00Z",
        "html_url": f"https://github.com/acme/widgets/issues/{n}",
        "labels": [{"name": l} for l in kw.get("labels", [])],
    }
    return d


# ---- Issue model ------------------------------------------------------
def test_issue_from_api():
    i = Issue.from_api(issue_json(7, labels=["bug", "status:Ready"]))
    assert i.number == 7
    assert i.has_label("status:Ready")
    assert not i.has_label("nope")


def test_parent_label_format():
    assert parent_label(5) == "parent-5"


# ---- ping -------------------------------------------------------------
async def test_ping_ok():
    c = make_client(lambda req: httpx.Response(200))
    assert await c.ping() is True


async def test_ping_down():
    def handler(req):
        raise httpx.ConnectError("boom")
    c = make_client(handler)
    assert await c.ping() is False


# ---- get / create / update -------------------------------------------
async def test_get_issue():
    c = make_client(lambda req: httpx.Response(200, json=issue_json(3, title="Hello")))
    i = await c.get_issue(3)
    assert i.title == "Hello"


async def test_get_issue_not_found():
    def handler(req):
        return httpx.Response(404, json={"message": "Not Found"})
    c = make_client(handler)
    with pytest.raises(GitHubNotFoundError):
        await c.get_issue(999)


async def test_create_issue_sends_labels():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json=issue_json(10, **seen["body"]))

    c = make_client(handler)
    out = await c.create_issue("T", "B", labels=["clams", "status:Ready"])
    assert out.number == 10
    assert seen["body"]["labels"] == ["clams", "status:Ready"]


async def test_update_issue_partial():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=issue_json(4, state="closed"))

    c = make_client(handler)
    out = await c.update_issue(4, state="closed")
    assert "title" not in seen["body"]
    assert out.state == "closed"


# ---- pagination -------------------------------------------------------
async def test_list_issues_paginates():
    calls = []

    def handler(req):
        page = int(req.url.params.get("page", 1))
        calls.append(page)
        if page == 1:
            return httpx.Response(200, json=[issue_json(i) for i in range(1, 101)])
        if page == 2:
            return httpx.Response(200, json=[issue_json(101)])
        return httpx.Response(200, json=[])

    c = make_client(handler)
    out = await c.list_issues(labels=["status:Ready"], per_page=100)
    assert len(out) == 101
    # page 2 returned a short page, so the loop stops there
    assert calls == [1, 2]


async def test_list_issues_passes_labels_and_since():
    seen = {}

    def handler(req):
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=[])

    c = make_client(handler)
    await c.list_issues(labels=["a", "b"], since="2026-01-01T00:00:00Z")
    assert seen["params"]["labels"] == "a,b"
    assert seen["params"]["since"] == "2026-01-01T00:00:00Z"


# ---- comments ---------------------------------------------------------
async def test_add_comment():
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": 1, "body": seen["body"]["body"]})

    c = make_client(handler)
    out = await c.add_comment(12, "evidence here")
    assert out["id"] == 1
    assert seen["path"].endswith("/issues/12/comments")


async def test_list_comments():
    c = make_client(lambda req: httpx.Response(200, json=[{"id": 1}, {"id": 2}]))
    assert len(await c.list_comments(5)) == 2


# ---- labels -----------------------------------------------------------
async def test_add_labels():
    c = make_client(lambda req: httpx.Response(201, json=[{"name": "x"}]))
    assert await c.add_labels(1, ["x"]) == ["x"]


async def test_set_status_replaces_only_status_label():
    calls = []

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=issue_json(2, labels=["clams", "status:Ready"]))
        if req.method == "PUT":
            calls.append(json.loads(req.content)["labels"])
            return httpx.Response(200, json=[{"name": l} for l in calls[-1]])
        return httpx.Response(400)

    c = make_client(handler)
    out = await c.set_status(2, STATUS_REVIEW)
    assert "clams" in out
    assert STATUS_REVIEW in out
    assert "status:Ready" not in out


async def test_remove_label_uses_label_in_path():
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        return httpx.Response(204)

    c = make_client(handler)
    await c.remove_label(1, "parent-5")
    assert seen["path"].endswith("/issues/1/labels/parent-5")


async def test_ensure_labels_creates_all_and_tolerates_existing():
    created = []

    def handler(req):
        name = json.loads(req.content)["name"]
        if name == "status:Done":
            return httpx.Response(422, json={"errors": [{"message": "already_exists"}]})
        created.append(name)
        return httpx.Response(201, json={"name": name})

    c = make_client(handler)
    await c.ensure_labels()
    assert len(created) == len(ALL_LABELS) - 1


async def test_ensure_labels_raises_on_unexpected_error():
    def handler(req):
        return httpx.Response(401, json={"message": "bad creds"})

    c = make_client(handler)
    with pytest.raises(GitHubError):
        await c.ensure_labels()


# ---- sub-issues -------------------------------------------------------
def gql_router(mutate_ok=True):
    """Mock transport that answers node-id lookups and the addSubIssue mutation."""
    calls = []

    def handler(req):
        payload = json.loads(req.content)
        q = payload["query"] if req.url.path == "/graphql" else ""
        calls.append((req.method, req.url.path, q))
        num = payload["variables"].get("number") if req.url.path == "/graphql" else None
        if "addSubIssue" in q:
            if not mutate_ok:
                return httpx.Response(200, json={"errors": [{"message": "denied"}]})
            return httpx.Response(200, json={"data": {"addSubIssue": {"issue": {"number": 9}}}})
        # node id lookup
        node_id = "PID" if num == 5 else f"CID{num}"
        return httpx.Response(200, json={"data": {"repository": {"issue": {"id": node_id}}}})

    return handler, calls


async def test_add_sub_issue_uses_graphql():
    handler, calls = gql_router(mutate_ok=True)
    c = make_client(handler)
    assert await c.add_sub_issue(5, 9) is True
    # two node-id lookups + one mutation
    assert len(calls) == 3
    assert "addSubIssue" in calls[-1][2]


async def test_add_sub_issue_falls_back_to_label():
    handler, calls = gql_router(mutate_ok=False)
    c = make_client(handler)
    ok = await c.add_sub_issue(5, 9)
    assert ok is False
    # the fallback label POST happened
    assert any(p.endswith("/issues/9/labels") for _, p, _ in calls)
    # verify via a separate client that a label POST was issued
    posted = []

    def h2(req):
        posted.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(201, json=[{"name": "parent-5"}])

    c2 = make_client(h2)
    out = await c2.add_labels(9, [parent_label(5)])
    assert out == ["parent-5"]
    assert posted[0][1].endswith("/issues/9/labels")


async def test_list_sub_issues_rest_ok():
    c = make_client(lambda req: httpx.Response(200, json=[issue_json(1), issue_json(2)]))
    out = await c.list_sub_issues(5)
    assert [i.number for i in out] == [1, 2]


async def test_list_sub_issues_falls_back_to_label_query():
    def handler(req):
        if "sub_issues" in req.url.path:
            return httpx.Response(404, json={"message": "Not Found"})
        # label listing
        assert req.url.params.get("labels") == "parent-5"
        return httpx.Response(200, json=[issue_json(42)])

    c = make_client(handler)
    out = await c.list_sub_issues(5)
    assert [i.number for i in out] == [42]


async def test_graphql_error_raises():
    def handler(req):
        return httpx.Response(200, json={"errors": [{"message": "boom"}]})

    c = make_client(handler)
    with pytest.raises(GitHubError, match="boom"):
        await c._graphql("query { x }", {})


# ---- retry behaviour --------------------------------------------------
async def test_retries_on_5xx_then_succeeds():
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] < 3:
            return httpx.Response(502, json={"message": "bad gateway"})
        return httpx.Response(200, json=issue_json(1))

    c = make_client(handler)
    i = await c.get_issue(1)
    assert i.number == 1
    assert state["n"] == 3


async def test_exhausted_retries_raise():
    def handler(req):
        return httpx.Response(500, json={"message": "down"})

    c = make_client(handler)
    with pytest.raises(GitHubError):
        await c.get_issue(1)
