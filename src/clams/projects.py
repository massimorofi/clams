"""GitHub Projects (V2) board sync (spec issue #5).

Clams drives card placement through the *same* state transitions that drive the
``status:*`` labels, so the label and the board column can never diverge: the
board is a projection of the label, not a second source of truth.

Two design rules make this safe:

1. **Resolve once at startup.** The project ID, the status field ID and the
   option ID for every status are looked up in one pass before any work runs.
   A renamed or missing status option therefore fails loudly at startup rather
   than failing per-item halfway through a run.
2. **Fail loudly, never silently.** Every mutation checks the GraphQL ``errors``
   payload and the ``updatedProjectV2FieldValue.errors`` payload. A write that
   GitHub rejected raises instead of returning quietly.

Requires a token with ``read:project`` and ``update:project`` scopes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Clams status label -> Projects V2 "Status" field option name.
DEFAULT_STATUS_MAP: dict[str, str] = {
    "status:Ready": "Todo",
    "status:In Progress": "In Progress",
    "status:Review": "In Review",
    "status:Blocked": "Blocked",
    "status:Done": "Done",
}


class ProjectConfigError(Exception):
    """The board is not configured the way Clams requires.

    Raised at startup, never per-item: a renamed status option is a deployment
    error and must stop the run rather than corrupt one card at a time.
    """


class ProjectSyncError(Exception):
    """A single board mutation was rejected by GitHub."""


def _extract_error(payload: Any) -> str:
    """Pull a human-readable message out of a GraphQL error payload."""
    if isinstance(payload, dict):
        errs = payload.get("errors")
        if errs:
            first = errs[0] if isinstance(errs, list) else errs
            if isinstance(first, dict):
                return first.get("message") or str(first)
            return str(first)
    return str(payload)


@dataclass
class ResolvedBoard:
    """Everything about the board that must be true before Clams touches it."""

    project_id: str
    title: str
    status_field_id: str
    option_ids: dict[str, str] = field(default_factory=dict)

    def option_id_for(self, option_name: str) -> str:
        try:
            return self.option_ids[option_name]
        except KeyError:
            raise ProjectConfigError(
                f"status option {option_name!r} not found on board "
                f"{self.title!r}. Available: "
                f"{sorted(self.option_ids)}"
            ) from None


class ProjectBoard:
    """Thin, explicit client over the Projects V2 GraphQL API."""

    def __init__(
        self,
        token: str,
        owner: str,
        project_number: int,
        *,
        api_base: str = "https://api.github.com",
        status_field_name: str = "Status",
        status_map: Optional[dict[str, str]] = None,
        client: Any = None,
    ) -> None:
        if not token:
            raise ProjectConfigError(
                "no GitHub token configured; Projects V2 requires a token with "
                "'read:project' and 'update:project' scopes"
            )
        self.token = token
        self.owner = owner
        self.project_number = int(project_number)
        self.api_base = api_base.rstrip("/")
        self.status_field_name = status_field_name
        self.status_map = dict(status_map or DEFAULT_STATUS_MAP)
        self._client = client
        self._resolved: Optional[ResolvedBoard] = None

    # -- transport -----------------------------------------------------
    async def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """POST a GraphQL query and return the raw decoded body.

        Unlike the REST client this deliberately does not raise on a 4xx: GraphQL
        reports authorisation and validation problems in the body with a 200, so
        the body is the only reliable signal.
        """
        import httpx

        if self._client is not None:
            resp = await self._client.post(
                f"{self.api_base}/graphql",
                json={"query": query, "variables": variables},
            )
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.api_base}/graphql",
                    json={"query": query, "variables": variables},
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                        "Accept": "application/vnd.github+json",
                        "GraphQL-Features": "projects_v2",
                    },
                )
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 - non-JSON error page
            body = {"errors": [{"message": resp.text[:300]}]}
        return body if isinstance(body, dict) else {}

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        body = await self._post(query, variables)
        if body.get("errors"):
            raise ProjectSyncError(_extract_error(body))
        return body.get("data") or {}

    # -- startup resolution -------------------------------------------
    async def resolve(self) -> ResolvedBoard:
        """Look up the project, its status field and every option, once.

        Raises ProjectConfigError with an actionable message if the project is
        missing, has no status field, or is missing any option Clams maps to.
        """
        query = """
