"""Runtime configuration for Clams (spec §2.2 HW-4, §5, §7.1).

All settings come from environment variables with the ``CLAMS__`` prefix, optionally
loaded from a ``.env`` file. Defaults encode the hard safety limits from the
specification; they are enforced in code, not merely documented.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Hostnames that always resolve to "this machine". Inside a container that means
# the container itself, never the host -- so a loopback URL is a bug there.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", "[::1]"})

# Host alias Docker's built-in DNS resolves to the host's gateway IP when paired
# with `extra_hosts: ["host.docker.internal:host-gateway"]`.
CONTAINER_HOST_ALIAS = "host.docker.internal"


def detect_in_container() -> bool:
    """Best-effort "am I inside a container?" probe.

    Checks the two markers that actually exist in practice: Docker's
    ``/.dockerenv`` sentinel and a cgroup that names a container runtime.
    Never raises -- an unreadable /proc just means "no evidence".
    """
    if os.path.exists("/.dockerenv"):
        return True
    if os.path.exists("/run/.containerenv"):  # podman
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
    except OSError:
        return False
    return any(tok in cgroup for tok in ("docker", "containerd", "kubepods", "lxc"))


@lru_cache(maxsize=1)
def in_container() -> bool:
    """Cached container detection (the answer cannot change mid-process)."""
    return detect_in_container()


def _is_loopback_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").strip("[]")
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS


def resolve_llm_base_url(base_url: str, *, inside_container: bool | None = None,
                        container_base_url: str | None = None
                        ) -> tuple[str, bool]:
    """Return ``(effective_url, rewrote)`` for an LLM endpoint.

    A loopback base URL is correct when Clams runs natively and wrong inside a
    container, where ``127.0.0.1`` is the container itself and the engine runs
    on the host. When both conditions hold -- containerised *and* loopback --
    the host part is swapped for ``host.docker.internal`` so the probe can
    actually reach the engine.

    An explicitly non-loopback URL is always used verbatim.
    """
    if inside_container is None:
        inside_container = in_container()
    if not inside_container or not _is_loopback_url(base_url):
        return base_url, False
    target = container_base_url or f"http://{CONTAINER_HOST_ALIAS}:8731"
    return target, True


class GitHubSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLAMS__GITHUB__", extra="ignore")

    owner: str = "massimorofi"
    repo: str = "claudio"
    token: str = ""
    api_base: str = "https://api.github.com"

    @field_validator("token")
    @classmethod
    def _strip_token(cls, v: str) -> str:
        return v.strip()

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLAMS__REDIS__", extra="ignore")

    url: str = "redis://127.0.0.1:6379/0"
    namespace: str = "clams"


class ClaudioSettings(BaseSettings):
    """A single Claudio agent instance (Planner or Executor)."""

    model_config = SettingsConfigDict(env_prefix="CLAMS__CLAUDIO__", extra="ignore")

    base_url: str = "http://127.0.0.1:8100"
    auth_token: str = ""
    request_timeout: float = 120.0


class PlannerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLAMS__PLANNER__", extra="ignore")

    base_url: str = "http://127.0.0.1:8100"
    auth_token: str = ""
    request_timeout: float = 120.0
    host: str = "0.0.0.0"
    port: int = 8080


class ExecutorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLAMS__EXECUTOR__", extra="ignore")

    base_url: str = "http://127.0.0.1:8101"
    auth_token: str = ""
    request_timeout: float = 120.0
    executor_id: str = "executor-1"
    lease_seconds: int = 900
    # How often the executor refreshes its heartbeat file. Must stay comfortably
    # below watchdog.heartbeat_max_age or the watchdog flags a live executor.
    heartbeat_interval: float = 20.0
    poll_interval: float = 1.0


class LlmSettings(BaseSettings):
    """Direct OpenAI-compatible inference endpoint (halogen / GUFO / llama.cpp)."""

    model_config = SettingsConfigDict(env_prefix="CLAMS__LLM__", extra="ignore")

    base_url: str = "http://127.0.0.1:8731"
    # Where the engine lives when Clams runs inside a container: the loopback
    # default above points at the container itself there, so it is rewritten to
    # this value (see resolve_llm_base_url).
    container_base_url: str = f"http://{CONTAINER_HOST_ALIAS}:8731"
    api_key: str = ""
    model: str = ""
    timeout: float = 300.0

    @field_validator("base_url", "container_base_url")
    @classmethod
    def _require_url(cls, v: str) -> str:
        v = (v or "").strip()
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"llm base_url must be an absolute http(s) URL, got {v!r}")
        return v

    @property
    def resolved_base_url(self) -> str:
        """The URL to actually dial from this process."""
        url, _ = resolve_llm_base_url(
            self.base_url, container_base_url=self.container_base_url
        )
        return url


class StrixperSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLAMS__STRIXPER__", extra="ignore")

    base_url: str = "http://127.0.0.1:8000"
    active_path: str = "/api/v1/runner/active"
    restart_path: str = "/api/v1/runner/restart"
    request_timeout: float = 15.0


class Limits(BaseSettings):
    """HW-4 non-gated local execution safety limits."""

    model_config = SettingsConfigDict(env_prefix="CLAMS__LIMITS__", extra="ignore")

    max_attempts: int = 3
    max_turns: int = 40
    max_wall_seconds: int = 1800
    max_replans: int = 3
    # HW-1 / HW-2 compute resource ceilings
    max_compute_queues: int = 8
    max_concurrent_engines: int = 2
    queues_per_engine: int = 3

    @field_validator("max_attempts", "max_turns", "max_wall_seconds", "max_replans")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("limit must be >= 1")
        return v

    @property
    def queues_needed(self) -> int:
        return self.max_concurrent_engines * self.queues_per_engine

    def validate_against_device(self) -> None:
        """HW-1: engines * queues must not exceed the device queue ceiling."""
        if self.queues_needed > self.max_compute_queues:
            raise ValueError(
                f"HW-1 violated: {self.max_concurrent_engines} engines x "
                f"{self.queues_per_engine} queues = {self.queues_needed} > "
                f"device ceiling {self.max_compute_queues}"
            )


class PathsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLAMS__PATHS__", extra="ignore")

    data_dir: str = "./data"
    db_path: str = "./data/clams.db"
    work_root: str = "./work"
    repo_path: str = "."
    heartbeat_dir: str = "./data/heartbeats"

    @field_validator("db_path", "work_root", "data_dir")
    @classmethod
    def _abs(cls, v: str) -> str:
        return str(Path(v).expanduser().resolve())


class WatchdogSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLAMS__WATCHDOG__", extra="ignore")

    interval_seconds: float = 15.0
    heartbeat_max_age: float = 60.0
    disk_min_free_bytes: int = 2 * 1024 * 1024 * 1024
    queue_max_pending_age: float = 15 * 60.0
    llm_probe_timeout: float = 5.0
    llm_failures_critical: int = 3
    github_failures_critical: int = 3
    max_recovery_attempts: int = 3


class Settings(BaseSettings):
    """Top-level Clams settings."""

    model_config = SettingsConfigDict(
        env_prefix="CLAMS__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_nested_delimiter="__",
    )

    github: GitHubSettings = Field(default_factory=GitHubSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    planner: PlannerSettings = Field(default_factory=PlannerSettings)
    executor: ExecutorSettings = Field(default_factory=ExecutorSettings)
    strixper: StrixperSettings = Field(default_factory=StrixperSettings)
    limits: Limits = Field(default_factory=Limits)
    paths: PathsSettings = Field(default_factory=PathsSettings)
    watchdog: WatchdogSettings = Field(default_factory=WatchdogSettings)
    poll_interval: float = 20.0
    intake_label: str = "status:Ready"
    inprogress_label: str = "status:In Progress"
    review_label: str = "status:Review"
    blocked_label: str = "status:Blocked"
    done_label: str = "status:Done"
    project_label: str = "clams"

    @classmethod
    def load(cls, **overrides: Any) -> "Settings":
        s = cls(**overrides)
        s.limits.validate_against_device()
        return s


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.load()
