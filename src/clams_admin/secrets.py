"""Secret generation, masking and a leak-proof string wrapper.

Three jobs:

``generate_secret()``
    Cryptographically strong URL-safe token (``secrets.token_urlsafe``) used
    for ``CLAMS_API_TOKEN`` and the Claudio agent ``server.auth_token`` values.

``redact(text, secrets)``
    Replace **every** registered secret in ``text`` with ``****``. Used before
    any config summary, diff or error message is printed or logged.

``SecretStr``
    A str-like wrapper whose ``repr``/``str`` can never leak the value. Only
    ``get_secret_value()`` reveals it, so an accidental ``f"{settings}"`` or a
    traceback that prints a dataclass stays safe.

Stdlib-only.
"""
from __future__ import annotations

import secrets as _secrets
import threading
from typing import Iterable, Mapping, Optional

__all__ = [
    "MASK",
    "SecretStr",
    "generate_secret",
    "redact",
    "register_secret",
    "registered_secrets",
    "clear_registered_secrets",
    "as_secret",
]

#: The literal that replaces every secret in redacted output.
MASK = "****"

_REGISTRY_LOCK = threading.Lock()
_REGISTRY: list[str] = []


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------
def generate_secret(nbytes: int = 32) -> str:
    """URL-safe token from the OS CSPRNG (``secrets.token_urlsafe``).

    ``nbytes=32`` -> 43 characters, the usual shape for a bearer token.
    """
    if not isinstance(nbytes, int) or isinstance(nbytes, bool) or nbytes < 1:
        raise ValueError(f"nbytes must be a positive integer, got {nbytes!r}")
    return _secrets.token_urlsafe(nbytes)


# --------------------------------------------------------------------------
# registry of known secrets
# --------------------------------------------------------------------------
def register_secret(value: Optional[str]) -> Optional[str]:
    """Remember a secret so :func:`redact` masks it even when not passed in."""
    if not value:
        return value
    with _REGISTRY_LOCK:
        if value not in _REGISTRY:
            _REGISTRY.append(value)
    return value


def registered_secrets() -> list[str]:
    with _REGISTRY_LOCK:
        return list(_REGISTRY)


def clear_registered_secrets() -> None:
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


# --------------------------------------------------------------------------
# SecretStr
# --------------------------------------------------------------------------
class SecretStr:
    """Hold a secret without ever printing it.

    >>> s = SecretStr("hunter2")
    >>> repr(s)
    "SecretStr('****')"
    >>> str(s)
    '****'
    >>> s.get_secret_value()
    'hunter2'
    """

    __slots__ = ("_value",)

    def __init__(self, value: object = "") -> None:
        if isinstance(value, SecretStr):
            real = value._value
        elif value is None:
            real = ""
        elif isinstance(value, str):
            real = value
        else:
            real = str(value)
        object.__setattr__(self, "_value", real)
        register_secret(real)

    # -- reveal ------------------------------------------------------------
    def get_secret_value(self) -> str:
        """The only supported way to read the plaintext."""
        return self._value

    #: Pydantic-compatible alias.
    reveal = get_secret_value

    def for_redaction(self) -> str:
        return self._value

    @property
    def is_empty(self) -> bool:
        return self._value == ""

    # -- hiding ------------------------------------------------------------
    def __repr__(self) -> str:  # never the plaintext
        return f"SecretStr({MASK!r})"

    def __str__(self) -> str:
        return MASK

    def __format__(self, format_spec: str) -> str:
        return MASK

    def __fspath__(self) -> str:  # pragma: no cover - defensive
        return MASK

    # -- comparison / hashing by value ------------------------------------
    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretStr):
            return self._value == other._value
        if isinstance(other, str):
            return self._value == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)


def as_secret(value: object) -> Optional[SecretStr]:
    """Coerce ``value`` to :class:`SecretStr`, keeping ``None`` as *unset*."""
    if value is None:
        return None
    if isinstance(value, SecretStr):
        return value
    return SecretStr(value)


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------
def _iter_candidates(secrets: Optional[Iterable[object] | Mapping[str, object]]) -> list[str]:
    values: list[str] = []
    if isinstance(secrets, Mapping):
        values.extend(str(v) for v in secrets.values())
    elif secrets is not None:
        values.extend(str(v) for v in secrets)
    values.extend(registered_secrets())
    # Longest first so a secret that contains another is masked completely.
    uniq = {v for v in values if v}
    return sorted(uniq, key=lambda v: (-len(v), v))


def redact(text: object, secrets: Optional[Iterable[object] | Mapping[str, object]] = None) -> str:
    """Replace every occurrence of every known secret in ``text`` with ``****``.

    ``secrets`` may be a list of values or a ``{name: value}`` mapping (the
    values are what get masked). Secrets registered earlier with
    :func:`register_secret` are always included.
    """
    if text is None:
        return ""
    body = text if isinstance(text, str) else str(text)
    for value in _iter_candidates(secrets):
        body = body.replace(value, MASK)
    return body
