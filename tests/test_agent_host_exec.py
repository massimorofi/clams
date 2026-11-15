"""Issue #116: the executor agent must not be told to use a host-exec socket
that is not mounted.

``host_execution_enabled: true`` makes every ``run_command`` fail with "host
executor unavailable", which killed essentially every task. Either the socket
has to exist, or the setting has to be off so commands run sandboxed inside the
container (the task worktree is bind-mounted there).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AGENT_CONFIGS = sorted((REPO / ".dev/agents").glob("*/config.yaml"))
DEV_SH = REPO / "scripts/dev.sh"

yaml = pytest.importorskip("yaml")


@pytest.mark.skipif(not AGENT_CONFIGS, reason="no .dev agent configs present")
@pytest.mark.parametrize("cfg", AGENT_CONFIGS, ids=lambda p: p.parent.name)
def test_host_execution_only_when_socket_exists(cfg):
    data = yaml.safe_load(cfg.read_text())
    agent = data.get("agent") or {}
    if not agent.get("host_execution_enabled"):
        return  # sandboxed in container: fine
    socket = agent.get("host_execution_socket") or ""
    assert socket and Path(socket).exists(), (
        f"{cfg} enables host execution but {socket!r} is not present; "
        "start the host-exec daemon or set host_execution_enabled: false"
    )


def test_dev_sh_provides_a_host_exec_path():
    """dev.sh must either mount the socket or force the setting off."""
    text = DEV_SH.read_text()
    assert "CLAMS_HOST_EXEC_SOCKET" in text
    assert "/run/claudio-host/host-exec.sock" in text
    assert "host_execution_enabled: false" in text


def test_dev_sh_mounts_socket_into_agent():
    text = DEV_SH.read_text()
    assert re.search(r'exec_mount=\(-v "\$\{CLAMS_HOST_EXEC_SOCKET\}'
                     r':/run/claudio-host/host-exec.sock"\)', text)
    assert '"${exec_mount[@]}"' in text


def test_agent_worktree_path_is_writable_for_the_agent():
    """The task worktree lives outside /workspace, so it must be an allowed root."""
    for cfg in AGENT_CONFIGS:
        data = yaml.safe_load(cfg.read_text())
        roots = (data.get("agent") or {}).get("workspace_allowed_roots") or []
        assert any(root != "/workspace" for root in roots), (
            f"{cfg} only allows /workspace; the executor cannot write its worktree"
        )


# ---- issue #121: the container healthcheck must probe the real port -------

def test_dev_sh_overrides_the_agent_healthcheck_port():
    """The image HEALTHCHECK hardcodes 127.0.0.1:8100.

    dev.sh runs the agents on 1072/1073, so the baked-in probe always failed and
    every agent container reported "unhealthy" while serving traffic fine.
    """
    text = DEV_SH.read_text()
    assert "--health-cmd" in text
    # The probe must use the agent's own port variable, not a literal 8100.
    assert re.search(r'--health-cmd "curl -fsS http://127\.0\.0\.1:\$\{port\}'
                     r'/healthz \|\| exit 1"', text)
    assert "8100" not in text.split("docker run -d")[1].split(" \"$AGENT_IMAGE\"")[0]


def test_agent_ports_are_not_the_stock_8100():
    """The ports are deliberately 1072/1073 so a stock instance cannot collide."""
    text = DEV_SH.read_text()
    assert 'CLAMS_PLANNER_AGENT_PORT:-1072' in text
    assert 'CLAMS_EXECUTOR_AGENT_PORT:-1073' in text
