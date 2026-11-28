"""CLAMS admin core package.

A **stdlib-only** control plane for editing, validating and applying CLAMS
runtime configuration.

Design rules (enforced by ``tests_admin/test_scaffold.py``):

* Importing :mod:`clams_admin` must not import ``yaml``, ``typer``, ``rich`` or
  ``questionary``. Optional UI/YAML dependencies are imported lazily *inside*
  functions and always have a stdlib fallback.
* Nothing at import time may touch the network, the database or the filesystem
  beyond the module itself.

Typical use::

    from clams_admin import AdminSettings, load_settings, AdminError
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .errors import (
    AdminError,
    ConfigFileError,
    ConfirmationRequired,
    SettingValidationError,
    format_error,
)

__version__ = "0.1.0"

__all__ = [
    "AdminError",
    "ConfigFileError",
    "ConfirmationRequired",
    "SettingValidationError",
    "format_error",
    "main",
    "__version__",
]

if TYPE_CHECKING:  # pragma: no cover - typing only, never executed at runtime
    from .cli import main as main


def __getattr__(name: str) -> Any:
    """Lazily expose CLI/settings helpers without importing them eagerly.

    ``cli`` pulls in ``argparse`` and the command implementations; keeping it
    behind ``__getattr__`` guarantees that ``import clams_admin`` stays as
    light as possible (no third-party module can leak into ``sys.modules``).
    """
    if name == "main":
        from .cli import main as _main

        return _main
    if name in {"AdminSettings", "load_settings", "save_settings", "effective_settings", "SCHEMA"}:
        from . import settings as _settings

        try:
            return getattr(_settings, name)
        except AttributeError as exc:  # pragma: no cover - defensive
            raise AttributeError(name) from exc
    raise AttributeError(f"module 'clams_admin' has no attribute {name!r}")
