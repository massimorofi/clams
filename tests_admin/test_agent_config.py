"""Tests for :mod:`clams_admin.render_agent_config`.

The tracked templates are read as the seed but every write lands under
``tmp_path``: each test copies the two tracked files into a throwaway root and
renders there, so ``config/agents/**`` in the repository is never modified.
"""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from clams_admin import render_agent_config as rac
from clams_admin import render_env, writer
from clams_admin.adminconfig import default_deployment
from clams_admin.envfile import EnvFile
from clams_admin.errors import ConfigFileError
from clams_admin.model import ExecutorInstance, SecretStr

REPO_ROOT = Path(__file__).resolve().parents[1]

try:  # optional dependency: the tests must not require it
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # type: ignore


@pytest.fixture()
def deployment():
    dep = default_deployment("/srv/clams")
    dep.executors = [
        ExecutorInstance(executor_id="executor-1", debug_port=1073, capacity=1),
        ExecutorInstance(executor_id="executor-2", debug_port=1077, capacity=1),
    ]
    dep.admin.executor_count = 2
    dep.clams.max_turns = 40
    dep.executor_defaults.max_turns = 60
    dep.clams.model_server_url = "http://model.local:8731/v1"
    dep.planner.llm_model = "planner-model"
    dep.planner.llm_api_key = SecretStr("planner-key")
    dep.executor_defaults.agent.llm_model = "worker-model"
    return dep


@pytest.fixture()
def tokens(deployment):
    return render_env.TokenBundle.for_deployment(deployment)


