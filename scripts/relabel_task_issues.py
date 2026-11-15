#!/usr/bin/env python3
"""Stop task sub-issues from wearing intake's ``status:Ready`` label.

``status:Ready`` is what intake sweeps to find *unclaimed requirements*. The
planner used to label every generated *task* with it too, so the board showed
dozens of "Ready" issues that intake was correct to ignore -- which read as
"Clams is not picking up my ready issues".

This re-labels those tasks ``status:In Progress`` (what a task of an
already-claimed requirement actually is). Dry run by default.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clams.config import Settings  # noqa: E402
from clams.gh import STATUS_IN_PROGRESS, STATUS_READY  # noqa: E402

PARENT_BODY = re.compile(r"^\s*Parent:\s*#\d+")
PARENT_LABEL = re.compile(r"^parent-\d+$")


def gh(settings: Settings, path: str, method: str = "GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{settings.github.api_base.rstrip('/')}{path}", data=data, method=method,
        headers={"Authorization": f"token {settings.github.token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def is_task_subissue(issue: dict) -> bool:
    labels = [l["name"] for l in issue.get("labels", [])]
    return any(PARENT_LABEL.match(l) for l in labels) or bool(
        PARENT_BODY.match(issue.get("body") or ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--owner", default="massimorofi")
    ap.add_argument("--repo", default="clams")
    args = ap.parse_args()

    s = Settings()
    issues = gh(s, f"/repos/{args.owner}/{args.repo}/issues"
                   f"?state=open&per_page=100&labels={STATUS_READY.replace(':', '%3A')}")
    tasks = [i for i in issues if is_task_subissue(i)]
    real = [i for i in issues if not is_task_subissue(i)]
    print(f"{args.repo}: {len(issues)} open {STATUS_READY!r} issues")
    print(f"  task sub-issues to relabel: {len(tasks)}")
    print(f"  genuine requirements:       {len(real)} -> {[i['number'] for i in real]}")

    if not args.apply:
        print("\ndry run; pass --apply to relabel")
        return 0

    changed = 0
    for i in tasks:
        labels = [l["name"] for l in i["labels"] if l["name"] != STATUS_READY]
        if STATUS_IN_PROGRESS not in labels:
            labels.append(STATUS_IN_PROGRESS)
        gh(s, f"/repos/{args.owner}/{args.repo}/issues/{i['number']}",
           method="PATCH", body={"labels": labels})
        changed += 1
    print(f"relabelled {changed} task issues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
