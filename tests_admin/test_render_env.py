"""Tests for :mod:`clams_admin.render_env`.

Every render is applied under ``tmp_path``; the repository's real ``.env`` is
never read or written.
"""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from clams_admin import compose_gen, render_env, writer
from clams_admin.adminconfig import default_deployment
from clams_admin.envfile import EnvFile
from clams_admin.model import AgentRoleSettings, ExecutorInstance, SecretStr


@pytest.fixture()
def deployment():
    """A two-executor deployment shaped like ``default_deployment()``."""
    dep = default_deployment("/srv/clams")
    dep.executors = [
        ExecutorInstance(executor_id="executor-1", debug_port=1073, capacity=1),
        ExecutorInstance(executor_id="executor-2", debug_port=1077, capacity=1),
    ]
    dep.admin.executor_count = 2
    dep.clams.max_turns = 40
    dep.executor_defaults.max_turns = 60
    dep.planner = AgentRoleSettings(
        role="planner",
        llm_base_url="http://model.local:8731/v1",
        llm_model="planner-model",
        llm_api_key=SecretStr("planner-key"),
    )
    return dep


@pytest.fixture()
def tokens(deployment):
    return render_env.TokenBundle.for_deployment(deployment)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _env(text: str) -> dict[str, str]:
    return EnvFile.parse(text).as_dict()


# --------------------------------------------------------------------------
# the root .env
# --------------------------------------------------------------------------
def test_root_env_carries_every_clams_setting(deployment, tokens) -> None:
    text = render_env.render_root_env(deployment, tokens)
    body = _env(text)

    for key, value in deployment.clams.to_env().items():
        assert body.get(key) == value, key
    assert body["CLAMS_REDIS_URL"] == "redis://redis:6379/0"
    assert body["CLAMS_ROLE"] == "planner"


def test_root_env_has_the_generated_token_trio(deployment, tokens) -> None:
    body = _env(render_env.render_root_env(deployment, tokens))

    assert body["CLAMS_API_TOKEN"] == tokens.api_token
    assert body["CLAMS_CLAUDIO_PLANNER_TOKEN"] == tokens.planner_token
    assert body["CLAMS_CLAUDIO_EXECUTOR_TOKEN"] == tokens.executor_token
    assert len({tokens.api_token, tokens.planner_token, tokens.executor_token}) == 3


def test_existing_tokens_are_reused_not_rotated(deployment) -> None:
    deployment.clams.claudio_planner_token = SecretStr("stable-planner-token")

    bundle = render_env.TokenBundle.for_deployment(deployment)

    assert bundle.planner_token == "stable-planner-token"
    assert bundle.api_token and bundle.api_token != "stable-planner-token"


# --------------------------------------------------------------------------
# the agent env files, and the token pairing
# --------------------------------------------------------------------------
def test_planner_env_token_matches_the_root_env(deployment, tokens) -> None:
    root = _env(render_env.render_root_env(deployment, tokens))
    planner = _env(render_env.render_planner_env(deployment, tokens))

    assert planner["CLAUDIO__SERVER__AUTH_TOKEN"] == root["CLAMS_CLAUDIO_PLANNER_TOKEN"]


@pytest.mark.parametrize("index", [1, 2])
def test_executor_env_token_matches_the_root_env(deployment, tokens, index) -> None:
    root = _env(render_env.render_root_env(deployment, tokens))
    executor = _env(render_env.render_executor_env(deployment, tokens, index))

    assert executor["CLAUDIO__SERVER__AUTH_TOKEN"] == root["CLAMS_CLAUDIO_EXECUTOR_TOKEN"]


def test_agent_env_files_carry_the_role_values(deployment, tokens) -> None:
    planner = _env(render_env.render_planner_env(deployment, tokens))
    executor = _env(render_env.render_executor_env(deployment, tokens, 1))

    assert planner["CLAUDIO__AGENT__APPROVAL_MODE"] == "off"
    assert executor["CLAUDIO__AGENT__APPROVAL_MODE"] == "off"
    assert "/work" in planner["CLAUDIO__AGENT__WORKSPACE_ALLOWED_ROOTS"].split(",")
    assert "/work" in executor["CLAUDIO__AGENT__WORKSPACE_ALLOWED_ROOTS"].split(",")


def test_planner_llm_overrides_land_in_its_env(deployment, tokens) -> None:
    planner = _env(render_env.render_planner_env(deployment, tokens))

    assert planner["CLAUDIO__LLM__BASE_URL"] == "http://model.local:8731/v1"
    assert planner["CLAUDIO__LLM__MODEL"] == "planner-model"
    assert planner["CLAUDIO__LLM__API_KEY"] == "planner-key"


def test_executor_env_file_names_follow_compose(deployment) -> None:
    assert render_env.executor_env_name(1) == ".env.claudio-executor"
    assert render_env.executor_env_name(2) == ".env.claudio-executor-2"
    assert render_env.executor_env_name(3) == ".env.claudio-executor-3"