@pytest.fixture()
def seed_root(tmp_path: Path) -> Path:
    """A tmp repo holding a copy of the two tracked templates."""
    for role in ("planner", "executor"):
        source = REPO_ROOT / rac.template_relpath(role)
        target = tmp_path / rac.template_relpath(role)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _value(text: str, key: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            return stripped.split(":", 1)[1].strip().strip('"')
    raise AssertionError(f"{key!r} not found")


# --------------------------------------------------------------------------
# where the files land
# --------------------------------------------------------------------------
def test_generated_paths_are_under_the_generated_dir() -> None:
    assert rac.generated_relpath("planner") == "config/agents/generated/planner/config.yaml"
    assert rac.generated_relpath("executor", 1) == "config/agents/generated/executor-1/config.yaml"
    assert rac.generated_relpath("executor", 3) == "config/agents/generated/executor-3/config.yaml"


def test_templates_are_the_tracked_ones() -> None:
    assert rac.TEMPLATE_RELPATHS == {
        "planner": "config/agents/planner/config.yaml",
        "executor": "config/agents/executor/config.yaml",
    }


def test_plan_writes_planner_plus_one_config_per_executor(seed_root: Path, deployment, tokens) -> None:
    report, _ = rac.apply(deployment, seed_root, tokens=tokens, template_root=seed_root)

    assert sorted(report.relative_paths()) == [
        "config/agents/generated/executor-1/config.yaml",
        "config/agents/generated/executor-2/config.yaml",
        "config/agents/generated/planner/config.yaml",
    ]
    for written in report.written:
        assert written.path.is_file()
        assert _mode(written.path) == 0o600, written.path


def test_tracked_template_paths_are_never_in_the_plan(seed_root: Path, deployment, tokens) -> None:
    plan, _ = rac.plan(deployment, seed_root, tokens=tokens, template_root=seed_root)

    assert not [p for p in plan.paths() if "/generated/" not in str(p)]


def test_missing_template_reports_the_fix(tmp_path: Path, deployment, tokens) -> None:
    with pytest.raises(ConfigFileError) as excinfo:
        rac.plan(deployment, tmp_path, tokens=tokens, template_root=tmp_path)

    assert "template not found" in excinfo.value.message
    assert excinfo.value.corrective_action


# --------------------------------------------------------------------------
# the values that make a generated config correct
# --------------------------------------------------------------------------
def test_auth_token_matches_the_rendered_root_env(seed_root: Path, deployment) -> None:
    env_report, tokens = render_env.apply(deployment, seed_root)
    body = EnvFile.parse((seed_root / ".env").read_text()).as_dict()

    planner_token = body["CLAMS_CLAUDIO_PLANNER_TOKEN"]
    executor_token = body["CLAMS_CLAUDIO_EXECUTOR_TOKEN"]

    report, _ = rac.apply(deployment, seed_root, tokens=tokens, template_root=seed_root)
    planner_text = (seed_root / rac.generated_relpath("planner")).read_text()
    executor_text = (seed_root / rac.generated_relpath("executor", 2)).read_text()

    assert _value(planner_text, "auth_token") == planner_token
    assert _value(executor_text, "auth_token") == executor_token
    assert env_report.relative_paths()


@pytest.mark.parametrize("index", [1, 2])
def test_every_executor_config_has_the_literal_approval_mode_line(
    seed_root: Path, deployment, tokens, index
) -> None:
    rac.apply(deployment, seed_root, tokens=tokens, template_root=seed_root)

    text = (seed_root / rac.generated_relpath("executor", index)).read_text()

    assert 'approval_mode: "off"' in text
    assert any(line.strip() == 'approval_mode: "off"' for line in text.splitlines())


def test_agent_names_and_max_turns(seed_root: Path, deployment, tokens) -> None:
    rac.apply(deployment, seed_root, tokens=tokens, template_root=seed_root)

    planner = (seed_root / rac.generated_relpath("planner")).read_text()
    executor = (seed_root / rac.generated_relpath("executor", 2)).read_text()

    assert _value(planner, "name") == "Planner"
    assert _value(executor, "name") == "Executor-2"
    assert _value(planner, "max_turns") == "40"
    assert _value(executor, "max_turns") == "60"


def test_cors_origins_cover_the_ui_origin(seed_root: Path, deployment, tokens) -> None:
    deployment.ui.port = 1099
    rac.apply(deployment, seed_root, tokens=tokens, template_root=seed_root)

    for role, index in (("planner", 1), ("executor", 1), ("executor", 2)):
        text = (seed_root / rac.generated_relpath(role, index)).read_text()
        assert "- http://localhost:1099" in text
        assert "- http://127.0.0.1:1099" in text


def test_workspace_allowed_roots_cover_work(seed_root: Path, deployment, tokens) -> None:
    rac.apply(deployment, seed_root, tokens=tokens, template_root=seed_root)

    for role, index in (("planner", 1), ("executor", 1), ("executor", 2)):
        text = (seed_root / rac.generated_relpath(role, index)).read_text()
        roots = rac.get_list(text, ("agent", "workspace_allowed_roots"))
        assert "/work" in roots
        assert roots.count("/work") == 1


def test_llm_values_are_written(seed_root: Path, deployment, tokens) -> None:
    spec = rac.spec_for_planner(deployment, tokens)
    spec.llm.provider = "openai"
    spec.llm.temperature = 0.3
    spec.llm.max_tokens = 8192
    spec.llm.timeout_seconds = 1800
    spec.llm.retry_attempts = 4
    template = (seed_root / rac.template_relpath("planner")).read_text()

    text = rac.render(spec, template)

    assert _value(text, "provider") == "openai"
    assert _value(text, "base_url") == "http://model.local:8731/v1"
    assert _value(text, "model") == "planner-model"
    assert _value(text, "api_key") == "planner-key"
    assert _value(text, "temperature") == "0.3"
    assert _value(text, "max_tokens") == "8192"
    assert _value(text, "timeout_seconds") == "1800"
    assert _value(text, "retry_attempts") == "4"


def test_unset_llm_fields_keep_the_template_value(seed_root: Path, deployment, tokens) -> None:
    template = (seed_root / rac.template_relpath("executor")).read_text()
    spec = rac.spec_for_executor(deployment, tokens, 1)
    spec.llm.model = None

    text = rac.render(spec, template)

    assert _value(text, "model") == _value(template, "model")


# --------------------------------------------------------------------------
# the template survives the patch
# --------------------------------------------------------------------------
def test_template_comments_survive(seed_root: Path, deployment, tokens) -> None:
    template = (seed_root / rac.template_relpath("executor")).read_text()
    comment = next(
        line for line in template.splitlines()
        if line.strip().startswith("#") and len(line.strip()) > 40
    )

    text = rac.render(rac.spec_for_executor(deployment, tokens, 1), template)

    assert comment in text
    assert "# GENERATED FILE" in text.splitlines()[0]


@pytest.mark.skipif(yaml is None, reason="PyYAML is not installed")
def test_generated_configs_are_valid_yaml(seed_root: Path, deployment, tokens) -> None:
    rac.apply(deployment, seed_root, tokens=tokens, template_root=seed_root)

    for index in (1, 2):
        text = (seed_root / rac.generated_relpath("executor", index)).read_text()
        document = yaml.safe_load(text)
        assert document["agent"]["approval_mode"] == "off"
        assert "/work" in document["agent"]["workspace_allowed_roots"]
        assert document["server"]["auth_token"] == tokens.executor_token


def test_failure_on_the_last_config_rolls_back_the_others(
    seed_root: Path, deployment, tokens
) -> None:
    existing = seed_root / rac.generated_relpath("planner")
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("# previous\n", encoding="utf-8")

    plan, _ = rac.plan(deployment, seed_root, tokens=tokens, template_root=seed_root)
    last = plan.items[-1].path

    with pytest.raises(writer.WriteError):
        writer.apply(plan, overwrite=True, fail_on=last)

    assert existing.read_text() == "# previous\n"
    assert not (seed_root / rac.generated_relpath("executor", 2)).exists()


def test_report_and_plan_never_print_a_token(seed_root: Path, deployment, tokens) -> None:
    plan, _ = rac.plan(deployment, seed_root, tokens=tokens, template_root=seed_root)
    secrets = set(tokens.as_dict().values()) | {"planner-key"}

    report = writer.apply(plan)
    printed = plan.describe() + report.summary() + repr(report.as_dict())

    for value in secrets:
        assert value not in printed


# --------------------------------------------------------------------------
# the text patcher itself
# --------------------------------------------------------------------------
MINI = """\
server:
  host: 0.0.0.0
  auth_token: dev-token-change-me
  cors_origins:
  - http://localhost:8080
  expose_raw_config: false

agent:
  name: Planner
  max_turns: 150
  approval_mode: 'off'
  workspace_allowed_roots:
    - /workspace
"""


def test_set_scalar_replaces_in_place_and_keeps_siblings() -> None:
    text = rac.set_scalar(MINI, ("server", "auth_token"), "tok-123", quote=True)

    assert '  auth_token: "tok-123"' in text.splitlines()
    assert "  host: 0.0.0.0" in text
    assert "  expose_raw_config: false" in text


def test_set_scalar_appends_a_missing_key_to_its_section() -> None:
    text = rac.set_scalar(MINI, ("server", "port"), 8100)

    assert "  port: 8100" in text
    assert text.index("  port: 8100") < text.index("agent:")


def test_set_list_replaces_a_same_indent_sequence() -> None:
    text = rac.set_list(MINI, ("server", "cors_origins"), ["http://a:1", "http://b:2"])

    lines = text.splitlines()
    assert lines[lines.index("  cors_origins:") + 1] == "  - http://a:1"
    assert lines[lines.index("  cors_origins:") + 2] == "  - http://b:2"
    assert "  - http://localhost:8080" not in lines
    assert "  expose_raw_config: false" in lines


def test_set_list_replaces_a_deeper_sequence() -> None:
    text = rac.set_list(
        MINI, ("agent", "workspace_allowed_roots"), ["/workspace", "/work"]
    )

    assert "    - /work" in text.splitlines()
    assert "  context_window" not in text  # nothing after the block was eaten


def test_get_scalar_and_get_list_read_the_document() -> None:
    assert rac.get_scalar(MINI, ("server", "auth_token")) == "dev-token-change-me"
    assert rac.get_scalar(MINI, ("agent", "max_turns")) == "150"
    assert rac.get_scalar(MINI, ("agent", "nope")) is None
    assert rac.get_list(MINI, ("server", "cors_origins")) == ["http://localhost:8080"]
    assert rac.get_list(MINI, ("agent", "workspace_allowed_roots")) == ["/workspace"]


def test_render_scalar_quotes_what_yaml_would_misread() -> None:
    assert rac.render_scalar("off") == '"off"'
    assert rac.render_scalar("no") == '"no"'
    assert rac.render_scalar("true") == '"true"'
    assert rac.render_scalar("42") == '"42"'
    assert rac.render_scalar("") == '""'
    assert rac.render_scalar("plain-value") == "plain-value"
    assert rac.render_scalar(None) == "null"
    assert rac.render_scalar(150) == "150"
    assert rac.render_scalar("http://x:1076") == "http://x:1076"


def test_rendering_is_idempotent(seed_root: Path, deployment, tokens) -> None:
    template = (seed_root / rac.template_relpath("executor")).read_text()
    spec = rac.spec_for_executor(deployment, tokens, 1)

    once = rac.render(spec, template)
    twice = rac.render(spec, once)

    assert once == twice


# --------------------------------------------------------------------------
# both renderers on one plan
# --------------------------------------------------------------------------
def test_env_and_configs_share_one_plan_and_one_token_bundle(
    seed_root: Path, deployment
) -> None:
    """The env files and the generated configs must agree on every token."""
    env_plan, tokens = render_env.plan(deployment, seed_root)
    config_plan, same_tokens = rac.plan(
        deployment, seed_root, tokens=tokens, template_root=seed_root
    )
    assert same_tokens is tokens
    combined = writer.WritePlan(root=seed_root)
    combined.extend(env_plan.items)
    combined.extend(config_plan.items)

    report = writer.apply(combined)

    assert len(report.written) == 4 + 3  # 4 env files, 3 agent configs
    root_body = EnvFile.parse((seed_root / ".env").read_text()).as_dict()
    pairs = [
        ("planner", 1, "CLAMS_CLAUDIO_PLANNER_TOKEN"),
        ("executor", 1, "CLAMS_CLAUDIO_EXECUTOR_TOKEN"),
        ("executor", 2, "CLAMS_CLAUDIO_EXECUTOR_TOKEN"),
    ]
    for role, index, key in pairs:
        config = (seed_root / rac.generated_relpath(role, index)).read_text()
        assert _value(config, "auth_token") == root_body[key]
    assert all(_mode(w.path) == 0o600 for w in report.written)


def test_a_config_failure_rolls_back_the_env_files_too(seed_root: Path, deployment) -> None:
    (seed_root / ".env").write_text("CLAMS_API_TOKEN=keep\n", encoding="utf-8")
    env_plan, tokens = render_env.plan(deployment, seed_root)
    config_plan, _ = rac.plan(
        deployment, seed_root, tokens=tokens, template_root=seed_root
    )
    combined = writer.WritePlan(root=seed_root)
    combined.extend(env_plan.items)
    combined.extend(config_plan.items)
    last = combined.items[-1].path

    with pytest.raises(writer.WriteError):
        writer.apply(combined, overwrite=True, fail_on=last)

    assert (seed_root / ".env").read_text() == "CLAMS_API_TOKEN=keep\n"
    assert not (seed_root / ".env.claudio-planner").exists()
    assert not (seed_root / rac.generated_relpath("executor", 2)).exists()
