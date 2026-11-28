"""Error types for the CLAMS admin tooling.

Every failure raised by :mod:`clams_admin` carries enough context for an
operator (or another agent) to fix it without reading the source: which
*setting* is at fault, which *file* it lives in, and the *corrective action*
to take.

This module is stdlib-only by design.
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "AdminError",
    "ConfigFileError",
    "SettingValidationError",
    "ConfirmationRequired",
    "format_error",
]


class AdminError(Exception):
    """Base error for all admin operations.

    Parameters
    ----------
    message:
        Short human readable summary.
    setting:
        Dotted setting name the error refers to (e.g. ``api.port``).
    file:
        Path of the file that should be edited to fix the problem.
    corrective_action:
        Concrete instruction, e.g. ``"set api.port to an integer between 1 and 65535"``.
    exit_code:
        Process exit status the CLI should return for this error class.
    """

    exit_code: int = 2

    def __init__(
        self,
        message: str,
        *,
        setting: Optional[str] = None,
        file: Optional[str] = None,
        corrective_action: Optional[str] = None,
        exit_code: Optional[int] = None,
        details: Optional[list[str]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.setting = setting
        self.file = str(file) if file is not None else None
        self.corrective_action = corrective_action
        if exit_code is not None:
            self.exit_code = exit_code
        self.details = list(details or [])

    # -- rendering ---------------------------------------------------------
    def as_lines(self) -> list[str]:
        """Return the error as printable lines (used for stderr output)."""
        lines = [f"error: {self.message}"]
        if self.setting:
            lines.append(f"  setting: {self.setting}")
        if self.file:
            lines.append(f"  file:    {self.file}")
        if self.corrective_action:
            lines.append(f"  fix:     {self.corrective_action}")
        for detail in self.details:
            lines.append(f"  - {detail}")
        return lines

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "\n".join(self.as_lines())


class ConfigFileError(AdminError):
    """The configuration file is missing, unreadable or malformed."""

    exit_code = 2


class SettingValidationError(AdminError):
    """A single setting value is invalid."""

    exit_code = 2

    def __init__(
        self,
        setting: str,
        message: str,
        *,
        file: Optional[str] = None,
        corrective_action: Optional[str] = None,
        value: Any = None,
    ) -> None:
        super().__init__(
            message,
            setting=setting,
            file=file,
            corrective_action=corrective_action,
        )
        self.value = value


class ConfirmationRequired(AdminError):
    """The command would change state but was run without ``--yes``."""

    exit_code = 3


def format_error(err: BaseException) -> str:
    """Render any exception the way the CLI renders :class:`AdminError`."""
    if isinstance(err, AdminError):
        return "\n".join(err.as_lines())
    return f"error: {type(err).__name__}: {err}"
