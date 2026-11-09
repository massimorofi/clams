"""Tests for issue #5: GitHub Projects (V2) board sync.

The board is driven by the same transition that writes the status label, and the
board's shape is validated once at startup rather than per item.
"""
from __future__ import annotations

import httpx
import pytest

from clams.projects import (
    DEFAULT_STATUS_MAP,
    ProjectBoard,
    ProjectConfigError,
    ProjectSyncError,
    board_from_settings,
)

PROJECT_NODE = {"id": "PVT_1", "title": "Claim"}

STATUS_OPTIONS = [
    {"id": "O_TODO", "name": "Todo"},
    {"id": "O_IP", "name": "In Progress"},
    {"id": "O_REVIEW", "name": "In Review"},
    {"id": "O_BLOCKED", "name": "Blocked"},
    {"id": "O_DONE", "name": "Done"},
]

FIELDS_OK = {
    "data": {
        "node": {
            "fields": {
                "nodes": [
                    {"id": "F1", "name": "Priority"},
                    {"id": "F_STATUS", "name": "Status", "options": STATUS_OPTIONS},
                ]
            }
        }
    }
}

OK_USER = {"data": {"user": {"projectV2": PROJECT_NODE}}}


def ok(payload):
    """Wrap a GraphQL payload the way GitHub does."""
    return {"data": payload}


