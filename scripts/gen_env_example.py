"""Generate .env.example from Settings defaults (CI parity source of truth)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from clams.config import Settings  # noqa: E402

s = Settings(_env_file=None)
lines = [
    "# Clams v2 — copy to .env and fill in.",
    "# Every key below equals a Settings field default (CI-enforced parity:",
    "# tests/unit/test_env_parity.py).",
    "",
]
groups = {
    "paths": ["data_dir", "db_path", "work_root", "config_file"],
    "bus / redis": ["redis_url", "stream_tasks", "stream_events", "stream_dlq",
                    "group_executors", "group_planner", "msg_max_bytes",
                    "dlq_max_deliveries"],
    "claudio agents": ["claudio_planner_url", "claudio_planner_token",
                       "claudio_executor_url", "claudio_executor_token",
                       "claudio_timeout_s", "claudio_workspace_root"],
    "github": ["github_token", "github_api_base", "github_graphql_base"],
    "api": ["api_host", "api_port", "api_token", "spa_dir"],
    "executor": ["executor_id", "executor_capacity", "heartbeat_interval_s",
                 "executor_alive_interval_s", "keep_failed_worktree_hours"],
    "planner loop": ["planner_cycle_s", "intake_every_n_cycles",
                     "planner_heartbeat_max_age_s", "events_batch",
                     "events_block_ms"],
    "limits (bounded everything)": [
        "max_attempts", "max_turns", "max_wall_s", "max_replans",
        "max_plan_tasks", "planning_llm_budget_per_cycle", "lease_dispatch_s",
        "lease_running_extend_s", "question_expiry_h", "outbox_max_attempts",
        "outbox_backoff_base_s", "retention_events_days",
        "retention_processed_days", "retention_sent_outbox_days"],
    "watchdog": ["watchdog_probe_interval_s", "watchdog_healthy_probes_to_resume",
                 "oldest_ready_alert_s", "min_free_disk_mb"],
    "misc": ["role"],
}
seen = set()
for group, fields in groups.items():
    lines.append(f"# --- {group} " + "-" * max(0, 70 - len(group)))
    for f in fields:
        lines.append(f"CLAMS_{f.upper()}={getattr(s, f)}")
        seen.add(f)
missing = set(s.model_fields) - seen
assert not missing, missing
out = Path(__file__).resolve().parents[1] / ".env.example"
out.write_text("\n".join(lines) + "\n")
print(f"wrote {out} with {len(seen)} keys")
