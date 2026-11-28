"""``compose_gen.render`` emits the complete effective compose document.

Every assertion is checked through :mod:`clams_admin.compose_index` (a stdlib
indentation scanner) or plain text matching, so this file needs no PyYAML and
no docker. Stdlib + pytest only.
"""
from __future__ import annotations

import re

import pytest

from clams_admin import compose_gen as gen
from clams_admin import compose_index as idx
from clams_admin.adminconfig import default_deployment
from clams_admin.model import (
    AdminConfig,
    ClamsSettings,
    ClaudioUI,
    Deployment,
    ExecutorInstance,
)

BASE_SERVICES = ["redis", "claudio-planner", "claudio-executor", "claudio-ui",
                 "planner", "executor-runner", "api"]


def deployment_with_executors(count: int, **overrides) -> Deployment:
    """The default deployment grown to *count* executor instances."""
    deployment = default_deployment()
    deployment.executors = [
        ExecutorInstance(executor_id=f"executor-{i}",
                         debug_port=gen.executor_host_port(i), capacity=1)
        for i in range(1, count + 1)
    ]
    deployment.admin = AdminConfig(name="clams", executor_count=count)
    for key, value in overrides.items():
        deployment.extra[key] = value
    return deployment


# --------------------------------------------------------------------------
# topology shape
# --------------------------------------------------------------------------
@pytest.mark.parametrize("count,expected", [(1, 7), (2, 9), (3, 11), (4, 13)])
def test_service_count_grows_by_two_per_extra_executor(count: int, expected: int) -> None:
    assert len(idx.services(gen.render_for_executors(count))) == expected


def test_single_executor_yields_exactly_seven_services() -> None:
    services = idx.services(gen.render_for_executors(1))
    assert services == BASE_SERVICES
    assert len(services) == 7
    assert services.count("claudio-ui") == 1
    assert services.count("claudio-executor") == 1
    assert services.count("executor-runner") == 1
    # instance 1 keeps the historical names (clamsctl greps them)
    assert "claudio-executor-1" not in services
    assert "executor-runner-1" not in services


def test_three_executors_yield_eleven_services_with_indexed_names() -> None:
    services = idx.services(gen.render_for_executors(3))
    assert len(services) == 11
    for name in ("claudio-executor-2", "claudio-executor-3",
                 "executor-runner-2", "executor-runner-3"):
        assert name in services
    for name in BASE_SERVICES:
        assert name in services
    assert len(set(services)) == len(services)


@pytest.mark.parametrize("count", [1, 2, 3, 5, 8])
def test_exactly_one_ui_for_any_n(count: int) -> None:
    text = gen.render_for_executors(count)
    services = idx.services(text)
    assert services.count("claudio-ui") == 1
    assert len([s for s in services if s.startswith("claudio-ui")]) == 1
    # ... and it is the only thing on ui-planner besides the planner alias
    index = idx.ComposeIndex(text)
    assert index.networks_of("claudio-ui") == ["ui-planner"]
    assert "ui-planner" in index.networks()
    assert "clams" in index.networks()


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_every_runner_depends_on_its_own_agent_healthy(count: int) -> None:
    index = idx.ComposeIndex(gen.render_for_executors(count))
    for i in range(1, count + 1):
        runner = gen.runner_service_name(i)
        agent = gen.agent_service_name(i)
        assert runner in index.services() and agent in index.services()
        deps = index.depends_on(runner)
        assert deps.get(agent) == "service_healthy", (runner, deps)
        assert deps.get("redis") == "service_healthy", (runner, deps)
        # a runner never waits on a *different* executor's agent
        for other in range(1, count + 1):
            if other != i:
                assert gen.agent_service_name(other) not in deps


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_runner_url_names_exactly_its_own_pair(count: int) -> None:
    index = idx.ComposeIndex(gen.render_for_executors(count))
    for i in range(1, count + 1):
        runner = gen.runner_service_name(i)
        agent = gen.agent_service_name(i)
        env = index.environment(runner)
        url = env.get("CLAMS_CLAUDIO_EXECUTOR_URL")
        assert url == f"http://{agent}:8100"
        # the URL must not accidentally match a sibling (executor-1 vs -10 style
        # prefix bugs): the host part is exactly this pair's service name
        host = re.fullmatch(r"http://([^:/]+):(\d+)", url)
        assert host is not None
        assert host.group(1) == agent
        assert int(host.group(2)) == gen.AGENT_CONTAINER_PORT
        assert env.get("CLAMS_EXECUTOR_ID") == f"executor-{i}"


