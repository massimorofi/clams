"""A **stdlib-only** structural index of a docker-compose document.

The admin tool must be verifiable in a container that has nothing but
``python3``/``pytest``/``git``/``grep``, so tests cannot import PyYAML to assert
topology. This module reads back what :mod:`clams_admin.compose_gen` wrote with
an indentation scanner that understands exactly the YAML subset compose uses
here: block mappings, block sequences, flow sequences (``[a, b]``), flow
mappings (``{condition: service_healthy}``), quoted scalars, comments and
``<<: *anchor`` merge keys.

Public surface (each function accepts either the document text or a
:class:`ComposeIndex`):

``services(doc)``                 -> ordered service names
``ports(doc, service=None)``      -> published ``"[[addr:]host:]container"`` strings
``env_files(doc, service)``       -> ``env_file`` entries
``volumes(doc, service=None)``    -> a service's mounts, or the top-level named volumes
``depends_on(doc, service)``      -> ``{dependency: condition}``
``image(doc, service)``           -> the service image string

plus ``environment``, ``merged_environment``, ``host_ports`` and
``published_host_ports`` for the checks the acceptance criteria care about.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Union

__all__ = [
    "ComposeIndex",
    "index",
    "services",
    "ports",
    "env_files",
    "volumes",
    "depends_on",
    "image",
    "environment",
    "merged_environment",
    "host_ports",
    "published_host_ports",
    "service_labels",
]

Document = Union[str, "ComposeIndex"]


# --------------------------------------------------------------------------
# lexing helpers
# --------------------------------------------------------------------------
def _strip_comment(line: str) -> str:
    """Remove a trailing ``#`` comment that is not inside quotes."""
    out: list[str] = []
    quote: Optional[str] = None
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


def _scalar(text: str) -> Any:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", "~", ""}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _split_flow(body: str) -> list[str]:
    """Split ``a, b`` on top-level commas (quotes and nesting aware)."""
    parts: list[str] = []
    depth = 0
    quote: Optional[str] = None
    current: list[str] = []
    for char in body:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            current.append(char)
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    if "".join(current).strip():
        parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _parse_flow(text: str) -> Any:
    """Parse ``[...]`` / ``{...}`` (possibly nested) into lists / dicts."""
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_parse_flow(part) for part in _split_flow(inner)] if inner else []
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        out: dict[str, Any] = {}
        if not inner:
            return out
        for part in _split_flow(inner):
            key, sep, rest = part.partition(":")
            if not sep:
                out[_scalar(key).strip() if isinstance(_scalar(key), str) else str(_scalar(key))] = {}
            else:
                out[str(_scalar(key))] = _parse_flow(rest)
        return out
    return _scalar(text)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


class _Line:
    __slots__ = ("indent", "text")

    def __init__(self, indent: int, text: str) -> None:
        self.indent = indent
        self.text = text


def _balanced(text: str) -> bool:
    """True when every ``[``/``{`` opened in *text* is closed (quotes aware)."""
    depth = 0
    quote: Optional[str] = None
    for char in text:
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
    return depth <= 0


def _lines(text: str) -> list[_Line]:
    """Logical lines: comments dropped, flow collections joined back together."""
    out: list[_Line] = []
    pending: Optional[_Line] = None
    for raw in text.splitlines():
        stripped = _strip_comment(raw.rstrip())
        if not stripped.strip():
            continue
        if pending is not None:
            pending.text += " " + stripped.strip()
            if _balanced(pending.text):
                out.append(pending)
                pending = None
            continue
        line = _Line(_indent(stripped), stripped.strip())
        if _balanced(line.text):
            out.append(line)
        else:
            pending = line
    if pending is not None:
        out.append(pending)
    return out


def _parse(lines: list[_Line], start: int, indent: int) -> tuple[Any, int]:
    """Parse a block starting at *start* with column *indent*."""
    if start >= len(lines):
        return {}, start
    if lines[start].text.startswith("- "):
        return _parse_sequence(lines, start, indent)
    return _parse_mapping(lines, start, indent)


