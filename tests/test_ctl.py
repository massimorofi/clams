"""Tests for the clamsctl Docker control script (issue #9)."""
from __future__ import annotations

import pytest

from clams import ctl


def test_parser_exposes_every_required_subcommand():
    p = ctl.build_parser()
    # The ticket asks for: build, recreate, start/stop, status, health, cleanup.
    for cmd in ("build", "recreate", "start", "stop", "restart",
                "status", "health", "cleanup", "logs"):
        args = p.parse_args([cmd] if cmd not in ("logs",) else ["logs", "planner"])
        assert args.cmd == cmd


def test_build_passes_services_through():
    args = ctl.build_parser().parse_args(["build", "planner", "executor"])
    assert args.services == ["planner", "executor"]


def test_recreate_refuses_without_yes_in_non_interactive_session(monkeypatch):
    monkeypatch.setattr(ctl.sys, "stdin", type("X", (), {"isatty": lambda self: False})())
    called = []
    monkeypatch.setattr(ctl, "_compose", lambda *a, **k: called.append(a))
    rc = ctl.cmd_recreate(ctl.build_parser().parse_args(["recreate"]))
    assert rc == 2
    assert called == []          # nothing was run


def test_recreate_runs_with_yes(monkeypatch):
    monkeypatch.setattr(ctl.sys, "stdin", type("X", (), {"isatty": lambda self: False})())
    calls = []

    class FakeCP:
        returncode = 0

    def fake_compose(*a, **k):
        calls.append(a)
        return FakeCP()

    monkeypatch.setattr(ctl, "_compose", fake_compose)
    rc = ctl.cmd_recreate(ctl.build_parser().parse_args(["recreate", "--yes"]))
    assert rc == 0
    assert calls[0] == ("down",)
    assert calls[1][:3] == ("up", "-d", "--remove-orphans")
    assert "--force-recreate" in calls[1]


def test_cleanup_refuses_without_yes(monkeypatch):
    monkeypatch.setattr(ctl.sys, "stdin", type("X", (), {"isatty": lambda self: False})())
    rc = ctl.cmd_cleanup(ctl.build_parser().parse_args(["cleanup"]))
    assert rc == 2


def test_cleanup_removes_volumes_when_confirmed(monkeypatch):
    monkeypatch.setattr(ctl.sys, "stdin", type("X", (), {"isatty": lambda self: False})())
    seen = []

    class FakeCP:
        returncode = 0

    monkeypatch.setattr(ctl, "_compose",
                       lambda *a, **k: (seen.append(("compose",) + a), FakeCP())[1])
    monkeypatch.setattr(ctl, "_run",
                       lambda cmd, **k: (seen.append(tuple(cmd)), FakeCP())[1])
    rc = ctl.cmd_cleanup(ctl.build_parser().parse_args(["cleanup", "--yes"]))
    assert rc == 0
    assert ("compose", "down", "--volumes") in seen
    assert ("docker", "image", "prune", "-f") in seen


def test_health_reports_failure_when_a_service_is_unhealthy(monkeypatch):
    monkeypatch.setattr(ctl, "_container_health",
                       lambda s: "unhealthy" if s == "planner" else "healthy")
    monkeypatch.setattr(ctl, "_probe_http", lambda url, exp, t: (True, "200"))
    rc = ctl.cmd_health(ctl.build_parser().parse_args(["health"]))
    assert rc == 1


def test_health_passes_when_everything_is_healthy(monkeypatch):
    monkeypatch.setattr(ctl, "_container_health", lambda s: "healthy")
    monkeypatch.setattr(ctl, "_probe_http", lambda url, exp, t: (True, "200"))
    rc = ctl.cmd_health(ctl.build_parser().parse_args(["health"]))
    assert rc == 0


def test_health_fails_when_endpoint_down(monkeypatch):
    monkeypatch.setattr(ctl, "_container_health", lambda s: "healthy")
    monkeypatch.setattr(ctl, "_probe_http", lambda url, exp, t: (False, "ConnectError"))
    rc = ctl.cmd_health(ctl.build_parser().parse_args(["health"]))
    assert rc == 1


def test_probe_http_reports_unexpected_status():
    ok, detail = ctl._probe_http("http://127.0.0.1:1/nope", 200, 1.0)
    assert ok is False


def test_services_list_matches_compose():
    """clamsctl.SERVICES must match the services defined in docker-compose.yml."""
    lines = open("docker-compose.yml").read().splitlines()
    defined: set[str] = set()
    in_services = False
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            key = raw.split(":", 1)[0].strip()
            in_services = key == "services"
            continue
        if in_services and indent == 2:
            defined.add(raw.split(":", 1)[0].strip())

    assert set(ctl.SERVICES) == defined, (
        f"clamsctl.SERVICES out of sync with docker-compose.yml: "
        f"missing={defined - set(ctl.SERVICES)} extra={set(ctl.SERVICES) - defined}"
    )
