"""GitHub webhook intake tests: signature, size bound, normalization, ledger.

Synchronous and stdlib-only: the pure functions in ``clams.github.webhooks``
are exercised directly and FastAPI is never imported (asserted at the bottom).
The DB half uses the real migrations, so ``github_events`` is tested as it will
actually be created.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any, Optional

import pytest

from clams.db import connect, migrate
from clams.db import repo_commands, repo_github_events
from clams.github import webhooks as wh

SECRET = "whsec_unit_test_secret"
ALLOW = ["acme/widgets"]
#: Marker for free-text that must never reach storage.
RAW_MARKER = "RAW-PAYLOAD-TEXT-MUST-NOT-BE-PERSISTED"


def pr_payload(action: str = "synchronize", **overrides: Any) -> dict:
    payload = {
        "action": action,
        "number": 42,
        "repository": {"full_name": "acme/widgets", "name": "widgets"},
        "sender": {"login": "octocat"},
        "pull_request": {
            "number": 42,
            "state": "open",
            "title": "Fix the flux capacitor",
            "body": RAW_MARKER + " " + ("filler " * 400),
            "base": {"ref": "main"},
            "head": {"ref": "feature/flux", "sha": "a" * 40},
            "user": {"login": "octocat"},
        },
    }
    payload.update(overrides)
    return payload


def encode(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def sign(body: bytes, secret: str = SECRET) -> str:
    return wh.compute_signature(secret, body)


def handle(raw_body: bytes, signature: Optional[str], delivery_id: str,
           event: Optional[str], conn: sqlite3.Connection):
    """Exactly what ``POST /webhooks/github`` does: validate, then persist.

    Returns ``(record_or_None, error_or_None)``. Because
    :func:`clams.github.webhooks.process_webhook_body` raises before any record
    exists, a rejection here provably cannot enqueue work.
    """
    try:
        record = wh.process_webhook_body(SECRET, raw_body, signature,
                                         delivery_id, event,
                                         allowed_repos=ALLOW)
    except wh.WebhookError as exc:
        return None, exc
    repo_github_events.record_delivery(conn, record)
    return record, None


@pytest.fixture()
def db(tmp_path):
    conn = connect(str(tmp_path / "clams.db"))
    migrate(conn)
    yield conn
    conn.close()


def command_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM commands ORDER BY command_id").fetchall()


def event_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM github_events ORDER BY delivery_id").fetchall()


# ------------------------------------------------------------- signature
def test_valid_signature_is_accepted() -> None:
    body = encode(pr_payload())
    assert wh.verify_webhook_signature(SECRET, body, sign(body)) is True


def test_tampered_body_is_rejected() -> None:
    body = encode(pr_payload())
    signature = sign(body)
    tampered = encode(pr_payload(number=43))
    assert wh.verify_webhook_signature(SECRET, tampered, signature) is False
    # a one-character flip in the digest must also fail
    flipped = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    assert wh.verify_webhook_signature(SECRET, body, flipped) is False


def test_wrong_secret_is_rejected() -> None:
    body = encode(pr_payload())
    assert wh.verify_webhook_signature("other-secret", body, sign(body)) is False


@pytest.mark.parametrize("header", [
    None, "", "   ", "sha256", "sha1=" + "ab" * 20, "sha256=",
    "sha256=zz" * 32, "sha256=" + "ab" * 20, "sha256=" + "ab" * 33,
    b"not-a-header", 12345,
])
def test_missing_or_malformed_signature_header_is_rejected(header: Any) -> None:
    body = encode(pr_payload())
    # never raises, always False: the failure path is uniform
    assert wh.verify_webhook_signature(SECRET, body, header) is False


def test_signature_verification_uses_constant_time_compare() -> None:
    import inspect
    source = inspect.getsource(wh.verify_webhook_signature)
    assert "hmac.compare_digest" in source
    # everything after the docstring must compare with compare_digest only
    start = source.index('"""', source.index('"""') + 3) + 3
    assert "==" not in source[start:]
    assert "hmac.compare_digest" in inspect.getsource(wh)


def test_missing_header_is_reported_as_401_and_never_parsed() -> None:
    body = encode(pr_payload())
    with pytest.raises(wh.SignatureMissingError) as exc:
        wh.process_webhook_body(SECRET, body, None, "d-1", "pull_request",
                                allowed_repos=ALLOW)
    assert exc.value.status_code == 401
    assert SECRET not in str(exc.value) and "sha256=" not in str(exc.value)


def test_invalid_signature_is_401() -> None:
    body = encode(pr_payload())
    with pytest.raises(wh.SignatureInvalidError) as exc:
        wh.process_webhook_body(SECRET, body, "sha256=" + "0" * 64, "d-1",
                                "pull_request", allowed_repos=ALLOW)
    assert exc.value.status_code == 401


def test_unconfigured_secret_fails_closed() -> None:
    body = encode(pr_payload())
    with pytest.raises(wh.WebhookNotConfiguredError) as exc:
        wh.process_webhook_body("", body, sign(body), "d-1", "pull_request",
                                allowed_repos=ALLOW)
    assert exc.value.status_code == 503


# ----------------------------------------------------------- size bound
def test_oversized_body_is_rejected_before_hashing() -> None:
    big = b"x" * 4096
    assert wh.enforce_size_limit(big, 4096) == 4096
    with pytest.raises(wh.BodyTooLargeError) as exc:
        wh.enforce_size_limit(big, 4095)
    assert exc.value.status_code == 413
    # str bodies are measured in bytes, not characters
    assert wh.byte_size("é" * 100) == 200


def test_oversized_delivery_is_rejected_end_to_end() -> None:
    body = encode(pr_payload())
    with pytest.raises(wh.BodyTooLargeError):
        wh.process_webhook_body(SECRET, body, sign(body), "d-1",
                                "pull_request", max_bytes=len(body) - 1,
                                allowed_repos=ALLOW)


def test_nonsensical_size_limit_is_a_programmer_error() -> None:
    with pytest.raises(ValueError):
        wh.enforce_size_limit(b"x", 0)


# ------------------------------------------------------- event/action set
@pytest.mark.parametrize("event,action", [
    ("pull_request", "opened"), ("pull_request", "reopened"),
    ("pull_request", "synchronize"), ("pull_request", "ready_for_review"),
    ("pull_request", "closed"), ("pull_request_review", "submitted"),
    ("check_suite", "completed"), ("check_run", "completed"),
])
def test_supported_actions_are_accepted(event: str, action: str) -> None:
    payload = pr_payload(action)
    if event == "check_suite":
        payload = {"action": action, "check_suite": {
            "id": 7001, "conclusion": "success", "head_sha": "b" * 40,
            "head_branch": "feature/flux", "check_runs_count": 3,
            "pull_requests": [{"number": 42}]},
            "repository": {"full_name": "acme/widgets"}}
    elif event == "check_run":
        payload = {"action": action, "check_run": {
            "id": 8002, "name": "unit-tests", "conclusion": "failure",
            "head_sha": "c" * 40,
            "check_suite": {"head_branch": "feature/flux"},
            "pull_requests": [{"number": 42}]},
            "repository": {"full_name": "acme/widgets"}}
    elif event == "pull_request_review":
        payload = {"action": action, "review": {
            "state": "changes_requested", "user": {"login": "reviewer"}},
            "repository": {"full_name": "acme/widgets"},
            "pull_request": {"number": 42, "title": "Fix the flux capacitor",
                             "head": {"ref": "feature/flux", "sha": "a" * 40}}}
    record = wh.normalize_event(payload, "d-" + event + action, event)
    assert record.event == event and record.action == action
    assert record.repo == "acme/widgets"
    assert record.pr_number == 42
    assert record.head_sha in {"a" * 40, "b" * 40, "c" * 40}


@pytest.mark.parametrize("event,action", [
    ("pull_request", "labeled"), ("pull_request", "edited"),
    ("pull_request", "assigned"), ("pull_request", "review_requested"),
    ("pull_request_review", "edited"), ("pull_request_review", "dismissed"),
    ("check_suite", "requested"), ("check_suite", "progressed"),
    ("check_run", "created"), ("check_run", "rerequested"),
    ("push", "created"), ("issue", "opened"), ("workflow_run", "completed"),
])
def test_unsupported_actions_are_rejected(event: str, action: str) -> None:
    payload = pr_payload(action)
    if event == "check_suite":
        payload = {"action": action, "check_suite": {"id": 1},
                   "repository": {"full_name": "acme/widgets"}}
    elif event == "check_run":
        payload = {"action": action, "check_run": {"id": 1},
                   "repository": {"full_name": "acme/widgets"}}
    elif event == "pull_request_review":
        payload = {"action": action, "review": {"state": "commented"},
                   "repository": {"full_name": "acme/widgets"}}
    elif event in ("push", "issue", "workflow_run"):
        payload = {"action": action, "repository": {"full_name": "acme/widgets"}}
    with pytest.raises(wh.UnsupportedEventError) as exc:
        wh.normalize_event(payload, "d-x", event)
    assert exc.value.status_code == 422


def test_unsupported_action_is_rejected_even_with_a_valid_signature() -> None:
    body = encode(pr_payload("labeled"))
    with pytest.raises(wh.UnsupportedEventError):
        wh.process_webhook_body(SECRET, body, sign(body), "d-9",
                                "pull_request", allowed_repos=ALLOW)


def test_event_header_must_be_a_supported_event() -> None:
    body = encode({"action": "completed",
                   "repository": {"full_name": "acme/widgets"}})
    with pytest.raises(wh.UnsupportedEventError):
        wh.normalize_event(json.loads(body), "d-10", "deployment_status")


def test_payload_without_repository_or_delivery_id_is_invalid() -> None:
    with pytest.raises(wh.PayloadInvalidError):
        wh.normalize_event({"action": "opened", "pull_request": {}}, "d-11")
    with pytest.raises(wh.PayloadInvalidError):
        wh.normalize_event(pr_payload("opened"), "")
    with pytest.raises(wh.PayloadInvalidError):
        wh.parse_payload(b"\xff\xfe not json")


# ------------------------------------------------------ summary redaction
def test_summary_is_bounded_and_single_line() -> None:
    payload = pr_payload("opened")
    payload["pull_request"]["title"] = ("evil\n" * 500) + "tail"
    record = wh.normalize_event(payload, "d-12", "pull_request")
    assert len(record.summary) <= wh.MAX_SUMMARY_CHARS
    assert "\n" not in record.summary and "\t" not in record.summary


def test_summary_redacts_credentials_and_drops_free_text() -> None:
    payload = pr_payload("opened")
    payload["pull_request"]["title"] = (
        "rotate ghp_" + "T" * 36 + " and https://user:hunter2@github.com/x")
    record = wh.normalize_event(payload, "d-13", "pull_request")
    assert "ghp_" not in record.summary
    assert "hunter2" not in record.summary
    assert "[redacted]" in record.summary
    assert RAW_MARKER not in record.summary          # body never surfaces
    assert RAW_MARKER not in json.dumps(record.as_row())


def test_head_sha_and_refs_are_bounded() -> None:
    payload = pr_payload("synchronize")
    payload["pull_request"]["head"]["sha"] = "d" * 400
    payload["pull_request"]["head"]["ref"] = "r" * 500
    record = wh.normalize_event(payload, "d-14", "pull_request")
    assert record.head_sha is None                   # not a plausible SHA
    assert record.head_ref is not None
    assert len(record.head_ref) <= wh.MAX_REF_CHARS


# ------------------------------------------------------------ allowlist
def test_repo_allowlist_fails_closed() -> None:
    assert wh.repo_is_allowed("acme/widgets", ["acme/widgets"]) is True
    assert wh.repo_is_allowed("ACME/Widgets", ["acme/widgets"]) is True
    assert wh.repo_is_allowed("acme/other", ["acme/*"]) is True
    assert wh.repo_is_allowed("acme/other", ["acme/widgets"]) is False
    assert wh.repo_is_allowed("evil/widgets", ["acme/widgets"]) is False
    assert wh.repo_is_allowed("acme/widgets", []) is False
    assert wh.repo_is_allowed("acme/widgets", None) is False


def test_foreign_repo_delivery_is_rejected() -> None:
    payload = pr_payload("opened")
    payload["repository"]["full_name"] = "evil/widgets"
    body = encode(payload)
    with pytest.raises(wh.RepoNotAllowedError) as exc:
        wh.process_webhook_body(SECRET, body, sign(body), "d-15",
                                "pull_request", allowed_repos=ALLOW)
    assert exc.value.status_code == 403


# ------------------------------------------------------- durable ledger
def test_github_events_table_exists_with_expected_columns(db) -> None:
    cols = {r["name"] for r in db.execute(
        "PRAGMA table_info(github_events)").fetchall()}
    assert cols == {"delivery_id", "repo", "event_action", "pr_number",
                    "head_ref", "head_sha", "check_id", "summary",
                    "received_at", "processed_state"}
    pk = [r["name"] for r in db.execute(
        "PRAGMA table_info(github_events)").fetchall() if r["pk"]]
    assert pk == ["delivery_id"]


def test_accepted_delivery_writes_ledger_and_exactly_one_command(db) -> None:
    body = encode(pr_payload("opened"))
    record, error = handle(body, sign(body), "d-100", "pull_request", db)
    assert error is None and record is not None
    events = event_rows(db)
    assert len(events) == 1
    assert events[0]["delivery_id"] == "d-100"
    assert events[0]["event_action"] == "pull_request.opened"
    assert events[0]["repo"] == "acme/widgets"
    assert events[0]["pr_number"] == 42
    assert events[0]["head_ref"] == "feature/flux"
    assert events[0]["head_sha"] == "a" * 40
    assert events[0]["processed_state"] == "new"
    assert events[0]["received_at"]
    commands = command_rows(db)
    assert len(commands) == 1
    assert commands[0]["kind"] == repo_github_events.COMMAND_KIND
    assert commands[0]["target"] == "acme/widgets"
    payload = json.loads(commands[0]["payload"])
    assert payload["delivery_id"] == "d-100"
    assert payload["source"] == "github_webhook"
    assert RAW_MARKER not in commands[0]["payload"]


def test_duplicate_delivery_id_insert_is_a_noop(db) -> None:
    body = encode(pr_payload("synchronize"))
    first, err = handle(body, sign(body), "d-200", "pull_request", db)
    assert err is None
    again, err2 = handle(body, sign(body), "d-200", "pull_request", db)
    assert err2 is None and again is not None
    assert len(event_rows(db)) == 1
    assert len(command_rows(db)) == 1          # never enqueued twice
    assert repo_github_events.insert_event(db, first) is False
    assert repo_github_events.get_event(db, "d-200")["delivery_id"] == "d-200"


def test_bad_signature_and_unsupported_action_enqueue_nothing(db) -> None:
    body = encode(pr_payload("opened"))
    handle(body, "sha256=" + "1" * 64, "d-300", "pull_request", db)
    handle(body, None, "d-301", "pull_request", db)
    tampered = encode(pr_payload("opened")) + b" "
    handle(tampered, sign(body), "d-302", "pull_request", db)
    labeled = encode(pr_payload("labeled"))
    handle(labeled, sign(labeled), "d-303", "pull_request", db)
    handle(b"{not json", sign(b"{not json"), "d-304", "pull_request", db)
    assert command_rows(db) == []
    assert event_rows(db) == []


def test_no_raw_payload_is_persisted_anywhere(db) -> None:
    payload = pr_payload("opened")
    payload["pull_request"]["body"] = RAW_MARKER + (" huge " * 2000)
    payload["sender"]["node_id"] = RAW_MARKER + "-sender"
    body = encode(payload)
    record, error = handle(body, sign(body), "d-400", "pull_request", db)
    assert error is None
    assert len(body) > 10_000                      # a genuinely big delivery
    stored = json.dumps([dict(r) for r in event_rows(db)]
                        + [dict(r) for r in command_rows(db)])
    assert RAW_MARKER not in stored
    row = dict(event_rows(db)[0])
    assert set(row) == {"delivery_id", "repo", "event_action", "pr_number",
                        "head_ref", "head_sha", "check_id", "summary",
                        "received_at", "processed_state"}
    assert len(row["summary"]) <= wh.MAX_SUMMARY_CHARS


def test_check_suite_delivery_records_check_identity(db) -> None:
    payload = {"action": "completed", "repository": {"full_name": "acme/widgets"},
               "check_suite": {"id": 7001, "conclusion": "success",
                               "head_sha": "b" * 40, "head_branch": "feature/flux",
                               "check_runs_count": 3,
                               "pull_requests": [{"number": 42}]}}
    body = encode(payload)
    record, error = handle(body, sign(body), "d-500", "check_suite", db)
    assert error is None
    row = event_rows(db)[0]
    assert row["check_id"] == "7001"
    assert row["event_action"] == "check_suite.completed"
    assert row["pr_number"] == 42 and row["head_sha"] == "b" * 40
    repo_github_events.mark_processed(db, "d-500", "done")
    assert repo_github_events.counts_by_state(db) == {"done": 1}
    with pytest.raises(ValueError):
        repo_github_events.mark_processed(db, "d-500", "wat")


# ------------------------------------------------------- import hygiene
def test_webhooks_module_imports_only_the_stdlib() -> None:
    import ast
    from pathlib import Path
    tree = ast.parse(Path(wh.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "hashlib", "hmac", "json", "dataclasses",
                        "typing"}, f"non-stdlib import in webhooks.py: {imported}"


# ------------------------------------------------------- secret hygiene
def test_webhook_settings_are_plain_properties_not_computed_fields() -> None:
    """The webhook secret must be invisible to settings serialization.

    A pydantic ``computed_field`` IS emitted by ``model_dump()`` /
    ``model_dump_json()``, so any settings dump - log line, debug payload -
    would echo the secret. Parsed from source so this stays stdlib-only.
    """
    import ast
    from pathlib import Path

    cfg = Path(wh.__file__).parents[1] / "config.py"
    tree = ast.parse(cfg.read_text(encoding="utf-8"))
    settings_cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "Settings")
    watched = {"github_webhook_secret", "github_allowed_repos"}
    found = 0
    for node in settings_cls.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in watched:
            continue
        found += 1
        decorators = {d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")
                      for d in node.decorator_list}
        assert "computed_field" not in decorators, (
            f"{node.name} is a computed_field, so model_dump() would leak it")
        assert "property" in decorators, f"{node.name} must be a property"
    assert found == 2, "both github webhook settings must exist on Settings"


def test_secret_never_appears_in_settings_dump(monkeypatch) -> None:
    """Runtime proof: attribute access works, serialization does not carry it."""
    pydantic_settings = pytest.importorskip("pydantic_settings")
    del pydantic_settings
    from clams.config import Settings

    canary = "CANARY-SECRET-MUST-NOT-BE-SERIALIZED"
    monkeypatch.setenv("CLAMS_GITHUB_WEBHOOK_SECRET", canary)
    monkeypatch.setenv("CLAMS_GITHUB_ALLOWED_REPOS", "acme/widgets, other/repo")
    s = Settings()

    assert s.github_webhook_secret == canary
    assert s.github_allowed_repos == ["acme/widgets", "other/repo"]

    dumped = s.model_dump()
    assert "github_webhook_secret" not in dumped
    assert canary not in json.dumps(dumped, default=str)
    assert canary not in s.model_dump_json()
