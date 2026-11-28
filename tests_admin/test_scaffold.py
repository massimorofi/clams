"""Scaffold tests for the stdlib-only ``clams_admin`` package.

These run in a container that only has ``python3``/``pytest``/``git``/``grep``,
so this file itself must not import anything beyond the standard library,
pytest and ``clams_admin``. Every write goes under ``tmp_path``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

FORBIDDEN = ("yaml", "typer", "rich", "questionary")
SUBCOMMANDS = ("configure", "validate", "preview", "apply")


# --------------------------------------------------------------------------
# Import purity
# --------------------------------------------------------------------------
def _child_import_check(modules: str) -> subprocess.CompletedProcess:
    """Import ``modules`` in a clean interpreter, report forbidden leaks."""
    script = (
        "import sys, importlib\n"
        f"for name in {list(modules.split(','))}:\n"
        "    importlib.import_module(name)\n"
        f"bad = [m for m in {list(FORBIDDEN)} if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC_DIR))
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return subprocess.run(
        [sys.executable, "-B", "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=120,
    )


def test_importing_clams_admin_pulls_in_no_optional_deps() -> None:
    """``import clams_admin`` must not import yaml/typer/rich/questionary."""
    proc = _child_import_check("clams_admin")
    assert proc.returncode == 0, proc.stderr
    leaked = proc.stdout.strip()
    assert leaked == "", f"importing clams_admin pulled in third-party modules: {leaked}"


def test_importing_whole_package_pulls_in_no_optional_deps() -> None:
    """Even after touching every module, the optional deps stay unimported."""
    proc = _child_import_check(
        "clams_admin,clams_admin.cli,clams_admin.commands,clams_admin.settings,"
        "clams_admin.errors,clams_admin.serialization"
    )
    assert proc.returncode == 0, proc.stderr
    leaked = proc.stdout.strip()
    assert leaked == "", f"clams_admin modules imported third-party packages: {leaked}"


def test_import_in_process_leaves_no_forbidden_modules() -> None:
    """In-process guard: importing clams_admin introduces none of them.

    A snapshot diff is used (rather than "not in sys.modules") so the test does
    not false-fail when the pytest runner itself happens to have imported one
    of these packages before the test ran.
    """
    before = set(sys.modules)
    import clams_admin  # noqa: F401
    import clams_admin.cli  # noqa: F401
    import clams_admin.settings  # noqa: F401
    import clams_admin.commands  # noqa: F401

    added = set(sys.modules) - before
    offenders = [m for m in FORBIDDEN if m in added]
    assert offenders == [], f"importing clams_admin introduced {offenders}"


def test_lazy_yaml_backend_is_used_only_inside_functions() -> None:
    """The YAML helpers import PyYAML lazily and fall back to stdlib parsing."""
    from clams_admin import serialization

    data = {"api": {"port": 9100, "host": "h"}, "admin": {"enabled_profiles": ["a", "b"]}}
    text = serialization.dump_yaml_fallback(data)
    assert serialization.load_yaml_fallback(text) == data

    before = set(sys.modules)
    serialization.load_yaml_fallback(text)
    serialization.dump_yaml_fallback(data)
    assert "yaml" not in set(sys.modules) - before


# --------------------------------------------------------------------------
# Packaging contract
# --------------------------------------------------------------------------
def test_pyproject_registers_clams_admin_script() -> None:
    """``pyproject.toml`` exposes ``clams-admin = "clams_admin.cli:main"``."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert "clams-admin" in scripts
    assert scripts["clams-admin"] == "clams_admin.cli:main"


def test_package_lives_under_src_and_is_importable() -> None:
    import clams_admin

    assert Path(clams_admin.__file__).resolve().parent == (SRC_DIR / "clams_admin").resolve()
    assert isinstance(clams_admin.__version__, str) and clams_admin.__version__


def test_package_exports_admin_error() -> None:
    from clams_admin import AdminError

    assert issubclass(AdminError, Exception)


# --------------------------------------------------------------------------
# errors.py contract
# --------------------------------------------------------------------------
def test_admin_error_carries_setting_file_and_fix() -> None:
    from clams_admin.errors import AdminError

    err = AdminError(
        "port out of range",
        setting="api.port",
        file="config/admin.toml",
        corrective_action="set api.port to an integer between 1 and 65535",
    )
    assert err.setting == "api.port"
    assert err.file == "config/admin.toml"
    assert err.corrective_action.startswith("set api.port")

    rendered = "\n".join(err.as_lines())
    for fragment in ("port out of range", "api.port", "config/admin.toml", "1 and 65535"):
        assert fragment in rendered


def test_admin_error_subclasses_have_exit_codes() -> None:
    from clams_admin.errors import AdminError, ConfigFileError, ConfirmationRequired

    assert issubclass(ConfigFileError, AdminError)
    assert ConfigFileError("x").exit_code == 2
    assert ConfirmationRequired("x").exit_code == 3


# --------------------------------------------------------------------------
# CLI surface
# --------------------------------------------------------------------------
def _run_module(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(SRC_DIR))
    return subprocess.run(
        [sys.executable, "-B", "-m", "clams_admin", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd or REPO_ROOT),
        env=env,
        timeout=120,
    )


def test_module_help_exits_zero_and_lists_subcommands() -> None:
    proc = _run_module("--help")
    assert proc.returncode == 0, proc.stderr
    for name in SUBCOMMANDS:
        assert name in proc.stdout, f"--help does not mention {name}"


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_each_subcommand_has_working_help(name: str) -> None:
    proc = _run_module(name, "--help")
    assert proc.returncode == 0, proc.stderr
    for flag in ("--config", "--non-interactive", "--root", "--yes"):
        assert flag in proc.stdout, f"{name} --help is missing {flag}"


