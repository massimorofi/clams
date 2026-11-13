"""Agent registry — the addressable agents of a Clams deployment (issue #19).

Clams coordinates four *roles*. Two of them (``planner`` and ``executor``) are
Claudio agent instances reached over HTTP; the other two (``orchestrator`` and
``watchdog``) are Clams-internal daemons that are still addressable so the
operator can point the UI at them.

This module owns that registry: the built-in defaults, validation, persistence
and lookup. It deliberately has no dependency on the HTTP layer so the CLI,
the API and the tests can all share it.

Persistence
    The registry is stored as JSON at ``paths.data_dir/agents.json``. If the
    file is missing it is seeded from :func:`default_agents`, which is what
    makes "two Claudio agents on 1072 and 1073" the out-of-the-box state.

Port choice
    Issue #19 asks for ports 1072/1073 specifically so the two agents never
    collide with a stock Claudio instance (8100) or with each other.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ValidationError, field_validator

# The four Clams roles. Order is the order the UI lists them in.
AGENT_ROLES: tuple[str, ...] = ("planner", "executor", "orchestrator", "watchdog")

# Roles that are actually Claudio agent instances, i.e. reachable through the
# Claudio HTTP contract (POST {base}/chat, GET {base}/healthz).
CLAUDIO_ROLES: frozenset[str] = frozenset({"planner", "executor"})

# Claudio's own API is mounted under /api/v1, so a Clams base_url must include
# it. Used when normalising addresses for the two Claudio roles.
CLAUDIO_API_SUFFIX = "/api/v1"

# Default ports requested in issue #19.
DEFAULT_PLANNER_PORT = 1072
DEFAULT_EXECUTOR_PORT = 1073

# Default auth token for a local dev deployment. Must match the agent's
# CLAUDIO__SERVER__AUTH_TOKEN or every call comes back 401.
DEFAULT_AUTH_TOKEN = "dev-token-change-me"

DEFAULT_AGENTS_FILENAME = "agents.json"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class AgentError(ValueError):
    """Raised for any invalid registry operation (duplicate id, bad role, ...)."""


def normalise_claudio_url(base_url: str) -> str:
    """Return ``base_url`` guaranteed to end with ``/api/v1``.

    Clams appends ``/chat`` and ``/healthz`` to the configured address, so a
    missing ``/api/v1`` is a misconfiguration that only surfaces at call time.
    Normalising on write means the operator cannot get it wrong. Any deeper
    path the operator typed is preserved.
    """
    base = (base_url or "").strip().rstrip("/")
    parts = urlsplit(base)
    path = parts.path or ""
    if path == CLAUDIO_API_SUFFIX or path.endswith(CLAUDIO_API_SUFFIX):
        return base
    return urlunsplit((parts.scheme, parts.netloc,
                      f"{path}{CLAUDIO_API_SUFFIX}", "", ""))


class AgentConfig(BaseModel):
    """One agent: a role bound to a network address."""

    id: str
    role: str
    base_url: str
    auth_token: str = ""
    enabled: bool = True
    request_timeout: float = 120.0

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not _ID_RE.match(v):
            raise AgentError(
                "agent id must start with a letter or digit and contain only "
                f"letters, digits, '-' or '_', got {v!r}"
            )
        return v

    @field_validator("role")
    @classmethod
    def _valid_role(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in AGENT_ROLES:
            raise AgentError(
                f"unknown agent role {v!r}; valid roles: {', '.join(AGENT_ROLES)}"
            )
        return v

    @field_validator("base_url")
    @classmethod
    def _valid_url(cls, v: str) -> str:
        v = (v or "").strip().rstrip("/")
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise AgentError(
                f"agent base_url must be an absolute http(s) URL, got {v!r}"
            )
        return v

    @field_validator("request_timeout")
    @classmethod
    def _positive_timeout(cls, v: float) -> float:
        if v <= 0:
            raise AgentError("request_timeout must be > 0")
        return v

    def effective_base_url(self) -> str:
        """The address Clams should actually dial for this agent.

        Claudio roles get the ``/api/v1`` suffix enforced; the Clams-internal
        roles keep whatever address was configured.
        """
        if self.role in CLAUDIO_ROLES:
            return normalise_claudio_url(self.base_url)
        return self.base_url

    def public_dict(self) -> dict[str, Any]:
        """Serialisation safe to send to the UI (secret masked)."""
        return {
            "id": self.id,
            "role": self.role,
            "base_url": self.base_url,
            "effective_base_url": self.effective_base_url(),
            "enabled": self.enabled,
            "request_timeout": self.request_timeout,
            "has_auth_token": bool(self.auth_token),
        }


def coerce_agent(data: dict[str, Any]) -> AgentConfig:
    """Build an :class:`AgentConfig`, reporting failures as :class:`AgentError`.

    Pydantic wraps the ``ValueError`` raised by our field validators in a
    ``ValidationError``. Callers (the API, the CLI) should only ever have to
    catch ``AgentError``, so translate here once instead of at every call site.
    """
    try:
        return AgentConfig(**data)
    except ValidationError as exc:
        errs = exc.errors()
        if errs:
            first = errs[0]
            loc = ".".join(str(p) for p in first.get("loc", ())) or "agent"
            raise AgentError(f"{loc}: {first.get('msg', 'invalid value')}") from exc
        raise AgentError(str(exc)) from exc


# --------------------------------------------------------------- defaults ----
def default_agents(*, host: str = "127.0.0.1",
                  auth_token: str = DEFAULT_AUTH_TOKEN,
                  planner_port: int = DEFAULT_PLANNER_PORT,
                  executor_port: int = DEFAULT_EXECUTOR_PORT,
                  request_timeout: float = 120.0) -> list[AgentConfig]:
    """The out-of-the-box registry: a planner and an executor agent.

    Issue #19 asks for exactly this — two Claudio agents, planner and executor,
    on ports 1072 and 1073 rather than Claudio's stock 8100.
    """
    return [
        AgentConfig(
            id="planner",
            role="planner",
            base_url=f"http://{host}:{planner_port}{CLAUDIO_API_SUFFIX}",
            auth_token=auth_token,
            enabled=True,
            request_timeout=request_timeout,
        ),
        AgentConfig(
            id="executor",
            role="executor",
            base_url=f"http://{host}:{executor_port}{CLAUDIO_API_SUFFIX}",
            auth_token=auth_token,
            enabled=True,
            request_timeout=request_timeout,
        ),
    ]


# ------------------------------------------------------------- registry ----
class AgentRegistry:
    """A validated, persisted list of :class:`AgentConfig`.

    The registry is the single source of truth for *where* each role lives.
    Writes are atomic (temp file + ``os.replace``) so a crash mid-save cannot
    leave a half-written registry behind.
    """

    def __init__(self, path: str | Path,
                 agents: Optional[list[AgentConfig]] = None) -> None:
        self.path = Path(path)
        self._agents: list[AgentConfig] = list(agents) if agents is not None \
            else default_agents()
        self._assert_unique_ids(self._agents)

    # -- construction / persistence -------------------------------------
    @classmethod
    def load(cls, path: str | Path,
            defaults: Optional[list[AgentConfig]] = None) -> "AgentRegistry":
        """Load from ``path``; seed and persist defaults when absent.

        ``defaults`` lets the caller seed from its own configuration (the
        API passes the ``CLAMS__AGENTS__*`` settings) instead of the
        built-in constants. When omitted, :func:`default_agents` is used.
        """
        p = Path(path)
        if not p.exists():
            reg = cls(p, defaults if defaults is not None else default_agents())
            reg.save()
            return reg
        try:
            raw = json.loads(p.read_text() or "[]")
        except json.JSONDecodeError as exc:
            raise AgentError(f"{p}: invalid JSON ({exc})") from exc
        if not isinstance(raw, list):
            raise AgentError(f"{p}: expected a JSON array of agents")
        agents = [coerce_agent(item) if isinstance(item, dict)
                 else item for item in raw]
        return cls(p, agents)

    def save(self) -> None:
        """Atomically persist the registry as JSON."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [a.model_dump() for a in self._agents], indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                  prefix=".agents-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- read ----------------------------------------------------------
    def all(self) -> list[AgentConfig]:
        return list(self._agents)

    def get(self, agent_id: str) -> Optional[AgentConfig]:
        needle = (agent_id or "").strip().lower()
        for a in self._agents:
            if a.id == needle:
                return a
        return None

    def for_role(self, role: str) -> list[AgentConfig]:
        needle = (role or "").strip().lower()
        return [a for a in self._agents if a.role == needle]

    def enabled_for_role(self, role: str) -> list[AgentConfig]:
        return [a for a in self.for_role(role) if a.enabled]

    def primary_for_role(self, role: str) -> Optional[AgentConfig]:
        """First enabled agent bound to ``role`` (the one Clams will dial)."""
        enabled = self.enabled_for_role(role)
        if enabled:
            return enabled[0]
        bound = self.for_role(role)
        return bound[0] if bound else None

    def __len__(self) -> int:
        return len(self._agents)

    # -- write ---------------------------------------------------------
    def add(self, agent: AgentConfig) -> AgentConfig:
        if not isinstance(agent, AgentConfig):
            agent = coerce_agent(agent)
        if self.get(agent.id) is not None:
            raise AgentError(f"an agent with id {agent.id!r} already exists")
        self._agents.append(agent)
        self.save()
        return agent

    def update(self, agent_id: str, changes: dict[str, Any]) -> AgentConfig:
        """Patch one agent. Only supplied keys change; ``id`` is immutable."""
        existing = self.get(agent_id)
        if existing is None:
            raise AgentError(f"no agent with id {agent_id!r}")
        changes = dict(changes)
        changes.pop("id", None)
        merged = existing.model_dump()
        merged.update(changes)
        updated = coerce_agent(merged)
        self._assert_unique_ids(
            [updated if a.id == updated.id else a for a in self._agents])
        self._agents = [updated if a.id == existing.id else a
                       for a in self._agents]
        self.save()
        return updated

    def remove(self, agent_id: str) -> None:
        existing = self.get(agent_id)
        if existing is None:
            raise AgentError(f"no agent with id {agent_id!r}")
        self._agents = [a for a in self._agents if a.id != existing.id]
        self.save()

    def replace(self, agents: list[AgentConfig]) -> None:
        """Replace the whole registry (used by the config hot-apply path)."""
        coerced = [a if isinstance(a, AgentConfig) else coerce_agent(a)
                  for a in agents]
        self._assert_unique_ids(coerced)
        self._agents = coerced
        self.save()

    def to_json_value(self) -> list[dict[str, Any]]:
        """JSON-safe value for storing inside the Settings blob."""
        return [a.model_dump() for a in self._agents]

    @staticmethod
    def _assert_unique_ids(agents: list[AgentConfig]) -> None:
        seen: set[str] = set()
        for a in agents:
            if a.id in seen:
                raise AgentError(f"duplicate agent id {a.id!r}")
            seen.add(a.id)


