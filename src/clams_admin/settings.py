"""Settings schema, loading, validation and diffing for CLAMS admin.

Stdlib-only. The schema below is the single source of truth used by
``configure``, ``validate``, ``preview`` and ``apply``.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from .errors import ConfigFileError, SettingValidationError
from .serialization import dump_document, load_document

__all__ = [
    "Setting",
    "SCHEMA",
    "AdminSettings",
    "defaults",
    "effective_settings",
    "load_settings",
    "save_settings",
    "validate_settings",
    "diff_settings",
    "DEFAULT_CONFIG_RELPATH",
]

DEFAULT_CONFIG_RELPATH = "config/admin.toml"


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Setting:
    """Definition of one admin-managed setting."""

    key: str
    """Dotted key, e.g. ``api.port`` -> ``{"api": {"port": ...}}``."""

    default: Any
    type: type = str
    """One of ``str``, ``int``, ``bool``, ``float`` or ``list``."""

    description: str = ""
    required: bool = True
    choices: Optional[Sequence[Any]] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    secret: bool = False
    """Secrets are never echoed back by ``preview``/``apply`` (shown as ***)."""

    @property
    def section(self) -> str:
        return self.key.split(".", 1)[0] if "." in self.key else ""

    @property
    def name(self) -> str:
        return self.key.split(".", 1)[1] if "." in self.key else self.key

    @property
    def type_name(self) -> str:
        return {str: "string", int: "int", bool: "bool", float: "float", list: "list"}[self.type]


SCHEMA: tuple[Setting, ...] = (
    Setting(
        "db.path",
        "var/clams.db",
        str,
        description="Path to the CLAMS SQLite database (relative paths resolve against --root).",
    ),
    Setting(
        "redis.url",
        "redis://127.0.0.1:6379/0",
        str,
        description="Redis URL used for the job bus.",
        choices=None,
    ),
    Setting(
        "claudio.base_url",
        "http://127.0.0.1:8787",
        str,
        description="Base URL of the Claudio agent HTTP API.",
    ),
    Setting(
        "claudio.model",
        "claude-sonnet-4-5",
        str,
        description="Default model name passed to Claudio.",
    ),
    Setting(
        "claudio.token",
        "",
        str,
        description="Bearer token for the Claudio API (optional).",
        required=False,
        secret=True,
    ),
    Setting(
        "planner.max_turns",
        60,
        int,
        description="Maximum agent turns per planning run.",
        minimum=1,
        maximum=500,
    ),
    Setting(
        "planner.timeout_seconds",
        900,
        int,
        description="Wall-clock budget for one planning run.",
        minimum=1,
        maximum=86400,
    ),
    Setting(
        "api.host",
        "127.0.0.1",
        str,
        description="Bind address for the CLAMS API.",
    ),
    Setting(
        "api.port",
        8080,
        int,
        description="Bind port for the CLAMS API.",
        minimum=1,
        maximum=65535,
    ),
    Setting(
        "admin.log_level",
        "INFO",
        str,
        description="Log level for admin-driven runs.",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    ),
    Setting(
        "admin.enabled_profiles",
        ["github_issue", "code_review"],
        list,
        description="Profiles the executor is allowed to run.",
        required=False,
    ),
)

SCHEMA_BY_KEY: dict[str, Setting] = {s.key: s for s in SCHEMA}


def defaults() -> dict[str, Any]:
    """Fresh nested dict of schema defaults."""
    data: dict[str, Any] = {}
    for setting in SCHEMA:
        _set_nested(data, setting.key, copy.deepcopy(setting.default))
    return data


def _set_nested(data: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    cursor: dict[str, Any] = data
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value


def _get_nested(data: Mapping[str, Any], key: str, default: Any = None) -> Any:
    cursor: Any = data
    for part in key.split("."):
        if not isinstance(cursor, Mapping) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor


# --------------------------------------------------------------------------
# AdminSettings container
# --------------------------------------------------------------------------
class AdminSettings:
    """Nested settings mapping with dotted-key access."""

    def __init__(self, data: Optional[Mapping[str, Any]] = None, *, source: Optional[Path] = None):
        self.data: dict[str, Any] = copy.deepcopy(dict(data or {}))
        self.source: Optional[Path] = Path(source) if source else None

    # -- access ------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return _get_nested(self.data, key, default)

    def set(self, key: str, value: Any) -> None:
        _set_nested(self.data, key, value)

    def flattened(self) -> dict[str, Any]:
        return {s.key: self.get(s.key) for s in SCHEMA}

    def merged_with_defaults(self) -> "AdminSettings":
        """Schema defaults overlaid with whatever this instance provides.

        Keys that are not part of the schema are preserved (``validate`` turns
        them into "unknown setting" errors instead of silently dropping them).
        """
        merged = defaults()

        def overlay(dst: dict[str, Any], src: Mapping[str, Any]) -> None:
            for key, value in src.items():
                if isinstance(value, Mapping) and isinstance(dst.get(key), dict):
                    overlay(dst[key], value)
                else:
                    dst[key] = copy.deepcopy(value)

        overlay(merged, self.data)
        return AdminSettings(merged, source=self.source)

    def schema_only(self) -> "AdminSettings":
        """Copy containing only keys defined by the schema (unknown keys dropped)."""
        data: dict[str, Any] = {}
        for setting in SCHEMA:
            value = self.get(setting.key, None)
            if value is not None:
                _set_nested(data, setting.key, copy.deepcopy(value))
        return AdminSettings(data, source=self.source)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)

    def __iter__(self) -> Iterator[str]:  # pragma: no cover - convenience
        return iter(self.flattened())

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AdminSettings) and other.data == self.data

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AdminSettings(source={self.source!r}, keys={sorted(self.flattened())})"


def effective_settings(data: Optional[Mapping[str, Any]] = None) -> AdminSettings:
    """Defaults overlaid with ``data`` (used by ``preview``)."""
    return AdminSettings(data).merged_with_defaults()


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------
def resolve_config_path(config: Optional[Path | str], root: Optional[Path | str] = None) -> Path:
    """Resolve ``--config`` against ``--root`` (default: cwd)."""
    root_path = Path(root).resolve() if root else Path.cwd()
    if config is None:
        return root_path / DEFAULT_CONFIG_RELPATH
    path = Path(config)
    return path if path.is_absolute() else (root_path / path)


def load_settings(path: Path | str, *, missing_ok: bool = False) -> AdminSettings:
    """Load settings from a TOML/YAML/JSON file."""
    p = Path(path)
    if not p.exists():
        if missing_ok:
            return AdminSettings({}, source=p)
        raise ConfigFileError(
            f"config file not found: {p}",
            file=str(p),
            corrective_action=(
                "create it with `clams-admin configure --config "
                f"{p}` (add --yes to write without prompting)"
            ),
        )
    if p.is_dir():
        raise ConfigFileError(
            f"config path is a directory: {p}",
            file=str(p),
            corrective_action="point --config at a .toml, .yaml or .json file",
        )
    try:
        data = load_document(p)
    except Exception as exc:  # malformed file of any supported format
        raise ConfigFileError(
            f"could not parse config file: {p}",
            file=str(p),
            corrective_action=f"fix the syntax ({type(exc).__name__}: {exc})",
        ) from exc
    return AdminSettings(data, source=p)


def save_settings(path: Path | str, settings: AdminSettings) -> Path:
    """Atomically write ``settings`` to ``path`` (creates parent dirs)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = dump_document(p, settings.as_dict())
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return p


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def _coerce(setting: Setting, value: Any) -> tuple[bool, Any]:
    """Return (ok, coerced_value). Booleans are strict: 1 is not a bool."""
    if setting.type is bool:
        return (isinstance(value, bool), value)
    if setting.type is int:
        if isinstance(value, bool):
            return False, value
        if isinstance(value, int):
            return True, value
        if isinstance(value, str):
            try:
                return True, int(value.strip())
            except ValueError:
                return False, value
        return False, value
    if setting.type is float:
        if isinstance(value, bool):
            return False, value
        if isinstance(value, (int, float)):
            return True, float(value)
        if isinstance(value, str):
            try:
                return True, float(value.strip())
            except ValueError:
                return False, value
        return False, value
    if setting.type is list:
        if isinstance(value, (list, tuple)):
            return True, [str(v) for v in value]
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",")]
            return True, [p for p in parts if p]
        return False, value
    # str
    if isinstance(value, str):
        return True, value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True, str(value)
    return False, value


