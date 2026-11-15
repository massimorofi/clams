#!/usr/bin/env python3
"""Data repair for issue #126: stuck requirements and the debris they left.

Depends on #114 (ack/redelivery) and #117 (stale re-dispatch) being in place --
running this before those land just lets the state re-corrupt immediately.

What it does (``--apply``; without it the script only reports):

1. Backs up ``data/clams.db`` (plus ``-wal``) before touching anything.
2. Purges the stale ``clams:tasks`` / ``clams:results`` streams. They are full
   of briefs and results from the broken run; replaying them after the fix
   would re-run work that is already reported. The database is the source of
   truth and the reaper re-derives whatever is genuinely missing.
3. Prunes git worktree registrations whose directory is gone, removes leftover
   ``work/p*-task-*`` trees that have no live claim and no uncommitted work, and
   deletes ``task/*`` branches that contain no commit (the #115 debris).

Requirements themselves are deliberately *not* rewritten: with #117 landed the
orchestrator re-dispatches the tasks with expired leases (Route A) and blocks
the requirement once the attempt cap is hit, which is the honest outcome.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clams.config import Settings            # noqa: E402
from clams.db import open_store              # noqa: E402
from clams.worktree import remove_worktree   # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _live_claims(store) -> set[tuple[int, int]]:
    return {(int(r["project_id"]), int(r["task_number"]))
            for r in store.task_claims.active()}


def missing_tasks(store) -> list[tuple[int, int, list[int]]]:
    """``(project_id, requirement, [tasks with no result and no live claim])``."""
    out = []
    for item in store.work_items.all():
        if item.status != "dispatched":
            continue
        rows = store.db.conn.execute(
            "SELECT payload FROM outbox WHERE kind='gh_comment'").fetchall()
        import json
        reported = set()
        for r in rows:
            raw = json.loads(r["payload"]).get("result")
            if isinstance(raw, dict) and int(raw.get("project_id", 0) or 0) == item.project_id:
                reported.add(int(raw["task_issue"]))
        live = {int(r["task_number"]) for r in store.task_claims.active()
                if int(r["project_id"]) == item.project_id}
        stuck = [t for t in item.task_numbers if t not in reported and t not in live]
        if stuck:
            out.append((item.project_id, item.requirement_number, stuck))
    return out


def backup_db(settings: Settings) -> Path:
    stamp = int(time.time())
    dst = Path(f"{settings.paths.db_path}.bak-{stamp}")
    shutil.copy(settings.paths.db_path, dst)
    wal = Path(str(settings.paths.db_path) + "-wal")
    if wal.exists():
        shutil.copy(wal, Path(str(dst) + "-wal"))
    return dst


def purge_streams(settings: Settings) -> list[str]:
    import asyncio
    from clams.bus import RedisBus, STREAM_RESULTS, STREAM_TASKS

    # Each stream has its own consumer group; asking the wrong one reports a
    # pending count of 0 and hides how much stale work is being dropped.
    groups = {STREAM_TASKS: "executors", STREAM_RESULTS: "planner"}

    async def run() -> list[str]:
        bus = RedisBus(settings.redis.url, settings.redis.namespace)
        purged = []
        try:
            await bus.connect()
            for stream, group in groups.items():
                info = await bus.pending(stream, group)
                await bus.purge(stream)
                purged.append(f"{stream} group={group} pending={info['count']}")
        except Exception as exc:
            print(f"  ! redis unavailable: {exc}")
        finally:
            await bus.close()
        return purged

    return asyncio.run(run())


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True)
    if proc.returncode != 0:
        return f"ERR {proc.stderr.strip()[:120]}"
    return proc.stdout.strip()


def prune_worktrees(store, settings: Settings, apply: bool) -> list[str]:
    """Remove task worktrees with no live claim; prune dead registrations."""
    actions: list[str] = []
    live = _live_claims(store)
    bases = [REPO] + [Path(p) for p in _clone_paths(store) if Path(p).exists()]
    for base in bases:
        listing = git(base, "worktree", "list")
        if listing.startswith("ERR"):
            continue
        for line in listing.splitlines()[1:]:
            path = line.split(" ", 1)[0]
            p = Path(path)
            if not p.exists():
                actions.append(f"{base.name}: prune dead registration {path}")
                if apply:
                    git(base, "worktree", "prune")
                continue
            if "task-" not in p.name:
                continue
            pid, task = _parse_worktree_name(p.name)
            if pid is not None and (pid, task) in live:
                continue                      # a live task owns this tree
            dirty = git(p, "status", "--porcelain")
            if dirty and not dirty.startswith("ERR"):
                actions.append(f"SKIP {path}: has uncommitted work")
                continue
            actions.append(f"{base.name}: remove {p.name}")
            if apply:
                remove_worktree(str(base), str(p))
    return actions


def _parse_worktree_name(name: str) -> tuple[int | None, int | None]:
    # p{project_id}-task-{n}
    try:
        head, _, tail = name.partition("-task-")
        return int(head[1:]), int(tail)
    except (ValueError, IndexError):
        return None, None


def _clone_paths(store) -> list[str]:
    return [r["worktree_path"] for r in store.projects.all()
            if r["worktree_path"]]


def prune_empty_branches(store, apply: bool) -> list[str]:
    """Delete task/* branches in the Clams checkout that hold no commit.

    Those branches are pure #115 debris: Claudio tasks branched off Clams' main
    that never committed anything.
    """
    actions: list[str] = []
    listing = git(REPO, "for-each-ref", "--format=%(refname:short)",
                  "refs/heads/task/")
    if listing.startswith("ERR"):
        return actions
    for branch in listing.split():
        ahead = git(REPO, "rev-list", "--count", f"main..{branch}")
        if ahead.startswith("ERR"):
            continue
        if int(ahead or 0) > 0:
            actions.append(f"KEEP {branch}: {ahead} commit(s) ahead of main")
            continue
        actions.append(f"{branch}: no commits, delete")
        if apply:
            git(REPO, "branch", "-D", branch)
    return actions


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="perform the repair")
    args = ap.parse_args()

    settings = Settings.load()
    store = open_store(settings.paths.db_path)

    print(f"=== clams#126 repair ({'APPLY' if args.apply else 'dry run'}) ===")
    print(f"db: {settings.paths.db_path}")

    print("\n-- requirements stuck in dispatched --")
    for pid, req, stuck in missing_tasks(store):
        print(f"  project {pid} req #{req}: tasks {stuck} have no result")
    if not missing_tasks(store):
        print("  none")

    print("\n-- stale claims (lease expired, no result) --")
    for r in store.task_claims.expired():
        print(f"  project {r['project_id']} task {r['task_number']}: "
              f"attempts={r['attempts']} lease={r['lease_expires_at']}")

    if args.apply:
        bak = backup_db(settings)
        print(f"\nbacked up database to {bak}")

    print("\n-- redis streams --")
    print("  " + ("; ".join(purge_streams(settings)) if args.apply
                  else "would purge clams:tasks and clams:results"))

    print("\n-- worktrees --")
    for a in prune_worktrees(store, settings, args.apply):
        print("  " + a)

    print("\n-- task branches in the Clams checkout (#115 debris) --")
    for a in prune_empty_branches(store, args.apply):
        print("  " + a)

    print("\nDone. With #117 in place the orchestrator re-dispatches the stuck"
          "\ntasks on its own; requirements reach status:Blocked at the attempt cap.")
    store.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
