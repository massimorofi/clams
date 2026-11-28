"""Typed model of everything ``clams-admin`` manages.

The field names here **mirror the real variables** the stack already reads —
``src/clams/config.py`` (``CLAMS_*``), ``.env.example``, ``.env.claudio.example``
and ``docker-compose.yml``. Nothing is invented: if a key is not read by the
runtime it does not get a field, and unknown-but-supported keys are kept in an
``extra`` mapping instead of being dropped.

Two states are distinct everywhere:

``None``
    **unset** — the key is absent from the file. On save it is *not written*.
``''``
    **explicitly empty** — the key is present as ``KEY=`` (this is meaningful:
    ``CLAMS_MODEL_SERVER_URL=`` turns the probe off, ``CLAMS_GITHUB_TOKEN=``
    means "no GitHub access"). On save it is written as ``KEY=``.

Sections
--------

:class:`ClamsSettings`
    the ``CLAMS_*`` environment (one field per key in ``.env.example``).
:class:`AgentRoleSettings`
    one Claudio agent container's env (planner or executor).
:class:`ExecutorDefaults`
    settings shared by every executor instance.
:class:`ExecutorInstance`
    one executor (id, host debug port, its own agent overrides).
:class:`ClaudioUI`
    the single Claudio web UI (host port, image, backend).
:class:`AdminConfig`
    admin-local, non-secret settings (``executor_count``, file names, ...).
:class:`Deployment`
    the whole document, which is what ``adminconfig`` reads and writes.

Stdlib-only.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping, Optional

from .secrets import SecretStr, as_secret

__all__ = [
    "ClamsSettings",
    "AgentRoleSettings",
    "ExecutorDefaults",
    "ExecutorInstance",
    "ClaudioUI",
    "AdminConfig",
    "Deployment",
    "UNSET",
    "env_key_for",
    "known_env_keys",
]

#: Sentinel for "this setting is not present in the file".
UNSET: Any = None


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------
def _kind_of(annotation: Any) -> str:
    """Coarse type kind from a (stringified) annotation."""
    text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", str(annotation))
    if "SecretStr" in text:
        return "secret"
    if "int" in text and "float" not in text:
        return "int"
    if "float" in text:
        return "float"
    if "list" in text:
        return "list"
    return "str"


def _coerce(kind: str, value: Any) -> Any:
    """Coerce a raw string coming from an env file into the field's type."""
    if value is None:
        return None
    if kind == "secret":
        return value if isinstance(value, SecretStr) else SecretStr(str(value))
    if kind == "int":
        if isinstance(value, bool):
            raise ValueError(f"expected an integer, got {value!r}")
        if isinstance(value, int):
            return value
        return int(str(value).strip())
    if kind == "float":
        if isinstance(value, (int, float)):
            return float(value)
        return float(str(value).strip())
    if kind == "list":
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        return [part.strip().strip("\"'") for part in text.split(",") if part.strip()]
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value if isinstance(value, str) else str(value)


def _render(kind: str, value: Any) -> Optional[str]:
    """Render a field value for an env file. ``None`` stays ``None`` (unset)."""
    if value is None:
        return None
    if kind == "secret":
        return value.get_secret_value() if isinstance(value, SecretStr) else str(value)
    if kind == "bool":
        return "true" if value else "false"
    if kind == "list":
        return ",".join(str(v) for v in value)
    return str(value)


def _json_value(kind: str, value: Any, *, include_secrets: bool) -> Any:
    if value is None:
        return None
    if kind == "secret":
        secret = value if isinstance(value, SecretStr) else SecretStr(str(value))
        return secret.get_secret_value() if include_secrets else None
    if kind == "list":
        return [str(v) for v in value]
    return value


def env_key_for(field_name: str, prefix: str = "CLAMS_") -> str:
    """``api_port`` -> ``CLAMS_API_PORT``."""
    return f"{prefix}{field_name.upper()}"


def known_env_keys(cls: type) -> tuple[str, ...]:
    """Every env key a section can represent (excluding ``extra``)."""
    section = cls()  # type: ignore[call-arg]
    keys = getattr(section, "env_keys", None)
    if keys is None:
        return ()
    return tuple(keys().values())


