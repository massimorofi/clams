"""Tests for :mod:`clams_admin.writer`.

Everything is written under ``tmp_path``; the repository is never touched.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from clams_admin import writer
from clams_admin.secrets import MASK, clear_registered_secrets, register_secret


@pytest.fixture(autouse=True)
def _clean_secret_registry():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --------------------------------------------------------------------------
# atomic write + permissions
# --------------------------------------------------------------------------
def test_secret_file_is_created_with_mode_0o600(tmp_path: Path) -> None:
    plan = writer.plan(tmp_path)
    plan.add(".env", "CLAMS_API_TOKEN=s3cr3t\n", secret=True, label="root env")

    report = writer.apply(plan)

    target = tmp_path / ".env"
    assert target.read_text() == "CLAMS_API_TOKEN=s3cr3t\n"
    assert _mode(target) == 0o600
    assert report.written[0].mode == 0o600
    assert report.written[0].created is True


def test_plain_file_is_created_with_mode_0o644(tmp_path: Path) -> None:
    plan = writer.plan(tmp_path)
    plan.add("notes.txt", "hello\n")

    writer.apply(plan)

    assert _mode(tmp_path / "notes.txt") == 0o644


def test_missing_parent_directories_are_created(tmp_path: Path) -> None:
    plan = writer.plan(tmp_path)
    plan.add("config/agents/generated/executor-1/config.yaml", "server: {}\n", secret=True)

    writer.apply(plan)

    target = tmp_path / "config/agents/generated/executor-1/config.yaml"
    assert target.is_file() and _mode(target) == 0o600


def test_no_temp_files_are_left_behind(tmp_path: Path) -> None:
    plan = writer.plan(tmp_path)
    plan.add(".env", "A=1\n", secret=True)
    plan.add("sub/b.txt", "B=2\n")

    writer.apply(plan)

    leftovers = [p.name for p in tmp_path.rglob("*") if ".tmp" in p.name]
    assert leftovers == []


# --------------------------------------------------------------------------
# backups
# --------------------------------------------------------------------------
def test_every_replaced_file_is_backed_up(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("OLD=1\n")
    os.chmod(tmp_path / ".env", 0o600)

    plan = writer.plan(tmp_path)
    plan.add(".env", "NEW=2\n", secret=True)

    report = writer.apply(plan, overwrite=True)

    backup = tmp_path / ".env.bak"
    assert backup.read_text() == "OLD=1\n"
    assert report.written[0].backup == backup
    assert report.written[0].created is False


def test_backup_can_be_disabled(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("old\n")
    plan = writer.plan(tmp_path)
    plan.add("a.txt", "new\n")

    writer.apply(plan, backup=False)

    assert not (tmp_path / "a.txt.bak").exists()
    assert (tmp_path / "a.txt").read_text() == "new\n"


# --------------------------------------------------------------------------
# refusing to clobber a secret
# --------------------------------------------------------------------------
def test_existing_secret_file_is_not_overwritten_without_overwrite(tmp_path: Path) -> None:
    original = "CLAMS_API_TOKEN=do-not-touch\n"
    (tmp_path / ".env").write_text(original)
    os.chmod(tmp_path / ".env", 0o600)

    plan = writer.plan(tmp_path)
    plan.add(".env", "CLAMS_API_TOKEN=whatever\n", secret=True)

    with pytest.raises(writer.SecretOverwriteError) as excinfo:
        writer.apply(plan)

    assert (tmp_path / ".env").read_text() == original
    assert not (tmp_path / ".env.bak").exists()
    assert "overwrite" in (excinfo.value.corrective_action or "")


def test_overwrite_true_replaces_the_secret_and_keeps_a_backup(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("OLD=1\n")
    plan = writer.plan(tmp_path)
    plan.add(".env", "NEW=2\n", secret=True)

    writer.apply(plan, overwrite=True)

    assert (tmp_path / ".env").read_text() == "NEW=2\n"
    assert (tmp_path / ".env.bak").read_text() == "OLD=1\n"


def test_non_secret_files_may_always_be_replaced(tmp_path: Path) -> None:
    (tmp_path / "compose.yml").write_text("old\n")
    plan = writer.plan(tmp_path)
    plan.add("compose.yml", "new\n")

    writer.apply(plan)

    assert (tmp_path / "compose.yml").read_text() == "new\n"


# --------------------------------------------------------------------------
# rollback
# --------------------------------------------------------------------------
def test_failure_on_the_last_file_restores_every_prior_file(tmp_path: Path) -> None:
    env_before = b"CLAMS_API_TOKEN=old-token\n"
    compose_before = b"name: clams\n"
    (tmp_path / ".env").write_bytes(env_before)
    (tmp_path / "docker-compose.yml").write_bytes(compose_before)
    os.chmod(tmp_path / ".env", 0o600)
    os.chmod(tmp_path / "docker-compose.yml", 0o644)

    plan = writer.plan(tmp_path)
    plan.add(".env", "CLAMS_API_TOKEN=new-token\n", secret=True)
    plan.add("docker-compose.yml", "name: changed\n")
    plan.add(".env.claudio-planner", "new file\n", secret=True)
    plan.add("config/agents/generated/planner/config.yaml", "server: {}\n", secret=True)

    with pytest.raises(writer.WriteError):
        writer.apply(plan, overwrite=True, fail_on="config/agents/generated/planner/config.yaml")

    # every earlier file is byte-identical to what it was before the call
    assert (tmp_path / ".env").read_bytes() == env_before
    assert (tmp_path / "docker-compose.yml").read_bytes() == compose_before
    # ... with its mode intact
    assert _mode(tmp_path / ".env") == 0o600
    assert _mode(tmp_path / "docker-compose.yml") == 0o644
    # a file that did not exist before is removed again
    assert not (tmp_path / ".env.claudio-planner").exists()
    assert not (tmp_path / "config/agents/generated/planner/config.yaml").exists()


def test_rollback_leaves_no_temp_files(tmp_path: Path) -> None:
    plan = writer.plan(tmp_path)
    plan.add("a.txt", "a\n")
    plan.add("b.txt", "b\n")

    with pytest.raises(writer.WriteError):
        writer.apply(plan, fail_on="b.txt")

    assert [p.name for p in tmp_path.rglob("*") if ".tmp" in p.name] == []
    assert not (tmp_path / "a.txt").exists()


def test_failure_on_the_first_file_writes_nothing(tmp_path: Path) -> None:
    plan = writer.plan(tmp_path)
    plan.add("a.txt", "a\n")
    plan.add("b.txt", "b\n")

    with pytest.raises(writer.WriteError):
        writer.apply(plan, fail_on="a.txt")

    assert not (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists()


def test_secret_overwrite_check_happens_before_any_write(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("keep me\n")
    plan = writer.plan(tmp_path)
    plan.add("first.txt", "written\n")
    plan.add(".env", "overwritten\n", secret=True)

    with pytest.raises(writer.SecretOverwriteError):
        writer.apply(plan)

    assert not (tmp_path / "first.txt").exists()
    assert (tmp_path / ".env").read_text() == "keep me\n"


# --------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------
def test_dry_run_reports_without_writing(tmp_path: Path) -> None:
    plan = writer.plan(tmp_path)
    plan.add(".env", "A=1\n", secret=True)

    report = writer.apply(plan, dry_run=True)

    assert report.dry_run is True
    assert report.relative_paths() == [".env"]
    assert not (tmp_path / ".env").exists()


# --------------------------------------------------------------------------
# the plan/report API never leaks a value
# --------------------------------------------------------------------------
def test_plan_and_report_summaries_are_redacted(tmp_path: Path) -> None:
    token = "Sup3rSecretTokenValue-1234567890"
    register_secret(token)

    plan = writer.plan(tmp_path)
    plan.add(".env", f"CLAMS_API_TOKEN={token}\n", secret=True, label="root env")

    assert token not in plan.describe()
    assert str(plan.items[0].path) in plan.describe()

    report = writer.apply(plan)
    assert token in (tmp_path / ".env").read_text()
    assert token not in report.summary()
    assert token not in repr(report.as_dict())
    assert MASK not in report.summary()  # nothing needed masking: values are absent


def test_report_exposes_only_paths_sizes_and_digests(tmp_path: Path) -> None:
    plan = writer.plan(tmp_path)
    plan.add(".env", "A=1\n", secret=True)

    report = writer.apply(plan)
    entry = report.as_dict()["files"][0]

    assert set(entry) == {
        "path", "bytes", "sha256", "mode", "secret", "created", "backup"
    }
    assert entry["mode"] == "0o600"
    assert entry["secret"] is True


# --------------------------------------------------------------------------
# single-file helper
# --------------------------------------------------------------------------
def test_write_atomic_helper(tmp_path: Path) -> None:
    report = writer.write_atomic(tmp_path / "out/.env", "A=1\n", secret=True)

    target = tmp_path / "out/.env"
    assert target.read_text() == "A=1\n"
    assert _mode(target) == 0o600
    assert report.paths() == [target]


def test_write_atomic_refuses_existing_secret_without_overwrite(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("original\n")

    with pytest.raises(writer.SecretOverwriteError):
        writer.write_atomic(target, "replacement\n", secret=True)

    assert target.read_text() == "original\n"


def test_apply_accepts_a_plan_relative_fail_on(tmp_path: Path) -> None:
    plan = writer.plan(tmp_path)
    plan.add("a.txt", "a\n")
    plan.add("nested/b.txt", "b\n")

    with pytest.raises(writer.WriteError):
        writer.apply(plan, fail_on=tmp_path / "nested/b.txt")

    assert not (tmp_path / "a.txt").exists()
