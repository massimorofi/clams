"""Implementations of the ``configure``, ``validate``, ``preview`` and
``apply`` commands.

Optional UI dependencies (``rich`` for styling, ``questionary`` for prompts) are
imported **inside** the functions that use them and always degrade to plain
``print`` / ``input`` when missing.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Sequence

from .errors import AdminError, ConfirmationRequired
from .settings import (
    SCHEMA,
    SCHEMA_BY_KEY,
    AdminSettings,
    diff_settings,
    load_settings,
    mask,
    render_settings,
    resolve_config_path,
    save_settings,
    validate_settings,
)

__all__ = ["Console", "cmd_configure", "cmd_validate", "cmd_preview", "cmd_apply"]


# --------------------------------------------------------------------------
# Console (rich is optional, lazily imported)
# --------------------------------------------------------------------------
class Console:
    """Thin printing/prompting wrapper with stdlib fallbacks."""

    def __init__(self, *, interactive: bool = False, use_rich: Optional[bool] = None,
                 stream: Any = None) -> None:
        self.stream = stream or sys.stdout
        self.err_stream = sys.stderr
        self.interactive = interactive and self.stream.isatty()
        self._rich = None
        self._rich_err = None
        want_rich = use_rich if use_rich is not None else bool(self.stream.isatty())
        if want_rich:
            try:
                from rich.console import Console as _RichConsole  # lazy optional dep

                self._rich = _RichConsole(file=self.stream)
                if self.err_stream.isatty():
                    self._rich_err = _RichConsole(file=self.err_stream)
            except Exception:
                self._rich = None
                self._rich_err = None

    @property
    def styled(self) -> bool:
        return self._rich is not None

    def print(self, message: str = "", *, style: Optional[str] = None) -> None:
        if self._rich is not None:
            self._rich.print(message, style=style)
        else:
            print(message, file=self.stream)

    def print_err(self, message: str = "", *, style: Optional[str] = "red") -> None:
        """Diagnostics go to stderr so stdout stays pipeable."""
        if self._rich_err is not None:
            self._rich_err.print(message, style=style)
        else:
            print(message, file=self.err_stream)

    def rule(self, title: str) -> None:
        if self._rich is not None:
            self._rich.rule(title)
        else:
            self.print(f"--- {title} " + "-" * max(0, 60 - len(title)))

    def ask(self, question: str, *, default: Any = None, secret: bool = False,
            choices: Optional[Sequence[Any]] = None) -> Any:
        """Prompt for one value. Uses questionary when available, else input()."""
        if self.interactive:
            answer = self._ask_questionary(question, default=default, secret=secret, choices=choices)
            if answer is not None:
                return answer
        return self._ask_builtin(question, default=default, choices=choices)

    # -- optional backends -------------------------------------------------
    def _ask_questionary(self, question: str, *, default: Any, secret: bool,
                         choices: Optional[Sequence[Any]]) -> Optional[Any]:
        try:
            import questionary  # lazy optional dep
        except Exception:
            return None
        try:
            if choices:
                return questionary.select(question, choices=[str(c) for c in choices],
                                          default=str(default)).ask()
            if secret:
                return questionary.password(question, default="" if default is None else str(default)).ask()
            return questionary.text(question, default="" if default is None else str(default)).ask()
        except Exception:
            return None

    def _ask_builtin(self, question: str, *, default: Any,
                     choices: Optional[Sequence[Any]] = None) -> Any:
        hint = ""
        if choices:
            hint = f" [{'|'.join(str(c) for c in choices)}]"
        elif default not in (None, ""):
            hint = f" [{default}]"
        self.stream.write(f"{question}{hint}: ")
        self.stream.flush()
        try:
            raw = sys.stdin.readline()
        except KeyboardInterrupt:
            raise AdminError("interrupted", corrective_action="re-run the command")
        if not raw:
            raise AdminError(
                "no input available on stdin",
                corrective_action="pass --non-interactive (with --set overrides) to run unattended",
            )
        answer = raw.strip()
        return answer or ("" if default is None else str(default))

    def confirm(self, question: str, *, default: bool = False) -> bool:
        suffix = " [Y/n]" if default else " [y/N]"
        self.stream.write(question + suffix + ": ")
        self.stream.flush()
        try:
            raw = sys.stdin.readline()
        except KeyboardInterrupt:
            return False
        if not raw:
            return default
        answer = raw.strip().lower()
        if not answer:
            return default
        return answer in {"y", "yes"}


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
def _paths(args: SimpleNamespace) -> tuple[Path, Path]:
    root = Path(getattr(args, "root", None) or Path.cwd()).resolve()
    config = resolve_config_path(getattr(args, "config", None), root)
    return root, config


def _parse_overrides(pairs: Sequence[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise AdminError(
                f"invalid --set expression: {pair!r}",
                setting=key.strip() or None,
                corrective_action="use --set section.key=value",
            )
        key = key.strip()
        if key not in SCHEMA_BY_KEY:
            raise AdminError(
                f"unknown setting: {key}",
                setting=key,
                corrective_action="run `clams-admin configure --help` to list valid settings",
            )
        overrides[key] = value.strip()
    return overrides


def _coerce_raw(setting_key: str, raw: str) -> Any:
    """Turn a CLI/prompt string into the schema type for ``setting_key``."""
    from .settings import _coerce  # local import keeps module import cheap

    setting = SCHEMA_BY_KEY[setting_key]
    ok, value = _coerce(setting, raw)
    if not ok:
        raise AdminError(
            f"cannot interpret {raw!r} for {setting_key}",
            setting=setting_key,
            corrective_action=f"provide a {setting.type_name}",
        )
    return value


def _desired(
    args: SimpleNamespace,
    console: Console,
    *,
    drop_unknown: bool = False,
) -> tuple[AdminSettings, AdminSettings, Path, Path]:
    """Load current + build desired settings for configure/preview/apply."""
    root, config = _paths(args)
    current = load_settings(config, missing_ok=True)
    base = current.schema_only() if drop_unknown else AdminSettings(current.as_dict(), source=config)
    desired = AdminSettings(base.as_dict(), source=config).merged_with_defaults()

    for key, raw in _parse_overrides(getattr(args, "set", None) or []).items():
        desired.set(key, _coerce_raw(key, raw))

    if console.interactive and not getattr(args, "non_interactive", False):
        _prompt_for_values(desired, console)

    return current, desired, root, config


def _prompt_for_values(desired: AdminSettings, console: Console) -> None:
    console.rule("Configure CLAMS (Enter keeps the value in brackets)")
    for setting in SCHEMA:
        value = desired.get(setting.key, setting.default)
        answer = console.ask(
            f"{setting.key}: {setting.description}" if setting.description else setting.key,
            default="" if setting.secret else value,
            secret=setting.secret,
            choices=setting.choices,
        )
        if answer is None or (setting.secret and answer == ""):
            continue
        desired.set(setting.key, _coerce_raw(setting.key, str(answer)))


def _report_errors(errors: Sequence[AdminError], console: Console) -> None:
    for err in errors:
        console.print_err("\n".join(err.as_lines()))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_configure(args: SimpleNamespace) -> int:
    """Create or update the admin config file."""
    console = Console(interactive=not args.non_interactive)
    _current, desired, root, config = _desired(args, console, drop_unknown=True)

    problems = validate_settings(desired)
    if problems:
        _report_errors(problems, console)
        console.print_err("configure aborted; nothing was written.", style="yellow")
        return 1

    exists = config.exists()
    if exists and not args.yes and not console.interactive:
        console.print(f"note: {config} already exists; rewriting it (pass --yes to silence).",
                      style="yellow")

    save_settings(config, desired)
    console.print(f"wrote {config}")
    console.print(render_settings(desired))
    console.print(f"\n{len(SCHEMA)} settings written for root {root}.", style="dim")
    return 0


def cmd_validate(args: SimpleNamespace) -> int:
    """Validate the config file against the schema.

    By default the *effective* settings are validated (file values overlaid on
    the schema defaults), so a partial override file is valid. Pass
    ``--strict`` to additionally require every setting to be present in the file.
    """
    console = Console(interactive=False)
    root, config = _paths(args)
    settings = load_settings(config)
    target = settings if getattr(args, "strict", False) else settings.merged_with_defaults()
    problems = validate_settings(target)
    if problems:
        _report_errors(problems, console)
        console.print_err(f"\nFAIL: {config} has {len(problems)} problem(s)")
        return 1
    mode = "strict" if getattr(args, "strict", False) else "effective"
    console.print(f"OK: {config} is valid ({mode}; {len(SCHEMA)} settings checked)")
    return 0


def cmd_preview(args: SimpleNamespace) -> int:
    """Show what apply would write, without writing anything."""
    console = Console(interactive=False)
    current, desired, root, config = _desired(args, console)

    problems = validate_settings(AdminSettings(desired.as_dict(), source=config))
    console.rule(f"preview: {config}")
    console.print(render_settings(desired))
    console.rule("changes")
    changes = diff_settings(current, desired)
    if not changes:
        console.print("  (no changes)")
    for key, before, after in changes:
        secret = SCHEMA_BY_KEY[key].secret
        console.print(f"  {key}: {mask(secret, before)} -> {mask(secret, after)}")
    if problems:
        console.rule("problems")
        _report_errors(problems, console)
        return 1
    return 0


def cmd_apply(args: SimpleNamespace) -> int:
    """Validate then write the config file atomically."""
    console = Console(interactive=not args.non_interactive)
    current, desired, root, config = _desired(args, console)

    problems = validate_settings(AdminSettings(desired.as_dict(), source=config))
    if problems:
        _report_errors(problems, console)
        console.print_err("\napply refused: fix the errors above and re-run.")
        return 1

    changes = diff_settings(current, desired)
    if not changes:
        console.print(f"nothing to apply: {config} already matches the desired settings")
        return 0

    if not args.yes and console.interactive:
        for key, before, after in changes:
            secret = SCHEMA_BY_KEY[key].secret
            console.print(f"  {key}: {mask(secret, before)} -> {mask(secret, after)}")
        if not console.confirm(f"Apply {len(changes)} change(s) to {config}?"):
            raise ConfirmationRequired(
                "apply cancelled by operator",
                file=str(config),
                corrective_action="re-run with --yes to apply without prompting",
            )
    elif not args.yes and not console.interactive:
        raise ConfirmationRequired(
            f"refusing to modify {config} without confirmation",
            file=str(config),
            corrective_action="re-run with --yes (or --non-interactive --yes) to apply",
        )

    backup = None
    if config.exists():
        backup = config.with_name(config.name + ".bak")
        backup.write_bytes(config.read_bytes())

    save_settings(config, desired)
    console.print(f"applied {len(changes)} change(s) -> {config}")
    if backup is not None:
        console.print(f"previous version saved to {backup}", style="dim")
    return 0
