# GitHub webhook intake

`POST /webhooks/github` turns an inbound GitHub delivery into (a) one durable
`github_events` ledger row and (b) one `github_event` command-inbox record.
It never writes domain state, and it never stores the raw payload.

## The security contract

Implemented in `src/clams/github/webhooks.py` (stdlib only: `hmac`, `hashlib`,
`json`, `dataclasses`), so the whole path is unit-testable without FastAPI.

`process_webhook_body(secret, raw_body, signature_header, delivery_id, event,
max_bytes=…, allowed_repos=…)` runs the checks **in this order** and raises
before anything exists, which is why a rejection cannot enqueue work:

| step | failure | answer |
|---|---|---|
| 1. secret configured | no `CLAMS_GITHUB_WEBHOOK_SECRET` | 503 (fail closed) |
| 2. size bound | body > `DEFAULT_MAX_BODY_BYTES` (1 MiB) | 413 |
| 3. signature | missing `X-Hub-Signature-256` | 401 |
| 3. signature | tampered body / wrong secret / malformed header | 401 |
| 4. parse | body is not a JSON object | 400 |
| 5. allowlist | `(event, action)` not supported | 422 |
| 6. allowlist | repository not in `CLAMS_GITHUB_ALLOWED_REPOS` | 403 |

**Constant-time failure.** `verify_webhook_signature` always performs exactly
one HMAC-SHA256 over the body and one `hmac.compare_digest` between two
equal-length buffers. A missing, short, non-hex or wrong-scheme header is
compared against a same-length zero buffer instead of returning early, so
timing does not reveal *why* verification failed.

**Authentication is the signature.** GitHub cannot present the operator bearer
token, so the route sits outside the `/api/v1` bearer gate on purpose; the HMAC
secret plus the repository allowlist are the credential. An empty allowlist
denies everything (fail closed).

## Supported events

Only these `(event, action)` pairs are accepted (`SUPPORTED_ACTIONS`):

* `pull_request`: `opened`, `reopened`, `synchronize`, `ready_for_review`, `closed`
* `pull_request_review`: `submitted`
* `check_suite`, `check_run`: `completed`

Everything else — `labeled`, `edited`, `push`, `workflow_run`, … — is a 422 and
enqueues nothing.

## What is stored

`github_events` (`migrations/013_github_events.sql`) holds only normalized,
bounded, redacted fields:

`delivery_id` (PRIMARY KEY, the idempotency key), `repo`, `event_action`
(e.g. `pull_request.synchronize`), `pr_number`, `head_ref`, `head_sha`,
`check_id`, `summary`, `received_at`, `processed_state`.

`summary` is a single line of at most `MAX_SUMMARY_CHARS` (240) characters.
Free text (PR bodies, review bodies, commit messages) is **dropped**, newlines
and control characters are collapsed, `scheme://user:pass@host` credentials and
token-shaped blobs (`ghp_…`, `github_pat_…`, `xoxb-…`, `-----BEGIN…`,
`…token=…`) are replaced with `[redacted]`, and the result is hard-truncated.

`delivery_id` is the PRIMARY KEY and the insert is `INSERT OR IGNORE` inside
the same `BEGIN IMMEDIATE` transaction as the command insert
(`repo_github_events.record_delivery`), so a GitHub redelivery is a no-op: one
ledger row, one command, ever. A duplicate answers `200 {"status":"duplicate"}`.

## Configuration

`src/clams/config.py`:

* `github_webhook_secret` — `CLAMS_GITHUB_WEBHOOK_SECRET`, **deployment env
  only**. It is a `computed_field`, not a Settings field, so it cannot be
  echoed by `.env.example` parity tooling or by a `model_dump()` of settings,
  and it is never logged: rejections report only the reason.
* `github_allowed_repos` — `CLAMS_GITHUB_ALLOWED_REPOS`, comma-separated
  `owner/repo` or `owner/*`.

They are computed fields rather than plain fields also because `.env.example`
is out of bounds for this change and `tests/unit/test_env_parity.py` requires
the two to match exactly.

## Migration note

`migrations/014_github_event_command.sql` adds `github_event` to the
`commands.kind` CHECK constraint (the same table-rebuild pattern as
004/007/008/012) — the DB CHECK, not `repo_commands.COMMAND_KINDS`, is the
authoritative gate.

## Remaining work

* Planner-side handling of `kind='github_event'` is not implemented here
  (`src/clams/planner/**` is out of scope). Until it lands, the planner drains
  the command and marks it `rejected (unknown command kind)` — visible, and no
  work is started.
* Nothing yet prunes old `github_events` rows; hook it into the retention pass.
* `GET /api/v1/github_events` is read-only visibility; a `processed_state`
  drain belongs with the planner consumer.
