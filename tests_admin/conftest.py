"""Test plumbing for ``tests_admin``.

This directory is deliberately **outside** ``tests/`` so that
``tests/conftest.py`` (which imports ``clams.db`` and therefore pydantic) is
never collected when the stdlib-only admin scaffold is verified in a container
that has nothing but ``python3``/``pytest``/``git``/``grep``.

Only the standard library and pytest are imported here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """A throwaway repository skeleton that tests may write into.

    Layout::

        <tmp_path>/repo/
            config/
            src/
            var/

    Nothing in ``tests_admin`` may ever write inside the real repository.
    """
    repo = tmp_path / "repo"
    for part in ("config", "src", "var"):
        (repo / part).mkdir(parents=True, exist_ok=True)
    return repo


@pytest.fixture()
def run_cli():
    """Call ``clams_admin.cli.main`` and return its exit code (no SystemExit)."""
    from clams_admin.cli import main

    def _run(*argv: str) -> int:
        return main(list(argv))

    return _run


@pytest.fixture()
def repo_pythonpath() -> str:
    """PYTHONPATH value pointing at ``<repo>/src`` for subprocess checks."""
    return str(SRC_DIR)
