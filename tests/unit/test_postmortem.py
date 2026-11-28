"""Post-mortem job: deterministic selection, read-only evidence snapshot,
versioned report contract, strict validation, and the CLI that writes it.

Synchronous and stdlib-only apart from pytest: a temp SQLite file plus a temp
reports directory, no network, no Redis, no LLM.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from clams.cli import main
from clams.db import connect, migrate, repo_attempts, repo_events
from clams.db import repo_projects, repo_tasks, repo_workflows
from clams.postmortem import (MAX_REPORT_BYTES, REPORT_SCHEMA_VERSION,
                              ReportValidationError, SnapshotBudget,
                              TRUNCATION_MARKER, build_report, build_snapshot,
                              run_postmortem, select_terminal_workflows,
                              validate_report)
from clams.postmortem.validate import validation_errors

PKG_DIR = Path(__file__).resolve().parents[2] / "src" / "clams" / "postmortem"

# fixed timestamps keep ordering (and therefore every assertion) stable
T0 = "2026-11-01 10:00:00"


def _touch(conn, table, pk, value, ts):
    conn.execute(f"UPDATE {table} SET created_at=?, updated_at=? "
                 f"WHERE {pk}=?", (ts, ts, value))


def _add_task(conn, wf, idx, title, status, attempt=1, ts=T0):
    tid = repo_tasks.create_task(
        conn, wf, idx, title, f"objective for {title}",
        f"acceptance for {title}", {"role": "worker"})
    if status != "pending":
        conn.execute("UPDATE tasks SET status=?, attempt=? WHERE task_id=?",
                     (status, attempt, tid))
    _touch(conn, "tasks", "task_id", tid, ts)
    return tid


def _seed(conn, *, extra_tasks=0):
    """One project, a mix of terminal + in-flight workflows, attempts, events."""
    pid = repo_projects.upsert_project(conn, "acme/widgets")
    specs = [
        ("wf-aaaa", "done", "2026-11-05 09:00:00"),
        ("wf-bbbb", "failed", "2026-11-04 09:00:00"),
        ("wf-cccc", "cancelled", "2026-11-03 09:00:00"),
        ("wf-dddd", "done", "2026-11-02 09:00:00"),
        ("wf-eeee", "failed", "2026-11-01 09:00:00"),
        ("wf-ffff", "running", "2026-11-06 09:00:00"),   # never eligible
    ]
    for wf, status, ts in specs:
        repo_workflows.create_workflow(conn, pid, "github", f"goal {wf}",
                                       workflow_id=wf)
        conn.execute("UPDATE workflows SET status=? WHERE workflow_id=?",
                     (status, wf))
        _touch(conn, "workflows", "workflow_id", wf, ts)

    # wf-bbbb: failed workflow, one task that burned 3 attempts on the gate
    t1 = _add_task(conn, "wf-bbbb", 1, "implement retry", "failed", attempt=3)
    for no, cls in ((1, "gate"), (2, "gate"), (3, "infra")):
        repo_attempts.record_attempt(
            conn, task_id=t1, workflow_id="wf-bbbb", attempt_no=no,
            status="failed", failure_class=cls,
            checks=[{"cmd": "pytest -q", "exit": 1, "tail": "1 failed"}],
            error_excerpt=f"attempt {no} blew up")
    # wf-eeee: failed workflow whose task predates migration 011
    _add_task(conn, "wf-eeee", 1, "legacy task", "failed", attempt=1)
    # wf-aaaa: clean success, one attempt recorded
    t3 = _add_task(conn, "wf-aaaa", 1, "add tests", "success", attempt=1)
    repo_attempts.record_attempt(conn, task_id=t3, workflow_id="wf-aaaa",
                                 attempt_no=1, status="success")
    # wf-dddd: succeeded after a replan
    conn.execute("UPDATE workflows SET replans=2 WHERE workflow_id='wf-dddd'")
    _add_task(conn, "wf-dddd", 1, "docs", "success", attempt=1)

    for wf in ("wf-bbbb", "wf-eeee", "wf-aaaa", "wf-dddd"):
        repo_events.append_event(conn, "workflow", wf, "info",
                                 f"workflow {wf} reached a terminal state")

    for i in range(extra_tasks):
        _add_task(conn, "wf-bbbb", 100 + i, f"bulk task {i}", "success",
                  attempt=1)
    return pid


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "clams.db")


@pytest.fixture()
def db(db_path):
    conn = connect(db_path)
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """CLI environment: an isolated, already-migrated db + data dir.

    The post-mortem subcommand is stdlib-only, so the fixture migrates the
    database directly instead of shelling through `clams migrate`.
    """
    db_file = tmp_path / "cli.db"
    conn = connect(str(db_file))
    migrate(conn)
    _seed(conn)
    conn.close()
    monkeypatch.setenv("CLAMS_DB_PATH", str(db_file))
    monkeypatch.setenv("CLAMS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLAMS_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("CLAMS_CONFIG_FILE", str(tmp_path / "none.yaml"))
    monkeypatch.delenv("CLAMS_ROLE", raising=False)
    return tmp_path


# --------------------------------------------------------------- selection ---
def test_selection_only_returns_terminal_workflows(db):
    _seed(db)
    rows = select_terminal_workflows(db)
    ids = [r["workflow_id"] for r in rows]
    assert "wf-ffff" not in ids                      # running: not eligible
    assert set(ids) == {"wf-aaaa", "wf-bbbb", "wf-cccc", "wf-dddd", "wf-eeee"}


def test_selection_is_deterministic_and_reproducible(db):
    """Two runs over the same data give byte-identical reports."""
    _seed(db)
    a = run_postmortem(db, generated_at="2026-11-26T00:00:00Z")
    b = run_postmortem(db, generated_at="2026-11-26T00:00:00Z")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert [r["workflow_id"] for r in select_terminal_workflows(db)] == \
        [r["workflow_id"] for r in select_terminal_workflows(db)]


def test_selection_is_reproducible_on_a_fresh_database(tmp_path):
    """Same seed, different file: same order, same report shape."""
    reports = []
    for i in (1, 2):
        conn = connect(str(tmp_path / f"clone{i}.db"))
        migrate(conn)
        _seed(conn)
        reports.append(run_postmortem(conn, generated_at="X"))
        conn.close()
    assert reports[0]["workflow_ids"] == reports[1]["workflow_ids"]
    assert [f["type"] for f in reports[0]["findings"]] == \
        [f["type"] for f in reports[1]["findings"]]


def test_selection_orders_newest_first_with_a_total_order(db):
    _seed(db)
    ids = [r["workflow_id"] for r in select_terminal_workflows(db)]
    assert ids == ["wf-aaaa", "wf-bbbb", "wf-cccc", "wf-dddd", "wf-eeee"]


def test_selection_honours_status_since_and_limit(db):
    _seed(db)
    assert [r["workflow_id"] for r in select_terminal_workflows(
        db, statuses=["failed"])] == ["wf-bbbb", "wf-eeee"]
    assert [r["workflow_id"] for r in select_terminal_workflows(
        db, since="2026-11-04 00:00:00")] == ["wf-aaaa", "wf-bbbb"]
    page = select_terminal_workflows(db, limit=2)
    assert [r["workflow_id"] for r in page] == ["wf-aaaa", "wf-bbbb"]


def test_selection_rejects_non_terminal_status_and_bad_limit(db):
    _seed(db)
    with pytest.raises(ValueError):
        select_terminal_workflows(db, statuses=["running"])
    with pytest.raises(ValueError):
        select_terminal_workflows(db, limit=0)
    with pytest.raises(ValueError):
        select_terminal_workflows(db, since="not-a-date")


# ----------------------------------------------------------------- snapshot ---
def test_snapshot_is_read_only(db):
    _seed(db)
    before = {t: db.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
              for t in ("workflows", "tasks", "task_attempts", "events")}
    ids = [r["workflow_id"] for r in select_terminal_workflows(db)]
    build_snapshot(db, ids)
    after = {t: db.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
             for t in ("workflows", "tasks", "task_attempts", "events")}
    assert before == after


def test_snapshot_captures_all_four_sources(db):
    _seed(db)
    snap = build_snapshot(db, ["wf-bbbb", "wf-aaaa"])
    kinds = {r["kind"] for r in snap.evidence.values()}
    assert kinds == {"workflow", "task", "task_attempt", "event"}
    assert "workflow:wf-bbbb" in snap.evidence
    assert any(r.startswith("attempt:") for r in snap.evidence)
    assert any(r.startswith("event:") for r in snap.evidence)
    assert any(r.startswith("task:") for r in snap.evidence)


def test_over_limit_snapshot_is_truncated_with_a_marker(db):
    """An over-budget snapshot is truncated and marked, never silent."""
    _seed(db, extra_tasks=6)
    budget = SnapshotBudget(max_tasks_per_workflow=2)
    ids = [r["workflow_id"] for r in select_terminal_workflows(db)]
    snap = build_snapshot(db, ids, budget=budget)
    assert snap.truncated is True
    assert any(t["collection"] == "tasks" for t in snap.truncations)
    for t in snap.truncations:
        assert t["marker"] == TRUNCATION_MARKER
        assert t["dropped"] > 0
    report = build_report(snap)
    assert report["truncated"] is True
    assert any(f["type"] == "evidence_truncated" for f in report["findings"])
    # the truncation finding cites evidence like every other finding
    for f in report["findings"]:
        assert f["evidence_refs"]


def test_workflow_page_limit_is_reported_as_truncation(db):
    _seed(db)
    snap = build_snapshot(db, ["wf-aaaa", "wf-bbbb", "wf-cccc"],
                          budget=SnapshotBudget(max_workflows=2))
    assert snap.workflow_ids == ["wf-aaaa", "wf-bbbb"]
    assert any(t["collection"] == "workflows" and t["marker"] == TRUNCATION_MARKER
               for t in snap.truncations)


def test_selection_page_truncation_is_recorded_in_the_report(db):
    _seed(db)
    report = run_postmortem(db, limit=2)
    assert report["selection"]["matched"] == 5
    assert report["selection"]["selected"] == 2
    assert report["selection"]["page_truncated"] is True


# ------------------------------------------------------------------- report ---
def test_report_declares_its_schema_version(db):
    _seed(db)
    report = run_postmortem(db)
    assert report["schema_version"] == REPORT_SCHEMA_VERSION
    assert report["report_type"] == "postmortem"
    assert report["generated_at"]


def test_every_finding_cites_evidence_that_exists(db):
    _seed(db)
    report = run_postmortem(db)
    assert report["findings"], "the seeded data must produce findings"
    evidence = report["evidence"]
    for f in report["findings"]:
        assert f["evidence_refs"], f"{f['finding_id']} cites nothing"
        for ref in f["evidence_refs"]:
            assert ref in evidence, f"{f['finding_id']} cites {ref}"
    # the failure we planted is reported
    types = {f["type"] for f in report["findings"]}
    assert {"workflow_failed", "task_failed", "gate_rejected"} <= types


def test_findings_are_derived_only_from_captured_evidence(db):
    _seed(db)
    report = run_postmortem(db)
    cited = {r for f in report["findings"] for r in f["evidence_refs"]}
    assert cited <= set(report["evidence"])
    assert set(report["evidence_refs"]) == set(report["evidence"])


def test_recommendations_are_always_proposals(db):
    _seed(db)
    report = run_postmortem(db)
    assert report["recommendations_are_proposals"] is True
    assert report["recommendations"], "findings must produce proposals"
    for r in report["recommendations"]:
        assert r["auto_applied"] is False
        assert r["status"] == "proposal"
        assert r["requires_human_approval"] is True


def test_an_auto_applied_recommendation_cannot_be_built():
    """There is no supported way to mark a recommendation as applied."""
    from clams.postmortem.report import new_recommendation
    rec = new_recommendation(recommendation_id="R999", title="t",
                             rationale="r")
    assert rec["auto_applied"] is False
    # even if a caller mutates it after the fact, validation rejects it
    bad = {"schema_version": REPORT_SCHEMA_VERSION, "report_type": "postmortem",
           "generated_at": "X", "workflow_ids": [], "findings": [],
           "recommendations": [dict(rec, auto_applied=True, status="applied",
                                    requires_human_approval=False)],
           "recommendations_are_proposals": True, "evidence": {}}
    with pytest.raises(ReportValidationError):
        validate_report(bad)


def test_a_finding_without_evidence_cannot_be_built():
    from clams.postmortem.report import new_finding
    with pytest.raises(ValueError):
        new_finding(finding_id="F1", ftype="task_failed", severity="error",
                    summary="no evidence", evidence_refs=[])
    with pytest.raises(ValueError):
        new_finding(finding_id="F1", ftype="task_failed", severity="error",
                    summary="no evidence", evidence_refs=["  "])


# --------------------------------------------------------------- validation ---
def test_validation_rejects_unknown_schema_version(db):
    _seed(db)
    report = run_postmortem(db)
    bad = copy.deepcopy(report)
    bad["schema_version"] = "99.0.0"
    with pytest.raises(ReportValidationError) as exc:
        validate_report(bad)
    assert any("schema_version" in e for e in exc.value.errors)


def test_validation_rejects_a_finding_without_evidence_refs(db):
    _seed(db)
    report = run_postmortem(db)
    bad = copy.deepcopy(report)
    bad["findings"][0]["evidence_refs"] = []
    with pytest.raises(ReportValidationError) as exc:
        validate_report(bad)
    assert any("evidence reference" in e for e in exc.value.errors)
    bad2 = copy.deepcopy(report)
    del bad2["findings"][0]["evidence_refs"]
    with pytest.raises(ReportValidationError):
        validate_report(bad2)


def test_validation_rejects_dangling_evidence_refs(db):
    _seed(db)
    bad = copy.deepcopy(run_postmortem(db))
    bad["findings"][0]["evidence_refs"] = ["task:does-not-exist"]
    with pytest.raises(ReportValidationError) as exc:
        validate_report(bad)
    assert any("unknown evidence reference" in e for e in exc.value.errors)


def test_validation_rejects_an_over_budget_payload(db):
    _seed(db)
    report = run_postmortem(db)
    assert MAX_REPORT_BYTES > len(json.dumps(report))
    with pytest.raises(ReportValidationError) as exc:
        validate_report(report, max_bytes=64)
    assert any("over the budget" in e for e in exc.value.errors)


def test_validation_rejects_a_missing_flag_or_field(db):
    _seed(db)
    report = run_postmortem(db)
    bad = copy.deepcopy(report)
    bad["recommendations_are_proposals"] = False
    with pytest.raises(ReportValidationError):
        validate_report(bad)
    bad2 = copy.deepcopy(report)
    del bad2["findings"]
    with pytest.raises(ReportValidationError):
        validate_report(bad2)
    assert validation_errors(report) == []


# ------------------------------------------------------------------ no SQL ---
def test_postmortem_package_issues_no_writes():
    """Grep the shipped source: no SQL write statement anywhere in the package."""
    import re
    banned = [r"\bINSERT\s+INTO\b", r"\bDELETE\s+FROM\b",
              r"\bUPDATE\s+\w+\s+SET\b", r"\bDROP\s+TABLE\b",
              r"\bALTER\s+TABLE\b", r"\bCREATE\s+TABLE\b",
              r"\bTRUNCATE\s+TABLE\b"]
    files = sorted(PKG_DIR.glob("*.py"))
    assert files, f"no package files found under {PKG_DIR}"
    for py in files:
        text = py.read_text(encoding="utf-8")
        for pattern in banned:
            assert re.search(pattern, text, re.I) is None, \
                f"{py.name} contains a write statement matching {pattern}"


def test_the_pipeline_only_ever_executes_select(db):
    """Behavioural version of the same rule: record every statement issued."""
    _seed(db)

    class Recorder:
        def __init__(self, conn):
            self._conn = conn
            self.stmts: list[str] = []

        def execute(self, sql, params=()):
            self.stmts.append(" ".join(str(sql).split()))
            return self._conn.execute(sql, params)

    rec = Recorder(db)
    run_postmortem(rec)
    assert rec.stmts, "the pipeline issued no SQL at all"
    assert all(s.upper().startswith("SELECT") for s in rec.stmts), \
        [s for s in rec.stmts if not s.upper().startswith("SELECT")]


# ---------------------------------------------------------------------- cli ---
def test_cli_writes_a_valid_report_under_reports(env, capsys):
    out_dir = env / "reports"
    assert main(["postmortem", "--out", str(out_dir)]) == 0
    written = sorted(out_dir.glob("postmortem_*.json"))
    assert len(written) == 1
    report = json.loads(written[0].read_text(encoding="utf-8"))
    assert report["schema_version"] == REPORT_SCHEMA_VERSION
    assert report["findings"]
    for f in report["findings"]:
        assert f["evidence_refs"]
        assert f["evidence_refs"][0] in report["evidence"]
    assert report["recommendations_are_proposals"] is True


def test_cli_accepts_filters_and_prints(env, capsys):
    capsys.readouterr()
    rc = main(["postmortem", "--status", "failed", "--limit", "1",
               "--out", str(env / "reports")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "postmortem report:" in out
    written = sorted((env / "reports").glob("postmortem_*.json"))
    report = json.loads(written[-1].read_text(encoding="utf-8"))
    assert report["workflow_ids"] == ["wf-bbbb"]


def test_cli_exits_non_zero_on_validation_failure(env, capsys):
    capsys.readouterr()
    rc = main(["postmortem", "--out", str(env / "reports"),
               "--max-report-bytes", "64"])
    assert rc != 0
    assert "postmortem invalid" in capsys.readouterr().err
    written = list((env / "reports").glob("postmortem_*.json")) \
        if (env / "reports").exists() else []
    assert written == [], "an invalid report must never be written"


def test_cli_fails_cleanly_without_a_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAMS_DB_PATH", str(tmp_path / "missing.db"))
    monkeypatch.setenv("CLAMS_CONFIG_FILE", str(tmp_path / "none.yaml"))
    capsys.readouterr()
    assert main(["postmortem", "--out", str(tmp_path / "reports")]) == 2


def test_event_budget_truncates_with_a_marker(db):
    for i in range(12):
        repo_events.append_event(db, "workflow", "wf-bbbb", "info",
                                 f"noise event {i}")
    snap = build_snapshot(db, ["wf-bbbb"],
                          budget=SnapshotBudget(max_events_per_workflow=3))
    events = [r for r in snap.evidence.values() if r["kind"] == "event"]
    assert len(events) == 3
    assert any(t["collection"] == "events" and t["marker"] == TRUNCATION_MARKER
               for t in snap.truncations)
    # the kept events are the deterministic (lowest-id) prefix
    assert [e["event_id"] for e in events] == sorted(e["event_id"]
                                                     for e in events)


def test_char_budget_truncates_rather_than_failing(db):
    _seed(db)
    ids = [r["workflow_id"] for r in select_terminal_workflows(db)]
    snap = build_snapshot(db, ids, budget=SnapshotBudget(max_total_chars=1500))
    assert snap.truncated is True
    assert _json_len(snap.evidence) <= 1500 + 2000  # marked, not exploded
    assert snap.evidence, "at least one record must survive"
    report = build_report(snap)
    assert any(f["type"] == "evidence_truncated" for f in report["findings"])


def _json_len(obj) -> int:
    return len(json.dumps(obj, default=str))


def test_long_fields_are_clipped_with_the_marker(db):
    _seed(db)
    db.execute("UPDATE workflows SET goal=? WHERE workflow_id='wf-bbbb'",
               ("x" * 5000,))
    snap = build_snapshot(db, ["wf-bbbb"],
                          budget=SnapshotBudget(max_field_chars=120))
    rec = snap.evidence["workflow:wf-bbbb"]
    assert rec["goal"].endswith(TRUNCATION_MARKER)
    assert len(rec["goal"]) <= 120
