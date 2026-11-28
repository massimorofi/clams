"""Validation: turn a :class:`~clams_admin.model.Deployment` into a list of errors.

Every rule the operator can get wrong is checked here, and every failure is a
:class:`ValidationError` that names **the setting** (dotted, e.g.
``clams.redis_url``), **the file** to edit (``.env``, ``.env.claudio-planner``,
``.clams-admin/config.json``) and a corrective action. ``validate_deployment``
returns a list — it never raises — so the CLI can print every problem at once.

Rules
-----

1. URL-shaped settings must be real URLs (``base_url``, ``redis_url``, the
   GitHub endpoints, the agent LLM base URLs, the UI backend).
2. Ports must be integers in ``1024-65535`` and must not be duplicated across
   the API / UI / Planner / Executor debug ports.
3. ``executor_count`` must be an integer ``>= 1``.
4. Required tokens must be present and non-empty.
5. Executor ``approval_mode`` must be ``off`` (nobody is there to approve).
6. ``CLAMS_CLAUDIO_PLANNER_TOKEN`` / ``CLAMS_CLAUDIO_EXECUTOR_TOKEN`` must match
   the corresponding agent ``server.auth_token``.

Stdlib-only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .errors import SettingValidationError
from .model import Deployment, ExecutorInstance
from .secrets import MASK, SecretStr

__all__ = [
    "ValidationError",
    "PORT_MIN",
    "PORT_MAX",
    "REQUIRED_TOKENS",
    "validate_deployment",
    "validate_root",
    "format_errors",
    "has_errors",
]

#: Unprivileged-but-not-reserved range the admin tool accepts for published ports.
PORT_MIN = 1024
PORT_MAX = 65535

#: Default file names used when reporting where a bad value lives.
ENV_FILE = ".env"
CONFIG_FILE = ".clams-admin/config.json"
PLANNER_ENV_FILE = ".env.claudio-planner"
EXECUTOR_ENV_FILE = ".env.claudio-executor"

#: Clams-side tokens that must be set (``.env``).
REQUIRED_TOKENS: tuple[str, ...] = (
    "clams.api_token",
    "clams.claudio_planner_token",
    "clams.claudio_executor_token",
)


class ValidationError(SettingValidationError):
    """One invalid setting. Carries ``setting`` and ``file`` like every admin error."""

    exit_code = 2


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _text(value: Any) -> Optional[str]:
    """Raw text of a field value; ``None`` stays ``None`` (unset)."""
    if value is None:
        return None
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value if isinstance(value, str) else str(value)


def _is_url(value: str, schemes: Sequence[str]) -> bool:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if parts.scheme.lower() not in schemes:
        return False
    if not parts.netloc or "@" in parts.netloc.split(":")[0]:
        return False
    host = parts.hostname or ""
    return bool(host)


def _error(setting: str, message: str, *, file: str, fix: str, value: Any = None) -> ValidationError:
    return ValidationError(
        setting=setting,
        message=message,
        file=file,
        corrective_action=fix,
        # never carry a plaintext secret on the error object
        value=MASK if isinstance(value, SecretStr) else value,
    )


def _check_url(
    errors: list[ValidationError],
    setting: str,
    value: Any,
    *,
    file: str,
    schemes: Sequence[str] = ("http", "https"),
    allow_empty: bool = False,
    label: Optional[str] = None,
) -> None:
    """Append an error unless ``value`` is a URL (or unset / allowed-empty)."""
    text = _text(value)
    what = label or setting
    if text is None:
        return
    if text == "":
        if allow_empty:
            return
        errors.append(_error(
            setting,
            f"{what} is set but empty; it must be a URL ({'/'.join(schemes)}://...)",
            file=file,
            fix=f"set {setting} to a {schemes[0]}:// URL, or remove the key to leave it unset",
            value=value,
        ))
        return
    if not _is_url(text, schemes):
        errors.append(_error(
            setting,
            f"{what} is not a valid URL: {text!r} (expected {'/'.join(schemes)}://host[:port]/path)",
            file=file,
            fix=f"set {setting} to a valid {'/'.join(schemes)} URL",
            value=value,
        ))


def _check_port(
    errors: list[ValidationError],
    setting: str,
    value: Any,
    *,
    file: str,
) -> Optional[int]:
    """Range-check one port. Returns the int when usable, else ``None``."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(_error(
            setting,
            f"{setting} must be an integer port, got {value!r}",
            file=file,
            fix=f"set {setting} to an integer between {PORT_MIN} and {PORT_MAX}",
            value=value,
        ))
        return None
    if not (PORT_MIN <= value <= PORT_MAX):
        errors.append(_error(
            setting,
            f"{setting}={value} is outside the allowed port range {PORT_MIN}-{PORT_MAX}",
            file=file,
            fix=f"set {setting} to a port between {PORT_MIN} and {PORT_MAX}",
            value=value,
        ))
        return value
    return value


def _effective(instance_value: Any, default_value: Any) -> Any:
    """Instance override wins; ``None`` falls back to the executor defaults."""
    return instance_value if instance_value is not None else default_value


