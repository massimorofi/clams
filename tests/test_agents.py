"""Tests for the agent registry and its HTTP surface (issue #19)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from clams.agents import (
    AGENT_ROLES,
    AgentConfig,
    AgentError,
    AgentRegistry,
    coerce_agent,
    default_agents,
    normalise_claudio_url,
    parse_agents_value,
)
from clams.api import create_app
from clams.config import Settings


@pytest.fixture()
def reg(tmp_path) -> AgentRegistry:
    return AgentRegistry(tmp_path / "agents.json", default_agents())


# --------------------------------------------------------------- defaults ----
def test_default_agents_are_planner_and_executor():
    agents = default_agents()
    assert [a.role for a in agents] == ["planner", "executor"]
    assert [a.id for a in agents] == ["planner", "executor"]


def test_default_agents_use_ports_1072_and_1073():
    """Issue #19 explicitly asks for 1072/1073, not Claudio's stock 8100."""
    planner, executor = default_agents()
    assert planner.base_url == "http://127.0.0.1:1072/api/v1"
    assert executor.base_url == "http://127.0.0.1:1073/api/v1"
    assert "8100" not in planner.base_url
    assert "8100" not in executor.base_url


def test_default_agents_are_enabled():
    assert all(a.enabled for a in default_agents())


# ------------------------------------------------------------ validation ---
def test_rejects_unknown_role():
    with pytest.raises(AgentError, match="unknown agent role"):
        coerce_agent({"id": "x", "role": "wizard",
                     "base_url": "http://127.0.0.1:1072"})


def test_accepts_all_four_roles():
    for role in AGENT_ROLES:
        a = coerce_agent({"id": f"a-{role}", "role": role,
                         "base_url": "http://127.0.0.1:1072"})
        assert a.role == role


def test_rejects_non_http_url():
    with pytest.raises(AgentError, match="absolute http"):
        coerce_agent({"id": "x", "role": "planner", "base_url": "ftp://host/api"})


def test_rejects_blank_url():
    with pytest.raises(AgentError):
        coerce_agent({"id": "x", "role": "planner", "base_url": "   "})


def test_rejects_bad_id():
    with pytest.raises(AgentError, match="agent id must"):
        coerce_agent({"id": "Bad Id", "role": "planner", "base_url": "http://h:1"})


def test_rejects_non_positive_timeout():
    with pytest.raises(AgentError, match="request_timeout"):
        coerce_agent({"id": "x", "role": "planner", "base_url": "http://h:1",
                     "request_timeout": 0})


# ------------------------------------------------------- url normalisation ---
@pytest.mark.parametrize("raw,expected", [
    ("http://h:1072", "http://h:1072/api/v1"),
    ("http://h:1072/", "http://h:1072/api/v1"),
    ("http://h:1072/api/v1", "http://h:1072/api/v1"),
    ("http://h:1072/api/v1/", "http://h:1072/api/v1"),
])
def test_normalise_claudio_url_is_idempotent(raw, expected):
    once = normalise_claudio_url(raw)
    assert once == expected
    assert normalise_claudio_url(once) == once


def test_effective_base_url_adds_suffix_only_for_claudio_roles():
    orch = AgentConfig(id="o", role="orchestrator", base_url="http://h:9000")
    assert orch.effective_base_url() == "http://h:9000"
    plan = AgentConfig(id="p", role="planner", base_url="http://h:1072")
    assert plan.effective_base_url() == "http://h:1072/api/v1"


# ---------------------------------------------------------- persistence ----
def test_load_seeds_defaults_when_file_missing(tmp_path):
    p = tmp_path / "agents.json"
    assert not p.exists()
    r = AgentRegistry.load(p)
    assert len(r) == 2
    assert p.exists()
    assert json.loads(p.read_text())[0]["role"] == "planner"


def test_roundtrip_preserves_agents(tmp_path):
    p = tmp_path / "agents.json"
    r = AgentRegistry(p, default_agents())
    r.add(AgentConfig(id="extra", role="watchdog", base_url="http://h:1"))
    reloaded = AgentRegistry.load(p)
    assert [a.id for a in reloaded.all()] == ["planner", "executor", "extra"]


def test_save_is_atomic_no_temp_leftovers(tmp_path):
    p = tmp_path / "agents.json"
    r = AgentRegistry(p, default_agents())
    r.save()
    leftovers = [f for f in tmp_path.iterdir() if f.name.startswith(".agents-")]
    assert leftovers == []


