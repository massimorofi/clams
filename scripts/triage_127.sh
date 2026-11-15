#!/usr/bin/env bash
# Issue #127 board triage: close the duplicate runaway task sub-issues.
# Keeps 88-94 (the one task set that will actually run for requirement #16)
# and 106-111 (#105's live set).
set -u
MSG="Duplicate runaway task sub-issue.

One of ~76 near-identical tasks produced by repeated runaway decomposition of
requirement #16 (and of its own task sub-issues). None could ever complete:
dispatch cut every project's tasks from the orchestrator's own checkout
(massimorofi/clams#115), the executor agent had no working shell
(massimorofi/clams#116), and results were lost on restart because nothing acked
the queue (massimorofi/clams#114). All three are fixed on main.

Closing as part of the massimorofi/clams#127 triage. The task set that will
actually be executed for #16 (88-94) stays open."
for n in $(seq 21 87) $(seq 95 103); do
  if gh issue close "$n" --repo massimorofi/clams --reason "not planned" \
       --comment "$MSG" >/dev/null 2>&1; then
    echo "closed $n"
  else
    echo "FAILED $n"
  fi
done
echo COMPLETE