def _label(instance: ExecutorInstance, index: int) -> str:
    return instance.executor_id or f"executor-{index + 1}"


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------
def validate_deployment(
    deployment: Deployment,
    *,
    env_file: str = ENV_FILE,
    config_file: str = CONFIG_FILE,
    planner_env_file: str = PLANNER_ENV_FILE,
    executor_env_file: str = EXECUTOR_ENV_FILE,
) -> list[ValidationError]:
    """Return every problem found in ``deployment`` (empty list == valid)."""
    errors: list[ValidationError] = []
    clams = deployment.clams
    admin = deployment.admin

    # -- 1. URLs -----------------------------------------------------------
    _check_url(errors, "clams.redis_url", clams.redis_url, file=env_file,
               schemes=("redis", "rediss", "http", "https"), label="CLAMS_REDIS_URL")
    _check_url(errors, "clams.claudio_planner_url", clams.claudio_planner_url,
               file=env_file, label="CLAMS_CLAUDIO_PLANNER_URL")
    _check_url(errors, "clams.claudio_executor_url", clams.claudio_executor_url,
               file=env_file, label="CLAMS_CLAUDIO_EXECUTOR_URL")
    _check_url(errors, "clams.github_api_base", clams.github_api_base,
               file=env_file, label="CLAMS_GITHUB_API_BASE")
    _check_url(errors, "clams.github_graphql_base", clams.github_graphql_base,
               file=env_file, label="CLAMS_GITHUB_GRAPHQL_BASE")
    # '' legitimately disables the model-server probe (see config.py comment).
    _check_url(errors, "clams.model_server_url", clams.model_server_url, file=env_file,
               allow_empty=True, label="CLAMS_MODEL_SERVER_URL")
    _check_url(errors, "planner.llm_base_url", deployment.planner.llm_base_url,
               file=planner_env_file, allow_empty=True, label="CLAUDIO__LLM__BASE_URL (planner)")
    for index, instance in enumerate(deployment.executors):
        _check_url(errors, f"executors[{_label(instance, index)}].llm_base_url",
                   _effective(instance.agent.llm_base_url, deployment.executor_defaults.agent.llm_base_url),
                   file=executor_env_file, allow_empty=True,
                   label=f"CLAUDIO__LLM__BASE_URL ({_label(instance, index)})")
    _check_url(errors, "ui.backend_url", deployment.ui.backend_url, file=config_file,
               allow_empty=True, label="Claudio UI backend URL")

    # -- 2. ports ----------------------------------------------------------
    published: list[tuple[str, str, int]] = []
    for setting, value, file in (
        ("api_port", deployment.api_port, config_file),
        ("planner_port", deployment.planner_port, config_file),
        ("ui.port", deployment.ui.port, config_file),
    ):
        port = _check_port(errors, setting, value, file=file)
        if port is not None:
            published.append((setting, file, port))
    for index, instance in enumerate(deployment.executors):
        setting = f"executors[{_label(instance, index)}].debug_port"
        port = _check_port(errors, setting, instance.debug_port, file=config_file)
        if port is not None:
            published.append((setting, config_file, port))
    # the in-container API bind port is a separate setting (CLAMS_API_PORT)
    _check_port(errors, "clams.api_port", clams.api_port, file=env_file)

    seen: dict[int, tuple[str, str]] = {}
    for setting, file, port in published:
        if port in seen:
            first_setting, _ = seen[port]
            errors.append(_error(
                setting,
                f"{setting}={port} duplicates {first_setting}; "
                f"API, UI, Planner and Executor debug ports must all differ",
                file=file,
                fix=f"give {setting} a different port than {first_setting} ({port})",
                value=port,
            ))
        else:
            seen[port] = (setting, file)

    # -- 3. executor_count -------------------------------------------------
    count = admin.executor_count
    if count is None:
        errors.append(_error(
            "admin.executor_count",
            "executor_count is not set; it must be an integer >= 1",
            file=config_file,
            fix="set admin.executor_count to the number of executor instances to render",
        ))
    elif isinstance(count, bool) or not isinstance(count, int):
        errors.append(_error(
            "admin.executor_count",
            f"executor_count must be an integer >= 1, got {count!r}",
            file=config_file,
            fix="set admin.executor_count to an integer >= 1",
            value=count,
        ))
    elif count < 1:
        errors.append(_error(
            "admin.executor_count",
            f"executor_count={count} is less than 1",
            file=config_file,
            fix="set admin.executor_count to an integer >= 1",
            value=count,
        ))

    # -- 4. required tokens ------------------------------------------------
    token_values = {
        "clams.api_token": clams.api_token,
        "clams.claudio_planner_token": clams.claudio_planner_token,
        "clams.claudio_executor_token": clams.claudio_executor_token,
    }
    for setting, value in token_values.items():
        text = _text(value)
        if text is None:
            errors.append(_error(
                setting,
                f"{_env_name(setting)} is required but not set",
                file=env_file,
                fix=f"generate one with `clams-admin secret` and set {_env_name(setting)} in {env_file}",
            ))
        elif text == "":
            errors.append(_error(
                setting,
                f"{_env_name(setting)} is set but empty; a required token cannot be empty",
                file=env_file,
                fix=f"set {_env_name(setting)} to a non-empty token in {env_file}",
                value=value,
            ))

    agent_tokens: list[tuple[str, str, Any]] = [
        ("planner.auth_token", planner_env_file, deployment.planner.auth_token)
    ]
    for index, instance in enumerate(deployment.executors):
        label = _label(instance, index)
        agent_tokens.append((
            f"executors[{label}].auth_token",
            executor_env_file,
            _effective(instance.agent.auth_token, deployment.executor_defaults.agent.auth_token),
        ))
    for setting, file, value in agent_tokens:
        text = _text(value)
        if text is None:
            errors.append(_error(
                setting,
                f"server.auth_token is required but not set ({setting})",
                file=file,
                fix=f"set CLAUDIO__SERVER__AUTH_TOKEN in {file} to the same value Clams uses",
            ))
        elif text == "":
            errors.append(_error(
                setting,
                f"server.auth_token is set but empty ({setting}); the agent must require a token",
                file=file,
                fix=f"set a non-empty auth token in {file}",
                value=value,
            ))

    # -- 5. executor approval_mode ----------------------------------------
    defaults_mode = deployment.executor_defaults.approval_mode
    for index, instance in enumerate(deployment.executors):
        label = _label(instance, index)
        setting = f"executors[{label}].approval_mode"
        mode = _effective(instance.agent.approval_mode, _effective(defaults_mode, instance.agent.approval_mode))
        if mode is None:
            errors.append(_error(
                setting,
                f"executor approval_mode is not set for {label}; the executor must run with approval_mode=off",
                file=executor_env_file,
                fix=f"set CLAUDIO__AGENT__APPROVAL_MODE=off in {executor_env_file}",
            ))
        elif mode != "off":
            errors.append(_error(
                setting,
                f"executor approval_mode must be 'off' for {label}, got {mode!r}",
                file=executor_env_file,
                fix=f"set CLAUDIO__AGENT__APPROVAL_MODE=off for {label} in {executor_env_file}",
                value=mode,
            ))
    if not deployment.executors and defaults_mode not in (None, "off"):
        errors.append(_error(
            "executor_defaults.approval_mode",
            f"executor approval_mode must be 'off', got {defaults_mode!r}",
            file=executor_env_file,
            fix=f"set CLAUDIO__AGENT__APPROVAL_MODE=off in {executor_env_file}",
            value=defaults_mode,
        ))

    # -- 6. Clams token <-> agent auth_token agreement ---------------------
    _check_token_match(
        errors,
        clams_setting="clams.claudio_planner_token",
        agent_setting="planner.auth_token",
        clams_value=clams.claudio_planner_token,
        agent_value=deployment.planner.auth_token,
        agent_file=planner_env_file,
        env_name="CLAMS_CLAUDIO_PLANNER_TOKEN",
    )
    for index, instance in enumerate(deployment.executors):
        label = _label(instance, index)
        _check_token_match(
            errors,
            clams_setting="clams.claudio_executor_token",
            agent_setting=f"executors[{label}].auth_token",
            clams_value=clams.claudio_executor_token,
            agent_value=_effective(instance.agent.auth_token,
                                   deployment.executor_defaults.agent.auth_token),
            agent_file=executor_env_file,
            env_name="CLAMS_CLAUDIO_EXECUTOR_TOKEN",
            label=label,
        )

    return errors


