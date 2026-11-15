#!/usr/bin/env python3
"""Unstick a Clams deployment whose pipeline has frozen (issue #18 fallout).

The multi-project work left three kinds of poison in the local database:

1. **Requirements that are really task sub-issues.** Before the intake filter
   existed, planner-generated sub-issues (``Parent: #n`` body / ``parent-n``
   label) were claimed as requirements. They must never be planned: planning a
   task decomposes it into sub-tasks and floods the repository.
2. **Outbox rows addressed to the wrong repository.** Before payloads carried a
   ``project_id``, every side effect went to the default repo, so clams issues
   were posted against claudio and came back 404.
3. **A blanket pause nobody could lift.** The watchdog paused every work item on
   a critical; the API's resume walked rows with the default ``project_id=0``
   and silently no-opped on the rest, leaving ``planned=0 dispatched=0`` forever.

Run with ``--apply`` to repair; without it the script only reports.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clams.config import Settings          # noqa: E402
from clams.db import open_store            # noqa: E402
from clams.intake import _is_task_subissue  # noqa: E402

OWNER_REPO = ("massimorofi", "clams")


class _Issue:
    """Minimal duck-typed issue for :func:`_is_task_subissue`."""

    def __init__(self, labels: list[str], body: str):
        self.labels = labels
        self.body = body


def _gh(settings: Settings, owner: str, repo: str, path: str):
    req = urllib.request.Request(
        f"{settings.github.api_base.rstrip('/')}/repos/{owner}/{repo}{path}",
        headers={"Authorization": f"token {settings.github.token}",
                 "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually mutate the DB")
    args = ap.parse_args()

    settings = Settings()
    store = open_store(settings.paths.db_path)
    owner, repo = OWNER_REPO

    items = store.work_items.all()
    print(f"work_items: {len(items)}")

    bogus: list[tuple[int, int]] = []
    for it in items:
        if it.project_id == 0:
            # Legacy rows predating project scoping; every one of them is
            # duplicated under a real project id.
            bogus.append((it.project_id, it.requirement_number))
            continue
        try:
            raw = _gh(settings, owner, repo, f"/issues/{it.requirement_number}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! could not check #{it.requirement_number}: {exc}")
            continue
        issue = _Issue([l["name"] for l in raw.get("labels", [])],
                       raw.get("body") or "")
        if _is_task_subissue(issue):
            bogus.append((it.project_id, it.requirement_number))
            print(f"  task-subissue: project={it.project_id} req=#{it.requirement_number} "
                  f"status={it.status}")
        elif raw.get("state") == "closed":
            bogus.append((it.project_id, it.requirement_number))
            print(f"  closed upstream: req=#{it.requirement_number}")

    paused = [i for i in items if i.user_paused]
    print(f"paused work items: {len(paused)}")

    failed_outbox = store.db.conn.execute(
        "SELECT id, kind, payload FROM outbox WHERE status = 'failed'").fetchall()
    print(f"failed outbox rows: {len(failed_outbox)}")

    unacked = store.db.conn.execute(
        "SELECT count(*) c FROM alerts WHERE acknowledged = 0").fetchone()["c"]
    print(f"unacked alerts: {unacked}")

    if not args.apply:
        print("\ndry run; pass --apply to repair")
        return 0

    if bogus:
        store.db.conn.executemany(
            "DELETE FROM work_items WHERE project_id = ? AND requirement_number = ?",
            bogus,
        )
        print(f"deleted {len(bogus)} bogus work items")

    store.work_items.set_paused_all(False)
    print("resumed every work item")

    store.db.conn.execute("DELETE FROM outbox WHERE status = 'failed'")
    print(f"purged {len(failed_outbox)} misrouted outbox rows")

    n = store.alerts.ack_all()
    print(f"acknowledged {n} alerts")

    store.db.conn.commit()
    print("committed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
