"""Watchdog liveness monitor: evidence-based lease renewal, stall policy
(alert + keep renewing, never kill), infra requeue (attempt preserved),
per-task budget alerts, alert hygiene."""
from __future__ import annotations

import json
import time

from clams.bus import InMemoryBus
from clams.config import Settings
from clams.db import (repo_alerts, repo_projects, repo_stall_evidence,
                      repo_tasks, repo_workflows)
from clams.planner.watchdog import Watchdog


class FakeCE:
    def __init__(self, healthy=True):
        self.healthy = healthy
        self.raises = False
        self.msgs = [{"role": "user", "content": "a", "ts": "t1"}]

    def health(self):
        return self.healthy

    def session_messages(self, ref, limit=200):
        if self.raises:
            raise ConnectionError("claudio unreachable")
        return list(self.msgs)


def _settings(**kw):
    return Settings(_env_file=None, watchdog_probe_interval_s=0,
                    model_server_url="", **kw)


def _running(db, s):
    pid = repo_projects.upsert_project(db, "o/r")
    wf = repo_workflows.create_workflow(db, pid, "adhoc", "g")
    repo_workflows.set_status(db, wf, "running")
    brief = {"objective": "o", "acceptance": "a", "run": ["echo x"],
             "writable": ["*.py"], "repo": "o/r"}
    t = repo_tasks.create_task(db, wf, 1, "t1", "o", "a", brief)
    repo_tasks.promote_ready(db)
    row = repo_tasks.claim_for_dispatch(db, t, 120)
    repo_tasks.on_started(db, t, row["attempt"], row["lease_token"], "e1",
                          "sess-1")
    return t


def _wd(ce, s):
    return Watchdog(InMemoryBus(), None, ce, s, disk_path=".")


def test_evidence_growth_renews_lease(db):
    s = _settings()
    t = _running(db, s)
    ce = FakeCE()
    wd = _wd(ce, s)
    wd.check_liveness(db)                       # baseline
    ce.msgs.append({"role": "assistant", "content": "b", "ts": "t2"})
    db.execute("UPDATE tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' "
               "WHERE task_id=?", (t,))         # simulate near-expiry
    wd.check_liveness(db)
    row = repo_tasks.get_task(db, t)
    assert row["lease_expires_at"] > repo_tasks.utcnow_s()   # renewed
    assert repo_alerts.open_count(db) == 0
    assert row["status"] == "running"


def test_frozen_session_keeps_lease_and_alerts_once(db):
    s = _settings(stall_alert_minutes=0, stall_escalate_minutes=10 ** 6)
    t = _running(db, s)
    wd = _wd(FakeCE(), s)
    wd.check_liveness(db)                       # baseline
    db.execute("UPDATE tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' "
               "WHERE task_id=?", (t,))
    assert wd.check_liveness(db) == 1           # frozen -> warn
    row = repo_tasks.get_task(db, t)
    assert row["status"] == "running"           # NOT killed
    assert row["lease_expires_at"] > repo_tasks.utcnow_s()  # still renewed
    probes = [a["probe"] for a in repo_alerts.list_open(db)]
    assert probes == [f"stall:{t}"]
    assert wd.check_liveness(db) == 0           # no duplicate
    # bundle persisted and referenced by attempt
    bundle = repo_stall_evidence.get(db, t, 1)
    assert bundle and bundle["session"]["count"] == 1


def test_frozen_past_escalate_window_escalates_to_error(db):
    s = _settings(stall_alert_minutes=0, stall_escalate_minutes=0)
    t = _running(db, s)
    wd = _wd(FakeCE(), s)
    wd.check_liveness(db)
    wd.check_liveness(db)                       # warn (escalate=0 -> error?)
    # with escalate 0 the first alert is already error
    alerts = {a["probe"]: a["severity"] for a in repo_alerts.list_open(db)}
    assert alerts[f"stall:{t}"] == "error"


def test_warn_then_error_escalation(db):
    s = _settings(stall_alert_minutes=0, stall_escalate_minutes=60)
    t = _running(db, s)
    wd = _wd(FakeCE(), s)
    wd.check_liveness(db)                       # baseline
    wd.check_liveness(db)                       # warn
    # age the frozen window past the escalate threshold in persisted state
    state = wd._load_liveness_state(db)
    state[t]["frozen_since"] = time.time() - 70 * 60
    wd._save_liveness_state(db, state)
    assert wd.check_liveness(db) == 1
    alerts = {a["probe"]: a["severity"] for a in repo_alerts.list_open(db)}
    assert alerts[f"stall:{t}"] == "error"      # upgraded, not duplicated


def test_claudio_down_infra_requeue_preserves_attempt(db):
    s = _settings()
    t = _running(db, s)
    ce = FakeCE(healthy=False)
    wd = _wd(ce, s)
    wd.check_liveness(db)                       # streak 1: no action
    assert repo_tasks.get_task(db, t)["status"] == "running"
    assert wd.check_liveness(db) == 1           # streak 2: infra requeue
    row = repo_tasks.get_task(db, t)
    assert row["status"] == "ready" and row["attempt"] == 0
    probes = [a["probe"] for a in repo_alerts.list_open(db)]
    assert f"infra:{t}" in probes
    again = repo_tasks.claim_for_dispatch(db, t, 120)
    assert again["attempt"] == 1                # same attempt number reused