def _fields_of(obj: Any, skip: tuple[str, ...] = ("extra",)) -> tuple[dataclasses.Field, ...]:
    return tuple(f for f in dataclasses.fields(obj) if f.name not in skip)


# --------------------------------------------------------------------------
# ClamsSettings
# --------------------------------------------------------------------------
@dataclass
class ClamsSettings:
    """The ``CLAMS_*`` environment read by ``src/clams/config.py``.

    One field per key in ``.env.example``; ``None`` means the key is absent,
    ``''`` means it is present and empty. Keys the model does not know about
    (a future ``CLAMS_*`` addition) survive in :attr:`extra`.
    """

    ENV_PREFIX: ClassVar[str] = "CLAMS_"
    SECRET_FIELDS: ClassVar[frozenset] = frozenset(
        {"claudio_planner_token", "claudio_executor_token", "api_token", "github_token"}
    )

    # --- paths ---------------------------------------------------------------
    data_dir: Optional[str] = None
    db_path: Optional[str] = None
    work_root: Optional[str] = None
    config_file: Optional[str] = None
    # --- bus / redis ----------------------------------------------------------
    redis_url: Optional[str] = None
    stream_tasks: Optional[str] = None
    stream_events: Optional[str] = None
    stream_dlq: Optional[str] = None
    group_executors: Optional[str] = None
    group_planner: Optional[str] = None
    msg_max_bytes: Optional[int] = None
    dlq_max_deliveries: Optional[int] = None
    # --- claudio agents --------------------------------------------------------
    claudio_planner_url: Optional[str] = None
    claudio_planner_token: Optional[SecretStr] = None
    claudio_executor_url: Optional[str] = None
    claudio_executor_token: Optional[SecretStr] = None
    claudio_workspace_root: Optional[str] = None
    worktree_uid: Optional[int] = None
    worktree_gid: Optional[int] = None
    # --- github -----------------------------------------------------------------
    github_token: Optional[SecretStr] = None
    github_api_base: Optional[str] = None
    github_graphql_base: Optional[str] = None
    # --- api ---------------------------------------------------------------------
    api_host: Optional[str] = None
    api_port: Optional[int] = None
    api_token: Optional[SecretStr] = None
    spa_dir: Optional[str] = None
    # --- executor -----------------------------------------------------------------
    executor_id: Optional[str] = None
    executor_capacity: Optional[int] = None
    heartbeat_interval_s: Optional[int] = None
    executor_alive_interval_s: Optional[int] = None
    keep_failed_worktree_hours: Optional[int] = None
    # --- planner loop ---------------------------------------------------------------
    planner_cycle_s: Optional[float] = None
    intake_every_n_cycles: Optional[int] = None
    planner_heartbeat_max_age_s: Optional[int] = None
    events_batch: Optional[int] = None
    events_block_ms: Optional[int] = None
    stall_alert_minutes: Optional[int] = None
    stall_escalate_minutes: Optional[int] = None
    # --- limits (bounded everything) -------------------------------------------------
    max_attempts: Optional[int] = None
    max_turns: Optional[int] = None
    max_replans: Optional[int] = None
    max_plan_tasks: Optional[int] = None
    planning_llm_budget_per_cycle: Optional[int] = None
    lease_dispatch_s: Optional[int] = None
    lease_running_extend_s: Optional[int] = None
    question_expiry_h: Optional[int] = None
    outbox_max_attempts: Optional[int] = None
    outbox_backoff_base_s: Optional[int] = None
    retention_events_days: Optional[int] = None
    retention_processed_days: Optional[int] = None
    retention_sent_outbox_days: Optional[int] = None
    # --- watchdog ----------------------------------------------------------------------
    watchdog_probe_interval_s: Optional[int] = None
    watchdog_healthy_probes_to_resume: Optional[int] = None
    oldest_ready_alert_s: Optional[int] = None
    min_free_disk_mb: Optional[int] = None
    model_server_url: Optional[str] = None
    max_db_size_mb: Optional[int] = None
    evidence_tail_limit: Optional[int] = None
    orphan_silence_s: Optional[int] = None
    # --- misc ----------------------------------------------------------------------------
    role: Optional[str] = None

    #: keys seen in the file that this model does not (yet) know about.
    extra: dict[str, Any] = field(default_factory=dict)

    SECTION: ClassVar[str] = "clams"

    # -- env mapping ---------------------------------------------------------
    def env_keys(self) -> dict[str, str]:
        """field name -> env key, in declaration order."""
        return {f.name: env_key_for(f.name, self.ENV_PREFIX) for f in _fields_of(self)}

    def to_env(self) -> dict[str, str]:
        """Render for an env file. Unset (``None``) keys are omitted."""
        out: dict[str, str] = {}
        for f in _fields_of(self):
            rendered = _render(_kind_of(f.type), getattr(self, f.name))
            if rendered is not None:
                out[env_key_for(f.name, self.ENV_PREFIX)] = rendered
        for key, value in self.extra.items():
            rendered = _render(_kind_of(type(value).__name__), value)
            if rendered is not None:
                out[str(key)] = rendered
        return out

    @classmethod
    def from_env(cls, mapping: Mapping[str, Any]) -> "ClamsSettings":
        """Build from a raw ``{ENV_KEY: value}`` mapping (unknown keys -> extra)."""
        by_key = {env_key_for(f.name, cls.ENV_PREFIX): f for f in dataclasses.fields(cls) if f.name != "extra"}
        kwargs: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for key, value in mapping.items():
            f = by_key.get(key)
            if f is None:
                extra[str(key)] = value
                continue
            try:
                kwargs[f.name] = _coerce(_kind_of(f.type), value)
            except (TypeError, ValueError):
                extra[str(key)] = value
        return cls(**kwargs, extra=extra)

    # -- JSON mapping --------------------------------------------------------
    def to_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in _fields_of(self):
            value = _json_value(_kind_of(f.type), getattr(self, f.name), include_secrets=include_secrets)
            if value is not None:
                out[f.name] = value
        if self.extra:
            out["extra"] = dict(self.extra)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ClamsSettings":
        by_name = {f.name: f for f in dataclasses.fields(cls) if f.name != "extra"}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key == "extra":
                continue
            f = by_name.get(key)
            if f is None:
                continue
            kwargs[f.name] = _coerce(_kind_of(f.type), value)
        return cls(**kwargs, extra=dict(data.get("extra") or {}))

    # -- secrets -------------------------------------------------------------
    def secret_values(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for f in _fields_of(self):
            if _kind_of(f.type) != "secret":
                continue
            value = getattr(self, f.name)
            if value is not None:
                out[env_key_for(f.name, self.ENV_PREFIX)] = value.get_secret_value()
        return out


# --------------------------------------------------------------------------
# AgentRoleSettings
# --------------------------------------------------------------------------
#: field -> env key for one Claudio agent container (see .env.claudio.example).
AGENT_ENV_KEYS: dict[str, str] = {
    "auth_token": "CLAUDIO__SERVER__AUTH_TOKEN",
    "approval_mode": "CLAUDIO__AGENT__APPROVAL_MODE",
    "workspace_allowed_roots": "CLAUDIO__AGENT__WORKSPACE_ALLOWED_ROOTS",
    "llm_base_url": "CLAUDIO__LLM__BASE_URL",
    "llm_api_key": "CLAUDIO__LLM__API_KEY",
    "llm_model": "CLAUDIO__LLM__MODEL",
    "github_token": "GITHUB_TOKEN",
    "gh_token": "GH_TOKEN",
    "git_author_name": "GIT_AUTHOR_NAME",
    "git_author_email": "GIT_AUTHOR_EMAIL",
    "git_committer_name": "GIT_COMMITTER_NAME",
    "git_committer_email": "GIT_COMMITTER_EMAIL",
    "git_config_count": "GIT_CONFIG_COUNT",
    "git_config_key_0": "GIT_CONFIG_KEY_0",
    "git_config_value_0": "GIT_CONFIG_VALUE_0",
}


@dataclass
class AgentRoleSettings:
    """One Claudio agent's own environment (planner or executor container).

    ``auth_token`` mirrors the agent's ``server.auth_token``; Clams presents it
    back as ``Authorization: Bearer <token>`` (``clams.claudio_client``).
    ``approval_mode`` is ``off | on_request | always`` — the executor must run
    with ``off`` because nobody is there to approve.
    """

    SECTION: ClassVar[str] = "agent"

    role: str = "planner"
    auth_token: Optional[SecretStr] = None
    approval_mode: Optional[str] = None
    workspace_allowed_roots: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_api_key: Optional[SecretStr] = None
    llm_model: Optional[str] = None
    github_token: Optional[SecretStr] = None
    gh_token: Optional[SecretStr] = None
    git_author_name: Optional[str] = None
    git_author_email: Optional[str] = None
    git_committer_name: Optional[str] = None
    git_committer_email: Optional[str] = None
    git_config_count: Optional[int] = None
    git_config_key_0: Optional[str] = None
    git_config_value_0: Optional[str] = None

    extra: dict[str, Any] = field(default_factory=dict)

    def env_keys(self) -> dict[str, str]:
        return {name: key for name, key in AGENT_ENV_KEYS.items()}

    def to_env(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for f in _fields_of(self):
            if f.name == "role":
                continue
            key = AGENT_ENV_KEYS.get(f.name)
            if key is None:
                continue
            rendered = _render(_kind_of(f.type), getattr(self, f.name))
            if rendered is not None:
                out[key] = rendered
        for key, value in self.extra.items():
            if value is not None:
                out[str(key)] = str(value)
        return out

    @classmethod
    def from_env(cls, mapping: Mapping[str, Any], *, role: str = "planner") -> "AgentRoleSettings":
        by_key = {key: name for name, key in AGENT_ENV_KEYS.items()}
        fields = {f.name: f for f in dataclasses.fields(cls) if f.name != "extra"}
        kwargs: dict[str, Any] = {"role": role}
        extra: dict[str, Any] = {}
        for key, value in mapping.items():
            name = by_key.get(str(key))
            if name is None:
                extra[str(key)] = value
                continue
            try:
                kwargs[name] = _coerce(_kind_of(fields[name].type), value)
            except (TypeError, ValueError):
                extra[str(key)] = value
        return cls(**kwargs, extra=extra)

    def to_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role}
        for f in _fields_of(self):
            if f.name == "role":
                continue
            value = _json_value(_kind_of(f.type), getattr(self, f.name), include_secrets=include_secrets)
            if value is not None:
                out[f.name] = value
        if self.extra:
            out["extra"] = dict(self.extra)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentRoleSettings":
        fields = {f.name: f for f in dataclasses.fields(cls) if f.name != "extra"}
        kwargs: dict[str, Any] = {"role": str(data.get("role") or "planner")}
        for key, value in data.items():
            if key in {"role", "extra"} or key not in fields:
                continue
            kwargs[key] = _coerce(_kind_of(fields[key].type), value)
        return cls(**kwargs, extra=dict(data.get("extra") or {}))

    def secret_values(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for f in _fields_of(self):
            if _kind_of(f.type) != "secret":
                continue
            value = getattr(self, f.name)
            if value is not None:
                out[AGENT_ENV_KEYS.get(f.name, f.name)] = value.get_secret_value()
        return out


# --------------------------------------------------------------------------
# executors
# --------------------------------------------------------------------------
@dataclass
class ExecutorDefaults:
    """Settings shared by every executor instance.

    The ``CLAMS_EXECUTOR_*`` / ``CLAMS_HEARTBEAT_*`` keys live here because the
    admin tool fans one value out to N executor containers; ``approval_mode``
    is the executor agent's own mode and must stay ``off``.
    """

    SECTION: ClassVar[str] = "executor_defaults"
    ENV_PREFIX: ClassVar[str] = "CLAMS_"
    ENV_OVERRIDES: ClassVar[dict[str, str]] = {"approval_mode": "CLAUDIO__AGENT__APPROVAL_MODE"}

    executor_id: Optional[str] = None
    executor_capacity: Optional[int] = None
    heartbeat_interval_s: Optional[int] = None
    executor_alive_interval_s: Optional[int] = None
    keep_failed_worktree_hours: Optional[int] = None
    max_turns: Optional[int] = None
    approval_mode: Optional[str] = None
    agent: AgentRoleSettings = field(default_factory=lambda: AgentRoleSettings(role="executor"))
    extra: dict[str, Any] = field(default_factory=dict)

    def env_keys(self) -> dict[str, str]:
        return {
            f.name: self.ENV_OVERRIDES.get(f.name, env_key_for(f.name, self.ENV_PREFIX))
            for f in _fields_of(self)
            if f.name != "agent"
        }

    def to_env(self) -> dict[str, str]:
        out: dict[str, str] = {}
        keys = self.env_keys()
        for f in _fields_of(self):
            if f.name in {"agent", "extra"}:
                continue
            rendered = _render(_kind_of(f.type), getattr(self, f.name))
            if rendered is not None:
                out[keys[f.name]] = rendered
        for key, value in self.extra.items():
            if value is not None:
                out[str(key)] = str(value)
        return out

    @classmethod
    def from_env(cls, mapping: Mapping[str, Any]) -> "ExecutorDefaults":
        keys = cls().env_keys()
        by_key = {key: name for name, key in keys.items()}
        fields = {f.name: f for f in dataclasses.fields(cls)}
        kwargs: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for key, value in mapping.items():
            name = by_key.get(str(key))
            if name is None:
                extra[str(key)] = value
                continue
            try:
                kwargs[name] = _coerce(_kind_of(fields[name].type), value)
            except (TypeError, ValueError):
                extra[str(key)] = value
        return cls(**kwargs, extra=extra)

    def to_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in _fields_of(self):
            if f.name == "agent":
                out["agent"] = self.agent.to_dict(include_secrets=include_secrets)
                continue
            value = _json_value(_kind_of(f.type), getattr(self, f.name), include_secrets=include_secrets)
            if value is not None:
                out[f.name] = value
        if self.extra:
            out["extra"] = dict(self.extra)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutorDefaults":
        fields = {f.name: f for f in dataclasses.fields(cls) if f.name not in {"agent", "extra"}}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key in {"extra", "agent"}:
                continue
            if key in fields:
                kwargs[key] = _coerce(_kind_of(fields[key].type), value)
        agent = AgentRoleSettings.from_dict(data.get("agent") or {"role": "executor"})
        return cls(**kwargs, agent=agent, extra=dict(data.get("extra") or {}))

    def secret_values(self) -> dict[str, str]:
        return self.agent.secret_values()


@dataclass
class ExecutorInstance:
    """One executor: its id, the host port its agent is reachable on, overrides."""

    SECTION: ClassVar[str] = "executors"
    ENV_PREFIX: ClassVar[str] = "CLAMS_"

    executor_id: Optional[str] = None
    debug_port: Optional[int] = None
    capacity: Optional[int] = None
    agent: AgentRoleSettings = field(default_factory=lambda: AgentRoleSettings(role="executor"))
    extra: dict[str, Any] = field(default_factory=dict)

    def env_keys(self) -> dict[str, str]:
        return {
            "executor_id": "CLAMS_EXECUTOR_ID",
            "capacity": "CLAMS_EXECUTOR_CAPACITY",
        }

    #: the env-facing fields and their coercion kind
    ENV_KINDS: ClassVar[dict[str, str]] = {"executor_id": "str", "capacity": "int"}

    def to_env(self) -> dict[str, str]:
        keys = self.env_keys()
        out: dict[str, str] = {}
        for name, key in keys.items():
            rendered = _render(self.ENV_KINDS[name], getattr(self, name))
            if rendered is not None:
                out[key] = rendered
        for key, value in self.extra.items():
            if value is not None:
                out[str(key)] = str(value)
        return out

    @classmethod
    def from_env(cls, mapping: Mapping[str, Any]) -> "ExecutorInstance":
        by_key = {key: name for name, key in cls().env_keys().items()}
        kwargs: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for key, value in mapping.items():
            name = by_key.get(str(key))
            if name is None:
                extra[str(key)] = value
                continue
            kwargs[name] = _coerce(cls.ENV_KINDS[name], value)
        return cls(**kwargs, extra=extra)

    def to_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in ("executor_id", "debug_port", "capacity"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        out["agent"] = self.agent.to_dict(include_secrets=include_secrets)
        if self.extra:
            out["extra"] = dict(self.extra)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutorInstance":
        return cls(
            executor_id=data.get("executor_id"),
            debug_port=_coerce("int", data["debug_port"]) if data.get("debug_port") is not None else None,
            capacity=_coerce("int", data["capacity"]) if data.get("capacity") is not None else None,
            agent=AgentRoleSettings.from_dict(data.get("agent") or {"role": "executor"}),
            extra=dict(data.get("extra") or {}),
        )

    def secret_values(self) -> dict[str, str]:
        return self.agent.secret_values()


# --------------------------------------------------------------------------
# ClaudioUI
# --------------------------------------------------------------------------
@dataclass
class ClaudioUI:
    """The single Claudio web UI (compose service ``claudio-ui``).

    Its nginx proxies same-origin ``/api`` to the planner agent; to drive the
    executor the operator points the UI at ``http://localhost:<executor port>``
    (direct mode). The UI token is the selected agent's ``server.auth_token``.
    """

    SECTION: ClassVar[str] = "ui"
    COMPOSE_KEYS: ClassVar[dict[str, str]] = {
        "port": "ports",
        "image": "image",
    }

    port: Optional[int] = None
    """Host port published by compose (default 1076)."""
    container_port: Optional[int] = None
    """Port the UI listens on inside its container (default 8080)."""
    image: Optional[str] = None
    """``CLAUDIO_WEB_IMAGE`` in compose (default ``claudio/web:latest``)."""
    backend_url: Optional[str] = None
    """Direct-mode backend URL, e.g. ``http://localhost:1073``. ``''`` = default (planner)."""
    token: Optional[SecretStr] = None
    """The selected agent's ``server.auth_token``."""

    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in _fields_of(self):
            value = _json_value(_kind_of(f.type), getattr(self, f.name), include_secrets=include_secrets)
            if value is not None:
                out[f.name] = value
        if self.extra:
            out["extra"] = dict(self.extra)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ClaudioUI":
        fields = {f.name: f for f in dataclasses.fields(cls) if f.name != "extra"}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key == "extra" or key not in fields:
                continue
            kwargs[key] = _coerce(_kind_of(fields[key].type), value)
        return cls(**kwargs, extra=dict(data.get("extra") or {}))

    def secret_values(self) -> dict[str, str]:
        return {"ui.token": self.token.get_secret_value()} if self.token is not None else {}


# --------------------------------------------------------------------------
# AdminConfig
# --------------------------------------------------------------------------
@dataclass
class AdminConfig:
    """Admin-local, **non-secret** settings.

    This is what lives in ``<root>/.clams-admin/config.json``: how many
    executors to render, which env files to edit, when the config was last
    applied. Tokens never go in here — they stay in the env files and are
    masked by :func:`clams_admin.secrets.redact` in any summary.
    """

    SECTION: ClassVar[str] = "admin"

    schema_version: int = 1
    name: Optional[str] = None
    root: Optional[str] = None
    executor_count: Optional[int] = None
    env_file: Optional[str] = ".env"
    planner_env_file: Optional[str] = ".env.claudio-planner"
    executor_env_file: Optional[str] = ".env.claudio-executor"
    log_level: Optional[str] = None
    enabled_profiles: Optional[list[str]] = None
    last_applied_at: Optional[str] = None

    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in _fields_of(self):
            value = getattr(self, f.name)
            if value is None:
                continue
            if _kind_of(f.type) == "list":
                value = _coerce("list", value)
            out[f.name] = value
        if self.extra:
            out["extra"] = dict(self.extra)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdminConfig":
        fields = {f.name: f for f in dataclasses.fields(cls) if f.name != "extra"}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key == "extra" or key not in fields:
                continue
            kind = _kind_of(fields[key].type)
            kwargs[key] = _coerce(kind, value)
        return cls(**kwargs, extra=dict(data.get("extra") or {}))

    def secret_values(self) -> dict[str, str]:
        return {}


# --------------------------------------------------------------------------
# Deployment
# --------------------------------------------------------------------------
@dataclass
class Deployment:
    """The whole deployment: Clams env + both agent roles + UI + admin state.

    ``api_port`` and ``planner_port`` are the *host* ports compose publishes
    (1075 and 1072 in ``docker-compose.yml``); ``clams.api_port`` is the port
    the API binds to *inside* its container (8200). They are different
    settings and both are modelled.
    """

    schema_version: int = 1
    name: Optional[str] = None
    api_port: Optional[int] = None
    planner_port: Optional[int] = None

    clams: ClamsSettings = field(default_factory=ClamsSettings)
    planner: AgentRoleSettings = field(default_factory=lambda: AgentRoleSettings(role="planner"))
    executor_defaults: ExecutorDefaults = field(default_factory=ExecutorDefaults)
    executors: list[ExecutorInstance] = field(default_factory=list)
    ui: ClaudioUI = field(default_factory=ClaudioUI)
    admin: AdminConfig = field(default_factory=AdminConfig)

    #: unknown top-level keys from the saved document, preserved on save.
    extra: dict[str, Any] = field(default_factory=dict)

    # -- serialisation -------------------------------------------------------
    def to_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "clams": self.clams.to_dict(include_secrets=include_secrets),
            "planner": self.planner.to_dict(include_secrets=include_secrets),
            "executor_defaults": self.executor_defaults.to_dict(include_secrets=include_secrets),
            "executors": [e.to_dict(include_secrets=include_secrets) for e in self.executors],
            "ui": self.ui.to_dict(include_secrets=include_secrets),
            "admin": self.admin.to_dict(include_secrets=include_secrets),
        }
        for name in ("name", "api_port", "planner_port"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.extra:
            out["extra"] = dict(self.extra)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Deployment":
        known = {"schema_version", "name", "api_port", "planner_port", "clams", "planner",
                 "executor_defaults", "executors", "ui", "admin", "extra"}
        extra = dict(data.get("extra") or {})
        extra.update({k: v for k, v in data.items() if k not in known})
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            name=data.get("name"),
            api_port=_coerce("int", data["api_port"]) if data.get("api_port") is not None else None,
            planner_port=_coerce("int", data["planner_port"]) if data.get("planner_port") is not None else None,
            clams=ClamsSettings.from_dict(data.get("clams") or {}),
            planner=AgentRoleSettings.from_dict(data.get("planner") or {"role": "planner"}),
            executor_defaults=ExecutorDefaults.from_dict(data.get("executor_defaults") or {}),
            executors=[ExecutorInstance.from_dict(e) for e in (data.get("executors") or [])],
            ui=ClaudioUI.from_dict(data.get("ui") or {}),
            admin=AdminConfig.from_dict(data.get("admin") or {}),
            extra=extra,
        )

    # -- env rendering -------------------------------------------------------
    def clams_env(self) -> dict[str, str]:
        """The ``.env`` body this deployment implies."""
        return self.clams.to_env()

    def planner_env(self) -> dict[str, str]:
        return self.planner.to_env()

    def executor_env(self, index: int = 0) -> dict[str, str]:
        """Executor env = defaults overlaid by that instance's own overrides."""
        out = self.executor_defaults.to_env()
        out.update(self.executor_defaults.agent.to_env())
        if 0 <= index < len(self.executors):
            out.update(self.executors[index].to_env())
            out.update(self.executors[index].agent.to_env())
        return out

    # -- secrets -------------------------------------------------------------
    def secret_values(self) -> dict[str, str]:
        """Every plaintext secret in the deployment, keyed for reporting.

        Only used to feed :func:`clams_admin.secrets.redact`; never serialised
        unless ``include_secrets=True`` is passed explicitly.
        """
        out: dict[str, str] = {}
        out.update({f"clams.{k}": v for k, v in self.clams.secret_values().items()})
        out.update({f"planner.{k}": v for k, v in self.planner.secret_values().items()})
        out.update({f"executor_defaults.{k}": v for k, v in self.executor_defaults.secret_values().items()})
        for position, executor in enumerate(self.executors):
            label = executor.executor_id or f"executor-{position + 1}"
            out.update({f"executors.{label}.{k}": v for k, v in executor.secret_values().items()})
        out.update({f"ui.{k}": v for k, v in self.ui.secret_values().items()})
        return {k: v for k, v in out.items() if v}

    def summary(self) -> str:
        """A printable, already-redacted overview (safe to log)."""
        from .secrets import redact

        lines = [
            f"deployment: {self.name or 'clams'}",
            f"  api: {self.clams.api_host or '127.0.0.1'}:{self.clams.api_port or '-'} "
            f"(host port {self.api_port or '-'})",
            f"  redis: {self.clams.redis_url or '-'}",
            f"  planner agent: {self.clams.claudio_planner_url or '-'} (host port {self.planner_port or '-'})",
            f"  executors: {len(self.executors)}",
            f"  ui: port {self.ui.port or '-'} image {self.ui.image or '-'}",
        ]
        text = "\n".join(lines)
        return redact(text, list(self.secret_values().values()))