query($owner: String!, $number: Int!) {
  user(login: $owner) {
    projectV2(number: $number) { id title }
  }
}
"""
        body = await self._post(query, {"owner": self.owner,
                                      "number": self.project_number})
        node = (body.get("data") or {}).get("user", {}) \
            .get("projectV2") if "errors" not in body else None
        if not node:
            # A user-scoped lookup can fail because the board lives under an
            # organisation instead; try that before declaring it missing.
            org_query = """
query($owner: String!, $number: Int!) {
  organization(login: $owner) {
    projectV2(number: $number) { id title }
  }
}
"""
            body = await self._post(org_query, {"owner": self.owner,
                                             "number": self.project_number})
            if body.get("errors"):
                raise ProjectConfigError(
                    f"cannot read project #{self.project_number} for "
                    f"{self.owner!r}: {_extract_error(body)}"
                )
            node = (body.get("data") or {}).get("organization", {}) \
                .get("projectV2")
        if not node:
            raise ProjectConfigError(
                f"project #{self.project_number} not found for {self.owner!r}"
            )

        project_id = node["id"]
        title = node.get("title") or f"project-{self.project_number}"

        field_query = """
query($id: ID!) {
  node(id: $id) {
    ... on ProjectV2 {
      fields(first: 30) {
        nodes {
          ... on ProjectV2Field { id name }
          ... on ProjectV2SingleSelectField {
            id name
            options { id name }
          }
        }
      }
    }
  }
}
"""
        body = await self._post(field_query, {"id": project_id})
        if body.get("errors"):
            raise ProjectConfigError(
                f"cannot read fields of project {title!r}: {_extract_error(body)}"
            )
        data = body.get("data") or {}
        nodes = (data.get("node") or {}).get("fields", {}).get("nodes", [])
        status_field = next(
            (n for n in nodes if n.get("name") == self.status_field_name
             and "options" in n),
            None,
        )
        if status_field is None:
            names = sorted(n.get("name", "?") for n in nodes)
            raise ProjectConfigError(
                f"project {title!r} has no single-select {self.status_field_name!r} "
                f"field. Fields present: {names}"
            )

        option_ids = {o["name"]: o["id"] for o in status_field.get("options", [])}
        if not option_ids:
            raise ProjectConfigError(
                f"{self.status_field_name!r} field on {title!r} has no options"
            )

        needed = sorted(set(self.status_map.values()))
        missing = [n for n in needed if n not in option_ids]
        if missing:
            raise ProjectConfigError(
                f"project {title!r} is missing status option(s) {missing} that "
                f"Clams maps to. Available: {sorted(option_ids)}"
            )

        self._resolved = ResolvedBoard(
            project_id=project_id,
            title=title,
            status_field_id=status_field["id"],
            option_ids=option_ids,
        )
        return self._resolved

    @property
    def resolved(self) -> ResolvedBoard:
        if self._resolved is None:
            raise ProjectConfigError(
                "board not resolved -- call await board.resolve() at startup"
            )
        return self._resolved

    # -- mutations -----------------------------------------------------
    async def add_item(self, content_id: str) -> str:
        """Add a content node (issue/PR) to the board and return the item ID."""
        mutation = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectV2Id: $projectId, contentId: $contentId}) {
    item { id }
    errors { field message }
  }
}
"""
        data = await self._graphql(
            mutation,
            {"projectId": self.resolved.project_id, "contentId": content_id},
        )
        payload = data.get("addProjectV2ItemById") or {}
        if payload.get("errors"):
            err = payload["errors"][0]
            raise ProjectSyncError(
                f"addProjectV2ItemById rejected: "
                f"{err.get('field')}: {err.get('message')}"
            )
        item = payload.get("item") or {}
        if not item.get("id"):
            raise ProjectSyncError("addProjectV2ItemById returned no item id")
        return item["id"]

    async def set_status_option(self, item_id: str, status_label: str) -> None:
        """Move card ``item_id`` to the column mapped from ``status_label``."""
        option_name = self.status_map.get(status_label)
        if option_name is None:
            raise ProjectConfigError(
                f"no board column mapped for status {status_label!r}; "
                f"mapped statuses: {sorted(self.status_map)}"
            )
        option_id = self.resolved.option_id_for(option_name)
        mutation = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: String!) {
  updateProjectV2ItemFieldValue(
    input: {projectV2Id: $projectId, itemId: $itemId, fieldId: $fieldId,
            value: {singleSelectOptionId: $value}}
  ) {
    projectV2Item { id }
    errors { field message }
  }
}
"""
        data = await self._graphql(
            mutation,
            {
                "projectId": self.resolved.project_id,
                "itemId": item_id,
                "fieldId": self.resolved.status_field_id,
                "value": option_id,
            },
        )
        payload = data.get("updateProjectV2ItemFieldValue") or {}
        if payload.get("errors"):
            err = payload["errors"][0]
            raise ProjectSyncError(
                f"updateProjectV2ItemFieldValue rejected for item {item_id}: "
                f"{err.get('field')}: {err.get('message')}"
            )

    async def find_item(self, content_id: str) -> Optional[str]:
        """Return the project item ID for a content node, if it is on the board."""
        query = """
