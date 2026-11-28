"""Validation tests: every rejection in the spec produces a named error.

Each case asserts the failure names **the offending setting** and **the file**
to fix, and that a correct deployment validates clean. Stdlib + pytest only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from clams_admin import adminconfig
from clams_admin.model import (
    AdminConfig,
    AgentRoleSettings,
    ClamsSettings,
    ClaudioUI,
    Deployment,
    ExecutorDefaults,
    ExecutorInstance,
)
from clams_admin.secrets import SecretStr, generate_secret
from clams_admin.validate import (
    CONFIG_FILE,
    ENV_FILE,
    EXECUTOR_ENV_FILE,
    PLANNER_ENV_FILE,
    PORT_MAX,
    PORT_MIN,
    ValidationError,
    format_errors,
    validate_deployment,
    validate_root,
)

PLANNER_TOKEN = "planner-" + generate_secret(12)
EXECUTOR_TOKEN = "executor-" + generate_secret(12)
API_TOKEN = "api-" + generate_secret(12)


def valid_deployment() -> Deployment:
    """A deployment that satisfies every rule (compose topology)."""
    return Deployment(
        name="clams",
        api_port=1075,
        planner_port=1072,
        clams=ClamsSettings(
            redis_url="redis://redis:6379/0",
            api_host="0.0.0.0",
            api_port=8200,
            api_token=SecretStr(API_TOKEN),
            claudio_planner_url="http://claudio-planner:8100",
            claudio_planner_token=SecretStr(PLANNER_TOKEN),
            claudio_executor_url="http://claudio-executor:8100",
            claudio_executor_token=SecretStr(EXECUTOR_TOKEN),
            github_api_base="https://api.github.com",
            github_graphql_base="https://api.github.com/graphql",
            model_server_url="",
            role="planner",
        ),
        planner=AgentRoleSettings(
            role="planner", auth_token=SecretStr(PLANNER_TOKEN), approval_mode="off",
            llm_base_url="http://host.docker.internal:8731/v1",
        ),
        executor_defaults=ExecutorDefaults(
            approval_mode="off",
            executor_capacity=1,
            agent=AgentRoleSettings(role="executor", auth_token=SecretStr(EXECUTOR_TOKEN)),
        ),
        executors=[ExecutorInstance(executor_id="executor-1", debug_port=1073, capacity=1)],
        ui=ClaudioUI(port=1076, container_port=8080, image="claudio/web:latest"),
        admin=AdminConfig(executor_count=1),
    )


def failing(deployment: Deployment) -> list[ValidationError]:
    errors = validate_deployment(deployment)
    assert errors, "expected at least one ValidationError"
    return errors


def settings_of(errors) -> set[str]:
    return {error.setting for error in errors}


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------
def test_a_correct_deployment_has_no_errors() -> None:
    assert validate_deployment(valid_deployment()) == []


def test_errors_are_admin_errors_with_a_fix_hint() -> None:
    deployment = valid_deployment()
    deployment.clams.redis_url = "redis-not-a-url"
    error = failing(deployment)[0]

    assert isinstance(error, ValidationError)
    assert error.setting == "clams.redis_url"
    assert error.file == ENV_FILE
    assert error.corrective_action and "clams.redis_url" in error.corrective_action
    assert "clams.redis_url" in format_errors([error])


# --------------------------------------------------------------------------
# 1. URLs
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "attribute, bad_value",
    [
        ("redis_url", "redis-not-a-url"),
        ("redis_url", "http://"),
        ("claudio_planner_url", "claudio-planner:8100"),
        ("claudio_executor_url", "not a url"),
        ("github_api_base", "api.github.com"),
        ("github_graphql_base", "https:/graphql"),
    ],
)
def test_non_url_endpoints_are_rejected(attribute: str, bad_value: str) -> None:
    deployment = valid_deployment()
    setattr(deployment.clams, attribute, bad_value)

    errors = failing(deployment)
    assert f"clams.{attribute}" in settings_of(errors)
    for error in errors:
        if error.setting == f"clams.{attribute}":
            assert error.file == ENV_FILE
            assert bad_value in error.message


def test_explicitly_empty_url_is_rejected_but_unset_is_not() -> None:
    """unset (key absent) and '' (key present, empty) are different failures."""
    empty = valid_deployment()
    empty.clams.redis_url = ""
    errors = failing(empty)
    assert any("empty" in e.message for e in errors if e.setting == "clams.redis_url")

    unset = valid_deployment()
    unset.clams.redis_url = None
    assert "clams.redis_url" not in settings_of(validate_deployment(unset))


def test_model_server_url_may_be_empty_because_empty_disables_the_probe() -> None:
    deployment = valid_deployment()
    deployment.clams.model_server_url = ""
    assert "clams.model_server_url" not in settings_of(validate_deployment(deployment))

    deployment.clams.model_server_url = "192.168.1.172:8731/v1"
    errors = failing(deployment)
    assert errors[0].setting == "clams.model_server_url"
    assert errors[0].file == ENV_FILE


def test_agent_llm_base_url_is_checked_against_the_agent_env_file() -> None:
    deployment = valid_deployment()
    deployment.planner.llm_base_url = "httpx//nope"

    errors = failing(deployment)
    assert errors[0].setting == "planner.llm_base_url"
    assert errors[0].file == PLANNER_ENV_FILE


def test_executor_llm_base_url_names_the_executor_instance() -> None:
    deployment = valid_deployment()
    deployment.executors[0].agent.llm_base_url = "not-a-url"

    errors = failing(deployment)
    assert errors[0].setting == "executors[executor-1].llm_base_url"
    assert errors[0].file == EXECUTOR_ENV_FILE


def test_ui_backend_url_is_checked() -> None:
    deployment = valid_deployment()
    deployment.ui.backend_url = "localhost:1073"

    errors = failing(deployment)
    assert errors[0].setting == "ui.backend_url"
    assert errors[0].file == CONFIG_FILE


# --------------------------------------------------------------------------
# 2. ports
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad_port", [1, 80, 1023, PORT_MAX + 1, 99999])
def test_ports_outside_the_allowed_range_are_rejected(bad_port: int) -> None:
    deployment = valid_deployment()
    deployment.api_port = bad_port

    errors = failing(deployment)
    assert errors[0].setting == "api_port"
    assert errors[0].file == CONFIG_FILE
    assert f"{PORT_MIN}-{PORT_MAX}" in errors[0].message


def test_boundary_ports_are_accepted() -> None:
    deployment = valid_deployment()
    deployment.api_port, deployment.planner_port, deployment.ui.port = PORT_MIN, PORT_MAX, 1076
    deployment.executors[0].debug_port = 4000
    deployment.clams.api_port = 8200
    assert validate_deployment(deployment) == []


def test_non_integer_port_is_rejected() -> None:
    deployment = valid_deployment()
    deployment.ui.port = "1076"

    errors = failing(deployment)
    assert errors[0].setting == "ui.port"
    assert "integer" in errors[0].message


@pytest.mark.parametrize(
    "field,value",
    [
        ("ui.port", 1072),          # collides with planner_port
        ("planner_port", 1075),     # collides with api_port
        ("ui.port", 1075),          # collides with api_port
    ],
)
def test_duplicated_published_ports_are_rejected(field: str, value: int) -> None:
    deployment = valid_deployment()
    target, _, name = field.partition(".")
    setattr(deployment if not name else getattr(deployment, target), name or target, value)

    errors = failing(deployment)
    assert field in settings_of(errors)
    duplicate = next(e for e in errors if e.setting == field)
    assert "duplicate" in duplicate.message
    assert duplicate.file == CONFIG_FILE


def test_executor_debug_port_colliding_with_the_api_port_is_rejected() -> None:
    deployment = valid_deployment()
    deployment.executors[0].debug_port = 1075

    errors = failing(deployment)
    assert "executors[executor-1].debug_port" in settings_of(errors)


def test_two_executors_sharing_a_debug_port_are_rejected() -> None:
    deployment = valid_deployment()
    deployment.admin.executor_count = 2
    deployment.executors.append(
        ExecutorInstance(executor_id="executor-2", debug_port=1073, capacity=1)
    )

    errors = failing(deployment)
    assert "executors[executor-2].debug_port" in settings_of(errors)


def test_in_container_api_port_is_range_checked_too() -> None:
    deployment = valid_deployment()
    deployment.clams.api_port = 80

    errors = failing(deployment)
    assert errors[0].setting == "clams.api_port"
    assert errors[0].file == ENV_FILE


# --------------------------------------------------------------------------
# 3. executor_count
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad_count", [0, -1, "2", 1.5, True, None])
def test_executor_count_must_be_an_integer_at_least_one(bad_count: object) -> None:
    deployment = valid_deployment()
    deployment.admin.executor_count = bad_count  # type: ignore[assignment]

    errors = failing(deployment)
    assert errors[0].setting == "admin.executor_count"
    assert errors[0].file == CONFIG_FILE
    if bad_count is None:
        assert "not set" in errors[0].message
    elif isinstance(bad_count, int) and not isinstance(bad_count, bool):
        assert "less than 1" in errors[0].message
    else:
        assert "integer" in errors[0].message


def test_executor_count_one_is_fine() -> None:
    deployment = valid_deployment()
    deployment.admin.executor_count = 1
    assert validate_deployment(deployment) == []


# --------------------------------------------------------------------------
# 4. required tokens
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "attribute", ["api_token", "claudio_planner_token", "claudio_executor_token"]
)
def test_missing_required_tokens_are_rejected(attribute: str) -> None:
    unset = valid_deployment()
    setattr(unset.clams, attribute, None)
    errors = failing(unset)
    assert f"clams.{attribute}" in settings_of(errors)
    missing = next(e for e in errors if e.setting == f"clams.{attribute}")
    assert missing.file == ENV_FILE
    assert "required" in missing.message and "not set" in missing.message

    empty = valid_deployment()
    setattr(empty.clams, attribute, SecretStr(""))
    errors = failing(empty)
    empty_error = next(e for e in errors if e.setting == f"clams.{attribute}")
    assert "empty" in empty_error.message
    assert empty_error.message != missing.message


def test_agent_auth_token_missing_is_reported_against_the_agent_env_file() -> None:
    deployment = valid_deployment()
    deployment.planner.auth_token = None

    errors = failing(deployment)
    assert "planner.auth_token" in settings_of(errors)
    error = next(e for e in errors if e.setting == "planner.auth_token")
    assert error.file == PLANNER_ENV_FILE
    assert "auth_token" in error.message


def test_executor_agent_auth_token_missing_is_reported_per_instance() -> None:
    deployment = valid_deployment()
    deployment.executor_defaults.agent.auth_token = None

    errors = failing(deployment)
    assert "executors[executor-1].auth_token" in settings_of(errors)


# --------------------------------------------------------------------------
# 5. executor approval_mode
# --------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["on_request", "always", "OFF"])
def test_executor_approval_mode_must_be_off(mode: str) -> None:
    deployment = valid_deployment()
    deployment.executor_defaults.approval_mode = mode

    errors = failing(deployment)
    assert errors[0].setting == "executors[executor-1].approval_mode"
    assert errors[0].file == EXECUTOR_ENV_FILE
    assert "off" in errors[0].message


def test_executor_approval_mode_unset_is_rejected() -> None:
    deployment = valid_deployment()
    deployment.executor_defaults.approval_mode = None

    errors = failing(deployment)
    assert errors[0].setting == "executors[executor-1].approval_mode"
    assert "not set" in errors[0].message


def test_one_bad_executor_is_reported_by_id() -> None:
    deployment = valid_deployment()
    deployment.admin.executor_count = 2
    deployment.executors.append(
        ExecutorInstance(
            executor_id="executor-2",
            debug_port=1074,
            agent=AgentRoleSettings(role="executor", approval_mode="always"),
        )
    )

    errors = failing(deployment)
    assert "executors[executor-2].approval_mode" in settings_of(errors)
    assert "executors[executor-1].approval_mode" not in settings_of(errors)


def test_planner_approval_mode_is_not_constrained() -> None:
    deployment = valid_deployment()
    deployment.planner.approval_mode = "on_request"
    assert validate_deployment(deployment) == []


# --------------------------------------------------------------------------
# 6. Clams token <-> agent auth_token agreement
# --------------------------------------------------------------------------
def test_planner_token_mismatch_is_rejected() -> None:
    deployment = valid_deployment()
    deployment.planner.auth_token = SecretStr("a-different-token")

    errors = failing(deployment)
    assert errors[0].setting == "clams.claudio_planner_token"
    assert errors[0].file == PLANNER_ENV_FILE
    assert "CLAMS_CLAUDIO_PLANNER_TOKEN" in errors[0].message
    assert "401" in errors[0].message


def test_executor_token_mismatch_is_rejected() -> None:
    deployment = valid_deployment()
    deployment.executors[0].agent.auth_token = SecretStr("another-token-entirely")

    errors = failing(deployment)
    assert errors[0].setting == "clams.claudio_executor_token"
    assert errors[0].file == EXECUTOR_ENV_FILE
    assert "CLAMS_CLAUDIO_EXECUTOR_TOKEN" in errors[0].message


def test_instance_token_overrides_the_default_before_comparing() -> None:
    deployment = valid_deployment()
    deployment.executors[0].agent.auth_token = SecretStr(PLANNER_TOKEN)

    errors = failing(deployment)
    assert "clams.claudio_executor_token" in settings_of(errors)


def test_mismatch_is_not_double_reported_when_a_side_is_unset() -> None:
    deployment = valid_deployment()
    deployment.planner.auth_token = None
    errors = failing(deployment)
    assert "clams.claudio_planner_token" not in settings_of(errors)


# --------------------------------------------------------------------------
# reporting / document integration
# --------------------------------------------------------------------------
def test_all_problems_are_reported_at_once() -> None:
    deployment = valid_deployment()
    deployment.clams.redis_url = "nope"
    deployment.api_port = 80
    deployment.admin.executor_count = 0
    deployment.executor_defaults.approval_mode = "always"
    deployment.clams.api_token = None

    errors = failing(deployment)
    assert len(errors) >= 5
    for error in errors:
        assert error.setting and error.file and error.corrective_action


def test_validate_root_reads_the_saved_document(tmp_path: Path) -> None:
    deployment = valid_deployment()
    adminconfig.save_deployment(tmp_path, deployment)

    errors = validate_root(tmp_path)
    # config.json is the *non-secret* document: tokens live in the env files,
    # so a bare load only trips the "required token is not set" rules.
    assert settings_of(errors) == {
        "clams.api_token",
        "clams.claudio_planner_token",
        "clams.claudio_executor_token",
        "planner.auth_token",
        "executors[executor-1].auth_token",
    }
    # everything non-secret survived the round trip
    assert adminconfig.load_deployment(tmp_path).to_dict() == deployment.to_dict()


def test_validate_root_reports_where_the_bad_value_lives(tmp_path: Path) -> None:
    deployment = valid_deployment()
    deployment.executors[0].debug_port = 22
    adminconfig.save_deployment(tmp_path, deployment)

    errors = validate_root(tmp_path)
    assert errors[0].setting == "executors[executor-1].debug_port"
    assert errors[0].file == CONFIG_FILE


def test_error_message_never_contains_a_secret() -> None:
    from clams_admin.secrets import MASK, redact

    deployment = valid_deployment()
    deployment.planner.auth_token = SecretStr("brand-new-planner-token")
    errors = failing(deployment)

    rendered = format_errors(errors)
    assert "brand-new-planner-token" not in rendered
    assert "brand-new-planner-token" not in redact(rendered, list(deployment.secret_values().values()))

    # an empty required token is reported without echoing the (empty) secret
    leaky = valid_deployment()
    leaky.clams.api_token = SecretStr("super-private-api-token")
    leaky.clams.redis_url = "not-a-url"          # force an unrelated error
    assert "super-private-api-token" not in format_errors(failing(leaky))

    empty = valid_deployment()
    empty.clams.api_token = SecretStr("")
    for error in failing(empty):
        assert "super-private-api-token" not in str(error)
        assert error.value in (None, MASK, "")