def validate_settings(settings: AdminSettings, *, coerce: bool = True) -> list[SettingValidationError]:
    """Return every problem found in ``settings`` (empty list == valid)."""
    errors: list[SettingValidationError] = []
    source = str(settings.source) if settings.source else None

    for setting in SCHEMA:
        value = settings.get(setting.key, _UNSET)
        if value is _UNSET:
            if setting.required:
                errors.append(
                    SettingValidationError(
                        setting.key,
                        "required setting is missing",
                        file=source,
                        corrective_action=(
                            f"add `{setting.key} = {setting.default!r}` "
                            "(or run `clams-admin configure`)"
                        ),
                    )
                )
            continue

        ok, coerced = _coerce(setting, value)
        if not ok:
            errors.append(
                SettingValidationError(
                    setting.key,
                    f"expected {setting.type_name}, got {type(value).__name__}",
                    file=source,
                    corrective_action=f"set {setting.key} to a {setting.type_name}",
                    value=value,
                )
            )
            continue

        if coerce:
            settings.set(setting.key, coerced)
            value = coerced

        if setting.choices and value not in list(setting.choices):
            errors.append(
                SettingValidationError(
                    setting.key,
                    f"{value!r} is not one of {', '.join(repr(c) for c in setting.choices)}",
                    file=source,
                    corrective_action=f"set {setting.key} to one of "
                    + ", ".join(repr(c) for c in setting.choices),
                    value=value,
                )
            )
        if setting.type in (int, float):
            if setting.minimum is not None and value < setting.minimum:
                errors.append(
                    SettingValidationError(
                        setting.key,
                        f"{value} is below the minimum {setting.minimum:g}",
                        file=source,
                        corrective_action=f"set {setting.key} to >= {setting.minimum:g}",
                        value=value,
                    )
                )
            if setting.maximum is not None and value > setting.maximum:
                errors.append(
                    SettingValidationError(
                        setting.key,
                        f"{value} is above the maximum {setting.maximum:g}",
                        file=source,
                        corrective_action=f"set {setting.key} to <= {setting.maximum:g}",
                        value=value,
                    )
                )
        if setting.type is str and setting.required and not str(value).strip():
            errors.append(
                SettingValidationError(
                    setting.key,
                    "must not be empty",
                    file=source,
                    corrective_action=f"set {setting.key} to a non-empty string",
                )
            )

    for key in _extra_keys(settings.data):
        errors.append(
            SettingValidationError(
                key,
                "unknown setting (not part of the admin schema)",
                file=source,
                corrective_action=f"remove `{key}` or add it to the schema in "
                "src/clams_admin/settings.py",
            )
        )
    return errors