def _parse_sequence(lines: list[_Line], start: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if line.indent != indent or not line.text.startswith("- "):
            break
        body = line.text[2:].strip()
        key, sep, rest = _split_key(body)
        if sep and not body.startswith(("[", "{", '"', "'")) and not _is_url(body):
            entry: dict[str, Any] = {}
            if rest.strip():
                entry[key.strip()] = _parse_flow(rest)
                items.append(entry)
                index += 1
                continue
            nxt = lines[index + 1] if index + 1 < len(lines) else None
            if nxt and nxt.indent > indent:
                child, index = _parse(lines, index + 1, nxt.indent)
                entry[key.strip()] = child
                items.append(entry)
                continue
            entry[key.strip()] = None
            items.append(entry)
            index += 1
            continue
        items.append(_parse_flow(body))
        index += 1
    return items, index


def _is_url(body: str) -> bool:
    return "://" in body


def _parse_mapping(lines: list[_Line], start: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    index = start
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.text.startswith("- "):
            break
        if line.indent > indent:  # stray deeper line: skip defensively
            index += 1
            continue
        key, sep, rest = _split_key(line.text)
        if not sep:
            index += 1
            continue
        key = key.strip()
        if key == "<<":
            result.setdefault("<<", []).append(rest.strip().lstrip("*"))
            index += 1
            continue
        rest = rest.strip()
        anchor_name = None
        if rest.startswith("&"):
            # ``x-clams: &clams`` - the anchor name decorates the block below it.
            anchor_name = rest[1:].strip()
            rest = ""
        if rest:
            result[key] = _parse_flow(rest)
            index += 1
            continue
        nxt = lines[index + 1] if index + 1 < len(lines) else None
        if nxt and nxt.indent > indent:
            child, index = _parse(lines, index + 1, nxt.indent)
            if anchor_name and isinstance(child, dict):
                child["__anchor__"] = anchor_name
            result[key] = child
        elif nxt and nxt.indent == indent and nxt.text.startswith("- "):
            child, index = _parse_sequence(lines, index + 1, indent)
            result[key] = child
        else:
            result[key] = None
            index += 1
    return result, index


def _split_key(text: str) -> tuple[str, str, str]:
    """Split ``key: value`` at the first top-level ``:`` (not inside quotes/flow)."""
    quote: Optional[str] = None
    depth = 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == ":" and depth == 0:
            if index + 1 >= len(text) or text[index + 1] == " ":
                return text[:index], ":", text[index + 1:]
    return text, "", ""


# --------------------------------------------------------------------------
# the index
# --------------------------------------------------------------------------
class ComposeIndex:
    """Structural view of one compose document (no PyYAML, no docker)."""

    def __init__(self, text: str) -> None:
        if isinstance(text, ComposeIndex):
            text = text.text
        self.text = text
        lines = _lines(text)
        self.document, _ = _parse(lines, 0, lines[0].indent if lines else 0)
        if not isinstance(self.document, dict):
            self.document = {}
        self.anchors: dict[str, dict[str, Any]] = {}
        for key, value in self.document.items():
            if isinstance(value, dict) and "__anchor__" in value:
                self.anchors[str(value["__anchor__"])] = value

    # -- discovery -----------------------------------------------------------
    def services(self) -> list[str]:
        """Service names in document order (``services:`` mapping keys)."""
        services = self.document.get("services")
        if not isinstance(services, dict):
            return []
        return [key for key in services if key != "__anchor__"]

    def service(self, name: str) -> dict[str, Any]:
        """The parsed mapping of one service (``{}`` when absent)."""
        services = self.document.get("services") or {}
        value = services.get(name) if isinstance(services, dict) else None
        return value if isinstance(value, dict) else {}

    def has_service(self, name: str) -> bool:
        return name in self.services()

    def networks(self) -> list[str]:
        networks = self.document.get("networks")
        if isinstance(networks, dict):
            return [key for key in networks if key != "__anchor__"]
        if isinstance(networks, list):
            return [str(item) for item in networks]
        return []

    def named_volumes(self) -> list[str]:
        return self.volumes()

    # -- per-service accessors ------------------------------------------------
    def _service_list(self, name: str, key: str) -> list[Any]:
        value = self.service(name).get(key)
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]

    def ports(self, service: Optional[str] = None) -> list[str]:
        """Published port strings. No *service* -> every service's ports."""
        names = [service] if service else self.services()
        out: list[str] = []
        for name in names:
            for entry in self._service_list(name or "", "ports"):
                out.append(str(entry))
        return out

    def env_files(self, service: str) -> list[str]:
        return [str(entry) for entry in self._service_list(service, "env_file")]

    def volumes(self, service: Optional[str] = None) -> list[str]:
        """A service's mounts, or the top-level named volumes when no service."""
        if service is None:
            top = self.document.get("volumes")
            if isinstance(top, dict):
                return [key for key in top if key != "__anchor__"]
            if isinstance(top, list):
                return [str(item) for item in top]
            return []
        return [str(entry) for entry in self._service_list(service, "volumes")]

    def depends_on(self, service: str) -> dict[str, str]:
        """``{dependency: condition}``; condition ``""`` when unspecified."""
        raw = self.service(service).get("depends_on")
        out: dict[str, str] = {}
        if isinstance(raw, dict):
            for name, value in raw.items():
                out[str(name)] = _condition_of(value)
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    for name, value in item.items():
                        out[str(name)] = _condition_of(value)
                else:
                    out[str(item)] = ""
        return out

    def image(self, service: str) -> Optional[str]:
        value = self.service(service).get("image")
        return None if value is None else str(value)

    def merged_service(self, service: str) -> dict[str, Any]:
        """The service mapping with ``<<: *anchor`` merges applied (shallow).

        Docker Compose resolves merge keys the same way YAML does: keys written
        on the service itself win, the anchor fills in the rest. Use this for
        fields a service inherits from ``x-clams`` (``restart``, ``networks``,
        ``env_file`` ...).
        """
        merged: dict[str, Any] = {}
        for anchor in self.service(service).get("<<") or []:
            block = self.anchors.get(str(anchor))
            if isinstance(block, dict):
                merged.update({k: v for k, v in block.items() if k != "__anchor__"})
        merged.update(self.service(service))
        return merged

    def environment(self, service: str) -> dict[str, str]:
        """The service's own ``environment:`` mapping (no anchor merge)."""
        raw = self.service(service).get("environment")
        return {str(k): _stringify(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def merged_environment(self, service: str) -> dict[str, str]:
        """``environment`` after applying ``<<: *anchor`` merges (shallow, as YAML does)."""
        merged: dict[str, str] = {}
        for anchor in self.service(service).get("<<") or []:
            block = self.anchors.get(str(anchor))
            if isinstance(block, dict):
                env = block.get("environment")
                if isinstance(env, dict):
                    merged.update({str(k): _stringify(v) for k, v in env.items()})
        merged.update(self.environment(service))
        return merged

    def service_labels(self, service: str) -> dict[str, str]:
        raw = self.service(service).get("labels")
        if isinstance(raw, dict):
            return {str(k): _stringify(v) for k, v in raw.items()}
        out: dict[str, str] = {}
        if isinstance(raw, list):
            for item in raw:
                text = str(item)
                key, sep, value = text.partition("=")
                out[key] = value if sep else ""
        return out

    def networks_of(self, service: str) -> list[str]:
        raw = self.service(service).get("networks")
        if isinstance(raw, dict):
            return [str(key) for key in raw]
        if isinstance(raw, list):
            return [str(item) for item in raw]
        return []

    # -- host-port helpers ----------------------------------------------------
    def host_ports(self) -> dict[str, int]:
        """``service -> published host port`` (first mapping wins)."""
        out: dict[str, int] = {}
        for name in self.services():
            for entry in self.ports(name):
                parsed = parse_port_mapping(entry)
                if parsed is not None:
                    out[name] = parsed[0]
                    break
        return out

    def published_host_ports(self) -> list[int]:
        """Every published host port, in service order (duplicates kept)."""
        return list(self.host_ports().values())

    def container_port(self, service: str) -> Optional[int]:
        for entry in self.ports(service):
            parsed = parse_port_mapping(entry)
            if parsed is not None:
                return parsed[1]
        return None


def _condition_of(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("condition") or "")
    if value is None:
        return ""
    return str(value)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def parse_port_mapping(entry: str) -> Optional[tuple[int, int]]:
    """``"127.0.0.1:1072:8100"`` / ``"1072:8100"`` / ``"8100"`` -> ``(host, container)``.

    A UDP suffix (``8100/udp``) is ignored. ``None`` when unparsable.
    """
    text = str(entry).strip().strip("\"'")
    if not text:
        return None
    if "/" in text:
        text = text.split("/", 1)[0]
    parts = text.split(":")
    try:
        if len(parts) == 1:
            return (int(parts[0]), int(parts[0]))
        if len(parts) == 2:
            return (int(parts[0]), int(parts[1]))
        return (int(parts[-2]), int(parts[-1]))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# module-level API (accepts text or an index)
# --------------------------------------------------------------------------
def index(doc: Document) -> ComposeIndex:
    """Return *doc* as a :class:`ComposeIndex`, parsing it when given text."""
    return doc if isinstance(doc, ComposeIndex) else ComposeIndex(doc)


def services(doc: Document) -> list[str]:
    return index(doc).services()


def ports(doc: Document, service: Optional[str] = None) -> list[str]:
    return index(doc).ports(service)


def env_files(doc: Document, service: str) -> list[str]:
    return index(doc).env_files(service)


def volumes(doc: Document, service: Optional[str] = None) -> list[str]:
    return index(doc).volumes(service)


def depends_on(doc: Document, service: str) -> dict[str, str]:
    return index(doc).depends_on(service)


def image(doc: Document, service: str) -> Optional[str]:
    return index(doc).image(service)


def environment(doc: Document, service: str) -> dict[str, str]:
    return index(doc).environment(service)


def merged_environment(doc: Document, service: str) -> dict[str, str]:
    return index(doc).merged_environment(service)


def service_labels(doc: Document, service: str) -> dict[str, str]:
    return index(doc).service_labels(service)


def host_ports(doc: Document) -> dict[str, int]:
    return index(doc).host_ports()


def published_host_ports(doc: Document) -> list[int]:
    return index(doc).published_host_ports()
