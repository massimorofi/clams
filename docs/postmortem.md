# Post-mortem job (`clams postmortem`)

Read-only, stdlib-only evidence capture for **terminal** workflows
(`done`, `failed`, `cancelled`). It never writes to a domain table and never
proposes to act on its own: every recommendation is a proposal for a human.

## Package layout — `src/clams/postmortem/`

| module      | responsibility |
|-------------|----------------|
| `select.py` | deterministic, bounded selection of terminal workflows by `status` / `since` / `limit`. Ordering is a total order (`updated_at DESC, created_at DESC, workflow_id ASC`), so two runs over unchanged data return the same list. |
| `snapshot.py` | read-only evidence snapshot built from `workflows`, `tasks`, `task_attempts` and `events`. Every record is addressable by a stable ref (`workflow:<id>`, `task:<id>`, `attempt:<task>:<n>`, `event:<id>`). Only `SELECT`s are issued. |
| `report.py` | the versioned report contract: `REPORT_SCHEMA_VERSION`, findings that each cite evidence refs, recommendations flagged as proposals (`recommendations_are_proposals: true`). |
| `validate.py` | strict validation. Rejects an unknown schema version, a finding without evidence refs, a dangling ref, a non-proposal recommendation, and an over-budget payload. |
| `__init__.py` | `run_postmortem(conn, ...)` = select → snapshot → report → validate, plus `report_filename()`. |

## Bounded everything

`SnapshotBudget` caps workflows, tasks per workflow, attempts per task, events
per workflow, chars per field and total payload chars; `MAX_REPORT_BYTES`
caps the serialised report. When a bound is hit the snapshot is **truncated and
marked** (`[TRUNCATED]` in `truncations[]`, plus an `evidence_truncated`
finding) — it never fails silently and never pretends to be complete.

## CLI

```
clams postmortem [--status done|failed|cancelled]... [--since 'YYYY-MM-DD HH:MM:SS']
                 [--limit N] [--out reports] [--out-file PATH] [--db PATH]
                 [--max-report-bytes N] [--print]
```

Writes `reports/postmortem_<workflowid>_<utc>.json`. Exit codes: `0` written,
`2` missing/unopenable database or bad arguments, `3` validation failure
(nothing is written in that case). The subcommand deliberately does not need
`pydantic-settings`, so it runs in a minimal environment; `--db` falls back to
`CLAMS_DB_PATH`.

## Report shape (schema_version 1.0.0)

```json
{
  "schema_version": "1.0.0",
  "report_type": "postmortem",
  "generated_at": "2026-11-26T12:00:00Z",
  "selection": {"statuses": ["done","failed","cancelled"], "since": null,
                 "limit": 25, "matched": 5, "selected": 5,
                 "page_truncated": false},
  "workflow_ids": ["wf-aaaa", "..."],
  "workflows": [ {"workflow_id": "wf-aaaa", "status": "done", "...": "..."} ],
  "findings": [ {"finding_id": "F001", "type": "workflow_failed",
                 "severity": "error", "workflow_id": "wf-bbbb",
                 "summary": "...", "evidence_refs": ["workflow:wf-bbbb"]} ],
  "recommendations": [ {"recommendation_id": "R001", "status": "proposal",
                        "auto_applied": false,
                        "requires_human_approval": true, "...": "..."} ],
  "recommendations_are_proposals": true,
  "truncated": false,
  "truncations": [],
  "evidence": {"workflow:wf-bbbb": {"kind": "workflow", "...": "..."}}
}
```

## Tests

`tests/unit/test_postmortem.py` (synchronous, stdlib-only, temp SQLite + temp
reports dir) covers: deterministic and reproducible mixed success/failure
selection across two runs; every finding citing existing evidence; an
auto-applied recommendation being impossible (built *and* validated);
over-limit snapshots truncated with a marker; validation rejections; the CLI
writing a valid report and exiting non-zero on validation failure.

```
PYTHONPATH=src python3 -m pytest tests/unit/test_postmortem.py -o addopts=''
```
