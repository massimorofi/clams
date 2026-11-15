#!/usr/bin/env bash
# Close the CLEANUP issues with the commit that actually contains each fix.
# NOTE: two commits (#114, #115) were swept into later commits, so the
# references below are the commits where the code actually landed.
set -u
R=massimorofi/clams
close() {
  local n="$1"; shift
  local body="$1"; shift
  if gh issue close "$n" --repo "$R" --reason "completed" --comment "$body" >/dev/null 2>&1; then
    echo "closed $n"
  else
    echo "FAILED $n"
  fi
}

close 115 "Fixed. \`Orchestrator._default_scope()\` now resolves the registered default project's provisioned \`worktree_path\` (falling back to \`settings.paths.repo_path\` only in legacy single-repo mode, \`default_project_id == 0\`), and \`step_dispatch\` skips a project with no provisioned tree and raises an alert instead of dispatching into the orchestrator's cwd. \`build_project_contexts\` no longer falls back to the Clams checkout when provisioning fails.

Landed in 28e405b (orchestrator) and 65c0e83 (project_setup.py, which was untracked). Regression tests: \`test_default_project_dispatch_uses_provisioned_worktree\`, \`test_unprovisioned_default_project_is_skipped_not_run_in_cwd\`."

close 114 "Fixed. \`Delivery\` now carries the message id, \`take_task/take_result/take_command/take_alert\` return it and the consumer acks after the work is durable; \`read_*\` remains as an explicit at-most-once convenience. \`RedisBus.redeliver()\` (XREADGROUP ... 0) and \`claim_idle()\` (XAUTOCLAIM) were added, and \`InMemoryBus\` now models the PEL so this is testable without Redis. The executor re-runs its PEL on start-up (\`recover_pending\`); the orchestrator re-records unacked results (\`recover_pending_results\`).

Landed in a08e5e7 (bus) and 28e405b (executor/orchestrator); tests in 65c0e83 (\`tests/test_bus_ack.py\`, incl. a live-Redis round-trip). Restart-mid-task is covered by \`test_restart_mid_task_redelivers_and_completes\`."

close 116 "Fixed. \`scripts/dev.sh\` now mounts a host-exec socket when \`CLAMS_HOST_EXEC_SOCKET\` points at one, and otherwise rewrites the agent config to \`host_execution_enabled: false\` so commands run sandboxed in the container where the worktree is bind-mounted. The live executor/planner configs were switched off and given the bind-mounted repo path in \`workspace_allowed_roots\`. Tests: \`tests/test_agent_host_exec.py\`. Commit 86be2c2."

close 117 "Fixed. New \`Orchestrator.step_reap_stale\` cycle step: a claimed task whose lease expired with no result is re-published and counted as an attempt (\`TaskClaimRepo.register_abandoned_attempt\` creates the row when the brief vanished before a claim); a task dispatched but never claimed is judged only after a grace window; at the \`max_attempts\` cap a synthetic failed result is published so the review gate completes the requirement and applies \`status:Blocked\`. Tests: \`tests/test_stale_redispatch.py\` (incl. the 3-attempts-blocks acceptance case). Commit 28e405b."

close 126 "Repaired. \`scripts/repair_126.py\` (dry run by default, backs up the DB first) was applied:
* \`clams:tasks\` (29 pending) and \`clams:results\` purged, so start-up redelivery replays nothing from the broken run;
* dead worktree registrations \`p2-task-47\`/\`p2-task-48\` pruned and \`p1-task-110\` removed (clean, no commits);
* the nine \`task/*\` branches in the Clams checkout deleted -- none held a commit. The untracked files in \`work/p2-task-47\` had already gone with the directory, so nothing was lost.

Route A (re-run) was chosen for claudio#36/#40: tasks 45, 47, 48 (and clams 110, 111) have expired leases and no result, so #117's reaper re-dispatches them and blocks the requirement at the attempt cap. Commit 30a8477."

close 119 "Fixed. \`extract_commands()\` splits an acceptance criterion into (commands, prose); only lines explicitly marked \`run:\`/\`cmd:\`/\`shell:\`/\`bash:\` are executed. A prose-only criterion no longer fails the task -- the outcome records it as not machine-verifiable and says so in the summary. The decomposer prompt now asks for \`run:\`-prefixed checks. Tests in \`tests/test_executor.py\`. Commit 0fa86fd."

close 122 "Fixed. \`executor.lease_seconds\` default is now 2100 (> \`max_wall_seconds\` 1800) and \`Settings.load()\` refuses a lease that does not exceed the wall budget. The executor also renews its claim every \`lease_renew_interval\` while a task runs, so a misconfigured short lease cannot expire mid-run. Tests: \`tests/test_lease_and_duplicate.py\`. Commit dd74e55."

close 118 "Fixed. The already-claimed path now publishes its \`blocked\` ExecutionResult instead of returning it to a caller that discarded it, so the task reports and the review gate can close the requirement. Test: \`test_already_claimed_task_publishes_its_result\`. Commit dd74e55."

close 125 "Fixed in code. \`clean_remote_url\`/\`find_token_bearing_remotes\`/\`scrub_remote_tokens\` were added, a \`clams scrub-credentials\` command exposes them, and a new watchdog check (\`git_clone_credentials\`) goes critical when a remote carries credentials. \`clone_repo\` already reset origin to the clean URL and is covered by \`test_clone_resets_token_from_remote\`. The existing clones on this box were verified clean (no token in any \`.git/config\` under \`work/\`).

STILL REQUIRES THE OPERATOR: the token that was written to disk during the earlier provisioning must be rotated in GitHub -- scrubbing the file does not un-leak it. Commit a08e5e7."

close 120 "Fixed. \`_pending_detail\` now calls \`client.xpending_range(name, group, min='-', max='+', count=100)\` (the old positional \`xpending\` raised TypeError on every call), probe failures are returned as an \`error\` key on \`pending()\` and surfaced by \`probe_queue\` as a failing check (NOGROUP on a fresh system still counts healthy), and \`_step_auto_recover\` no longer calls \`cleanup_stale_worktrees(work_root, set())\` -- it passes the paths derived from live claims and skips the prune when they cannot be determined. Tests: \`tests/test_queue_probe_and_credentials.py\`. Commit a08e5e7."

close 124 "Fixed. A new \`runtime_state\` key/value table persists \`operator_paused\`/\`operator_stopped\` and the watchdog's own pause flag; the API restores the global pause on start-up and writes it on pause/stop/resume; \`_lift_watchdog_pause\` re-applies the operator pause after clearing its own. Tests: \`tests/test_pause_provenance.py\`. Commit a69bd32."

close 123 "Fixed. \`Orchestrator.project_paused()\` is checked in \`step_intake\`, \`step_plan\`, \`step_dispatch\` and \`step_reap_stale\`, so pausing a project really stops it and resume restarts it. The dead publish to \`clams:planner-actions\` (a stream nothing consumed) was removed; the response shape is unchanged. Tests: \`tests/test_project_pause.py\`. Commit 4a1b520."

close 121 "Fixed. \`scripts/dev.sh\` now overrides the image HEALTHCHECK with \`--health-cmd \"curl -fsS http://127.0.0.1:\${port}/healthz || exit 1\"\` so the agent containers report healthy on 1072/1073. Tests in \`tests/test_agent_host_exec.py\`. Commit 1cbfcf1."

close 127 "Done. Clams: 76 duplicate runaway task sub-issues closed (#21-#87, #95-#103); 88-94 (#16's live set) and 106-111 (#105's live set) left open. Claudio: 43, 44, 46, 49-52 closed as not planned (they reported but #115 meant their work was cut from the Clams repo, so nothing landed); 45, 47, 48 left open for #117's reaper. Intake already skips task sub-issues via \`_is_task_subissue\`, so closing them cannot re-trigger planning. Scripts kept: \`scripts/triage_127.sh\`, \`scripts/triage_127_claudio.sh\`. Commit 19fad73."
echo COMPLETE