def test_orphan_requeue_on_beat_silence(db):
    """Runner died (beats silent) + session frozen -> free requeue."""
    from clams.db.conn import epoch_to_iso
    s = _settings(orphan_silence_s=60)
    t = _running(db, s)
    db.execute("UPDATE tasks SET heartbeat_json=? WHERE task_id=?",
               (json.dumps({"turns_used": 10, "elapsed_s": 100,
                            "ts": epoch_to_iso(time.time() - 3600),
                            "progress_ts": epoch_to_iso(time.time() - 3600)}),
                t))
    wd = _wd(FakeCE(), s)
    wd.check_liveness(db)                       # baseline
    assert wd.check_liveness(db) == 1           # silent + frozen -> requeue
    row = repo_tasks.get_task(db, t)
    assert row["status"] == "ready" and row["attempt"] == 0
    probes = [a["probe"] for a in repo_alerts.list_open(db)]
    assert f"infra:{t}" in probes


def test_silent_but_growing_alerts_orphan_without_requeue(db):
    """claudio still advancing after runner death: alert, keep the task."""
    from clams.db.conn import epoch_to_iso
    s = _settings(orphan_silence_s=60)
    t = _running(db, s)
    db.execute("UPDATE tasks SET heartbeat_json=? WHERE task_id=?",
               (json.dumps({"turns_used": 10, "elapsed_s": 100,
                            "ts": epoch_to_iso(time.time() - 3600),
                            "progress_ts": epoch_to_iso(time.time() - 3600)}),
                t))
    ce = FakeCE()
    wd = _wd(ce, s)
    wd.check_liveness(db)
    ce.msgs.append({"role": "assistant", "content": "b", "ts": "t2"})
    assert wd.check_liveness(db) == 0           # grew: no attention counter
    assert repo_tasks.get_task(db, t)["status"] == "running"
    probes = [a["probe"] for a in repo_alerts.list_open(db)]
    assert f"orphan:{t}" in probes


def test_session_api_error_counts_frozen_not_down(db):
    s = _settings(stall_alert_minutes=0, stall_escalate_minutes=10 ** 6)
    t = _running(db, s)
    ce = FakeCE()
    ce.raises = True
    wd = _wd(ce, s)
    wd.check_liveness(db)                       # baseline (err -> not grew)
    wd.check_liveness(db)                       # frozen -> warn, NOT requeue
    assert repo_tasks.get_task(db, t)["status"] == "running"
    assert [a["probe"] for a in repo_alerts.list_open(db)] == [f"stall:{t}"]


def test_no_evidence_source_fails_open(db):
    s = _settings()
    t = _running(db, s)
    wd = _wd(None, s)                           # ce=None
    db.execute("UPDATE tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' "
               "WHERE task_id=?", (t,))
    wd.check_liveness(db)
    row = repo_tasks.get_task(db, t)
    assert row["lease_expires_at"] > repo_tasks.utcnow_s()  # renewed
    assert repo_alerts.open_count(db) == 0                  # no alert


def test_turns_over_budget_alerts_without_kill(db):
    s = _settings()
    t = _running(db, s)
    db.execute("UPDATE tasks SET heartbeat_json=? WHERE task_id=?",
               (json.dumps({"turns_used": 999, "elapsed_s": 1,
                            "ts": "x", "progress_ts": "x"}), t))
    wd = _wd(FakeCE(), s)
    wd.check_liveness(db)
    probes = [a["probe"] for a in repo_alerts.list_open(db)]
    assert f"turns:{t}" in probes
    assert repo_tasks.get_task(db, t)["status"] == "running"


def test_final_attempt_alerts(db):
    s = _settings()
    t = _running(db, s)                          # attempt 1 of max 3
    db.execute("UPDATE tasks SET attempt=3 WHERE task_id=?", (t,))
    wd = _wd(FakeCE(), s)
    wd.check_liveness(db)
    probes = [a["probe"] for a in repo_alerts.list_open(db)]
    assert f"attempts:{t}" in probes


def test_alert_hygiene_clears_stale_alerts(db):
    s = _settings()
    t = _running(db, s)
    repo_tasks.transition(db, t, "success")     # task finished
    repo_alerts.raise_alert(db, f"stall:{t}", "warn", "stale")
    repo_alerts.raise_alert(db, "turns:" + t, "warn", "stale")
    repo_alerts.raise_alert(db, "workflow", "error", "stale blocked")
    wd = _wd(FakeCE(), s)
    wd.probe(db)                                # probe runs hygiene
    open_probes = [a["probe"] for a in repo_alerts.list_open(db)]
    assert f"stall:{t}" not in open_probes
    assert f"turns:{t}" not in open_probes
    assert "workflow" not in open_probes


def test_liveness_state_survives_restart(db):
    s = _settings(stall_alert_minutes=0, stall_escalate_minutes=10 ** 6)
    t = _running(db, s)
    ce = FakeCE()
    wd1 = _wd(ce, s)
    wd1.check_liveness(db)                      # baseline
    wd1.check_liveness(db)                      # frozen -> warn
    wd2 = _wd(ce, s)                            # "planner restart"
    assert wd2.check_liveness(db) == 0          # severity remembered
    alerts = [a["probe"] for a in repo_alerts.list_open(db)]
    assert alerts == [f"stall:{t}"]             # still exactly one


def test_new_attempt_resets_window(db):
    s = _settings(stall_alert_minutes=0, stall_escalate_minutes=10 ** 6)
    t = _running(db, s)
    wd = _wd(FakeCE(), s)
    wd.check_liveness(db)
    wd.check_liveness(db)                       # warn on attempt 1
    repo_alerts.clear_alert(db, f"stall:{t}")
    # simulate re-dispatch at attempt 2
    db.execute("UPDATE tasks SET status='ready', attempt=1 WHERE task_id=?",
               (t,))
    row = repo_tasks.claim_for_dispatch(db, t, 120)
    repo_tasks.on_started(db, t, row["attempt"], row["lease_token"], "e1",
                          "sess-2")
    assert wd.check_liveness(db) == 0           # fresh baseline, no stale warn
