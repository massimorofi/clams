"""WP1: config boot guards + yaml loading."""
from __future__ import annotations

import pytest

from clams.config import ConfigError, Settings


def test_defaults_match_plan():
    s = Settings(_env_file=None)
    assert s.stream_tasks == "clams:tasks"
    assert s.stream_events == "clams:events"
    assert s.max_attempts == 3
    assert s.max_replans == 3
    assert s.lease_dispatch_s == 120
    assert s.lease_running_extend_s == 180
    assert s.api_port == 8200
    assert s.msg_max_bytes == 16384
    assert s.planner_cycle_s == 5.0
    assert s.intake_every_n_cycles == 12


def test_api_role_requires_token():
    s = Settings(_env_file=None, role="api", api_token="")
    with pytest.raises(ConfigError):
        s.boot_guard()
    s2 = Settings(_env_file=None, role="api", api_token="secret")
    s2.boot_guard()  # no raise


def test_role_validation():
    with pytest.raises(Exception):
        Settings(_env_file=None, role="bogus")


def test_limits_guards(tmp_path):
    s = Settings(_env_file=None, max_attempts=0)
    with pytest.raises(ConfigError):
        s.boot_guard()
    s = Settings(_env_file=None, max_plan_tasks=9)
    with pytest.raises(ConfigError):
        s.boot_guard()


def test_env_override(monkeypatch):
    monkeypatch.setenv("CLAMS_DB_PATH", "/tmp/x.db")
    monkeypatch.setenv("CLAMS_API_TOKEN", "tok")
    s = Settings(_env_file=None)
    assert s.db_path == "/tmp/x.db"
    assert s.api_token == "tok"


def test_load_projects_yaml(tmp_path):
    cfg = tmp_path / "clams.yaml"
    cfg.write_text(
        "projects:\n  - repo: owner/name\n    enabled: true\nlimits: {}\n")
    s = Settings(_env_file=None, config_file=str(cfg))
    data = s.load_projects_yaml()
    assert data["projects"][0]["repo"] == "owner/name"


def test_load_projects_yaml_missing(tmp_path):
    s = Settings(_env_file=None, config_file=str(tmp_path / "nope.yaml"))
    assert s.load_projects_yaml() == {}
