"""``clams-admin`` command line interface (argparse, stdlib only).

Subcommands: ``configure``, ``validate``, ``preview``, ``apply``.

Shared flags: ``--config PATH``, ``--root PATH``, ``--non-interactive``,
``--yes``. All flags are accepted before *or* after the subcommand.
"""
from __future__ import annotations

import argparse
import sys
from types import SimpleNamespace
from typing import Any, Optional, Sequence

from . import __version__
from .errors import AdminError, format_error

__all__ = ["build_parser", "main", "run"]

SUBCOMMANDS = ("configure", "validate", "preview", "apply")

_EPILOG = """\
examples:
  clams-admin configure --non-interactive --yes
  clams-admin configure --set api.port=9000 --non-interactive --yes
  clams-admin validate --config config/admin.toml
  clams-admin preview  --root /srv/clams --set planner.max_turns=40
  clams-admin apply    --yes --non-interactive

exit codes:
  0 success          1 validation failed
  2 config/usage error
  3 confirmation required (re-run with --yes)
"""


def _add_shared_flags(parser: argparse.ArgumentParser) -> None:
    """Flags shared by the top level parser and every subcommand.

    ``default=argparse.SUPPRESS`` keeps a value set on the parent parser from
    being clobbered by the subparser (and vice versa).
    """
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="path to the admin config file (default: <root>/config/admin.toml)",
    )
    parser.add_argument(
        "--root",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="repository root used to resolve relative paths (default: cwd)",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        default=argparse.SUPPRESS,
        help="never prompt; use defaults plus any --set overrides",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=argparse.SUPPRESS,
        help="assume yes and skip confirmation prompts",
    )


def _add_target_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--set",
        metavar="KEY=VALUE",
        action="append",
        default=argparse.SUPPRESS,
        help="override one setting (repeatable), e.g. --set api.port=9000",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the full argument parser."""
    parser = argparse.ArgumentParser(
        prog="clams-admin",
        description="Configure, validate and apply CLAMS runtime settings.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="show the version and exit",
    )
    _add_shared_flags(parser)

    subparsers = parser.add_subparsers(
        dest="command",
        metavar="{configure,validate,preview,apply}",
        description="run `clams-admin <command> --help` for command specific options",
    )

    configure = subparsers.add_parser(
        "configure",
        parents=[_shared_parent()],
        help="create or update the admin config file",
        description="Interactively (or non-interactively) write the admin config file.",
    )
    _add_target_flags(configure)

    validate = subparsers.add_parser(
        "validate",
        parents=[_shared_parent()],
        help="check the config file against the settings schema",
        description="Validate an existing config file. Exits 1 when invalid.",
    )
    validate.add_argument(
        "--strict",
        action="store_true",
        default=argparse.SUPPRESS,
        help="require every schema setting to be present in the file",
    )

    preview = subparsers.add_parser(
        "preview",
        parents=[_shared_parent()],
        help="show the effective settings and pending changes (writes nothing)",
        description="Render the effective settings and the diff apply would perform.",
    )
    _add_target_flags(preview)

    apply_ = subparsers.add_parser(
        "apply",
        parents=[_shared_parent()],
        help="validate then write the config file atomically",
        description="Validate and apply settings to the config file (a .bak is kept).",
    )
    _add_target_flags(apply_)

    return parser


_PARENT_CACHE: Optional[argparse.ArgumentParser] = None


def _shared_parent() -> argparse.ArgumentParser:
    """A parent parser carrying only the shared flags (no automatic --help)."""
    global _PARENT_CACHE
    if _PARENT_CACHE is None:
        parent = argparse.ArgumentParser(add_help=False)
        _add_shared_flags(parent)
        _PARENT_CACHE = parent
    return _PARENT_CACHE


def _normalise(args: argparse.Namespace) -> SimpleNamespace:
    """Fill in defaults for SUPPRESSed flags and return a plain namespace."""
    return SimpleNamespace(
        command=getattr(args, "command", None),
        config=getattr(args, "config", None),
        root=getattr(args, "root", None),
        non_interactive=bool(getattr(args, "non_interactive", False)),
        yes=bool(getattr(args, "yes", False)),
        set=list(getattr(args, "set", []) or []),
        strict=bool(getattr(args, "strict", False)),
    )


def run(args: SimpleNamespace) -> int:
    """Dispatch one parsed command."""
    from .commands import cmd_apply, cmd_configure, cmd_preview, cmd_validate

    handlers = {
        "configure": cmd_configure,
        "validate": cmd_validate,
        "preview": cmd_preview,
        "apply": cmd_apply,
    }
    handler = handlers[args.command]
    return handler(args)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point used by ``python -m clams_admin`` and the console script."""
    parser = build_parser()
    try:
        parsed = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # --help / --version / argparse errors
        return int(exc.code or 0)

    args = _normalise(parsed)
    if not args.command:
        parser.print_help()
        return 0

    try:
        return run(args)
    except AdminError as exc:
        print(format_error(exc), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted", file=sys.stderr)
        return 130
    except OSError as exc:
        print(format_error(AdminError(str(exc), corrective_action="check file permissions")),
              file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