class FakeClient:
    """Routes GraphQL requests to canned bodies by a substring of the query."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def post(self, url, json=None, **kw):
        query = (json or {}).get("query", "")
        self.calls.append((url, json))
        for needle, payload in self.routes:
            if needle in query:
                return _Resp(payload)
        return _Resp({"errors": [{"message": f"unrouted query"}]})


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = str(payload)

    def json(self):
        return self._payload


def make_board(routes, **kw):
    return ProjectBoard(
        token="tok", owner="acme", project_number=7,
        api_base="https://api.github.com",
        client=FakeClient(routes), **kw
    )


BASE_ROUTES = [
    ("user(login: $owner)", OK_USER),
    ("fields(first: 30)", FIELDS_OK),
]


# ---- startup resolution -------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_returns_ids():
    board = make_board(BASE_ROUTES)
    resolved = await board.resolve()
    assert resolved.project_id == "PVT_1"
    assert resolved.status_field_id == "F_STATUS"
    assert resolved.option_ids["Todo"] == "O_TODO"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_organization_lookup():
    org = {"data": {"organization": {"projectV2": {"id": "PVT_ORG", "title": "Org Board"}}}}
    routes = [
        ("user(login: $owner)", {"data": {"user": {"projectV2": None}}}),
        ("organization(login: $owner)", org),
        ("fields(first: 30)", FIELDS_OK),
    ]
    board = make_board(routes)
    resolved = await board.resolve()
    assert resolved.project_id == "PVT_ORG"


@pytest.mark.asyncio
async def test_resolve_fails_when_project_missing():
    routes = [
        ("user(login: $owner)", {"data": {"user": {"projectV2": None}}}),
        ("organization(login: $owner)",
         {"data": {"organization": {"projectV2": None}}}),
    ]
    board = make_board(routes)
    with pytest.raises(ProjectConfigError, match="not found"):
        await board.resolve()


@pytest.mark.asyncio
async def test_resolve_fails_when_status_field_missing():
    no_status = {"data": {"node": {"fields": {"nodes": [{"id": "F1", "name": "Priority"}]}}}}
    routes = [
        ("user(login: $owner)", OK_USER),
        ("fields(first: 30)", no_status),
    ]
    board = make_board(routes)
    with pytest.raises(ProjectConfigError, match="no single-select"):
        await board.resolve()


@pytest.mark.asyncio
async def test_resolve_fails_when_option_renamed():
    """A renamed column must fail at startup, not per-item (issue #5 acceptance)."""
    renamed = {
        "data": {
            "node": {
                "fields": {
                    "nodes": [
                        {
                            "id": "F_STATUS",
                            "name": "Status",
                            "options": [
                                {"id": "O_TODO", "name": "Todo"},
                                {"id": "O_IP", "name": "Doing"},
                            ],
                        }
                    ]
                }
            }
        }
    }
    routes = [
        ("user(login: $owner)", OK_USER),
        ("fields(first: 30)", renamed),
    ]
    board = make_board(routes)
    with pytest.raises(ProjectConfigError) as exc:
        await board.resolve()
    assert "missing status option" in str(exc.value)
    assert "In Review" in str(exc.value)


@pytest.mark.asyncio
async def test_resolve_fails_on_insufficient_scope():
    routes = [("user(login: $owner)",
               {"errors": [{"message": "Bad credentials"}]})]
    board = make_board(routes)
    with pytest.raises(ProjectConfigError, match="cannot read project"):
        await board.resolve()


@pytest.mark.asyncio
async def test_accessing_unresolved_board_raises():
    board = make_board([])
    with pytest.raises(ProjectConfigError, match="not resolved"):
        _ = board.resolved


@pytest.mark.asyncio
async def test_empty_token_rejected_at_construction():
    with pytest.raises(ProjectConfigError, match="read:project"):
        ProjectBoard(token="", owner="acme", project_number=1)


# ---- mutations ----------------------------------------------------------

@pytest.mark.asyncio
async def test_set_status_option_uses_mapped_option_id():
    routes = BASE_ROUTES + [
        ("updateProjectV2ItemFieldValue",
         ok({"updateProjectV2ItemFieldValue":
             {"projectV2Item": {"id": "ITEM_1"}, "errors": []}})),
    ]
    board = make_board(routes)
    await board.resolve()
    await board.set_status_option("ITEM_1", "status:Done")
    call = board._client.calls[-1][1]
    assert call["variables"]["value"] == "O_DONE"
    assert call["variables"]["fieldId"] == "F_STATUS"


@pytest.mark.asyncio
async def test_set_status_unknown_label_raises():
    board = make_board(BASE_ROUTES)
    await board.resolve()
    with pytest.raises(ProjectConfigError, match="no board column mapped"):
        await board.set_status_option("ITEM_1", "status:Bogus")


@pytest.mark.asyncio
async def test_set_status_surfaces_graphql_rejection():
    routes = BASE_ROUTES + [
        ("updateProjectV2ItemFieldValue",
         {"errors": [{"message": "Could not resolve to a node"}]}),
    ]
    board = make_board(routes)
    await board.resolve()
    with pytest.raises(ProjectSyncError):
        await board.set_status_option("ITEM_BAD", "status:Done")


@pytest.mark.asyncio
async def test_set_status_surfaces_field_level_errors():
    """GitHub can answer 200 with business errors inside the payload."""
    routes = BASE_ROUTES + [
        ("updateProjectV2ItemFieldValue",
         ok({"updateProjectV2ItemFieldValue":
             {"projectV2Item": None,
              "errors": [{"field": "singleSelectOptionId",
                         "message": "Invalid option"}]}})),
    ]
    board = make_board(routes)
    await board.resolve()
    with pytest.raises(ProjectSyncError, match="Invalid option"):
        await board.set_status_option("ITEM_1", "status:Done")


@pytest.mark.asyncio
async def test_add_item_returns_id():
    routes = BASE_ROUTES + [
        ("addProjectV2ItemById",
         ok({"addProjectV2ItemById": {"item": {"id": "ITEM_9"},
                                     "errors": []}})),
    ]
    board = make_board(routes)
    await board.resolve()
    assert await board.add_item("ISSUE_NODE") == "ITEM_9"


@pytest.mark.asyncio
async def test_add_item_surfaces_business_error():
    routes = BASE_ROUTES + [
        ("addProjectV2ItemById",
         ok({"addProjectV2ItemById":
             {"item": None,
              "errors": [{"field": "contentId",
                         "message": "already a project member"}]}})),
    ]
    board = make_board(routes)
    await board.resolve()
    with pytest.raises(ProjectSyncError, match="already a project member"):
        await board.add_item("ISSUE_NODE")


@pytest.mark.asyncio
async def test_find_item_paginates():
    page1 = ok({"node": {"items": {
        "pageInfo": {"hasNextPage": True, "endCursor": "C1"},
        "nodes": [{"id": "I1", "content": {"id": "OTHER"}}]}}})
    page2 = ok({"node": {"items": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [{"id": "I2", "content": {"id": "WANTED"}}]}}})

    class Router(FakeClient):
        def __init__(self):
            super().__init__([])
            self.n = 0

        async def post(self, url, json=None, **kw):
            self.calls.append((url, json))
            self.n += 1
            query = (json or {}).get("query", "")
            if "fields(first: 30)" in query:
                return _Resp(FIELDS_OK)
            if "user(login: $owner)" in query:
                return _Resp(OK_USER)
            return _Resp(page1 if self.n <= 3 else page2)

    board = ProjectBoard(token="tok", owner="acme", project_number=7,
                        client=Router())
    await board.resolve()
    assert await board.find_item("WANTED") == "I2"
    assert await board.find_item("MISSING") is None