@pytest.mark.parametrize("count", [1, 2, 3, 6])
def test_all_published_host_ports_are_unique(count: int) -> None:
    index = idx.ComposeIndex(gen.render_for_executors(count))
    published = index.published_host_ports()
    assert len(published) == len(set(published)), published
    reserved = {gen.API_HOST_PORT, gen.UI_HOST_PORT, gen.PLANNER_HOST_PORT}
    for service, port in index.host_ports().items():
        if service in ("api", "claudio-ui", "claudio-planner"):
            continue
        assert port not in reserved, (service, port)


@pytest.mark.parametrize("count", [1, 2, 3, 5])
def test_executor_port_formula(count: int) -> None:
    index = idx.ComposeIndex(gen.render_for_executors(count))
    assert index.host_ports()["claudio-planner"] == 1072
    assert index.host_ports()["claudio-executor"] == 1073
    assert index.host_ports()["api"] == 1075
    assert index.host_ports()["claudio-ui"] == 1076
    for i in range(2, count + 1):
        assert index.host_ports()[f"claudio-executor-{i}"] == 1077 + (i - 2)


@pytest.mark.parametrize("count", [1, 2, 3, 7])
def test_no_in_container_url_uses_loopback(count: int) -> None:
    text = gen.render_for_executors(count)
    offenders = [url for url in re.findall(r"[a-zA-Z][\w+.-]*://[^\s\"']+", text)
                 if url.split("://", 1)[1].startswith("127.0.0.1")]
    assert offenders == []
    # every service-to-service URL goes through a compose DNS name
    for i in range(1, count + 1):
        assert f"http://{gen.agent_service_name(i)}:8100" in text
    assert "http://claudio-agent:8100" in text
    assert "http://claudio-planner:8100" in text
    assert "redis://redis:6379/0" in text


# --------------------------------------------------------------------------
# managed region markers
# --------------------------------------------------------------------------
@pytest.mark.parametrize("count", [1, 2, 3])
def test_managed_region_markers_are_present(count: int) -> None:
    text = gen.render_for_executors(count)
    assert gen.MARKER_BEGIN in text
    assert gen.MARKER_END in text
    assert "# BEGIN CLAMS-ADMIN GENERATED" in text
    assert "# END CLAMS-ADMIN GENERATED" in text
    begins = [line for line in text.splitlines() if line.strip().startswith(gen.MARKER_BEGIN)]
    ends = [line for line in text.splitlines() if line.strip().startswith(gen.MARKER_END)]
    assert len(begins) == len(ends) >= 1
    # every generated executor pair is wrapped
    for i in range(2, count + 1):
        assert f"{gen.MARKER_BEGIN}: executor pair {i}" in text
        assert f"{gen.MARKER_END}: executor pair {i}" in text


def test_managed_region_extracts_and_splices() -> None:
    text = gen.render_for_executors(2)
    before, region, after = gen.split_managed_region(text)
    assert region.lstrip().startswith(gen.MARKER_BEGIN)
    assert region.rstrip().endswith(gen.MARKER_END) or gen.MARKER_END in region
    assert "claudio-ui" in before + region
    assert "api:" in after
    assert gen.managed_region(text) == region


# --------------------------------------------------------------------------
# the template is seeded from the tracked default
# --------------------------------------------------------------------------
def test_template_keeps_the_tracked_default_features() -> None:
    text = gen.render_for_executors(1)
    assert text.startswith("# ")
    assert "\nname: clams\n" in text
    assert "x-clams: &clams" in text
    # anchor aliases: planner, executor-runner and api all merge the anchor
    assert text.count("<<: *clams") >= 3
    for service in BASE_SERVICES:
        assert f"  {service}:" in text
    assert "image: clams:local" in text
    assert "build: ." in text
    assert "env_file: .env" in text
    assert "restart: unless-stopped" in text
    assert "pull_policy: never" in text
    assert "networks: [clams]" in text
    assert "networks: {clams: {}, ui-planner: {}}" in text


def test_anchor_environment_and_gitconfig_wiring() -> None:
    text = gen.render_for_executors(2)
    assert "GIT_CONFIG_GLOBAL: /etc/clams-gitconfig" in text
    assert "CLAMS_DATA_DIR: /data" in text
    assert "CLAMS_DB_PATH: /data/clams.db" in text
    assert "CLAMS_WORK_ROOT: /work" in text
    assert "CLAMS_API_HOST: 0.0.0.0" in text
    assert "CLAMS_CLAUDIO_PLANNER_URL: http://claudio-planner:8100" in text
    assert "./config/clams-gitconfig:/etc/clams-gitconfig:ro" in text


