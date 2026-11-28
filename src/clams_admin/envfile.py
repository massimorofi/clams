"""Parse and serialise ``.env``-style files **without losing anything**.

The admin tool rewrites ``.env``, ``.env.claudio-planner`` and
``.env.claudio-executor`` in place, so a naive ``KEY=value`` writer would
destroy the operator's comments, section headers, blank lines and key order —
which is exactly the context that makes those files usable by hand.

: class:`EnvFile` keeps the file as an ordered list of lines. Every line that
is not a ``KEY=value`` pair (comments, blank lines, unparseable leftovers) is
kept verbatim and re-emitted unchanged, so ``parse(text).text() == text``.

Two states are distinguished everywhere in this package:

* **unset** — the key is *absent* from the file. ``get()`` returns ``None``.
* **explicitly empty** — the key is present as ``KEY=``. ``get()`` returns
  ``''``.

Writing ``None`` removes the key; writing ``''`` keeps it as ``KEY=``.

Stdlib-only, like the rest of :mod:`clams_admin`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional

__all__ = [
    "EnvLine",
    "EnvFile",
    "parse",
    "loads",
    "load",
    "dumps",
    "dump",
]

#: ``KEY=value`` with an optional ``export`` prefix and optional whitespace.
_PAIR_RE = re.compile(r"^(?P<indent>[ \t]*)(?:export[ \t]+)?(?P<key>[A-Za-z_][A-Za-z0-9_.]*)[ \t]*=[ \t]?(?P<value>.*)$")
_RAW_PAIR_RE = re.compile(r"^(?P<indent>[ \t]*)(?:export[ \t]+)?(?P<key>[A-Za-z_][A-Za-z0-9_.]*)[ \t]*$")


def _split_inline_comment(value: str) -> tuple[str, str]:
    """Split an unquoted value into ``(value, inline_comment)``.

    A ``#`` only starts a comment when preceded by whitespace, matching the
    behaviour of docker-compose / python-dotenv for unquoted values. The
    whitespace run in front of the ``#`` belongs to the comment so the file
    re-renders byte for byte.
    """
    quote: str | None = None
    for index, char in enumerate(value):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        if char == "#" and index > 0 and value[index - 1] in " \t":
            start = index - 1
            while start > 0 and value[start - 1] in " \t":
                start -= 1
            return value[:start].rstrip(), value[start:]
    return value, ""


@dataclass
class EnvLine:
    """One physical line of a ``.env`` file."""

    kind: str
    """``pair`` | ``comment`` | ``blank`` | ``raw``."""

    key: str = ""
    value: Optional[str] = None
    """``None`` only for non-pair kinds. ``''`` means *explicitly empty*."""

    inline_comment: str = ""
    indent: str = ""
    quoted: bool = False
    raw: str = ""
    """Original text, preserved verbatim for non-pair lines."""

    # -- rendering ---------------------------------------------------------
    def render(self) -> str:
        if self.kind != "pair":
            return self.raw
        body = f"{self.key}={self._render_value()}{self.inline_comment}"
        return f"{self.indent}{body}"

    def _render_value(self) -> str:
        text = "" if self.value is None else str(self.value)
        if self.quoted:
            return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return text


@dataclass
class EnvFile:
    """An ordered, comment-preserving ``.env`` document."""

    lines: list[EnvLine] = field(default_factory=list)
    path: Optional[Path] = None
    trailing_newline: bool = True

    # -- construction ------------------------------------------------------
    @classmethod
    def parse(cls, text: str, *, path: Optional[Path | str] = None) -> "EnvFile":
        lines: list[EnvLine] = []
        body = text
        trailing = body.endswith("\n") or body == ""
        if body.endswith("\n"):
            body = body[:-1]
        for raw in body.split("\n"):
            lines.append(_parse_line(raw))
        doc = cls(lines=lines, path=Path(path) if path else None, trailing_newline=trailing)
        return doc

    # ``loads`` mirrors the json module naming used elsewhere in the package.
    loads = parse

    @classmethod
    def load(cls, path: Path | str) -> "EnvFile":
        p = Path(path)
        return cls.parse(p.read_text(encoding="utf-8"), path=p)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> "EnvFile":
        doc = cls(lines=[])
        doc.update(mapping)
        return doc

    # -- access ------------------------------------------------------------
    def keys(self) -> list[str]:
        return [line.key for line in self.lines if line.kind == "pair"]

    def items(self) -> list[tuple[str, str]]:
        return [(line.key, line.value or "") for line in self.lines if line.kind == "pair"]

    def as_dict(self) -> dict[str, str]:
        """Present keys only. An absent key is *unset* and simply missing."""
        return dict(self.items())

    def has(self, key: str) -> bool:
        return any(line.kind == "pair" and line.key == key for line in self.lines)

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Return the raw string value, ``default`` when the key is unset."""
        for line in self.lines:
            if line.kind == "pair" and line.key == key:
                return line.value if line.value is not None else ""
        return default

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.has(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def __len__(self) -> int:
        return len(self.keys())

    # -- mutation ----------------------------------------------------------
    def set(self, key: str, value: Optional[object]) -> None:
        """Set ``key``. ``None`` means *unset* and removes the line entirely."""
        if value is None:
            self.remove(key)
            return
        text = value if isinstance(value, str) else _scalar_to_text(value)
        for line in self.lines:
            if line.kind == "pair" and line.key == key:
                line.value = text
                return
        self.lines.append(EnvLine(kind="pair", key=key, value=text))

    def update(self, mapping: Mapping[str, Optional[object]]) -> None:
        for key, value in mapping.items():
            self.set(key, value)

    def remove(self, key: str) -> bool:
        """Delete a key (and nothing else). Returns True when it was present."""
        removed = False
        kept: list[EnvLine] = []
        for line in self.lines:
            if line.kind == "pair" and line.key == key:
                removed = True
                continue
            kept.append(line)
        self.lines = kept
        return removed

    def set_section(self, header: str) -> None:
        """Append a commented section header (used when creating a new file)."""
        rule = f"# --- {header} " + "-" * max(0, 70 - len(header))
        self.lines.append(EnvLine(kind="comment", raw=rule))

    # -- serialisation -----------------------------------------------------
    def text(self) -> str:
        if not self.lines:
            return ""
        body = "\n".join(line.render() for line in self.lines)
        return body + ("\n" if self.trailing_newline else "")

    dumps = text

    def save(self, path: Optional[Path | str] = None) -> Path:
        target = Path(path) if path is not None else self.path
        if target is None:  # pragma: no cover - defensive
            raise ValueError("EnvFile.save needs a path")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.text(), encoding="utf-8")
        self.path = target
        return target


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _scalar_to_text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _parse_line(raw: str) -> EnvLine:
    stripped = raw.strip()
    if not stripped:
        return EnvLine(kind="blank", raw=raw)
    if stripped.startswith("#"):
        return EnvLine(kind="comment", raw=raw)

    match = _PAIR_RE.match(raw)
    if match:
        indent = match.group("indent")
        key = match.group("key")
        value = match.group("value")
        quoted = False
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            quoted = True
            inner = value[1:-1]
            if value[0] == '"':
                inner = inner.replace('\\"', '"').replace("\\\\", "\\")
            return EnvLine(kind="pair", key=key, value=inner, indent=indent, quoted=quoted, raw=raw)
        value, inline = _split_inline_comment(value)
        return EnvLine(kind="pair", key=key, value=value, inline_comment=inline, indent=indent, raw=raw)

    bare = _RAW_PAIR_RE.match(raw)
    if bare:
        # ``KEY`` with no ``=``: treat as present-but-empty so it is not lost.
        return EnvLine(kind="pair", key=bare.group("key"), value="", indent=bare.group("indent"), raw=raw)
    return EnvLine(kind="raw", raw=raw)


# --------------------------------------------------------------------------
# module-level convenience functions
# --------------------------------------------------------------------------
def parse(text: str, *, path: Optional[Path | str] = None) -> EnvFile:
    return EnvFile.parse(text, path=path)


loads = parse


def load(path: Path | str) -> EnvFile:
    return EnvFile.load(path)


def dumps(doc: EnvFile) -> str:
    return doc.text()


def dump(doc: EnvFile, path: Path | str) -> Path:
    return doc.save(path)


def merge_defaults(doc: EnvFile, defaults: Iterable[tuple[str, Optional[str]]]) -> EnvFile:
    """Add missing keys from ``defaults`` without reordering existing ones."""
    for key, value in defaults:
        if not doc.has(key) and value is not None:
            doc.set(key, value)
    return doc
