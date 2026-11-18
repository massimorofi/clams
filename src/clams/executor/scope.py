"""Writable-scope enforcement: changed files must fit the brief's globs."""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Iterable


def _matches(path: str, pattern: str) -> bool:
    p = PurePosixPath(path)
    pat = PurePosixPath(pattern)
    # "src/**" style: any depth below prefix; also allow bare dir prefix
    if pattern.endswith("/**") or pattern.endswith("/"):
        prefix = pattern.rstrip("/*")
        return p == PurePosixPath(prefix) or prefix in [x.as_posix() for x in p.parents] \
            or str(p).startswith(prefix + "/")
    try:
        return p.match(pattern)
    except ValueError:
        return False


def is_writable(path: str, writable: Iterable[str], forbidden: Iterable[str]) -> bool:
    w = list(writable)
    f = list(forbidden)
    if any(_matches(path, pat) for pat in f):
        return False
    return any(_matches(path, pat) for pat in w)


def check_scope(changed_files: list[str], writable: list[str],
                forbidden: list[str]) -> tuple[bool, list[str]]:
    """Returns (ok, violations). Empty changed_files is a violation upstream
    (the gate requires >=1 commit); here it simply passes."""
    violations = [f for f in changed_files
                  if not is_writable(f, writable, forbidden)]
    return (not violations), violations
