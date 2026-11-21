"""Clams v2 configuration — pydantic Settings with boot guards.

Every field maps to an env var ``CLAMS_<FIELD_UPPER>``. ``.env.example`` must
list exactly these keys with the same defaults (enforced by
``tests/unit/test_env_parity.py``).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Role = Literal["planner", "executor", "api"]


class ConfigError(Exception):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLAMS_", env_file=".env", env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- paths ---------------------------------------------------------------
    data_dir: str = "./data"
    db_path: str = "./data/clams.db"
    work_root: str = "./work"
    config_file: str = "./config/clams.yaml"

    # --- bus / redis -----------------------------------------------------------
    redis_url: str = "redis://127.0.0.1:6379/0"
    stream_tasks: str = "clams:tasks"
    stream_events: str = "clams:events"
    stream_dlq: str = "clams:dlq"
    group_executors: str = "executors"
    group_planner: str = "planner"
    msg_max_bytes: int = 16384          # payload <= 16 KB enforced
    dlq_max_deliveries: int = 5

    # --- claudio agents ---------------------------------------------------------
    claudio_planner_url: str = "http://127.0.0.1:1072"
    claudio_planner_token: str = ""
    claudio_executor_url: str = "http://127.0.0.1:1073"
    claudio_executor_token: str = ""
    claudio_timeout_s: float = 120.0
    # Root inside which Claudio-E may bind workspaces (worktrees live under it).
    claudio_workspace_root: str = "./work"
    # Owner of provisioned worktrees: the runner (root in Docker) hands files
    # to the unprivileged Claudio-E uid so the agent can edit and commit.
    # Set empty/0 to disable the chown (e.g. native dev mode).
    worktree_uid: int = 1000
    worktree_gid: int = 1000

    # --- github -------------------------------------------------------------------
    github_token: str = ""
    github_api_base: str = "https://api.github.com"
    github_graphql_base: str = "https://api.github.com/graphql"

    # --- api -----------------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8200
    api_token: str = ""
    spa_dir: str = "./frontend/dist"

    # --- executor -------------------------------------------------------------------
    executor_id: str = "executor-1"
    executor_capacity: int = 1
    heartbeat_interval_s: int = 60
    executor_alive_interval_s: int = 30
    keep_failed_worktree_hours: int = 24

    # --- planner loop -----------------------------------------------------------------
    planner_cycle_s: float = 5.0
    intake_every_n_cycles: int = 12
    planner_heartbeat_max_age_s: int = 60
    events_batch: int = 100
    events_block_ms: int = 1000

    # --- limits (bounded everything) ----------------------------------------------------
    max_attempts: int = 3
    max_turns: int = 40
    max_wall_s: int = 1800
    max_replans: int = 3
    max_plan_tasks: int = 7
    planning_llm_budget_per_cycle: int = 3
    lease_dispatch_s: int = 120
    lease_running_extend_s: int = 180
    question_expiry_h: int = 24
    outbox_max_attempts: int = 5
    outbox_backoff_base_s: int = 2
    retention_events_days: int = 14
    retention_processed_days: int = 7
    retention_sent_outbox_days: int = 14

    # --- watchdog ----------------------------------------------------------------------
    watchdog_probe_interval_s: int = 30
    watchdog_healthy_probes_to_resume: int = 3
    oldest_ready_alert_s: int = 300
    min_free_disk_mb: int = 500

    # --- misc ----------------------------------------------------------------------------
    role: str = "planner"

    @field_validator("role", mode="before")
    @classmethod
    def _norm_role(cls, v: object) -> str:
        v = str(v).lower()
        if v not in ("planner", "executor", "api"):
            raise ValueError(f"role must be planner|executor|api, got {v!r}")
        return v

    # ------------------------------------------------------------------ guards
    def boot_guard(self, role: Optional[str] = None) -> None:
        """Fail fast on impossible/unsafe configuration for this role."""
        role = role or self.role
        if role == "api" and not self.api_token:
            raise ConfigError(
                "CLAMS_API_TOKEN is required for the api role (no unauthenticated "
                "control plane). Set it in .env."
            )
        if role == "planner":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        if self.max_attempts < 1:
            raise ConfigError("max_attempts must be >= 1")
        if not (1 <= self.max_plan_tasks <= 7):
            raise ConfigError("max_plan_tasks must be between 1 and 7")
        if self.lease_dispatch_s < 10:
            raise ConfigError("lease_dispatch_s must be >= 10")
        if self.dlq_max_deliveries < 1:
            raise ConfigError("dlq_max_deliveries must be >= 1")

    def db_dir(self) -> Path:
        return Path(self.db_path).parent

    def load_projects_yaml(self) -> dict:
        """Read config/clams.yaml (projects, limits, agents). Missing file = empty."""
        p = Path(self.config_file)
        if not p.exists():
            return {}
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{self.config_file} must contain a mapping")
        return data


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
