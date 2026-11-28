"""Atomic, backed-up, rollback-capable file writes for ``clams-admin``.

Every file this tool touches goes through :func:`apply` on a :class:`WritePlan`.
The plan is built first (pure, no I/O) and then executed in two phases, so a
failure half-way through cannot leave a half-written deployment behind:

1. **check** - every target is validated up front. An existing *secret* file is
   refused unless ``overwrite=True`` is passed.
2. **stage then replace** - each file's content is written to a temp file in the
   destination directory with its final mode already applied (a secret is
   therefore never briefly world-readable), then ``os.replace``-d onto its
   target after the previous content has been copied to ``<name>.bak``.

If the replace phase fails on any file, every file already replaced in this run
is restored byte-for-byte (files that did not exist before are removed) and the
error is re-raised.

Nothing here prints or returns a secret: :class:`WritePlan` and
:class:`WriteReport` expose paths, byte counts and digests only, and every
summary string is passed through :func:`clams_admin.secrets.redact` as a
belt-and-braces measure.

Stdlib-only, like the rest of :mod:`clams_admin`.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from .errors import AdminError
from .secrets import redact

__all__ = [
    "SECRET_FILE_MODE",
    "PLAIN_FILE_MODE",
    "BACKUP_SUFFIX",
    "WriteError",
    "SecretOverwriteError",
    "WriteItem",
    "WritePlan",
    "WrittenFile",
    "WriteReport",
    "plan",
    "apply",
    "write_atomic",
    "resolve_target",
]

#: mode for every file that carries a secret: owner read/write only (0o600).
SECRET_FILE_MODE = 0o600
#: mode for everything else.
PLAIN_FILE_MODE = 0o644
#: suffix of the backup written next to every replaced file.
BACKUP_SUFFIX = ".bak"


class WriteError(AdminError):
    """A planned write could not be completed.

    Raised *after* a rollback: when this error escapes :func:`apply`, the
    filesystem is exactly as it was before the call.
    """

    exit_code = 5


class SecretOverwriteError(WriteError):
    """A secret file already exists and ``overwrite=True`` was not passed."""

    exit_code = 3


# --------------------------------------------------------------------------
# plan items
# --------------------------------------------------------------------------
def resolve_target(root: Path | str, path: Path | str) -> Path:
    """Absolute target for ``path`` (relative paths are taken against ``root``)."""
    p = Path(path)
    if not p.is_absolute():
        p = Path(root) / p
    return p


@dataclass
class WriteItem:
    """One file the plan intends to write. Pure: no I/O happens on construction."""

    path: Path
    text: str
    secret: bool = False
    label: str = ""
    """Short human tag used in reports (``planner env``, ``executor-2 config``)."""

    @property
    def mode(self) -> int:
        """``0o600`` for secret-bearing files, ``0o644`` otherwise."""
        return SECRET_FILE_MODE if self.secret else PLAIN_FILE_MODE

    @property
    def n_bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    def digest(self) -> str:
        """Short content fingerprint (never the content itself)."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:12]

    def rel(self, root: Optional[Path | str] = None) -> str:
        """Display path: relative to ``root`` when it lives under it."""
        target = self.path
        if root is not None:
            try:
                return str(target.relative_to(Path(root)))
            except ValueError:
                pass
        return str(target)

    def summary(self, root: Optional[Path | str] = None) -> str:
        """Redaction-safe one-line description (path + size + mode only)."""
        tag = f" ({self.label})" if self.label else ""
        secret = ", secret 0o600" if self.secret else ""
        return (
            f"{self.rel(root)} [{self.n_bytes} B, "
            f"sha256:{self.digest()}, mode 0o{self.mode:o}{secret}]{tag}"
        )