def test_healthchecks_and_service_healthy_conditions() -> None:
    index = idx.ComposeIndex(gen.render_for_executors(3))
    for service in ("redis", "claudio-planner", "claudio-executor", "planner",
                    "executor-runner", "executor-runner-2", "executor-runner-3"):
        assert index.service(service).get("healthcheck"), service
    assert index.depends_on("planner")["claudio-planner"] == "service_healthy"
    assert index.depends_on("api")["planner"] == "service_healthy"
    assert index.depends_on("claudio-ui")["claudio-planner"] == "service_healthy"
    for service in index.services():
        if service == "redis":
            continue
        assert index.merged_service(service).get("restart") == "unless-stopped", service


def test_named_volumes_match_the_topology() -> None:
    index = idx.ComposeIndex(gen.render_for_executors(3))
    named = index.named_volumes()
    for volume in ("clams-db", "redis-data", "claudio-sessions", "claudio-planner-data",
                   "claudio-executor-data", "claudio-executor-2-data",
                   "claudio-executor-3-data", "worktrees"):
        assert volume in named, volume
    # every volume a service mounts is declared at the top level
    for service in index.services():
        for mount in index.volumes(service):
            source = str(mount).split(":")[0]
            if source.startswith("/") or source.startswith("."):
                continue
            assert source in named, (service, mount)


def test_executor_pair_mounts_are_isolated_and_shared_correctly() -> None:
    index = idx.ComposeIndex(gen.render_for_executors(3))
    for i in range(1, 4):
        agent = gen.agent_service_name(i)
        runner = gen.runner_service_name(i)
        data_volume = "claudio-executor-data" if i == 1 else f"claudio-executor-{i}-data"
        agent_mounts = index.volumes(agent)
        assert data_volume in " ".join(agent_mounts)
        assert any(m.startswith("claudio-sessions:") for m in agent_mounts)
        assert any(m.startswith("worktrees:") for m in agent_mounts)
        assert any(m.startswith("worktrees:") for m in index.volumes(runner))
        config_dir = "./config/agents/executor" if i == 1 else f"./config/agents/generated/executor-{i}"
        assert any(m.startswith(config_dir + ":") for m in agent_mounts), (i, agent_mounts)
        assert index.env_files(agent) == [gen.ComposeOptions().env_file_for(i)]
    # distinct data volume per executor, never shared
    sources = []
    for i in range(1, 4):
        mounts = index.volumes(gen.agent_service_name(i))
        sources += [m.split(":")[0] for m in mounts if m.startswith("claudio-executor")]
    assert len(sources) == len(set(sources)) == 3


def test_planner_is_the_default_ui_backend_via_network_alias() -> None:
    text = gen.render_for_executors(2)
    index = idx.ComposeIndex(text)
    planner_networks = index.service("claudio-planner").get("networks") or {}
    assert "ui-planner" in planner_networks
    assert planner_networks["ui-planner"]["aliases"] == ["claudio-agent"]
    assert index.networks_of("claudio-ui") == ["ui-planner"]


def test_generated_ui_backend_registry_lists_every_agent() -> None:
    index = idx.ComposeIndex(gen.render_for_executors(3))
    labels = index.service_labels("claudio-ui")
    env = index.environment("claudio-ui")
    assert labels["clams.ui.backend.planner.label"] == "Planner"
    assert labels["clams.ui.backend.default"] == "claudio-agent"
    assert labels["clams.ui.backend.count"] == "4"
    for i in range(1, 4):
        executor_id = f"executor-{i}"
        assert labels[f"clams.ui.backend.{executor_id}.label"] == f"Executor {i}"
        assert labels[f"clams.ui.backend.{executor_id}.url"] == f"http://{gen.agent_service_name(i)}:8100"
    assert env["CLAMS_UI_BACKEND_LABELS"] == "Planner,Executor 1,Executor 2,Executor 3"
    assert env["CLAMS_UI_BACKEND_DEFAULT"] == "http://claudio-agent:8100"
    # the registry itself is inside the managed region
    region = gen.managed_region(gen.render_for_executors(3))
    assert "clams.ui.backend.planner.label" in region