def test_load_rejects_invalid_json(tmp_path):
    p = tmp_path / "agents.json"
    p.write_text("{not json")
    with pytest.raises(AgentError, match="invalid JSON"):
        AgentRegistry.load(p)


def test_load_rejects_non_array(tmp_path):
    p = tmp_path / "agents.json"
    p.write_text('{"role": "planner"}')
    with pytest.raises(AgentError, match="expected a JSON array"):
        AgentRegistry.load(p)


# ---------------------------------------------------------------- CRUD -----
def test_add_rejects_duplicate_id(reg):
    with pytest.raises(AgentError, match="already exists"):
        reg.add(AgentConfig(id="planner", role="watchdog", base_url="http://h:1"))


def test_update_changes_address_and_role(reg):
    updated = reg.update("planner", {"base_url": "http://10.0.0.5:1072",
                                   "role": "executor"})
    assert updated.base_url == "http://10.0.0.5:1072"
    assert updated.role == "executor"
    assert reg.get("planner").base_url == "http://10.0.0.5:1072"


def test_update_leaves_omitted_fields_untouched(reg):
    before = reg.get("executor").model_dump()
    after = reg.update("executor", {"base_url": "http://new:1073"}).model_dump()
    assert after["auth_token"] == before["auth_token"]
    assert after["enabled"] == before["enabled"]
    assert after["base_url"] == "http://new:1073"


def test_update_cannot_change_id(reg):
    after = reg.update("planner", {"id": "hacked"})
    assert after.id == "planner"


def test_update_unknown_agent_raises(reg):
    with pytest.raises(AgentError, match="no agent with id"):
        reg.update("ghost", {"role": "planner"})


def test_remove_drops_agent(reg):
    reg.remove("executor")
    assert reg.get("executor") is None
    assert len(reg) == 1


def test_remove_unknown_raises(reg):
    with pytest.raises(AgentError, match="no agent with id"):
        reg.remove("ghost")


def test_primary_for_role_prefers_enabled(reg):
    reg.update("planner", {"enabled": False})
    primary = reg.primary_for_role("planner")
    # No enabled planner -> falls back to the (disabled) bound agent.
    assert primary is not None and primary.enabled is False


def test_primary_for_role_missing_returns_none(reg):
    assert reg.primary_for_role("watchdog") is None


def test_replace_rejects_duplicate_ids(reg):
    dup = [AgentConfig(id="a", role="planner", base_url="http://h:1"),
           AgentConfig(id="a", role="executor", base_url="http://h:2")]
    with pytest.raises(AgentError, match="duplicate agent id"):
        reg.replace(dup)


# ------------------------------------------------------- value coercion ----
def test_parse_agents_value_from_json_string():
    payload = json.dumps([
        {"id": "p", "role": "planner", "base_url": "http://h:1072"},
    ])
    out = parse_agents_value(payload)
    assert len(out) == 1 and out[0].id == "p"


def test_parse_agents_value_none_gives_defaults():
    assert len(parse_agents_value(None)) == 2


def test_parse_agents_value_rejects_scalar():
    with pytest.raises(AgentError, match="must be a JSON array"):
        parse_agents_value(42)


# ------------------------------------------------------------- API ---------
@pytest.fixture()
def client(tmp_path):
    settings = Settings.load()
    settings.paths.agents_file = str(tmp_path / "agents.json")
    app = create_app(settings=settings)
    with TestClient(app) as c:
        yield c


def test_api_lists_default_agents(client):
    r = client.get("/api/v1/agents")
    assert r.status_code == 200
    body = r.json()
    assert [a["id"] for a in body["agents"]] == ["planner", "executor"]
    assert body["roles"] == list(AGENT_ROLES)
    assert body["defaults"]["planner_port"] == 1072
    assert body["defaults"]["executor_port"] == 1073


def test_api_masks_auth_token(client):
    r = client.get("/api/v1/agents")
    for a in r.json()["agents"]:
        assert "auth_token" not in a
        assert a["has_auth_token"] is True


def test_api_creates_agent(client):
    r = client.post("/api/v1/agents", json={
        "id": "executor-2", "role": "executor",
        "base_url": "http://127.0.0.1:1074/api/v1",
        "auth_token": "tok-2",
    })
    assert r.status_code == 201, r.text
    assert len(r.json()["agents"]) == 3
    got = client.get("/api/v1/agents").json()["agents"]
    assert any(a["id"] == "executor-2" for a in got)