@pytest.mark.asyncio
async def test_move_adds_card_when_absent():
    empty = ok({"node": {"items": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": []}}})
    routes = BASE_ROUTES + [
        ("items(first: 100", empty),
        ("addProjectV2ItemById",
         ok({"addProjectV2ItemById": {"item": {"id": "NEW_ITEM"},
                                     "errors": []}})),
        ("updateProjectV2ItemFieldValue",
         ok({"updateProjectV2ItemFieldValue":
             {"projectV2Item": {"id": "NEW_ITEM"}, "errors": []}})),
    ]
    board = make_board(routes)
    await board.resolve()
    assert await board.move("ISSUE_NODE", "status:Review") == "NEW_ITEM"


# ---- settings wiring ----------------------------------------------------

def test_project_settings_defaults():
    from clams.config import Settings
    s = Settings()
    assert s.project.enabled is False


def test_board_from_settings_disabled_returns_none():
    import asyncio
    from clams.config import Settings
    s = Settings()
    assert asyncio.run(board_from_settings(s)) is None


def test_board_from_settings_requires_number():
    import asyncio
    from clams.config import Settings
    s = Settings()
    s.project.enabled = True
    s.project.number = 0
    with pytest.raises(ProjectConfigError, match="NUMBER is not set"):
        asyncio.run(board_from_settings(s))


def test_parsed_status_map_override():
    from clams.config import Settings
    s = Settings()
    s.project.status_map = "status:Ready=Backlog,status:Done=Shipped"
    m = s.project.parsed_status_map(DEFAULT_STATUS_MAP)
    assert m["status:Ready"] == "Backlog"
    assert m["status:Done"] == "Shipped"
    assert m["status:Review"] == "In Review"  # untouched default


def test_parsed_status_map_rejects_garbage():
    from clams.config import Settings
    s = Settings()
    s.project.status_map = "nonsense"
    with pytest.raises(ValueError, match="status:Label=Column"):
        s.project.parsed_status_map(DEFAULT_STATUS_MAP)


# ---- label/board coupling ----------------------------------------------

def _issue_body(number=1, labels=None):
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "body",
        "state": "open",
        "updated_at": "2026-11-08T00:00:00Z",
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
        "labels": [{"name": l} for l in (labels or ["clams"])],
    }


def _gh_transport(request):
    path = request.url.path
    if request.method == "PUT" and path.endswith("/labels"):
        return httpx.Response(200, json=[{"name": "clams"},
                                        {"name": "status:Done"}])
    return httpx.Response(200, json=_issue_body())


@pytest.mark.asyncio
async def test_set_status_moves_card_with_label():
    """The label write and the card move happen in one transition."""
    from clams.gh import GitHubClient

    moved = {}

    class StubBoard:
        async def move(self, content_id, status_label):
            moved["content"] = content_id
            moved["status"] = status_label
            return "ITEM_1"

    def handler(request):
        if request.url.path.endswith("/graphql"):
            return httpx.Response(200, json={"data": {
                "repository": {"issue": {"id": "I_NODE"}}}})
        return _gh_transport(request)

    gh = GitHubClient("acme", "widgets", "tok",
                      transport=httpx.MockTransport(handler),
                      board=StubBoard())
    await gh.set_status(42, "status:Done")
    assert moved["status"] == "status:Done"
    assert moved["content"] == "I_NODE"


@pytest.mark.asyncio
async def test_set_status_without_board_is_unchanged():
    """No board attached -> behaviour identical to before the integration."""
    from clams.gh import GitHubClient

    gh = GitHubClient("acme", "widgets", "tok",
                      transport=httpx.MockTransport(_gh_transport))
    assert gh.board is None
    out = await gh.set_status(1, "status:Done")
    assert "status:Done" in out