class _Unset:
    def __repr__(self) -> str:  # pragma: no cover
        return "<unset>"


_UNSET = _Unset()


def _extra_keys(data: Mapping[str, Any], prefix: str = "") -> Iterable[str]:
    """Yield dotted keys present in ``data`` but absent from the schema."""
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if prefix == "" and f"{key}" in {s.section for s in SCHEMA if s.section}:
            if isinstance(value, Mapping):
                yield from _extra_keys(value, prefix=f"{key}.")
                continue
        if dotted not in SCHEMA_BY_KEY:
            yield dotted


# --------------------------------------------------------------------------
# Diffing (used by preview / apply)
# --------------------------------------------------------------------------
def mask(secret: bool, value: Any) -> str:
    """Render a value, hiding secrets."""
    if secret:
        return "***" if value else "<empty>"
    return repr(value)


def diff_settings(old: AdminSettings, new: AdminSettings) -> list[tuple[str, Any, Any]]:
    """Return ``[(key, old_value, new_value), ...]`` for changed settings."""
    changes: list[tuple[str, Any, Any]] = []
    old_flat = old.flattened()
    new_flat = new.flattened()
    for key in SCHEMA_BY_KEY:
        before = old_flat.get(key, _UNSET)
        after = new_flat.get(key, _UNSET)
        if before is _UNSET or before != after:
            changes.append((key, None if before is _UNSET else before, after))
    return changes


def render_settings(settings: AdminSettings, *, width: int = 26) -> str:
    """Human readable, aligned rendering of the settings."""
    lines: list[str] = []
    current_section: Optional[str] = None
    for setting in SCHEMA:
        section = setting.section
        if section != current_section:
            if current_section is not None:
                lines.append("")
            lines.append(f"[{section}]")
            current_section = section
        value = settings.get(setting.key, None)
        lines.append(f"  {setting.name:<{width}} = {mask(setting.secret, value)}")
    return "\n".join(lines).rstrip()
