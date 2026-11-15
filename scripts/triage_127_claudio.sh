#!/usr/bin/env bash
# Issue #127 (Claudio side): triage the task sub-issues of requirements #36/#40.
#
# Outcome chosen for #36/#40 in massimorofi/clams#126 was Route A (re-run):
# tasks 45, 47 and 48 never reported and have expired leases, so the new stale
# reaper (massimorofi/clams#117) re-dispatches them. They stay open.
#
# 43, 44, 46, 49, 50, 51, 52 did report -- but they were cut from the *Clams*
# repository because of massimorofi/clams#115, so nothing landed here. They are
# closed as not planned; the requirements will be re-planned against the right
# working tree now that #115/#116/#114 are fixed.
set -u
KEEP="45 47 48"
MSG="Task sub-issue closed during the massimorofi/clams#127 board triage.

This task reported, but it was executed against the wrong repository: the
default project's dispatch scope was the orchestrator's own checkout, so
Claudio work was cut from the Clams repo (massimorofi/clams#115). Nothing
landed here. #115, #116 (no working executor shell) and #114 (results lost on
restart) are fixed on main, and requirement #36/#40 will be re-run against the
correctly provisioned working tree.

Tasks that never reported (45, 47, 48) are deliberately left open: the new
stale-task reaper (massimorofi/clams#117) re-dispatches them."
for n in 43 44 45 46 47 48 49 50 51 52; do
  case " $KEEP " in
    *" $n "*) echo "kept $n (will be re-dispatched)"; continue;;
  esac
  if gh issue close "$n" --repo massimorofi/claudio --reason "not planned" \
       --comment "$MSG" >/dev/null 2>&1; then
    echo "closed $n"
  else
    echo "FAILED $n"
  fi
done
echo COMPLETE
