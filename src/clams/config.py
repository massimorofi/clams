"""Clams v2 configuration — pydantic Settings with boot guards.

Every field maps to an env var ``CLAMS_<FIELD_UPPER>``. ``.env.example`` must
list exactly these keys with the same defaults (enforced by
``tests/unit/test_env_parity.py``).
"""
from __future__ import annotations

import os
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

    # --- github webhook intake (env-only, never persisted, never logged) ----
    # Plain ``@property`` (NOT ``computed_field``) on purpose: a
    # ``computed_field`` is serialized into ``model_dump()`` /
    # ``model_dump_json()``, so any settings dump - log line, debug endpoint,
    # planner payload - would echo the secret. A plain property is reachable as
    # ``settings.github_webhook_secret`` but invisible to serialization.
    #   * ``github_webhook_secret`` exists only in the deployment environment,
    #     so it is also absent from ``.env.example`` parity tooling and can
    #     never be committed.
    #   * both are re-read on access, so a redeploy with a new secret takes
    #     effect without a settings-cache reset.
    # Nothing logs these; the webhook endpoint only ever reports *that*
    # verification failed, never the secret or the expected digest.
    @property
    def github_webhook_secret(self) -> str:
        return os.environ.get("CLAMS_GITHUB_WEBHOOK_SECRET", "").strip()

    @property
    def github_allowed_repos(self) -> list[str]:
        """Comma-separated ``owner/repo`` (or ``owner/*``) allowlist."""
        raw = os.environ.get("CLAMS_GITHUB_ALLOWED_REPOS", "")
        return [item.strip() for item in raw.split(",") if item.strip()]

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
    # on workflow done, merge that workflow's integration ref into the project
    # base branch and push it. Env-only (not runtime-editable: the settings grid
    # renders numbers and text only). A protected base branch rejects the push
    # — that raises an alert and changes nothing.
    merge_to_base_on_done: bool = True

    # --- planner loop -----------------------------------------------------------------
    planner_cycle_s: float = 5.0
    intake_every_n_cycles: int = 12
    planner_heartbeat_max_age_s: int = 60
    events_batch: int = 100
    events_block_ms: int = 1000
    # stall probe: alert (never kill) when a running task's session evidence
    # shows no progress for this long
    stall_alert_minutes: int = 15
    # frozen past this long -> escalate the stall alert warn -> error (the
    # watchdog keeps renewing; a human decides via the UI)
    stall_escalate_minutes: int = 60

    # --- limits (bounded everything) ----------------------------------------------------
    max_attempts: int = 3
    max_turns: int = 40
    max_replans: int = 3
    max_plan_tasks: int = 7
    planning_llm_budget_per_cycle: int = 3
    lease_dispatch_s: int = 600
    lease_running_extend_s: int = 600
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
    # model server reachability probe (OpenAI-compatible base URL); empty = off
    model_server_url: str = "http://192.168.1.172:8731/v1"
    max_db_size_mb: int = 500
    # session messages fetched per running task for liveness evidence
    evidence_tail_limit: int = 200
    # executor-runner death signal: its beat thread dies with the process, so
    # heartbeat silence beyond this + frozen session evidence = orphaned task
    orphan_silence_s: int = 180

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
        if self.lease_running_extend_s < 30:
            raise ConfigError("lease_running_extend_s must be >= 30")
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


#: Settings the operator may tune at runtime from the UI (planner-side only;
#: env/infra settings like paths, URLs and tokens require a container
#: restart). name -> {type, min, max, label}. Enforced by the settings_patch
#: command and advertised by GET /api/v1/config so the form is generated
#: from this single source.
EDITABLE_SETTINGS: dict[str, dict] = {
    "max_attempts": {"type": "int", "min": 1, "max": 10,
                     "label": "Task attempts before failure"},
    "max_replans": {"type": "int", "min": 0, "max": 10,
                    "label": "Workflow replans before blocked"},
    "max_turns": {"type": "int", "min": 1, "max": 500,
                  "label": "Agent turns per task (the real work bound)"},
    "max_plan_tasks": {"type": "int", "min": 1, "max": 20,
                       "label": "Max tasks per plan"},
    "planning_llm_budget_per_cycle": {"type": "int", "min": 1, "max": 10,
                                      "label": "Planning LLM calls per cycle"},
    "planner_cycle_s": {"type": "float", "min": 1, "max": 300,
                        "label": "Planner cycle (s)"},
    "intake_every_n_cycles": {"type": "int", "min": 1, "max": 720,
                              "label": "GitHub intake every N cycles"},
    "lease_running_extend_s": {"type": "int", "min": 30, "max": 7200,
                               "label": "Lease TTL: watchdog renews from session evidence (s)"},
    "stall_alert_minutes": {"type": "int", "min": 1, "max": 1440,
                            "label": "Stall alert: no session progress (min)"},
    "stall_escalate_minutes": {"type": "int", "min": 5, "max": 1440,
                               "label": "Stall escalation to error (min)"},
    "max_db_size_mb": {"type": "int", "min": 10, "max": 10_000_000,
                       "label": "Alert: max DB+WAL size (MB)"},
    "orphan_silence_s": {"type": "int", "min": 60, "max": 3600,
                         "label": "Orphan requeue: executor beat silence (s)"},
    "model_server_url": {"type": "str",
                         "label": "Model server URL ('' disables probe)"},
    "question_expiry_h": {"type": "int", "min": 1, "max": 720,
                          "label": "Question expiry before blocked (h)"},
    "outbox_max_attempts": {"type": "int", "min": 1, "max": 20,
                            "label": "Outbox delivery attempts"},
    "oldest_ready_alert_s": {"type": "int", "min": 30, "max": 86400,
                             "label": "Alert: oldest ready task age (s)"},
    "min_free_disk_mb": {"type": "int", "min": 50, "max": 10_000_000,
                         "label": "Alert: minimum free disk (MB)"},
}


def coerce_setting(name: str, value):
    """Validate + coerce one editable setting. Raises ValueError."""
    spec = EDITABLE_SETTINGS.get(name)
    if spec is None:
        raise ValueError(f"setting {name!r} is not editable at runtime")
    if spec["type"] == "int":
        v = int(value)
    elif spec["type"] == "float":
        v = float(value)
    elif spec["type"] == "str":
        return str(value)
    else:
        raise ValueError(f"unknown type {spec['type']}")
    if not (spec["min"] <= v <= spec["max"]):
        raise ValueError(f"{name}={v} outside [{spec['min']}, {spec['max']}]")
    return v