# --------------------------------------------------------------------------
# options: exposure address and debug-port publishing
# --------------------------------------------------------------------------
def test_exposure_address_prefixes_every_published_port() -> None:
    text = gen.render_for_executors(2, exposure_address="0.0.0.0")
    index = idx.ComposeIndex(text)
    assert index.ports("claudio-planner") == ["0.0.0.0:1072:8100"]
    assert index.ports("api") == ["0.0.0.0:1075:8200"]
    assert index.ports("claudio-executor-2") == ["0.0.0.0:1077:8100"]
    assert gen.render_for_executors(1, exposure_address="").count('"1072:8100"') == 1


def test_agent_debug_ports_can_be_left_unpublished() -> None:
    text = gen.render_for_executors(3, publish_agent_debug_ports=False)
    index = idx.ComposeIndex(text)
    for service in ("claudio-planner", "claudio-executor", "claudio-executor-2",
                    "claudio-executor-3"):
        assert index.ports(service) == [], service
        assert "127.0.0.1" not in " ".join(index.ports(service))
    # the UI and the API are not agent debug ports: they stay published
    assert index.ports("claudio-ui") == ['127.0.0.1:1076:8080']
    assert index.ports("api") == ['127.0.0.1:1075:8200']
    # in-container port is still 8100 for every agent
    for i in range(1, 4):
        env = index.environment(gen.runner_service_name(i))
        assert env["CLAMS_CLAUDIO_EXECUTOR_URL"].endswith(":8100")
    assert "ports:" not in "\n".join(
        line for line in text.splitlines() if "1073" in line or "1077" in line or "1078" in line
    )


def test_container_ports_are_stable() -> None:
    index = idx.ComposeIndex(gen.render_for_executors(2))
    assert index.container_port("claudio-planner") == 8100
    assert index.container_port("claudio-executor") == 8100
    assert index.container_port("claudio-executor-2") == 8100
    assert index.container_port("claudio-ui") == 8080
    assert index.container_port("api") == 8200


# --------------------------------------------------------------------------
# config sources
# --------------------------------------------------------------------------
def test_render_accepts_a_deployment_and_a_plain_mapping() -> None:
    from_yaml = gen.render(deployment_with_executors(3))
    from_dict = gen.render(deployment_with_executors(3).to_dict())
    assert idx.services(from_yaml) == idx.services(from_dict)
    assert len(idx.services(from_dict)) == 11


def test_render_defaults_to_the_tracked_single_executor_topology() -> None:
    services = idx.services(gen.render(None))
    assert services == BASE_SERVICES
    assert idx.services(gen.render(default_deployment())) == BASE_SERVICES


def test_render_reads_ports_images_and_env_files_from_the_deployment() -> None:
    deployment = deployment_with_executors(2)
    deployment.planner_port = 2072
    deployment.api_port = 2075
    deployment.ui = ClaudioUI(port=2076, container_port=9080, image="claudio/web:1.2.3")
    deployment.admin = AdminConfig(name="clams", executor_count=2,
                                   env_file=".env", planner_env_file=".env.planner",
                                   executor_env_file=".env.claudio-executor")
    index = idx.ComposeIndex(gen.render(deployment))
    assert index.host_ports() == {"claudio-planner": 2072, "claudio-ui": 2076, "api": 2075,
                                  "claudio-executor": 1073, "claudio-executor-2": 1077}
    assert index.ports("claudio-ui") == ["127.0.0.1:2076:9080"]
    assert "claudio/web:1.2.3" in index.image("claudio-ui")
    assert index.env_files("claudio-planner") == [".env.planner"]
    assert index.env_files("claudio-executor-2") == [".env.claudio-executor-2"]


def test_render_honours_executor_debug_port_overrides_without_collisions() -> None:
    deployment = default_deployment()
    deployment.executors = [
        ExecutorInstance(executor_id="executor-1", debug_port=3001),
        ExecutorInstance(executor_id="executor-2", debug_port=3002),
        ExecutorInstance(executor_id="executor-3", debug_port=3003),
    ]
    deployment.admin = AdminConfig(name="clams", executor_count=3)
    index = idx.ComposeIndex(gen.render(deployment))
    assert index.host_ports()["claudio-executor"] == 3001
    assert index.host_ports()["claudio-executor-3"] == 3003
    published = index.published_host_ports()
    assert len(published) == len(set(published))


def test_render_is_deterministic_and_self_contained() -> None:
    assert gen.render_for_executors(3) == gen.render_for_executors(3)
    text = gen.render_for_executors(3)
    assert text.endswith("\n")
    assert "@@" not in text  # no unsubstituted template tokens
    assert "%%" not in text


def test_render_rejects_a_nonsense_config() -> None:
    with pytest.raises(gen.ComposeGenError):
        gen.render(42)