def test_rendering_into_an_existing_file_keeps_comments(deployment, tokens) -> None:
    base = "# operator note: keep this\nCLAMS_DATA_DIR=./data\n"

    text = render_env.render_root_env(deployment, tokens, base=base)

    assert "# operator note: keep this" in text
    assert "CLAMS_DATA_DIR=./data" in text
    assert "CLAMS_API_TOKEN" in text


# --------------------------------------------------------------------------
# writing the files
# --------------------------------------------------------------------------
def test_plan_writes_root_planner_and_one_file_per_executor(tmp_path: Path, deployment) -> None:
    report, tokens = render_env.apply(deployment, tmp_path)

    assert sorted(report.relative_paths()) == [
        ".env",
        ".env.claudio-executor",
        ".env.claudio-executor-2",
        ".env.claudio-planner",
    ]
    for written in report.written:
        assert _mode(written.path) == 0o600, written.path
        assert written.secret is True
    assert (tmp_path / ".env").read_text().count(tokens.executor_token) == 1


def test_written_files_hold_the_matching_tokens(tmp_path: Path, deployment) -> None:
    report, tokens = render_env.apply(deployment, tmp_path)

    root = _env((tmp_path / ".env").read_text())
    planner = _env((tmp_path / ".env.claudio-planner").read_text())
    executor_1 = _env((tmp_path / ".env.claudio-executor").read_text())
    executor_2 = _env((tmp_path / ".env.claudio-executor-2").read_text())

    assert planner["CLAUDIO__SERVER__AUTH_TOKEN"] == root["CLAMS_CLAUDIO_PLANNER_TOKEN"]
    assert executor_1["CLAUDIO__SERVER__AUTH_TOKEN"] == root["CLAMS_CLAUDIO_EXECUTOR_TOKEN"]
    assert executor_2["CLAUDIO__SERVER__AUTH_TOKEN"] == root["CLAMS_CLAUDIO_EXECUTOR_TOKEN"]


def test_existing_env_is_not_overwritten_without_overwrite(tmp_path: Path, deployment) -> None:
    (tmp_path / ".env").write_text("CLAMS_API_TOKEN=mine\n")

    with pytest.raises(writer.SecretOverwriteError):
        render_env.apply(deployment, tmp_path)

    assert (tmp_path / ".env").read_text() == "CLAMS_API_TOKEN=mine\n"
    assert not (tmp_path / ".env.claudio-planner").exists()


def test_re_render_with_overwrite_keeps_a_backup(tmp_path: Path, deployment) -> None:
    first, tokens = render_env.apply(deployment, tmp_path)
    assert first.relative_paths()

    second, same_tokens = render_env.apply(
        deployment, tmp_path, tokens=tokens, overwrite=True
    )

    assert (tmp_path / ".env.bak").is_file()
    assert same_tokens is tokens
    assert _env((tmp_path / ".env").read_text())["CLAMS_API_TOKEN"] == tokens.api_token


def test_a_failure_on_the_last_env_file_rolls_everything_back(tmp_path: Path, deployment) -> None:
    (tmp_path / ".env").write_text("CLAMS_API_TOKEN=original\n")
    plan, tokens = render_env.plan(deployment, tmp_path)
    last = plan.items[-1].path

    with pytest.raises(writer.WriteError):
        writer.apply(plan, overwrite=True, fail_on=last)

    assert (tmp_path / ".env").read_text() == "CLAMS_API_TOKEN=original\n"
    assert not (tmp_path / ".env.claudio-planner").exists()
    assert not (tmp_path / ".env.claudio-executor").exists()


# --------------------------------------------------------------------------
# nothing that leaves the tool carries a secret
# --------------------------------------------------------------------------
def test_plan_and_report_never_print_a_token(tmp_path: Path, deployment) -> None:
    plan, tokens = render_env.plan(deployment, tmp_path)
    secrets = set(tokens.as_dict().values())

    plan_text = plan.describe()
    report = writer.apply(plan)
    report_text = report.summary() + repr(report.as_dict())

    for value in secrets:
        assert value not in plan_text
        assert value not in report_text


def test_rendered_compose_text_contains_no_token(deployment, tokens) -> None:
    deployment.clams.api_token = SecretStr(tokens.api_token)
    deployment.clams.claudio_planner_token = SecretStr(tokens.planner_token)
    deployment.clams.claudio_executor_token = SecretStr(tokens.executor_token)

    compose = compose_gen.render(deployment)

    for value in tokens.as_dict().values():
        assert value not in compose


def test_deployment_summary_is_redacted(deployment, tokens) -> None:
    deployment.clams.claudio_planner_token = SecretStr(tokens.planner_token)

    assert tokens.planner_token not in deployment.summary()
