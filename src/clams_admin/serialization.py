"""Serialisation helpers with **lazy** optional dependencies.

``tomllib`` is in the stdlib (Python >= 3.11) so TOML is the primary format.
For YAML files we try :mod:`yaml` (PyYAML) *inside the function*; when it is
not installed we fall back to a small reader/writer that understands the
subset of YAML this package emits (nested mappings, scalars, inline lists and
block lists).

Nothing in this module imports a third-party package at import time.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, Mapping

__all__ = ["load_document", "dump_document", "has_pyyaml"]


def has_pyyaml() -> bool:
    """True when PyYAML is importable (checked lazily, result not cached)."""
    try:
        import yaml  # noqa: F401  (lazy optional dependency)
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------
# TOML (stdlib)
# --------------------------------------------------------------------------
def _scalar_to_toml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar_to_toml(v) for v in value) + "]"
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dump_toml(data: Mapping[str, Any]) -> str:
    """Dump a mapping of scalars / lists / nested mappings as TOML text."""
    lines: list[str] = []
    tables: list[tuple[str, Mapping[str, Any]]] = []

    for key, value in data.items():
        if isinstance(value, Mapping):
            tables.append((str(key), value))
        else:
            lines.append(f"{key} = {_scalar_to_toml(value)}")

    for name, table in tables:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"[{name}]")
        for key, value in table.items():
            lines.append(f"{key} = {_scalar_to_toml(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def load_toml(text: str) -> dict[str, Any]:
    return tomllib.loads(text)


# --------------------------------------------------------------------------
# YAML (optional dependency, stdlib fallback)
# --------------------------------------------------------------------------
def _yaml_scalar(text: str) -> Any:
    text = text.strip()
    if not text:
        return ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", "~"}:
        return None
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_yaml_scalar(part) for part in inner.split(",")]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _strip_comment(line: str) -> str:
    out: list[str] = []
    quote: str | None = None
    for index, char in enumerate(line):
        if quote:
            out.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        if char == "#" and (index == 0 or line[index - 1] in " \t"):
            break
        out.append(char)
    return "".join(out).rstrip()


def load_yaml_fallback(text: str) -> Any:
    """Parse the YAML subset emitted by :func:`dump_yaml_fallback`.

    Supports nested block mappings, block lists of scalars, inline lists and
    comments. Used only when PyYAML is not installed.
    """
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = _strip_comment(raw.rstrip())
        if not line.strip():
            continue
        lines.append((len(line) - len(line.lstrip(" ")), line.strip()))

    def parse_list(index: int, indent: int) -> tuple[list[Any], int]:
        values: list[Any] = []
        while index < len(lines):
            ind, body = lines[index]
            if ind != indent or not body.startswith("- "):
                break
            values.append(_yaml_scalar(body[2:]))
            index += 1
        return values, index

    def parse(index: int, indent: int) -> tuple[dict[str, Any], int]:
        result: dict[str, Any] = {}
        while index < len(lines):
            ind, body = lines[index]
            if ind < indent or body.startswith("- "):
                break
            if ind > indent:
                raise ValueError(f"unexpected indentation on line: {body!r}")
            key, sep, rest = body.partition(":")
            if not sep:
                raise ValueError(f"expected 'key: value', got {body!r}")
            key = key.strip().strip("\"'")
            rest = rest.strip()
            if rest:
                result[key] = _yaml_scalar(rest)
                index += 1
                continue
            nxt = lines[index + 1] if index + 1 < len(lines) else None
            if nxt and nxt[0] > ind:
                if nxt[1].startswith("- "):
                    values, index = parse_list(index + 1, nxt[0])
                    result[key] = values
                else:
                    child, index = parse(index + 1, nxt[0])
                    result[key] = child
            else:
                result[key] = {}
                index += 1
        return result, index

    if not lines:
        return {}
    data, _ = parse(0, lines[0][0])
    return data


def dump_yaml_fallback(data: Any, _indent: int = 0) -> str:
    """Serialise nested mappings / lists / scalars as simple YAML."""
    pad = " " * _indent
    lines: list[str] = []
    if isinstance(data, Mapping):
        for key, value in data.items():
            if isinstance(value, Mapping):
                lines.append(f"{pad}{key}:")
                lines.append(dump_yaml_fallback(value, _indent + 2).rstrip("\n"))
            elif isinstance(value, (list, tuple)):
                if not value:
                    lines.append(f"{pad}{key}: []")
                else:
                    lines.append(f"{pad}{key}:")
                    for item in value:
                        lines.append(f"{pad}  - {_scalar_to_yaml(item)}")
            else:
                lines.append(f"{pad}{key}: {_scalar_to_yaml(value)}")
    elif isinstance(data, (list, tuple)):
        for item in data:
            lines.append(f"{pad}- {_scalar_to_yaml(item)}")
    else:
        lines.append(f"{pad}{_scalar_to_yaml(data)}")
    return "\n".join(lines) + "\n"


def _scalar_to_yaml(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar_to_yaml(v) for v in value) + "]"
    text = str(value)
    if text == "" or any(ch in text for ch in ":#") or text != text.strip():
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def load_yaml(text: str) -> Any:
    """Parse YAML using PyYAML when available, else the stdlib fallback."""
    try:
        import yaml  # lazy optional dependency
    except Exception:
        return load_yaml_fallback(text)
    return yaml.safe_load(text) or {}


def dump_yaml(data: Any) -> str:
    """Serialise YAML using PyYAML when available, else the stdlib fallback."""
    try:
        import yaml  # lazy optional dependency
    except Exception:
        return dump_yaml_fallback(data)
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
def load_document(path: Path | str) -> dict[str, Any]:
    """Load a ``.toml`` / ``.yaml`` / ``.yml`` / ``.json`` file into a dict."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        data = load_yaml(text)
    elif suffix == ".json":
        data = json.loads(text) if text.strip() else {}
    else:
        data = load_toml(text)
    if not isinstance(data, dict):
        raise ValueError(f"{p} must contain a mapping at the top level")
    return data


def dump_document(path: Path | str, data: Mapping[str, Any]) -> str:
    """Serialise ``data`` for ``path`` based on the file suffix."""
    suffix = Path(path).suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return dump_yaml(data)
    if suffix == ".json":
        return json.dumps(data, indent=2, sort_keys=False) + "\n"
    return dump_toml(data)
