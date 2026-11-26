"""Crash-loop fix: oversized task.result must trim, not kill the runner."""
from __future__ import annotations

import json

from clams import contracts
from clams.bus import Delivered
from clams.config import Settings
from clams.contracts import Brief, TaskDispatch
from clams.executor.runner import ExecutorRunner


class RecBus:
    def __init__(self):
        self.published = []
        self.acked = []

    def publish(self, stream, payload):
        contracts.parse_message(payload)          # enforces the cap like real
        self.published.append((stream, payload))

    def ack(self, stream, group, id):
        self.acked.append(id)


def _runner():
    r = ExecutorRunner.__new__(ExecutorRunner)
    r.s = Settings(_env_file=None)
    r.bus = RecBus()
    r.executor_id = "e"
    return r


def _dispatch() -> TaskDispatch:
    return TaskDispatch(
        task_id="t1", workflow_id="w1", attempt=1, lease_token="l" * 12,
        brief=Brief(objective="o", acceptance="a", run=["x"],
                    writable=["*.py"], repo="o/r"))


def _huge_evidence():
    return {"passed": False, "reason": "run: exit 2",
            "checks": [{"check": "run", "cmd": f"pytest file{i}.py -q",
                        "exit": 2, "ok": False,
                        "output_tail": "E collection error line\n" * 200}
                       for i in range(30)]}


def test_oversized_result_is_trimmed_not_fatal():
    r = _runner()
    huge = _huge_evidence()
    r._emit_result(_dispatch(), "failed", session_ref="s", branch="b",
                   evidence=huge, log_tail="z" * 50_000,
                   changed_files=[f"f{i}.py" for i in range(500)])
    assert len(r.bus.published) == 1
    stream, payload = r.bus.published[0]
    assert len(payload.encode()) <= contracts.MAX_PAYLOAD_BYTES
    msg = json.loads(payload)
    assert msg["outcome"] == "failed"
    assert msg["evidence"].get("trimmed") or \
        len(str(msg["evidence"])) < len(str(huge))


def test_scope_list_no_longer_forces_last_resort():
    """The scope check's changed-file list used to survive trimming, pushing
    the ladder to the stage that discards every check — leaving the operator
    with a bare reason and no way to tell which check failed (claudio#133)."""
    r = _runner()
    ev = {"passed": False, "reason": "scope: out of writable scope",
          "checks": [{"check": "run", "cmd": "pytest -q", "exit": 0,
                      "output_tail": "x" * 2000, "ok": True},
                     {"check": "scope", "ok": False,
                      "changed": [f"benchmark/results/r{i}.json"
                                  for i in range(600)]}]}
    r._emit_result(_dispatch(), "failed", session_ref="s", branch="b",
                   evidence=ev, log_tail="z" * 50_000)
    stream, payload = r.bus.published[0]
    assert len(payload.encode()) <= contracts.MAX_PAYLOAD_BYTES
    msg = json.loads(payload)
    assert msg["evidence"].get("trimmed")
    assert msg["evidence"]["reason"] == "scope: out of writable scope"
    # checks survived, so the failing one is still identifiable
    assert len(msg["evidence"]["checks"]) == 2
    assert len(msg["evidence"]["checks"][1]["changed"]) <= 21


def test_trimmed_result_still_names_the_failing_check():
    """Invariant: however far the ladder trims, the operator can still see
    WHICH command failed — not just the bare reason string."""
    r = _runner()
    ev = {"passed": False, "reason": "run: empty output",
          "checks": [{"check": "run", "cmd": "git commit -q || true",
                      "exit": 0, "ok": False, "detail": "empty output",
                      "output_tail": ""}] +
                     [{"check": "run", "cmd": f"pytest t{i}.py", "exit": 0,
                       "ok": True, "output_tail": "y" * 500}
                      for i in range(200)]}
    r._emit_result(_dispatch(), "failed", session_ref="s", branch="b",
                   evidence=ev, log_tail="z" * 50_000,
                   changed_files=[f"f{i}.py" for i in range(5000)])
    stream, payload = r.bus.published[0]
    assert len(payload.encode()) <= contracts.MAX_PAYLOAD_BYTES
    msg = json.loads(payload)
    assert msg["evidence"]["reason"] == "run: empty output"
    assert "git commit -q || true" in json.dumps(msg["evidence"])


def test_small_result_untouched():
    r = _runner()
    ev = {"passed": True, "checks": [{"check": "run", "cmd": "x", "exit": 0,
                                      "output_tail": "ok\n", "ok": True}]}
    r._emit_result(_dispatch(), "success", session_ref="s", branch="b",
                   evidence=ev)
    msg = json.loads(r.bus.published[0][1])
    assert msg["evidence"] == ev and "trimmed" not in msg


def test_unhandled_run_task_error_degrades_and_acks():
    r = _runner()

    def boom(msg, d):
        raise RuntimeError("simulated runner bug")
    r.run_task = boom
    msg = _dispatch()
    d = Delivered(id="1-0", payload=contracts.dump_message(msg))
    r._handle(d)           # must NOT raise
    results = [json.loads(p) for _, p in r.bus.published]
    assert any(m["type"] == "task.result" and m["outcome"] == "failed"
               and m["error_code"] == "infra:runner" for m in results)
    assert r.bus.acked == ["1-0"]


def test_scope_violations_survive_trimming():
    """Scope failures carry their data in `violations`, not `detail`, so the
    last-resort payload used to report "scope: failed" naming nothing."""
    r = _runner()
    files = [f"benchmark/results/r{i}.json" for i in range(600)]
    ev = {"passed": False, "reason": "scope: failed",
          "checks": [{"check": "scope", "ok": False, "violations": files,
                      "changed": files}]}
    r._emit_result(_dispatch(), "failed", session_ref="s", branch="b",
                   evidence=ev, log_tail="z" * 50_000)
    stream, payload = r.bus.published[0]
    assert len(payload.encode()) <= contracts.MAX_PAYLOAD_BYTES
    msg = json.loads(payload)
    dumped = json.dumps(msg["evidence"])
    assert "violations" in dumped and "violation_count" in dumped