@dataclass
class WritePlan:
    """An ordered list of :class:`WriteItem` s to apply under one ``root``."""

    root: Path = field(default_factory=Path.cwd)
    items: list[WriteItem] = field(default_factory=list)

    def add(
        self,
        path: Path | str,
        text: str,
        *,
        secret: bool = False,
        label: str = "",
    ) -> WriteItem:
        """Append one file to the plan and return the item."""
        item = WriteItem(path=Path(path), text=text, secret=secret, label=label)
        self.items.append(item)
        return item

    def extend(self, items: Sequence[WriteItem]) -> "WritePlan":
        self.items.extend(items)
        return self

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def paths(self) -> list[Path]:
        return [resolve_target(self.root, item.path) for item in self.items]

    def targets(self) -> dict[str, WriteItem]:
        """``resolved path -> item`` (used by callers that must not collide)."""
        return {str(p): item for p, item in zip(self.paths(), self.items)}

    def describe(self) -> str:
        """Printable plan. Contains no file contents, and is redacted anyway."""
        lines = [f"write plan: {len(self.items)} file(s) under {self.root}"]
        lines += [f"  {item.summary(self.root)}" for item in self.items]
        return redact("\n".join(lines))


def plan(root: Optional[Path | str] = None) -> WritePlan:
    """Start an empty plan rooted at ``root`` (default: cwd)."""
    return WritePlan(root=Path(root) if root is not None else Path.cwd())
# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
@dataclass
class WrittenFile:
    """What actually landed on disk for one plan item."""

    path: Path
    n_bytes: int
    mode: int
    digest: str
    secret: bool = False
    created: bool = True
    """``False`` when an existing file was replaced (a backup then exists)."""
    backup: Optional[Path] = None

    def rel(self, root: Optional[Path | str] = None) -> str:
        if root is not None:
            try:
                return str(self.path.relative_to(Path(root)))
            except ValueError:
                pass
        return str(self.path)

    def summary(self, root: Optional[Path | str] = None) -> str:
        verb = "created" if self.created else "replaced"
        backup = f", backup {self.backup.name}" if self.backup else ""
        secret = ", secret 0o600" if self.secret else ""
        return (
            f"{self.rel(root)}: {verb} [{self.n_bytes} B, "
            f"sha256:{self.digest}, mode 0o{self.mode:o}{secret}{backup}]"
        )


