"""Runtime configuration for Clams (spec §2.2 HW-4, §5, §7.1).

All settings come from environment variables with the ``CLAMS__`` prefix, optionally
loaded from a ``.env`` file. Defaults encode the hard safety limits from the
specification; they are enforced in code, not merely documented.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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


class LlmSettings(BaseSettings):
    """Direct OpenAI-compatible inference endpoint (halogen / GUFO / llama.cpp)."""

    model_config = SettingsConfigDict(env_prefix="CLAMS__LLM__", extra="ignore")

    base_url: str = "http://127.0.0.1:8731"
    api_key: str = ""
    model: str = ""
    timeout: float = 300.0


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
