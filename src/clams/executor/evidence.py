"""Deterministic evidence gate (§7.6) — Clams-run, never model text.

Checks, all mandatory:
  * every ``run:`` command exits 0 (empty output is flagged, not fatal)
  * >= 1 commit on the task branch vs base_ref
  * changed files subset of writable globs and disjoint from forbidden
  * ``git status --porcelain`` empty (everything committed)
Any check missing/unparseable => FAIL (fail-closed).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

from .scope import check_scope
from .worktree import WorktreeManager


class EvidenceGate:
    def __init__(self, wt: WorktreeManager,
                 command_timeout_s: Optional[int] = None):
        self.wt = wt
        self.command_timeout_s = command_timeout_s

    def run(self, worktree: Path, brief: dict[str, Any],
            base_ref: str, scope_base: Optional[str] = None) -> dict[str, Any]:
        """`base_ref` anchors "did this branch do any work" (>=1 commit).
        `scope_base` anchors "what may this task touch" and is the ref the
        worktree was SEEDED from. They differ on retry work reuse: attempt N
        starts from attempt N-1's branch, so charging a task every file its
        predecessors committed makes the scope check unsatisfiable — the
        violations are already on the branch it was handed and no agent edit
        can remove them (claudio#133 T5: 172 of 176 files)."""
        scope_base = scope_base or base_ref
        checks: list[dict[str, Any]] = []
        passed = True

        # 1. run: commands
        run_cmds = brief.get("run") or []
        if not run_cmds:
            checks.append({"check": "run_commands", "ok": False,
                           "detail": "no run commands in brief"})
            passed = False
        for cmd in run_cmds:
            entry: dict[str, Any] = {"check": "run", "cmd": cmd}
            try:
                proc = subprocess.run(
                    cmd, shell=True, cwd=str(worktree),
                    capture_output=True, text=True,
                    timeout=self.command_timeout_s, env=self._env())
                output = (proc.stdout or "") + (proc.stderr or "")
                entry["exit"] = proc.returncode
                entry["output_tail"] = output[-2000:]
                # The exit code is the verdict. Requiring non-empty output made
                # any silent-by-design command unsatisfiable: claudio#133 T4
                # burned 3 attempts on `git commit -q ... || true`, which
                # succeeded, printed nothing, and was therefore "failed".
                # Emptiness is recorded as a flag for the planner, never fatal.
                entry["ok"] = proc.returncode == 0
                if proc.returncode != 0:
                    entry["detail"] = f"exit {proc.returncode}"
                elif not output.strip():
                    entry["empty_output"] = True
            except subprocess.TimeoutExpired:
                entry.update(ok=False, detail=f"timeout > {self.command_timeout_s}s")
            except Exception as exc:  # noqa: BLE001
                entry.update(ok=False, detail=f"unrunnable: {exc}")
            checks.append(entry)
            passed = passed and entry["ok"]

        # 2. >= 1 commit on branch
        try:
            ahead = self.wt.commits_ahead(worktree, base_ref)
            ok = ahead >= 1
            checks.append({"check": "commits", "count": ahead, "ok": ok,
                           **({} if ok else {"detail": "no commit on branch"})})
            passed = passed and ok
        except Exception as exc:  # noqa: BLE001
            checks.append({"check": "commits", "ok": False,
                           "detail": f"unparseable: {exc}"})
            passed = False

        # 3. scope
        try:
            changed = self.wt.changed_files(worktree, scope_base)
            ok, violations = check_scope(
                changed, brief.get("writable") or [],
                brief.get("forbidden") or [])
            checks.append({"check": "scope", "changed": changed, "ok": ok,
                           **({"violations": violations} if violations else {})})
            passed = passed and ok
        except Exception as exc:  # noqa: BLE001
            changed = []
            checks.append({"check": "scope", "ok": False,
                           "detail": f"unparseable: {exc}"})
            passed = False

        # 4. clean tree
        try:
            dirty = self.wt.status_porcelain(worktree).strip()
            ok = not dirty
            checks.append({"check": "clean_tree", "ok": ok,
                           **({"detail": dirty[:500]} if not ok else {})})
            passed = passed and ok
        except Exception as exc:  # noqa: BLE001
            checks.append({"check": "clean_tree", "ok": False,
                           "detail": f"unparseable: {exc}"})
            passed = False

        reason = "" if passed else "; ".join(
            f"{c['check']}: {c.get('detail', 'failed')}"
            for c in checks if not c.get("ok"))
        return {"passed": passed, "reason": reason[:1000], "checks": checks,
                "changed_files": changed}

    def _env(self) -> dict[str, str]:
        import os
        from .worktree import trust_env
        # brief run: commands may call git; the trees are agent-owned while
        # the gate runs as root -> same trust config as the worktree ops
        env = trust_env(dict(os.environ))
        env.update(self.wt.git_env)
        return env