@dataclass
class WriteReport:
    """Result of :func:`apply`. Paths and counts only - never file contents."""

    root: Path = field(default_factory=Path.cwd)
    written: list[WrittenFile] = field(default_factory=list)
    dry_run: bool = False

    def paths(self) -> list[Path]:
        return [w.path for w in self.written]

    def relative_paths(self) -> list[str]:
        return [w.rel(self.root) for w in self.written]

    def summary(self) -> str:
        head = "dry run, nothing written" if self.dry_run else "applied"
        lines = [f"{head}: {len(self.written)} file(s) under {self.root}"]
        lines += [f"  {w.summary(self.root)}" for w in self.written]
        return redact("\n".join(lines))

    def as_dict(self) -> dict[str, object]:
        """Machine-readable, redaction-safe view of the report."""
        return {
            "root": str(self.root),
            "dry_run": self.dry_run,
            "files": [
                {
                    "path": w.rel(self.root),
                    "bytes": w.n_bytes,
                    "sha256": w.digest,
                    "mode": f"0o{w.mode:o}",
                    "secret": w.secret,
                    "created": w.created,
                    "backup": w.backup.name if w.backup else None,
                }
                for w in self.written
            ],
        }


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------
def _stage(item: WriteItem, target: Path) -> str:
    """Write ``item`` to a temp file next to ``target`` and return its path.

    The mode is applied to the temp file *before* the replace, so a secret never
    exists on disk with a permissive mode, not even for an instant.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(item.text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, item.mode)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return tmp


def _restore(record: dict) -> None:
    """Undo one replace: put the prior bytes (or nothing) back at ``target``."""
    target: Path = record["target"]
    prior: Optional[bytes] = record["prior"]
    if prior is None:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        return
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".rollback")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(prior)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, record["prior_mode"])
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply(
    write_plan: WritePlan,
    *,
    overwrite: bool = False,
    backup: bool = True,
    fail_on: Optional[Path | str] = None,
    dry_run: bool = False,
) -> WriteReport:
    """Execute ``write_plan`` all-or-nothing and return a redacted report.

    Parameters
    ----------
    overwrite:
        Replace existing **secret** files. Without it, an existing secret file
        raises :class:`SecretOverwriteError` *before* anything is written.
        Non-secret files are always replaceable (their prior content is backed up).
    backup:
        Copy every replaced file to ``<name>.bak`` next to it.
    fail_on:
        Path (or plan-relative name) whose replacement must fail. Used by tests
        - and by ``--drill`` - to prove the rollback path works. The error is
        raised after every earlier file has really been written, and then all of
        them are restored byte-identically.
    dry_run:
        Validate and report only; nothing is written.
    """
    root = Path(write_plan.root)
    targets = [resolve_target(root, item.path) for item in write_plan.items]
    fail_target = None
    if fail_on is not None:
        fail_target = resolve_target(root, Path(fail_on))
        if not fail_target.is_absolute():
            fail_target = (root / fail_on).resolve()
        else:
            fail_target = fail_target.resolve()

    # -- phase 1: check everything before touching the filesystem -------------
    for item, target in zip(write_plan.items, targets):
        if item.secret and target.exists() and not overwrite:
            raise SecretOverwriteError(
                f"refusing to overwrite existing secret file: {target}",
                file=str(target),
                corrective_action=(
                    "pass overwrite=True (the previous content is kept as "
                    f"{target.name}{BACKUP_SUFFIX}) or delete the file first"
                ),
            )

    report = WriteReport(root=root, dry_run=dry_run)
    if dry_run:
        for item, target in zip(write_plan.items, targets):
            report.written.append(
                WrittenFile(
                    path=target,
                    n_bytes=item.n_bytes,
                    mode=item.mode,
                    digest=item.digest(),
                    secret=item.secret,
                    created=not target.exists(),
                    backup=None,
                )
            )
        return report

    # -- phase 2: stage every temp file (still reversible) --------------------
    staged: list[tuple[WriteItem, Path, str]] = []
    try:
        for item, target in zip(write_plan.items, targets):
            staged.append((item, target, _stage(item, target)))
    except OSError as exc:
        for _, _, tmp in staged:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise WriteError(
            f"could not stage {exc.filename or 'a temp file'}: {exc.strerror}",
            file=str(exc.filename or ""),
            corrective_action="check the directory exists and is writable",
        ) from exc

    # -- phase 3: replace, recording enough state to undo each one ------------
    applied: list[dict] = []
    try:
        for index, (item, target, tmp) in enumerate(staged):
            if fail_target is not None and target.resolve() == fail_target:
                raise WriteError(
                    f"simulated failure on {target}",
                    file=str(target),
                    corrective_action="drop fail_on to let the write proceed",
                )
            prior: Optional[bytes] = None
            prior_mode = item.mode
            if target.exists():
                prior = target.read_bytes()
                prior_mode = target.stat().st_mode & 0o777
            record = {"target": target, "prior": prior, "prior_mode": prior_mode}
            backup_path: Optional[Path] = None
            if prior is not None and backup:
                backup_path = target.with_name(target.name + BACKUP_SUFFIX)
                shutil.copy2(target, backup_path)
            os.replace(tmp, target)
            os.chmod(target, item.mode)
            applied.append(record)
            report.written.append(
                WrittenFile(
                    path=target,
                    n_bytes=item.n_bytes,
                    mode=item.mode,
                    digest=item.digest(),
                    secret=item.secret,
                    created=prior is None,
                    backup=backup_path,
                )
            )
    except BaseException as exc:
        # undo in reverse order, then drop the still-staged temp files
        for record in reversed(applied):
            try:
                _restore(record)
            except OSError:  # pragma: no cover - best effort
                pass
        for _, _, tmp in staged[len(applied):]:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if isinstance(exc, AdminError):
            raise
        raise WriteError(
            f"write failed: {exc}",
            corrective_action="the previous state has been restored; retry",
        ) from exc

    return report


def write_atomic(
    path: Path | str,
    text: str,
    *,
    root: Optional[Path | str] = None,
    secret: bool = False,
    overwrite: bool = False,
    backup: bool = True,
    label: str = "",
) -> WriteReport:
    """Single-file convenience wrapper around :func:`apply`.

    ``overwrite`` defaults to ``False`` here too: writing over an existing
    secret always has to be asked for explicitly.
    """
    base = Path(root) if root is not None else Path(path).resolve().parent
    single = WritePlan(root=base)
    single.add(Path(path), text, secret=secret, label=label)
    return apply(single, overwrite=overwrite, backup=backup)