def test_api_rejects_duplicate_agent(client):
    r = client.post("/api/v1/agents", json={
        "id": "planner", "role": "watchdog", "base_url": "http://h:1"})
    assert r.status_code == 422
    assert "already exists" in r.text


def test_api_rejects_bad_role(client):
    r = client.post("/api/v1/agents", json={
        "id": "zz", "role": "wizard", "base_url": "http://h:1"})
    assert r.status_code == 422
    assert "unknown agent role" in r.text


def test_api_updates_agent_address(client):
    r = client.put("/api/v1/agents/planner", json={
        "base_url": "http://192.168.1.9:1072/api/v1"})
    assert r.status_code == 200
    planner = [a for a in r.json()["agents"] if a["id"] == "planner"][0]
    assert planner["base_url"] == "http://192.168.1.9:1072/api/v1"


def test_api_update_syncs_live_settings(client):
    """A planner address change must reach settings.planner.base_url live."""
    client.put("/api/v1/agents/planner",
              json={"base_url": "http://10.1.2.3:1072"})
    cfg = client.get("/api/v1/config").json()["settings"]
    assert cfg["planner"]["base_url"] == "http://10.1.2.3:1072/api/v1"


def test_api_update_404_on_unknown(client):
    assert client.put("/api/v1/agents/ghost",
                     json={"role": "planner"}).status_code == 422


def test_api_delete_agent(client):
    r = client.delete("/api/v1/agents/executor")
    assert r.status_code == 200
    assert [a["id"] for a in r.json()["agents"]] == ["planner"]


def test_api_delete_unknown_404(client):
    assert client.delete("/api/v1/agents/ghost").status_code == 404


def test_api_probe_reports_failure_for_dead_agent(tmp_path):
    """A probe against an address with nothing listening must report not-ok.

    Uses a port nothing binds, so the assertion holds whether or not the real
    agent containers happen to be up.
    """
    dead_url = "http://127.0.0.1:1/api/v1"  # port 1: never bound
    settings = Settings.load()
    settings.paths.agents_file = str(tmp_path / "agents.json")
    with TestClient(create_app(settings=settings)) as c:
        c.post("/api/v1/agents", json={
            "id": "ghost", "role": "orchestrator", "base_url": dead_url})
        r = c.post("/api/v1/agents/ghost/probe")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["url"].endswith("/healthz")


def test_api_probe_unknown_agent_404(client):
    assert client.post("/api/v1/agents/ghost/probe").status_code == 404


def test_load_uses_supplied_defaults(tmp_path):
    """A caller-supplied default list wins over the built-in constants."""
    custom = [AgentConfig(id="planner", role="planner",
                         base_url="http://10.0.0.1:9001/api/v1")]
    p = tmp_path / "agents.json"
    r = AgentRegistry.load(p, defaults=custom)
    assert len(r) == 1
    assert r.get("planner").base_url == "http://10.0.0.1:9001/api/v1"


def test_app_seeds_registry_from_settings(tmp_path):
    """CLAMS__AGENTS__* must actually drive the seeded addresses."""
    settings = Settings.load()
    settings.paths.agents_file = str(tmp_path / "agents.json")
    settings.agents.host = "10.5.5.5"
    settings.agents.planner_port = 1201
    settings.agents.executor_port = 1202
    settings.agents.auth_token = "seeded-token"
    with TestClient(create_app(settings=settings)) as c:
        agents = c.get("/api/v1/agents").json()["agents"]
    assert agents[0]["base_url"] == "http://10.5.5.5:1201/api/v1"
    assert agents[1]["base_url"] == "http://10.5.5.5:1202/api/v1"
    assert agents[0]["has_auth_token"] is True
    # And the seeded file on disk agrees.
    on_disk = json.loads((tmp_path / "agents.json").read_text())
    assert on_disk[0]["base_url"] == "http://10.5.5.5:1201/api/v1"


def test_registry_persists_across_app_rebuild(tmp_path):
    settings = Settings.load()
    settings.paths.agents_file = str(tmp_path / "agents.json")
    with TestClient(create_app(settings=settings)) as c1:
        c1.put("/api/v1/agents/planner",
               json={"base_url": "http://10.9.9.9:1072/api/v1"})
    with TestClient(create_app(settings=settings)) as c2:
        agents = c2.get("/api/v1/agents").json()["agents"]
        planner = [a for a in agents if a["id"] == "planner"][0]
        assert planner["base_url"] == "http://10.9.9.9:1072/api/v1"
