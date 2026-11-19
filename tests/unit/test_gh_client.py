"""WP5: GitHub client against a mocked transport (no network)."""
from __future__ import annotations

import json
import time

import httpx
import pytest

from clams.github.client import GitHubClient, GitHubError


def make_client(handler):
    return GitHubClient("tok", transport=httpx.MockTransport(handler))


def test_list_issues_pagination():
    def handler(req):
        page = int(req.url.params["page"])
        if page == 1:
            body = [{"number": i, "labels": []} for i in range(100)]
        else:
            body = [{"number": 999, "labels": []}]
        return httpx.Response(200, json=body)
    gh = make_client(handler)
    out = gh.list_issues_by_label("o/r", "status:Ready")
    assert len(out) == 101


def test_add_comment_and_labels():
    seen = []

    def handler(req):
        seen.append((req.method, str(req.url)))
        return httpx.Response(200, json={"ok": True})
    gh = make_client(handler)
    gh.add_comment("o/r", 5, "hello")
    gh.set_labels("o/r", 5, ["status:Done"])
    assert seen[0][0] == "POST" and "comments" in seen[0][1]
    assert seen[1][0] == "PATCH"


def test_error_raises():
    gh = make_client(lambda req: httpx.Response(404, json={"m": "nope"}))
    with pytest.raises(GitHubError) as ei:
        gh.get_issue("o/r", 1)
    assert ei.value.status == 404


def test_rate_limit_header_respected():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=[], headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(time.time()) + 1)})
        return httpx.Response(200, json=[])
    gh = GitHubClient("t", transport=httpx.MockTransport(handler),
                      max_rate_wait_s=2)
    out = gh.list_issues_by_label("o/r", "l")
    assert out == []          # second call succeeded after bounded wait


def test_graphql_subissue():
    def handler(req):
        body = json.loads(req.content)
        if "repository" in body["query"]:
            return httpx.Response(200, json={"data": {
                "repository": {"issue": {"id": "I_1"}}}})
        return httpx.Response(200, json={"data": {"addSubIssue": {}}})
    gh = make_client(handler)
    assert gh.add_subissue("o/r", 1, 2) is True


def test_graphql_errors_raise():
    gh = make_client(lambda req: httpx.Response(
        200, json={"errors": [{"message": "bad"}]}))
    with pytest.raises(GitHubError):
        gh.resolve_project("o", 1)


def test_ensure_label_creates_when_missing():
    def handler(req):
        if req.method == "GET":
            return httpx.Response(404, json={})
        return httpx.Response(201, json={})
    gh = make_client(handler)
    gh.ensure_label("o/r", "status:Ready")


# ------------------------------------------------------------- board-driven


def _board_graphql_handler(calls):
    def handler(req):
        q = json.loads(req.content)["query"]
        if "items(first: 100) { nodes { content { ... on Issue { number } }" in q \
                and "fieldValues" in q:
            return httpx.Response(200, json={"data": {"node": {"items": {"nodes": [
                {"content": {"number": 56},
                 "fieldValues": {"nodes": [
                     {"name": "Ready", "field": {"name": "Status"}}]}},
                {"content": {"number": 57},
                 "fieldValues": {"nodes": [
                     {"name": "Done", "field": {"name": "Status"}}]}},
                {"content": None,
                 "fieldValues": {"nodes": [
                     {"name": "Backlog", "field": {"name": "Status"}}]}},
            ]}}}})
        if "items(first: 100)" in q:  # _card_item
            return httpx.Response(200, json={"data": {"node": {"items": {"nodes": [
                {"id": "PVTI_1", "content": {"number": 56}}]}}}})
        if "fields(first: 20)" in q:  # _status_field_and_options
            return httpx.Response(200, json={"data": {"node": {"fields": {"nodes": [
                {"id": "PVTSSF_1", "name": "Status",
                 "options": [{"id": "opt_ready", "name": "Ready"},
                             {"id": "opt_done", "name": "Done"}]}]}}}})
        calls.append(json.loads(req.content))  # mutation
        return httpx.Response(200, json={"data": {"updateProjectV2ItemFieldValue": {}}})
    return handler


def test_list_board_items():
    gh = make_client(_board_graphql_handler([]))
    assert gh.list_board_items("PVT_1") == {56: "Ready", 57: "Done"}


def test_move_card_resolves_option_by_name():
    calls = []
    gh = make_client(_board_graphql_handler(calls))
    assert gh.move_card("PVT_1", 56, "Done") is True
    assert calls[-1]["variables"] == {"p": "PVT_1", "i": "PVTI_1",
                                      "f": "PVTSSF_1", "v": "opt_done"}


def test_move_card_unknown_option_raises():
    gh = make_client(_board_graphql_handler([]))
    with pytest.raises(GitHubError, match="no Status option"):
        gh.move_card("PVT_1", 56, "Blocked")


def test_move_card_missing_card_raises():
    gh = make_client(_board_graphql_handler([]))
    with pytest.raises(GitHubError, match="no card"):
        gh.move_card("PVT_1", 999, "Done")