def _env_name(setting: str) -> str:
    """``clams.api_token`` -> ``CLAMS_API_TOKEN``."""
    return "CLAMS_" + setting.split(".", 1)[-1].upper()


def _check_token_match(
    errors: list[ValidationError],
    *,
    clams_setting: str,
    agent_setting: str,
    clams_value: Any,
    agent_value: Any,
    agent_file: str,
    env_name: str,
    label: str = "planner",
) -> None:
    """Both sides must be set and equal, or Clams gets 401 on every call."""
    left, right = _text(clams_value), _text(agent_value)
    if not left or not right:
        return  # already reported as missing
    if left != right:
        errors.append(_error(
            clams_setting,
            f"{env_name} does not match {agent_setting} (server.auth_token) for {label}; "
            f"the agent will reject every request with 401",
            file=agent_file,
            fix=f"make {env_name} in {ENV_FILE} and CLAUDIO__SERVER__AUTH_TOKEN in {agent_file} identical",
        ))


# --------------------------------------------------------------------------
# convenience
# --------------------------------------------------------------------------
def validate_root(root: Path | str, **kwargs: Any) -> list[ValidationError]:
    """Load ``<root>/.clams-admin/config.json`` and validate it."""
    from .adminconfig import load_deployment

    return validate_deployment(load_deployment(root), **kwargs)


def has_errors(errors: Iterable[ValidationError]) -> bool:
    return any(True for _ in errors)


def format_errors(errors: Sequence[ValidationError]) -> str:
    """Render every error the way the CLI prints them."""
    return "\n".join("\n".join(err.as_lines()) for err in errors)
