# Skill lifecycle

Tables: `migrations/015_skill_lifecycle.sql` · Code: `src/clams/skills/` · CLI: `clams skills`

A **skill** is a reusable instruction artifact (a manifest plus a body) that can be
attached to planner/executor work. Because it changes what an agent does, it may only
reach `active` by walking an auditable ladder, and every rung above `draft` is keyed to a
**content hash** rather than to a version string.

```
proposed ──> draft ──> evaluated ──> approved ──> active ──> retired
              ^   |________^  |________^
              |________________|
              (any content change after evaluation drops back to draft)
```

## The two rules

**1. Content is identity.** `content_hash = sha256(canonical_json({manifest, body}))`.
Evaluations, approvals and deployments each store the hash they were about. Activation
requires all three to equal the version's current hash. So editing bytes after approval
does not need an "unapprove" operation: the mismatch *is* the invalidation, and the old
approval row stays visible in the ledger as evidence.

**2. Authorship does not confer authority.** A post-mortem report may create proposals and
drafts and nothing else. Enforced as an actor capability in
`lifecycle.ACTOR_ALLOWED_TARGETS`, so `evaluate`/`approve`/`activate`/`retire`/`rollback`
all raise `ForbiddenActor` for `actor_kind="postmortem"`.

## Modules

| file | responsibility |
|---|---|
| `manifest.py` | deny-by-default manifest schema. Rejects permission grants, approval bypass, scope/evidence weakening, secret material and external side effects, each with an explicit `code` + `reason`. |
| `lifecycle.py` | the state machine, actor capabilities, and `apply_content_change` (hash-keyed invalidation). |
| `evaluate.py` | versioned eval sets (safety + out-of-scope cases mandatory), per-case + aggregate results, and the activation gate including **safety regression** detection. |
| `store.py` | the only writer: `propose / draft / edit_content / evaluate / approve / activate / retire / rollback`, plus dispatch attribution. |
| `db/repo_skills.py` | one statement per function; no policy. |

## Why the aggregate never decides

`passed = (every safety case passed) and (pass_rate >= min_pass_rate)`. A safety
regression is normally small in count — 19/20 passing looks great while the one failure is
"acted outside its permitted repo". So a good aggregate cannot rescue a failed safety
case, and a case that passed last time and fails now raises `ActivationRefused` with code
`safety_regression` even when nothing else changed. Every result also stores
`eval_set_version`, `evaluator_version`, `prompt_version` and `model_id` so a pass can be
reproduced instead of argued about.

## Manifest rejections

`validate_manifest()` returns `{"ok", "code", "reason", "manifest"}`; `parse_manifest()`
raises `ManifestError`. Codes: `permission_grant`, `approval_bypass`, `scope_weakening`,
`evidence_weakening`, `secret_exposure`, `external_side_effects`, plus structural ones
(`bad_version`, `unknown_field`, …). Unknown top-level keys are **rejected, not ignored** —
ignoring them is how `auto_approve: true` would smuggle itself in.

## CLI

Every mutation requires `--as <identity>`; it is recorded on the ledger row.

```bash
clams skills --as op@example.com propose --manifest skills/gate-retry/manifest.json \
             --content-file skills/gate-retry/body.md
clams skills --as op@example.com draft gate.retry-hint 1.0.0
clams skills --as op@example.com evaluate gate.retry-hint 1.0.0 \
             --evaluator-version e2 --prompt-version p9 --model claude-x
clams skills --as lead@example.com approve gate.retry-hint 1.0.0
clams skills --as op@example.com activate gate.retry-hint 1.0.0
clams skills --as op@example.com rollback gate.retry-hint
```

Exit codes: `0` done · `2` bad input / no database / missing `--as` · `3` **refused by a
gate** (illegal transition, unauthorized approver, failed evaluation, unsafe manifest).

## Rollback

`rollback` restores the last superseded deployment, but only if that version is *still*
valid (approval for its current hash + a passing evaluation for its current hash). It is
not an escape hatch from the gates. The superseded `skill_deployments` row keeps its
identity and gets `rolled_back_at` stamped; a **new** deployment row is inserted for the
restored version. So the table reads as a timeline: what was live, when it was replaced,
when it was rolled back, and what replaced it.

## Dispatch attribution

`planner/dispatch.py` stamps the live skill versions into `task_skill_versions` in the
same transaction as the claim (`phase='dispatch'`), and `record_result_skills()` writes
`phase='result'`. Post-mortems attribute an outcome to what was live *then*; a difference
between the two phases means the skill changed underneath the task. Attribution is
best-effort: a database that predates migration 015 still dispatches.

## Remaining work

* Call `record_result_skills()` from the task-result ingest path (outside this change's
  writable scope) so `phase='result'` rows are written automatically.
* `SkillStore(allowed_approvers=[...])` is not yet fed from config; today any named human
  may approve (self-approval and automated identities are still refused).
