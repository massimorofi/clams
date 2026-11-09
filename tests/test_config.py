"""S1 — configuration tests."""
from __future__ import annotations

import pytest

from clams.config import Limits, Settings


def test_defaults_match_spec_hw4():
    s = Settings()
    assert s.limits.max_attempts == 3
    assert s.limits.max_turns == 40
    assert s.limits.max_wall_seconds == 1800
    assert s.limits.max_replans == 3


def test_hw1_queue_ceiling_ok():
    # 2 engines x 3 queues = 6 <= 8
    lim = Limits()
    assert lim.queues_needed == 6
    lim.validate_against_device()


def test_hw1_violation_raises():
    lim = Limits(max_concurrent_engines=3, queues_per_engine=3)
    with pytest.raises(ValueError, match="HW-1 violated"):
        lim.validate_against_device()


def test_limits_reject_nonpositive():
    with pytest.raises(ValueError):
        Limits(max_attempts=0)
    with pytest.raises(ValueError):
        Limits(max_turns=-1)


def test_env_override(monkeypatch):
    monkeypatch.setenv("CLAMS__GITHUB__OWNER", "acme")
    monkeypatch.setenv("CLAMS__GITHUB__REPO", "widgets")
    monkeypatch.setenv("CLAMS__LIMITS__MAX_TURNS", "17")
    s = Settings()
    assert s.github.owner == "acme"
    assert s.github.repo == "widgets"
    assert s.github.full_name == "acme/widgets"
    assert s.limits.max_turns == 17


def test_status_labels_present():
    s = Settings()
    assert s.intake_label == "status:Ready"
    assert s.inprogress_label == "status:In Progress"
    assert s.review_label == "status:Review"
    assert s.blocked_label == "status:Blocked"
    assert s.done_label == "status:Done"


def test_load_validates_device_limits():
    # defaults satisfy HW-1
    s = Settings.load()
    assert s.limits.max_concurrent_engines == 2
    assert s.limits.queues_needed <= s.limits.max_compute_queues


def test_load_rejects_bad_device_limits(monkeypatch):
    monkeypatch.setenv("CLAMS__LIMITS__MAX_CONCURRENT_ENGINES", "4")
    with pytest.raises(ValueError, match="HW-1 violated"):
        Settings.load()


def test_paths_are_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAMS__PATHS__DB_PATH", str(tmp_path / "x.db"))
    s = Settings()
    assert s.paths.db_path == str(tmp_path / "x.db")
