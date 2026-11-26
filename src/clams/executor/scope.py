"""Writable-scope enforcement: changed files must fit the brief's globs."""
from __future__ import annotations

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Iterable


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Glob -> regex where `**` spans path separators and `*` does not."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("/**/", i):
            out.append("(?:.*/)?")
            i += 4
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        ch = pattern[i]
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out))


def _matches(path: str, pattern: str) -> bool:
    p = PurePosixPath(path)
    pat = PurePosixPath(pattern)
    # "src/**" style: any depth below prefix; also allow bare dir prefix
    if pattern.endswith("/**") or pattern.endswith("/"):
        prefix = pattern.rstrip("/*")
        return p == PurePosixPath(prefix) or prefix in [x.as_posix() for x in p.parents] \
            or str(p).startswith(prefix + "/")
    if "**" in pattern:
        # mid-path "**" (e.g. "benchmark/results/**/judge.json"). PurePosixPath
        # .match() treats "**" as "*", so it matched a single directory level
        # and charged a task its own writable files (claudio#133 T5: 172 of 176
        # "violations" were files the brief explicitly allowed).
        return bool(_glob_to_regex(pattern).fullmatch(path))
    try:
        return p.match(pattern)
    except ValueError:
        return False


#: patterns that mean "everything" — LLM plans routinely emit
#: forbidden: ["*"] alongside specific writable globs to mean "nothing
#: *beyond* writable". A catch-all must not veto an explicitly writable file;
#: specific forbidden patterns (e.g. "*.env", "src/secret.py") always veto.
_CATCHALL = {"*", "**", "*/**"}


def is_writable(path: str, writable: Iterable[str], forbidden: Iterable[str]) -> bool:
    w = list(writable)
    f = [pat for pat in forbidden if pat.strip("/") not in _CATCHALL]
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