# ------------------------------------------------------------- health ------
def probe_agent(agent: AgentConfig, *, timeout: float = 3.0) -> dict[str, Any]:
    """Probe an agent's ``/healthz`` endpoint.

    Returns a small dict the UI renders directly. Never raises: a dead agent is
    an expected state, not an exceptional one.
    """
    url = agent.effective_base_url().rstrip("/") + "/healthz"
    req = urllib.request.Request(url, method="GET")
    if agent.auth_token:
        req.add_header("Authorization", f"Bearer {agent.auth_token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
            return {"ok": True, "status": resp.status, "url": url,
                    "detail": body[:200]}
    except Exception as exc:  # noqa: BLE001 — any failure is a failed probe
        return {"ok": False, "status": None, "url": url,
                "detail": type(exc).__name__}


def parse_agents_value(value: Any) -> list[AgentConfig]:
    """Coerce a config value into a list of :class:`AgentConfig`.

    Accepts a JSON string (handy for ``CLAMS__AGENTS='[...]'``), a list of
    dicts, or an existing list of models.
    """
    if value is None:
        return default_agents()
    if isinstance(value, str):
        value = json.loads(value or "[]")
    if not isinstance(value, list):
        raise AgentError("agents must be a JSON array")
    out: list[AgentConfig] = []
    for item in value:
        if isinstance(item, AgentConfig):
            out.append(item)
        elif isinstance(item, dict):
            out.append(coerce_agent(item))
        else:
            raise AgentError(f"invalid agent entry: {item!r}")
    AgentRegistry._assert_unique_ids(out)
    return out