query($id: ID!) {
  node(id: $id) {
    ... on ProjectV2ItemList {
      nodes { id content { ... on Issue { id } } }
    }
  }
}
"""
        # Prefer the project's own items connection: it is paginated but stable.
        items_query = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on ProjectV2 {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id content { ... on Issue { id } } }
      }
    }
  }
}
"""
        cursor: Optional[str] = None
        while True:
            data = await self._graphql(items_query,
                                     {"id": self.resolved.project_id,
                                      "cursor": cursor})
            items = (data.get("node") or {}).get("items") or {}
            for node in items.get("nodes", []):
                if (node.get("content") or {}).get("id") == content_id:
                    return node.get("id")
            page = items.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return None
            cursor = page.get("endCursor")

    async def move(self, content_id: str, status_label: str) -> str:
        """Ensure ``content_id`` is on the board and in the right column.

        Returns the item ID. Adds the card first if it is not on the board yet,
        so a status transition never fails just because the card was created
        outside Clams.
        """
        item_id = await self.find_item(content_id)
        if item_id is None:
            item_id = await self.add_item(content_id)
        await self.set_status_option(item_id, status_label)
        return item_id


async def board_from_settings(settings, *, resolve: bool = True):
    """Build (and by default validate) a ProjectBoard from Clams settings.

    Returns ``None`` when the integration is disabled. When enabled, resolution
    runs here -- at startup -- so a missing project, a renamed status field or a
    missing status option raises ProjectConfigError before any work item is
    touched, rather than corrupting one card at a time mid-run.
    """
    from .projects import DEFAULT_STATUS_MAP, ProjectBoard

    ps = settings.project
    if not ps.enabled:
        return None
    owner = ps.effective_owner or settings.github.owner
    if not ps.number:
        raise ProjectConfigError(
            "project sync is enabled but CLAMS__PROJECT__NUMBER is not set"
        )
    status_map = ps.parsed_status_map(DEFAULT_STATUS_MAP)
    board = ProjectBoard(
        token=settings.github.token,
        owner=owner,
        project_number=ps.number,
        api_base=settings.github.api_base,
        status_field_name=ps.status_field,
        status_map=status_map,
    )
    if resolve:
        await board.resolve()
    return board
