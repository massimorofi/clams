"""GitHub Work Plane client (spec §3.2, FR-2/FR-3).

REST for issues/labels/comments, GraphQL for native sub-issues.

Verified constraint (2026-11-08, this repo): the REST sub-issue route
``POST /repos/{owner}/{repo}/issues/{n}/sub_issues`` returns 404 because the
repository is a personal-account repo without Issue Types enabled. The GraphQL
``addSubIssue`` mutation works, so GraphQL is the primary path and a
``parent-N`` label plus a body marker is the durable fallback.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx


class GitHubError(Exception):
    def __init__(self, message: str, status: int = 0, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class GitHubNotFoundError(GitHubError):
    pass


# Canonical status labels used across the orchestrator.
STATUS_READY = "status:Ready"
STATUS_IN_PROGRESS = "status:In Progress"
STATUS_REVIEW = "status:Review"
STATUS_BLOCKED = "status:Blocked"
STATUS_DONE = "status:Done"

PROJECT_LABEL = "clams"

ALL_LABELS: list[tuple[str, str, str]] = [
    (PROJECT_LABEL, "0e8a16", "Clams orchestrator managed issue"),
    (STATUS_READY, "c2e0c6", "Ready for intake"),
    (STATUS_IN_PROGRESS, "fbca04", "Actively being executed"),
    (STATUS_REVIEW, "d4c5f9", "Awaiting review with evidence"),
    (STATUS_BLOCKED, "e9969a", "Blocked / needs attention"),
    (STATUS_DONE, "0e8a16", "Completed and verified"),
]


def parent_label(parent_number: int) -> str:
    """Fallback hierarchy marker.

    Deliberately avoids ``:`` and ``#``: httpx normalises percent-encoding in the
    request path, so a label containing those characters cannot be relied upon in
    a URL path segment.
    """
    return f"parent-{parent_number}"


@dataclass
class Issue:
    number: int
    title: str
    body: str = ""
    state: str = "open"
    labels: list[str] = field(default_factory=list)
    updated_at: str = ""
    html_url: str = ""

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Issue":
        return cls(
            number=int(d["number"]),
            title=d.get("title", ""),
            body=d.get("body") or "",
            state=d.get("state", "open"),
            labels=[l["name"] for l in d.get("labels", []) if isinstance(l, dict)],
            updated_at=d.get("updated_at", ""),
            html_url=d.get("html_url", ""),
        )

    def has_label(self, name: str) -> bool:
        return name in self.labels


class GitHubClient:
    """Thin async GitHub client with retry and typed errors."""

    MAX_RETRIES = 3
    BACKOFF_BASE = 1.5

    def __init__(
        self,
        owner: str,
        repo: str,
        token: str,
        api_base: str = "https://api.github.com",
        transport: Optional[httpx.AsyncBaseTransport] = None,
        board=None,
    ):
        self.owner = owner
        self.repo = repo
        self.token = token
        self.api_base = api_base.rstrip("/")
        self._transport = transport
        # Optional Projects V2 board. When set, every status label change also
        # moves the card, so the label and the column cannot diverge.
        self.board = board

    # -- transport -------------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "clams-orchestrator",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return httpx.AsyncClient(
            base_url=self.api_base, headers=headers, timeout=30.0,
            transport=self._transport,
        )

    async def _request(self, method: str, path: str, *, json_body: Any = None,
                      params: dict | None = None, expect_status: tuple[int, ...] = (200, 201)) -> Any:
        last_exc: Optional[Exception] = None
        for attempt in range(self.MAX_RETRIES):
            try:
                async with self._client() as client:
                    resp = await client.request(method, path, json=json_body, params=params)
                if resp.status_code in expect_status:
                    if resp.status_code == 204 or not resp.content:
                        return {}
                    return resp.json()
                if resp.status_code == 404:
                    raise GitHubNotFoundError(
                        f"404 on {method} {path}", status=404, body=_safe_json(resp)
                    )
                if resp.status_code >= 500 or resp.status_code == 403:
                    last_exc = GitHubError(
                        f"{resp.status_code} on {method} {path}",
                        status=resp.status_code, body=_safe_json(resp),
                    )
                    await _sleep(self.BACKOFF_BASE * (attempt + 1))
                    continue
                raise GitHubError(
                    f"{resp.status_code} on {method} {path}",
                    status=resp.status_code, body=_safe_json(resp),
                )
            except (httpx.TransportError,) as exc:
                last_exc = exc
                await _sleep(self.BACKOFF_BASE * (attempt + 1))
        raise GitHubError(f"exhausted retries for {method} {path}: {last_exc}")

    # -- reachability ----------------------------------------------------
    async def ping(self) -> bool:
        """HEAD api.github.com — watchdog reachability probe."""
        try:
            async with self._client() as client:
                resp = await client.head("/")
            return resp.status_code < 500
        except Exception:
            return False

    # -- issues ----------------------------------------------------------
    async def get_issue(self, number: int) -> Issue:
        data = await self._request("GET", f"/repos/{self.owner}/{self.repo}/issues/{number}")
        return Issue.from_api(data)

    async def list_issues(self, state: str = "open", labels: Optional[list[str]] = None,
                        since: Optional[str] = None, per_page: int = 100) -> list[Issue]:
        out: list[Issue] = []
        page = 1
        while True:
            params: dict[str, Any] = {"state": state, "per_page": per_page, "page": page}
            if labels:
                params["labels"] = ",".join(labels)
            if since:
                params["since"] = since
            data = await self._request(
                "GET", f"/repos/{self.owner}/{self.repo}/issues", params=params
            )
            if not data:
                break
            out.extend(Issue.from_api(d) for d in data)
            if len(data) < per_page:
                break
            page += 1
        return out

    async def create_issue(self, title: str, body: str = "",
                         labels: Optional[list[str]] = None) -> Issue:
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        data = await self._request(
            "POST", f"/repos/{self.owner}/{self.repo}/issues", json_body=payload
        )
        return Issue.from_api(data)

    async def update_issue(self, number: int, *, title: Optional[str] = None,
                          body: Optional[str] = None, state: Optional[str] = None) -> Issue:
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        data = await self._request(
            "PATCH", f"/repos/{self.owner}/{self.repo}/issues/{number}", json_body=payload
        )
        return Issue.from_api(data)

    # -- comments --------------------------------------------------------
    async def add_comment(self, number: int, body: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/repos/{self.owner}/{self.repo}/issues/{number}/comments",
            json_body={"body": body},
        )

    async def list_comments(self, number: int) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", f"/repos/{self.owner}/{self.repo}/issues/{number}/comments"
        )
        return data or []

    # -- labels ----------------------------------------------------------
    async def add_labels(self, number: int, labels: list[str]) -> list[str]:
        data = await self._request(
            "POST", f"/repos/{self.owner}/{self.repo}/issues/{number}/labels",
            json_body={"labels": labels},
        )
        return [l["name"] for l in (data or []) if isinstance(l, dict)]

    async def replace_labels(self, number: int, labels: list[str]) -> list[str]:
        data = await self._request(
            "PUT", f"/repos/{self.owner}/{self.repo}/issues/{number}/labels",
            json_body={"labels": labels},
        )
        return [l["name"] for l in (data or []) if isinstance(l, dict)]

    async def remove_label(self, number: int, label: str) -> None:
        await self._request(
            "DELETE",
            f"/repos/{self.owner}/{self.repo}/issues/{number}/labels/{_quote(label)}",
            expect_status=(200, 204),
        )

    async def set_status(self, number: int, status_label: str,
                        keep: Optional[list[str]] = None) -> list[str]:
        """Replace the status:* label with ``status_label`` keeping non-status labels.

        When a Projects V2 board is attached the card is moved in the same call,
        so the label and the board column are written from one transition and
        cannot drift apart.
        """
        issue = await self.get_issue(number)
        non_status = [l for l in issue.labels if not l.startswith("status:")]
        wanted = list(dict.fromkeys((keep or []) + non_status + [status_label]))
        result = await self.replace_labels(number, wanted)
        if self.board is not None:
            content_id = await self.issue_node_id(number)
            await self.board.move(content_id, status_label)
        return result

    async def ensure_labels(self, labels: Optional[list[tuple[str, str, str]]] = None) -> None:
        """Create any missing labels from the canonical set."""
        wanted = labels or ALL_LABELS
        for name, color, desc in wanted:
            try:
                await self._request(
                    "POST", f"/repos/{self.owner}/{self.repo}/labels",
                    json_body={"name": name, "color": color, "description": desc},
                    expect_status=(201, 200),
                )
            except GitHubError as exc:
                # already exists -> GitHub returns 422 with a name-pair error
                if exc.status != 422:
                    raise

    # -- native sub-issues (GraphQL) ------------------------------------
    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        data = await self._request(
            "POST", "/graphql", json_body={"query": query, "variables": variables}
        )
        if isinstance(data, dict) and data.get("errors"):
            raise GitHubError(
                f"GraphQL error: {data['errors'][0].get('message', 'unknown')}",
                status=data.get("status", 200), body=data,
            )
        return data.get("data", {})

    async def issue_node_id(self, number: int) -> str:
        q = (
            'query($owner: String!, $repo: String!, $number: Int!) {'
            ' repository(owner: $owner, name: $repo) {'
            '  issue(number: $number) { id } } }'
        )
        data = await self._graphql(q, {"owner": self.owner, "repo": self.repo, "number": number})
        node = data.get("repository", {}).get("issue")
        if not node:
            raise GitHubNotFoundError(f"issue #{number} not found", status=404)
        return node["id"]

    async def add_sub_issue(self, parent_number: int, child_number: int) -> bool:
        """Link ``child_number`` as a native sub-issue of ``parent_number``.

        Falls back to a ``parent-N`` label if the native mutation is refused, so
        the hierarchy is never silently lost.
        """
        parent_id = await self.issue_node_id(parent_number)
        child_id = await self.issue_node_id(child_number)
        mutation = (
            'mutation($p: ID!, $s: ID!) {'
            ' addSubIssue(input: {issueId: $p, subIssueId: $s}) {'
            '  issue { number } } }'
        )
        try:
            await self._graphql(mutation, {"p": parent_id, "s": child_id})
            return True
        except GitHubError:
            await self.add_labels(child_number, [parent_label(parent_number)])
            return False

    async def remove_sub_issue(self, parent_number: int, child_number: int) -> bool:
        parent_id = await self.issue_node_id(parent_number)
        child_id = await self.issue_node_id(child_number)
        mutation = (
            'mutation($p: ID!, $s: ID!) {'
            ' removeSubIssue(input: {issueId: $p, subIssueId: $s}) {'
            '  issue { number } } }'
        )
        try:
            await self._graphql(mutation, {"p": parent_id, "s": child_id})
            return True
        except GitHubError:
            try:
                await self.remove_label(child_number, parent_label(parent_number))
            except GitHubError:
                pass
            return False

    async def list_sub_issues(self, parent_number: int) -> list[Issue]:
        try:
            data = await self._request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/issues/{parent_number}/sub_issues",
                params={"per_page": 100},
            )
            if isinstance(data, list):
                return [Issue.from_api(d) for d in data]
        except GitHubError:
            pass
        # fallback: label-based discovery
        issues = await self.list_issues(state="open",
                                      labels=[parent_label(parent_number)])
        return issues


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text[:500]


def _quote(label: str) -> str:
    from urllib.parse import quote
    return quote(label, safe="")


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
