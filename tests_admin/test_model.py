"""Tests for the typed admin model, the env-file reader and the local document.

Covers, per the acceptance criteria:

* every field name mirrors a real ``CLAMS_*`` key (no invented variables),
* **unset** (``None``) vs **explicitly empty** (``''``) survives every hop,
* unknown-but-supported keys are retained in ``extra`` and re-emitted on save,
* ``.env`` parsing keeps key order, comments and blank lines,
* ``<root>/.clams-admin/config.json`` round-trips (load -> save -> load),
* ``redact()`` masks every registered secret with ``****`` and ``SecretStr``
  never leaks through ``repr``/``str``/format.

Stdlib + pytest only; every write goes under ``tmp_path``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from clams_admin import adminconfig, envfile, secrets as sec
from clams_admin.errors import ConfigFileError
from clams_admin.model import (
    AdminConfig,
    AgentRoleSettings,
    ClamsSettings,
    ClaudioUI,
    Deployment,
    ExecutorDefaults,
    ExecutorInstance,
    env_key_for,
)
from clams_admin.secrets import MASK, SecretStr, as_secret, generate_secret, redact

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
CLAUDIO_ENV_EXAMPLE = REPO_ROOT / ".env.claudio.example"


# --------------------------------------------------------------------------
# key names mirror the real variables
# --------------------------------------------------------------------------
def test_clams_field_names_are_exactly_the_keys_in_env_example() -> None:
    """No invented variables: fields <-> ``.env.example`` keys, both ways."""
    documented = envfile.load(ENV_EXAMPLE).keys()
    modelled = list(ClamsSettings().env_keys().values())

    assert set(modelled) == set(documented), (
        f"model has extra keys {set(modelled) - set(documented)}, "
        f"missing {set(documented) - set(modelled)}"
    )
    # declaration order follows the file, so a rewritten .env stays readable
    assert modelled == documented


def test_env_key_derivation_is_the_documented_prefix() -> None:
    assert env_key_for("api_port") == "CLAMS_API_PORT"
    assert env_key_for("claudio_planner_token") == "CLAMS_CLAUDIO_PLANNER_TOKEN"
    assert env_key_for("redis_url", prefix="") == "REDIS_URL"


def test_agent_field_names_are_the_keys_in_env_claudio_example() -> None:
    """The agent section mirrors ``.env.claudio.example`` (nothing invented).

    Two keys are legitimate additions to the *parsed* key list:
    ``CLAUDIO__SERVER__AUTH_TOKEN`` mirrors the agent's ``server.auth_token``
    (documented in the example's header comment) and
    ``CLAUDIO__AGENT__WORKSPACE_ALLOWED_ROOTS`` is present but commented out.
    """
    documented = set(envfile.load(CLAUDIO_ENV_EXAMPLE).keys())
    documented |= {"CLAUDIO__SERVER__AUTH_TOKEN", "CLAUDIO__AGENT__WORKSPACE_ALLOWED_ROOTS"}
    modelled = set(AgentRoleSettings().env_keys().values())

    invented = modelled - documented
    assert invented == set(), f"agent model invented keys: {sorted(invented)}"
    assert "CLAUDIO__AGENT__APPROVAL_MODE" in modelled
    assert "CLAUDIO__LLM__BASE_URL" in modelled


def test_executor_and_clams_sections_reuse_real_clams_keys() -> None:
    documented = set(envfile.load(ENV_EXAMPLE).keys())
    for key in ExecutorDefaults().env_keys().values():
        assert key in documented or key.startswith("CLAUDIO__"), key
    assert ExecutorInstance(executor_id="e", capacity=2).to_env() == {
        "CLAMS_EXECUTOR_ID": "e",
        "CLAMS_EXECUTOR_CAPACITY": "2",
    }


# --------------------------------------------------------------------------
# unset vs explicitly empty
# --------------------------------------------------------------------------
def test_unset_and_empty_are_different_in_the_model() -> None:
    settings = ClamsSettings(github_token=None, model_server_url="")

    assert settings.model_server_url == ""          # present, empty
    assert settings.github_token is None            # absent
    env = settings.to_env()
    assert env["CLAMS_MODEL_SERVER_URL"] == ""
    assert "CLAMS_GITHUB_TOKEN" not in env


def test_unset_and_empty_survive_an_env_round_trip() -> None:
    original = ClamsSettings(api_port=8200, model_server_url="", spa_dir="./frontend/dist")
    env = original.to_env()
    restored = ClamsSettings.from_env(env)

    assert restored.model_server_url == ""
    assert restored.api_port == 8200
    assert restored.spa_dir == "./frontend/dist"
    assert restored.data_dir is None
    assert restored.to_env() == env


def test_env_file_distinguishes_absent_key_from_empty_key() -> None:
    doc = envfile.parse("CLAMS_ROLE=planner\nCLAMS_MODEL_SERVER_URL=\n")

    assert doc.get("CLAMS_ROLE") == "planner"
    assert doc.get("CLAMS_MODEL_SERVER_URL") == ""
    assert doc.get("CLAMS_API_TOKEN") is None
    assert doc.has("CLAMS_MODEL_SERVER_URL") is True
    assert doc.has("CLAMS_API_TOKEN") is False
    assert "CLAMS_API_TOKEN" not in doc.as_dict()


def test_setting_none_unsets_a_key_and_empty_string_keeps_it() -> None:
    doc = envfile.parse("CLAMS_API_TOKEN=secret-token\nCLAMS_ROLE=planner\n")

    doc.set("CLAMS_API_TOKEN", None)
    doc.set("CLAMS_ROLE", "")

    assert doc.has("CLAMS_API_TOKEN") is False
    assert doc.text() == "CLAMS_ROLE=\n"


def test_int_and_float_fields_are_coerced_from_env_text() -> None:
    settings = ClamsSettings.from_env(
        {"CLAMS_API_PORT": "8200", "CLAMS_PLANNER_CYCLE_S": "5.0", "CLAMS_MSG_MAX_BYTES": "16384"}
    )
    assert settings.api_port == 8200 and isinstance(settings.api_port, int)
    assert settings.planner_cycle_s == 5.0 and isinstance(settings.planner_cycle_s, float)
    assert settings.msg_max_bytes == 16384


# --------------------------------------------------------------------------
# unknown-but-supported keys
# --------------------------------------------------------------------------
def test_unknown_clams_keys_are_kept_in_extra_and_re_emitted() -> None:
    settings = ClamsSettings.from_env(
        {"CLAMS_API_PORT": "8200", "CLAMS_NEW_WATCHDOG_FLAG": "on", "CLAMS_EXPERIMENT_MODE": ""}
    )
    assert settings.extra == {"CLAMS_NEW_WATCHDOG_FLAG": "on", "CLAMS_EXPERIMENT_MODE": ""}
    env = settings.to_env()
    assert env["CLAMS_NEW_WATCHDOG_FLAG"] == "on"
    assert env["CLAMS_EXPERIMENT_MODE"] == ""
    assert env["CLAMS_API_PORT"] == "8200"


def test_unknown_agent_keys_survive() -> None:
    agent = AgentRoleSettings.from_env(
        {"CLAUDIO__LLM__MODEL": "local-model", "CLAUDIO__FUTURE__THING": "42"}, role="planner"
    )
    assert agent.llm_model == "local-model"
    assert agent.extra == {"CLAUDIO__FUTURE__THING": "42"}
    assert agent.to_env()["CLAUDIO__FUTURE__THING"] == "42"


def test_unknown_json_keys_survive_a_round_trip(tmp_path: Path) -> None:
    deployment = Deployment(name="clams", api_port=1075, admin=AdminConfig(executor_count=1))
    deployment.extra["future_admin_feature"] = {"enabled": True}
    deployment.clams.extra["CLAMS_NOT_YET_MODELLED"] = "keepme"
    deployment.admin.extra["some_future_key"] = "also-kept"

    adminconfig.save_deployment(tmp_path, deployment)
    restored = adminconfig.load_deployment(tmp_path)

    assert restored.extra["future_admin_feature"] == {"enabled": True}
    assert restored.clams.extra["CLAMS_NOT_YET_MODELLED"] == "keepme"
    assert restored.admin.extra["some_future_key"] == "also-kept"
    assert restored.to_dict() == deployment.to_dict()


# --------------------------------------------------------------------------
# envfile: order, comments, blanks
# --------------------------------------------------------------------------
def test_envfile_round_trips_the_repository_example_byte_for_byte() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    doc = envfile.parse(text)
    assert doc.text() == text


def test_envfile_preserves_key_order_comments_and_blank_lines() -> None:
    text = (
        "# header\n"
        "\n"
        "# --- section ---\n"
        "CLAMS_API_PORT=8200   # host port\n"
        'CLAMS_REDIS_URL="redis://redis:6379/0"\n'
        "\n"
        "CLAMS_ROLE=planner\n"
    )
    doc = envfile.parse(text)

    assert doc.keys() == ["CLAMS_API_PORT", "CLAMS_REDIS_URL", "CLAMS_ROLE"]
    assert doc.text() == text
    assert doc.get("CLAMS_REDIS_URL") == "redis://redis:6379/0"
    assert doc.get("CLAMS_API_PORT") == "8200"
    kinds = [line.kind for line in doc.lines]
    assert kinds == ["comment", "blank", "comment", "pair", "pair", "blank", "pair"]


def test_envfile_set_replaces_in_place_and_appends_new_keys() -> None:
    doc = envfile.parse("A=1\n# note\nB=2\n")
    doc.set("A", "9")
    doc.set("C", "3")

    assert doc.keys() == ["A", "B", "C"]
    assert doc.text() == "A=9\n# note\nB=2\nC=3\n"


def test_envfile_keeps_inline_comment_when_updating() -> None:
    doc = envfile.parse("CLAMS_API_PORT=8200 # published port\n")
    doc.set("CLAMS_API_PORT", "1075")
    assert doc.text() == "CLAMS_API_PORT=1075 # published port\n"


def test_envfile_load_and_save_a_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("CLAMS_ROLE=planner\n", encoding="utf-8")

    doc = envfile.load(path)
    doc.set("CLAMS_API_TOKEN", generate_secret(16))
    doc.save()

    assert envfile.load(path).get("CLAMS_ROLE") == "planner"
    assert envfile.load(path).has("CLAMS_API_TOKEN")


# --------------------------------------------------------------------------
# adminconfig: the local JSON document
# --------------------------------------------------------------------------
def test_config_document_lives_under_dot_clams_admin(tmp_path: Path) -> None:
    assert adminconfig.config_path(tmp_path) == tmp_path / ".clams-admin" / "config.json"


def test_config_round_trip_preserves_unset_vs_empty_and_extras(tmp_path: Path) -> None:
    deployment = Deployment(
        name="clams",
        api_port=1075,
        planner_port=1072,
        clams=ClamsSettings(api_port=8200, model_server_url="", redis_url="redis://redis:6379/0"),
        planner=AgentRoleSettings(role="planner", approval_mode="off"),
        executor_defaults=ExecutorDefaults(approval_mode="off"),
        executors=[ExecutorInstance(executor_id="executor-1", debug_port=1073)],
        ui=ClaudioUI(port=1076, backend_url=""),
        admin=AdminConfig(executor_count=1),
    )
    deployment.clams.extra["CLAMS_FUTURE_KEY"] = "kept"

    path = adminconfig.save_deployment(tmp_path, deployment)
    assert path == tmp_path / ".clams-admin" / "config.json"
    assert adminconfig.exists(tmp_path)

    first = adminconfig.load_deployment(tmp_path)
    adminconfig.save_deployment(tmp_path, first)
    second = adminconfig.load_deployment(tmp_path)

    # unset vs explicitly empty
    assert second.clams.model_server_url == ""
    assert second.clams.data_dir is None
    assert second.ui.backend_url == ""
    assert "model_server_url" in json.loads(path.read_text(encoding="utf-8"))["clams"]
    assert "data_dir" not in json.loads(path.read_text(encoding="utf-8"))["clams"]
    # unknown-but-supported keys
    assert second.clams.extra == {"CLAMS_FUTURE_KEY": "kept"}
    assert second.to_dict() == first.to_dict()


def test_config_document_never_contains_secrets(tmp_path: Path) -> None:
    deployment = Deployment(clams=ClamsSettings(api_token=SecretStr("super-secret-token")))
    adminconfig.save_deployment(tmp_path, deployment)
    text = adminconfig.config_path(tmp_path).read_text(encoding="utf-8")

    assert "super-secret-token" not in text
    assert "api_token" not in text


def test_saving_twice_keeps_a_backup(tmp_path: Path) -> None:
    deployment = Deployment(name="first")
    adminconfig.save_deployment(tmp_path, deployment)
    adminconfig.save_deployment(tmp_path, Deployment(name="second"))
    backup = tmp_path / ".clams-admin" / "config.json.bak"

    assert backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8"))["name"] == "first"
    assert adminconfig.load_deployment(tmp_path).name == "second"


def test_missing_document_loads_as_empty_default(tmp_path: Path) -> None:
    assert adminconfig.load_document(tmp_path) == {}
    assert adminconfig.load_deployment(tmp_path).clams.to_env() == {}


def test_malformed_document_raises_config_file_error(tmp_path: Path) -> None:
    path = adminconfig.config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ConfigFileError) as exc:
        adminconfig.load_document(tmp_path)
    assert str(path) in str(exc.value)
    assert exc.value.file == str(path)


def test_admin_config_section_can_be_saved_on_its_own(tmp_path: Path) -> None:
    adminconfig.save_deployment(tmp_path, Deployment(name="clams", api_port=1075))
    adminconfig.save_admin_config(tmp_path, AdminConfig(executor_count=3))

    reloaded = adminconfig.load_deployment(tmp_path)
    assert reloaded.admin.executor_count == 3
    assert reloaded.name == "clams" and reloaded.api_port == 1075


# --------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------
def test_generate_secret_is_url_safe_and_unique() -> None:
    values = {generate_secret() for _ in range(50)}
    assert len(values) == 50
    token = generate_secret(32)
    assert len(token) >= 40
    assert all(ch.isalnum() or ch in "-_" for ch in token)
    assert generate_secret(8) != generate_secret(8)


def test_generate_secret_rejects_bad_sizes() -> None:
    with pytest.raises(ValueError):
        generate_secret(0)


def test_redact_masks_every_registered_secret() -> None:
    planner = generate_secret(16)
    executor = generate_secret(16)
    api = generate_secret(16)
    sec.clear_registered_secrets()

    summary = (
        "CLAMS_CLAUDIO_PLANNER_TOKEN=" + planner + "\n"
        "CLAMS_CLAUDIO_EXECUTOR_TOKEN=" + executor + "\n"
        "CLAMS_API_TOKEN=" + api + "\n"
    )
    redacted = redact(summary, [planner, executor, api])

    for secret in (planner, executor, api):
        assert secret not in redacted
        assert MASK in redacted
    assert "CLAMS_API_TOKEN=" + MASK in redacted


def test_redact_uses_the_registry_when_no_list_is_passed() -> None:
    sec.clear_registered_secrets()
    token = SecretStr(generate_secret(16))
    text = f"Authorization: Bearer {token.get_secret_value()}"

    assert token.get_secret_value() not in redact(text)
    assert redact(text) == f"Authorization: Bearer {MASK}"


def test_redact_accepts_a_mapping_and_handles_none() -> None:
    assert redact("token: abc123", {"api": "abc123"}) == f"token: {MASK}"
    assert redact(None) == ""
    assert redact("nothing to hide") == "nothing to hide"


def test_secretstr_repr_and_str_never_leak() -> None:
    secret = SecretStr("hunter2-super-secret")

    assert repr(secret) == f"SecretStr({MASK!r})"
    assert str(secret) == MASK
    assert f"{secret}" == MASK
    assert "hunter2" not in repr(secret) and "hunter2" not in str(secret)
    assert "hunter2" not in repr(ClamsSettings(api_token=secret))
    assert secret.get_secret_value() == "hunter2-super-secret"


def test_secretstr_compares_by_value_and_tracks_emptiness() -> None:
    assert SecretStr("abc") == SecretStr("abc")
    assert SecretStr("abc") == "abc"
    assert SecretStr("abc") != SecretStr("xyz")
    assert bool(SecretStr("")) is False
    assert bool(SecretStr("x")) is True
    assert SecretStr(None).get_secret_value() == ""
    assert as_secret(None) is None
    assert as_secret("plain").get_secret_value() == "plain"


def test_deployment_secret_values_and_redacted_summary() -> None:
    planner_token = generate_secret(16)
    deployment = Deployment(
        name="clams",
        clams=ClamsSettings(
            claudio_planner_token=SecretStr(planner_token),
            api_token=SecretStr(generate_secret(16)),
        ),
        planner=AgentRoleSettings(role="planner", auth_token=SecretStr(planner_token)),
    )
    secrets = deployment.secret_values()

    assert planner_token in secrets.values()
    assert "Bearer " + planner_token not in redact(f"Bearer {planner_token}", list(secrets.values()))
    assert planner_token not in deployment.summary()


# --------------------------------------------------------------------------
# deployment shape
# --------------------------------------------------------------------------
def test_deployment_renders_the_three_env_files() -> None:
    deployment = Deployment(
        clams=ClamsSettings(api_port=8200, api_token=SecretStr("t" * 20)),
        planner=AgentRoleSettings(role="planner", approval_mode="off"),
        executor_defaults=ExecutorDefaults(approval_mode="off", executor_capacity=1),
        executors=[ExecutorInstance(executor_id="executor-1", debug_port=1073)],
    )
    assert deployment.clams_env()["CLAMS_API_TOKEN"] == "t" * 20
    assert deployment.planner_env()["CLAUDIO__AGENT__APPROVAL_MODE"] == "off"
    assert deployment.executor_env(0)["CLAUDIO__AGENT__APPROVAL_MODE"] == "off"
    assert deployment.executor_env(0)["CLAMS_EXECUTOR_CAPACITY"] == "1"


def test_executor_instance_overrides_defaults() -> None:
    defaults = ExecutorDefaults(agent=AgentRoleSettings(role="executor", llm_model="base-model"))
    instance = ExecutorInstance(
        executor_id="executor-2",
        agent=AgentRoleSettings(role="executor", llm_model="override-model"),
    )
    deployment = Deployment(executor_defaults=defaults, executors=[instance])

    env = deployment.executor_env(0)
    assert env["CLAUDIO__LLM__MODEL"] == "override-model"


def test_default_deployment_matches_the_compose_topology() -> None:
    deployment = adminconfig.default_deployment(Path("/srv/clams"))

    assert deployment.api_port == 1075
    assert deployment.planner_port == 1072
    assert deployment.executors[0].debug_port == 1073
    assert deployment.ui.port == 1076
    assert deployment.admin.root == "/srv/clams"
    assert set(deployment.clams.to_env()) <= set(ClamsSettings().env_keys().values())
