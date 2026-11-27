# Worker progress and operator handoffs

Schema: `migrations/012_worker_progress_handoffs.sql` (schema_version 12).

Two new append-only tables plus the code paths that write and read them. Both
exist so a running worker can be *seen* and can *ask for help* without ever
becoming a writer of domain state.

## Guarantees

| Guarantee | Where it is enforced |
| --- | --- |
| A stale worker cannot write progress | `repo_progress.append_progress` fences on `tasks.attempt` + `tasks.lease_token` |
| Replayed / out-of-order lines are dropped | `seq` must be strictly greater than the stored max for `(task_id, attempt_no)`; `UNIQUE(task_id, attempt_no, seq)` backs it at the storage layer |
| Free text cannot grow unbounded | `clams.handoffs.bounded_text` clamps every text field and appends `...[truncated]` |
| `kind` / `status` / `disposition` are closed vocabularies | `_ClosedEnum.coerce` raises on anything else |
| Handoff history survives a restart | plain durable rows (`synchronous=FULL`); tests close and reopen the DB, including read-only |
| Dispositions never move backwards | `handoffs.HANDOFF_TRANSITIONS` forward-only table; re-applying the current status is a no-op |
| Task briefs are immutable | no code path in the new modules issues `UPDATE/INSERT/DELETE` on `tasks`; a `scope_change` handoff stores the ask in `requested_action` only |
| The Planner stays the only domain writer | the API enqueues a `dispose_handoff` command; `planner/commands.py` applies it |

## worker_progress

```
id, task_id, attempt_no, lease_token, seq, kind, text, created_at
UNIQUE (task_id, attempt_no, seq)
```

`kind` is one of `status | log | tool | note | warning | error`.

Fencing order (first failure wins, and a rejection is a pure read):

1. `unknown_task` — no such task
2. `stale_attempt` — `attempt_no != tasks.attempt` (the task was retried)
3. `stale_lease` — `lease_token != tasks.lease_token` (the lease was reclaimed)
4. `non_monotonic_seq` — `seq <= MAX(seq)` already stored for that attempt

Each attempt gets its own seq space, so a retry restarts at 1 under the new
lease. Rejected messages are surfaced as a `warn` event on the timeline rather
than silently dropped: a stale executor is worth seeing, but it changes nothing.

## handoffs

```
id, workflow_id, task_id, attempt_no, kind, reason, requested_action,
status, disposition, disposed_by, created_at, updated_at
```

* `kind`: `blocked | needs_decision | scope_change | stalled | other`
* `status`: `open -> acknowledged -> resolved | rejected`
  (a direct `open -> resolved|rejected` dismissal is allowed; nothing moves
  backwards, and re-applying the current status is a no-op so a replayed
  command cannot corrupt history)
* `disposition`: `'' | accepted | denied | deferred | retried | cancelled | other`

Recording is idempotent on the natural key
`(task_id, attempt_no, kind, reason, requested_action)`: a redelivered bus
message appends nothing and returns the existing id with `created=False`.
Handoffs for tasks that a replan has since deleted are still recorded — the
history outlives the task row.

Every accepted transition is mirrored into the append-only `events` timeline,
which is what makes the disposition trail append-safe rather than a single
mutable column.

### Length bounds

| Field | Max | Marker when cut |
| --- | --- | --- |
| `worker_progress.text` | 500 | `...[truncated]` |
| `handoffs.reason` | 1000 | `...[truncated]` |
| `handoffs.requested_action` | 500 | `...[truncated]` |
| `handoffs.disposed_by` | 100 | `...[truncated]` |
| ids / short labels | 200 | `...[truncated]` |

The marker is inside the budget, so stored text is always `<= max` characters.
Newlines and tabs are collapsed so one progress row stays one line.

## Ingestion

`planner/loop.py::_consume_events` routes `task.progress` and `task.handoff`
messages to `planner/commands.py::ingest_worker_payload` **before** the pydantic
message contracts are consulted (the contracts only model the core event types,
and `contracts.py` is out of scope here). Ingestion is idempotent on the bus
message id via `processed_events`.

```
executor -> clams:events -> loop._consume_events
                              -> commands.ingest_worker_payload
                                   -> repo_progress.append_progress (fenced)
                                   -> repo_handoffs.record_handoff  (deduped)
                                   -> events row (surfaced to the operator)
```

## Operator surface (API)

Read-only (`mode=ro` connection):

* `GET /api/v1/handoffs?status=&workflow_id=&task_id=&kind=&limit=`
* `GET /api/v1/handoffs/{id}` — handoff + surrounding progress + audit trail
* `GET /api/v1/tasks/{id}/progress?attempt_no=&after_id=&limit=`

Write path — **through the command inbox only**:

* `POST /api/v1/handoffs/{id}/dispose` `{status, disposition?, note?}` -> `202`
  with a `command_id`. The API inserts a `dispose_handoff` row into `commands`
  (its only permitted write target); the planner applies the transition when it
  drains the inbox, so the handoff stays `open` until then.

## Brief immutability

A `scope_change` handoff is a *request*: the ask lands in `requested_action`
and an event says so explicitly ("brief left immutable - answer via
dispose_handoff or replan"). The operator answers by disposing the handoff
and, if they agree, replanning through the normal `replan` / `apply_plan`
commands. `tests/unit/test_handoffs.py` asserts the task's
`title/objective/acceptance/brief_json` are byte-identical across recording,
ingestion and disposition, and statically guards the new modules against any
write to `tasks`.

## Known pre-existing failures (not touched by this change)

* `tests/unit/test_db_repos.py::test_migrations_apply_once` and two
  `tests/unit/test_cli.py` tests hardcode `schema_version == 8`; they already
  failed at `11` before this change and now report `12`.
* `tests/unit/test_bus.py::test_redis_bus_roundtrip` needs a live Redis.
