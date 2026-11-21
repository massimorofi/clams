"""GitHub client: REST for issues/labels/comments, GraphQL for sub-issues and
Projects V2 cards. Honours rate-limit headers (bounded sleep). Transport is
injectable so tests never hit the network.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

log = logging.getLogger("clams.github")

STATUS_READY = "status:Ready"
STATUS_IN_PROGRESS = "status:In Progress"
STATUS_REVIEW = "status:Review"
STATUS_BLOCKED = "status:Blocked"
STATUS_DONE = "status:Done"
STATUS_CANCELLED = "status:Cancelled"
CLAMS_TASK_LABEL = "clams:task"


class GitHubError(Exception):
    def __init__(self, message: str, status: int = 0, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class GitHubClient:
    def __init__(self, token: str, *, api_base: str = "https://api.github.com",
                 graphql_base: str = "https://api.github.com/graphql",
                 transport: Optional[httpx.BaseTransport] = None,
                 max_rate_wait_s: float = 60.0):
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.graphql_base = graphql_base
        self._transport = transport
        self.max_rate_wait_s = max_rate_wait_s

    # ------------------------------------------------------------------ core
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _request(self, method: str, path: str, *, json_body: Any = None,
                 params: Optional[dict] = None) -> Any:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        with httpx.Client(transport=self._transport, timeout=30.0) as c:
            r = c.request(method, url, headers=self._headers(),
                          json=json_body, params=params)
        self._respect_rate_limit(r)
        if r.status_code >= 400:
            raise GitHubError(f"{method} {path} -> {r.status_code}: "
                              f"{r.text[:300]}", status=r.status_code,
                              body=r.text[:2000])
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def _graphql(self, query: str, variables: dict) -> Any:
        with httpx.Client(transport=self._transport, timeout=30.0) as c:
            r = c.post(self.graphql_base, headers=self._headers(),
                       json={"query": query, "variables": variables})
        self._respect_rate_limit(r)
        if r.status_code >= 400:
            raise GitHubError(f"graphql -> {r.status_code}: {r.text[:300]}",
                              status=r.status_code)
        data = r.json()
        if data.get("errors"):
            raise GitHubError(f"graphql errors: {data['errors'][:2]}")
        return data.get("data")

    def _respect_rate_limit(self, r: httpx.Response) -> None:
        try:
            remaining = int(r.headers.get("x-ratelimit-remaining", "1"))
            reset = int(r.headers.get("x-ratelimit-reset", "0"))
        except ValueError:
            return
        if remaining == 0 and reset > time.time():
            wait = min(reset - time.time(), self.max_rate_wait_s)
            log.warning("github rate limit; sleeping %.0fs", wait)
            time.sleep(wait)

    # ---------------------------------------------------------------- issues
    def list_issues_by_label(self, repo: str, label: str,
                             state: str = "open") -> list[dict]:
        out = []
        page = 1
        while True:
            batch = self._request(
                "GET", f"/repos/{repo}/issues",
                params={"labels": label, "state": state, "per_page": 100,
                        "page": page}) or []
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def get_issue(self, repo: str, number: int) -> dict:
        return self._request("GET", f"/repos/{repo}/issues/{number}")

    def create_issue(self, repo: str, title: str, body: str,
                     labels: list[str]) -> dict:
        return self._request("POST", f"/repos/{repo}/issues",
                             json_body={"title": title, "body": body,
                                        "labels": labels})

    def add_comment(self, repo: str, number: int, body: str, *,
                    idempotency_key: str = "") -> dict:
        if idempotency_key:
            # crash-safe dedup: if a comment carrying this key's marker already
            # exists, the previous attempt landed -> skip (no duplicate comment).
            existing = self._request(
                "GET", f"/repos/{repo}/issues/{number}/comments") or []
            if any(idempotency_key in (c.get("body") or "") for c in existing):
                return {"deduped": True}
        return self._request("POST", f"/repos/{repo}/issues/{number}/comments",
                             json_body={"body": body})

    def set_labels(self, repo: str, number: int, labels: list[str], *,
                   idempotency_key: str = "") -> dict:
        return self._request(
            "PATCH", f"/repos/{repo}/issues/{number}",
            json_body={"labels": labels})

    def ensure_label(self, repo: str, name: str, color: str = "0e8a16",
                     description: str = "") -> None:
        try:
            self._request("GET", f"/repos/{repo}/labels/{name}")
        except GitHubError as exc:
            if exc.status == 404:
                self._request("POST", f"/repos/{repo}/labels",
                              json_body={"name": name, "color": color,
                                         "description": description})
            else:
                raise

    # ------------------------------------------------------------- sub-issues
    def add_subissue(self, repo: str, parent: int, child: int, *,
                     idempotency_key: str = "") -> bool:
        """Native GraphQL addSubIssue; returns True on success."""
        owner, name = repo.split("/", 1)
        parent_id = self._issue_node_id(owner, name, parent)
        child_id = self._issue_node_id(owner, name, child)
        if not parent_id or not child_id:
            return False
        self._graphql(
            "mutation($parent: ID!, $child: ID!) { "
            "addSubIssue(input: {issueId: $parent, subIssueId: $child}) "
            "{ issue { number } } }",
            {"parent": parent_id, "child": child_id})
        return True

    def _issue_node_id(self, owner: str, name: str, number: int) -> Optional[str]:
        data = self._graphql(
            "query($o: String!, $r: String!, $n: Int!) { "
            "repository(owner: $o, name: $r) { issue(number: $n) { id } } }",
            {"o": owner, "r": name, "n": number})
        try:
            return data["repository"]["issue"]["id"]
        except (TypeError, KeyError):
            return None

    # ---------------------------------------------------------- Projects V2
    def resolve_project(self, owner: str, number: int) -> Optional[dict]:
        """Return {node_id, status_field_id, options: {name: option_id}}."""
        data = self._graphql(
            "query($o: String!, $n: Int!) { "
            "user(login: $o) { projectV2(number: $n) { id "
            "fields(first: 20) { nodes { ... on ProjectV2SingleSelectField "
            "{ id name options { id name } } } } } } }",
            {"o": owner, "n": number})
        proj = (data or {}).get("user", {}).get("projectV2")
        if not proj:
            return None
        status_field = None
        for f in proj["fields"]["nodes"]:
            if f.get("name") == "Status":
                status_field = f
                break
        if not status_field:
            return None
        return {"node_id": proj["id"],
                "status_field_id": status_field["id"],
                "options": {o["name"]: o["id"]
                            for o in status_field["options"]}}

    def list_board_items(self, project_node_id: str) -> dict[int, str]:
        """Map issue number -> Status option name for every issue card on the
        board (first 100 items). Drafts/PRs and items without a Status value
        are skipped."""
        data = self._graphql(
            "query($p: ID!) { node(id: $p) { ... on ProjectV2 { "
            "items(first: 100) { nodes { content { ... on Issue { number } } "
            "fieldValues(first: 20) { nodes { "
            "... on ProjectV2ItemFieldSingleSelectValue { name "
            "field { ... on ProjectV2FieldCommon { name } } } } } } } } } }",
            {"p": project_node_id})
        out: dict[int, str] = {}
        try:
            nodes = data["node"]["items"]["nodes"]
        except (TypeError, KeyError):
            return out
        for n in nodes:
            number = (n.get("content") or {}).get("number")
            if number is None:
                continue
            for fv in (n.get("fieldValues") or {}).get("nodes", []):
                if (fv.get("field") or {}).get("name") == "Status" and fv.get("name"):
                    out[number] = fv["name"]
                    break
        return out

    def move_card(self, project_node_id: Optional[str], issue_number: int,
                  option_name: str, *, idempotency_key: str = "") -> bool:
        """Move the issue's project card to the Status option with this name.

        Raises GitHubError when the card or option is missing so the outbox
        retries (and eventually dead-letters) instead of silently no-oping.
        """
        if not project_node_id:
            raise GitHubError("move_card: no project node id")
        item = self._card_item(project_node_id, issue_number)
        if not item:
            raise GitHubError(
                f"move_card: issue #{issue_number} has no card on board "
                f"{project_node_id}")
        field_id, options = self._status_field_and_options(project_node_id)
        option_id = options.get(option_name)
        if not option_id:
            raise GitHubError(
                f"move_card: board has no Status option {option_name!r} "
                f"(has: {sorted(options)})")
        self._graphql(
            "mutation($p: ID!, $i: ID!, $f: ID!, $v: String!) { "
            "updateProjectV2ItemFieldValue(input: {projectId: $p, itemId: $i, "
            "fieldId: $f, value: {singleSelectOptionId: $v}}) { "
            "projectV2Item { id } } }",
            {"p": project_node_id, "i": item, "f": field_id, "v": option_id})
        return True

    def _card_item(self, project_node_id: str, issue_number: int) -> Optional[str]:
        data = self._graphql(
            "query($p: ID!) { node(id: $p) { ... on ProjectV2 { "
            "items(first: 100) { nodes { id content { ... on Issue { number } } } } } } }",
            {"p": project_node_id})
        try:
            for n in data["node"]["items"]["nodes"]:
                if (n.get("content") or {}).get("number") == issue_number:
                    return n["id"]
        except (TypeError, KeyError):
            return None
        return None

    def _status_field_and_options(self, project_node_id: str) -> tuple[str, dict]:
        """Return (status_field_id, {option_name: option_id})."""
        data = self._graphql(
            "query($p: ID!) { node(id: $p) { ... on ProjectV2 { "
            "fields(first: 20) { nodes { ... on ProjectV2SingleSelectField "
            "{ id name options { id name } } } } } } }", {"p": project_node_id})
        try:
            for f in data["node"]["fields"]["nodes"]:
                if f.get("name") == "Status":
                    return f["id"], {o["name"]: o["id"] for o in f["options"]}
        except (TypeError, KeyError):
            pass
        raise GitHubError(f"board {project_node_id} has no Status field")

    def health(self) -> bool:
        """HEAD reachability probe (watchdog)."""
        try:
            with httpx.Client(transport=self._transport, timeout=10.0) as c:
                return c.head(self.api_base).status_code < 500
        except Exception:
            return False