def test_no_arguments_prints_help_and_exits_zero() -> None:
    proc = _run_module()
    assert proc.returncode == 0
    assert "configure" in proc.stdout


def test_unknown_subcommand_exits_nonzero() -> None:
    proc = _run_module("frobnicate")
    assert proc.returncode != 0
    assert "invalid choice" in proc.stderr or "frobnicate" in proc.stderr


# --------------------------------------------------------------------------
# Command behaviour (everything writes under tmp_path only)
# --------------------------------------------------------------------------
def test_configure_writes_config_into_tmp_repo(tmp_repo: Path, run_cli, capsys) -> None:
    config = tmp_repo / "config" / "admin.toml"
    code = run_cli("configure", "--root", str(tmp_repo), "--non-interactive", "--yes")
    assert code == 0
    assert config.exists()

    text = config.read_text(encoding="utf-8")
    assert "[api]" in text and "[db]" in text

    code = run_cli("validate", "--root", str(tmp_repo), "--non-interactive")
    assert code == 0
    assert "OK" in capsys.readouterr().out


def test_configure_honours_set_overrides(tmp_repo: Path, run_cli, capsys) -> None:
    code = run_cli(
        "configure",
        "--root",
        str(tmp_repo),
        "--non-interactive",
        "--yes",
        "--set",
        "api.port=9100",
        "--set",
        "admin.log_level=WARNING",
    )
    assert code == 0
    text = (tmp_repo / "config" / "admin.toml").read_text(encoding="utf-8")
    assert "port = 9100" in text
    assert 'log_level = "WARNING"' in text


def test_configure_rejects_unknown_setting(tmp_repo: Path, run_cli, capsys) -> None:
    code = run_cli("preview", "--root", str(tmp_repo), "--non-interactive", "--set", "nope=1")
    assert code == 2
    assert "unknown setting" in capsys.readouterr().err


def test_validate_fails_on_bad_values(tmp_repo: Path, run_cli, capsys) -> None:
    config = tmp_repo / "config" / "admin.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[api]\nport = 99999\nadmin_log = "NOPE"\n', encoding="utf-8")

    code = run_cli("validate", "--config", str(config), "--non-interactive")
    assert code == 1
    err = capsys.readouterr().err
    assert "api.port" in err
    assert "fix:" in err


def test_validate_reports_missing_file_with_corrective_action(tmp_repo: Path, run_cli, capsys) -> None:
    missing = tmp_repo / "config" / "absent.toml"
    code = run_cli("validate", "--config", str(missing), "--non-interactive")
    assert code == 2
    err = capsys.readouterr().err
    assert "not found" in err and "configure" in err


def test_preview_writes_nothing(tmp_repo: Path, run_cli, capsys) -> None:
    before = sorted(p.name for p in tmp_repo.rglob("*") if p.is_file())
    code = run_cli("preview", "--root", str(tmp_repo), "--non-interactive", "--set", "api.port=9200")
    assert code == 0
    out = capsys.readouterr().out
    assert "api.port" in out and "9200" in out
    after = sorted(p.name for p in tmp_repo.rglob("*") if p.is_file())
    assert after == before == []


def test_apply_requires_confirmation(tmp_repo: Path, run_cli, capsys) -> None:
    code = run_cli("apply", "--root", str(tmp_repo), "--non-interactive", "--set", "api.port=9300")
    assert code == 3
    assert "--yes" in capsys.readouterr().err
    assert not (tmp_repo / "config" / "admin.toml").exists()


def test_apply_writes_and_backs_up(tmp_repo: Path, run_cli, capsys) -> None:
    config = tmp_repo / "config" / "admin.toml"
    assert run_cli("configure", "--root", str(tmp_repo), "--non-interactive", "--yes") == 0
    original = config.read_text(encoding="utf-8")

    code = run_cli(
        "apply", "--root", str(tmp_repo), "--non-interactive", "--yes", "--set", "api.port=9400"
    )
    assert code == 0
    assert "port = 9400" in config.read_text(encoding="utf-8")
    assert config.with_name("admin.toml.bak").read_text(encoding="utf-8") == original

    assert run_cli("validate", "--root", str(tmp_repo), "--non-interactive") == 0


def test_apply_is_idempotent(tmp_repo: Path, run_cli, capsys) -> None:
    assert run_cli("configure", "--root", str(tmp_repo), "--non-interactive", "--yes") == 0
    capsys.readouterr()
    code = run_cli("apply", "--root", str(tmp_repo), "--non-interactive", "--yes")
    assert code == 0
    assert "nothing to apply" in capsys.readouterr().out


def test_yaml_and_json_configs_round_trip(tmp_repo: Path, run_cli) -> None:
    for name in ("admin.yaml", "admin.json"):
        config = tmp_repo / "config" / name
        assert run_cli("configure", "--config", str(config), "--non-interactive", "--yes") == 0
        assert config.exists()
        assert run_cli("validate", "--config", str(config), "--non-interactive") == 0


def test_tests_never_write_into_the_repo(tmp_repo: Path, run_cli) -> None:
    """Guard: the default --config path must be resolved inside tmp_repo."""
    from clams_admin.settings import resolve_config_path

    assert resolve_config_path(None, tmp_repo) == tmp_repo / "config" / "admin.toml"
    assert resolve_config_path("config/admin.toml", tmp_repo).is_relative_to(tmp_repo)
    assert resolve_config_path(tmp_repo / "x.toml", tmp_repo) == tmp_repo / "x.toml"

    tracked = {p for p in (REPO_ROOT / "config").iterdir()}
    run_cli("configure", "--root", str(tmp_repo), "--non-interactive", "--yes")
    assert {p for p in (REPO_ROOT / "config").iterdir()} == tracked
