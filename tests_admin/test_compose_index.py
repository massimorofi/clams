"""``compose_index`` reads compose topology back **without PyYAML**.

The admin scaffold must be verifiable in a container that only has
``python3``/``pytest``, so these tests (a) prove the scanner never imports
``yaml`` and (b) pin its behaviour on hand-written snippets *and* on what
:mod:`clams_admin.compose_gen` actually emits. Stdlib + pytest only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from clams_admin import compose_gen as gen
from clams_admin import compose_index as idx

SNIPPET = """
name: clams

x-clams: &clams
  image: clams:local
  build: .
  env_file: .env
  environment:
    CLAMS_REDIS_URL: redis://redis:6379/0
    GIT_CONFIG_GLOBAL: /etc/clams-gitconfig
  restart: unless-stopped
  networks: [clams]

services:
  redis:
    image: redis:7-alpine
    volumes: [redis-data:/data]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
    networks: [clams]
    restart: unless-stopped

  claudio-planner:
    image: ${CLAUDIO_IMAGE:-claudio/agent:latest}
    ports: ["127.0.0.1:1072:8100"]
    env_file: .env.claudio-planner
    volumes:
      - claudio-sessions:/sessions
      - ./config/agents/planner:/srv/claudio/claudio-backend/config  # dir mount
    networks:
      clams: {}
      ui-planner: { aliases: [claudio-agent] }

  planner:
    <<: *clams
    command: ["planner"]
    volumes: [clams-db:/data]
    depends_on:
      redis: {condition: service_healthy}
      claudio-planner: {condition: service_healthy}

  executor-runner:
    <<: *clams
    depends_on:
      redis:
        condition: service_healthy
      claudio-executor:
        condition: service_started
    env_file:
      - .env
      - .env.claudio-executor

networks: {clams: {}, ui-planner: {}}
volumes: {clams-db: {}, redis-data: {}, claudio-sessions: {}}
"""


# --------------------------------------------------------------------------
# stdlib-only guarantee
# --------------------------------------------------------------------------
def test_indexing_does_not_import_pyyaml() -> None:
    """A fresh interpreter must not see ``yaml`` after importing the index."""
    import os
    import subprocess

    src_root = str(Path(idx.__file__).resolve().parent.parent)
    code = ("import sys, clams_admin.compose_index as ci; "
            "ci.ComposeIndex('services:\\n  a:\\n    image: x\\n'); "
            "print('yaml' in sys.modules)")
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONPATH": src_root},
    )
    assert result.stdout.strip() == "False", "compose_index must stay stdlib-only"


def test_module_functions_accept_text_or_an_index() -> None:
    from_text = idx.services(SNIPPET)
    from_index = idx.services(idx.index(SNIPPET))
    assert from_text == from_index
    assert idx.ports(SNIPPET, "claudio-planner") == idx.ports(idx.index(SNIPPET), "claudio-planner")
    assert idx.image(SNIPPET, "redis") == "redis:7-alpine"
    assert isinstance(idx.index(idx.index(SNIPPET)), idx.ComposeIndex)


# --------------------------------------------------------------------------
# services()
# --------------------------------------------------------------------------
def test_services_are_returned_in_document_order() -> None:
    assert idx.services(SNIPPET) == ["redis", "claudio-planner", "planner", "executor-runner"]


def test_services_of_generated_documents_match_the_template() -> None:
    for count in (1, 2, 3):
        services = idx.services(gen.render_for_executors(count))
        assert len(services) == 5 + 2 * count
        assert services[0] == "redis"
        assert services[-1] == "api"


def test_missing_service_is_empty_not_an_error() -> None:
    index = idx.ComposeIndex(SNIPPET)
    assert index.service("nope") == {}
    assert index.ports("nope") == []
    assert index.env_files("nope") == []
    assert index.volumes("nope") == []
    assert index.depends_on("nope") == {}
    assert index.image("nope") is None
    assert not index.has_service("nope")


def test_empty_document_is_fine() -> None:
    index = idx.ComposeIndex("")
    assert index.services() == []
    assert index.volumes() == []
    assert index.networks() == []
    assert index.published_host_ports() == []


# --------------------------------------------------------------------------
# ports()
# --------------------------------------------------------------------------
def test_ports_are_strings_and_keep_the_address() -> None:
    index = idx.ComposeIndex(SNIPPET)
    assert index.ports("claudio-planner") == ["127.0.0.1:1072:8100"]
    assert index.ports("redis") == []
    assert index.ports() == ["127.0.0.1:1072:8100"]
    assert index.container_port("claudio-planner") == 8100


def test_host_ports_are_parsed_from_generated_text() -> None:
    index = idx.ComposeIndex(gen.render_for_executors(3))
    assert index.host_ports() == {
        "claudio-planner": 1072,
        "claudio-executor": 1073,
        "claudio-ui": 1076,
        "claudio-executor-2": 1077,
        "claudio-executor-3": 1078,
        "api": 1075,
    }
    assert sorted(index.published_host_ports()) == [1072, 1073, 1075, 1076, 1077, 1078]


@pytest.mark.parametrize("entry,expected", [
    ("127.0.0.1:1072:8100", (1072, 8100)),
    ("1072:8100", (1072, 8100)),
    ("8100", (8100, 8100)),
    ("0.0.0.0:1075:8200", (1075, 8200)),
    ("8100/udp", (8100, 8100)),
    ('"1076:8080"', (1076, 8080)),
    ("not-a-port", None),
])
def test_parse_port_mapping(entry: str, expected) -> None:
    assert idx.parse_port_mapping(entry) == expected


# --------------------------------------------------------------------------
# env_files()
# --------------------------------------------------------------------------
def test_env_files_scalar_and_list_forms() -> None:
    index = idx.ComposeIndex(SNIPPET)
    assert index.env_files("claudio-planner") == [".env.claudio-planner"]
    assert index.env_files("executor-runner") == [".env", ".env.claudio-executor"]
    assert index.env_files("redis") == []


def test_env_files_of_generated_executors() -> None:
    index = idx.ComposeIndex(gen.render_for_executors(3))
    assert index.env_files("claudio-executor") == [".env.claudio-executor"]
    assert index.env_files("claudio-executor-2") == [".env.claudio-executor-2"]
    assert index.env_files("claudio-executor-3") == [".env.claudio-executor-3"]
    assert index.env_files("executor-runner-3") == [".env", ".env.claudio-executor-3"]


# --------------------------------------------------------------------------
# volumes()
# --------------------------------------------------------------------------
def test_volumes_block_and_flow_forms() -> None:
    index = idx.ComposeIndex(SNIPPET)
    assert index.volumes("redis") == ["redis-data:/data"]
    assert index.volumes("claudio-planner") == [
        "claudio-sessions:/sessions",
        "./config/agents/planner:/srv/claudio/claudio-backend/config",
    ]
    assert index.volumes("planner") == ["clams-db:/data"]


def test_top_level_named_volumes_and_networks() -> None:
    index = idx.ComposeIndex(SNIPPET)
    assert index.volumes() == ["clams-db", "redis-data", "claudio-sessions"]
    assert index.named_volumes() == index.volumes()
    assert index.networks() == ["clams", "ui-planner"]
    generated = idx.ComposeIndex(gen.render_for_executors(2))
    assert "claudio-executor-2-data" in generated.volumes()
    assert generated.networks() == ["clams", "ui-planner"]
    assert generated.networks_of("claudio-ui") == ["ui-planner"]


# --------------------------------------------------------------------------
# depends_on()
# --------------------------------------------------------------------------
def test_depends_on_flow_mapping_form() -> None:
    assert idx.depends_on(SNIPPET, "planner") == {
        "redis": "service_healthy",
        "claudio-planner": "service_healthy",
    }


def test_depends_on_block_form() -> None:
    assert idx.depends_on(SNIPPET, "executor-runner") == {
        "redis": "service_healthy",
        "claudio-executor": "service_started",
    }


def test_depends_on_generated_pairs() -> None:
    index = idx.ComposeIndex(gen.render_for_executors(3))
    assert index.depends_on("executor-runner-2") == {
        "redis": "service_healthy",
        "claudio-executor-2": "service_healthy",
    }
    assert index.depends_on("api") == {"planner": "service_healthy"}
    assert index.depends_on("redis") == {}


# --------------------------------------------------------------------------
# image()
# --------------------------------------------------------------------------
def test_image_keeps_compose_variable_interpolation_verbatim() -> None:
    index = idx.ComposeIndex(SNIPPET)
    assert index.image("redis") == "redis:7-alpine"
    assert index.image("claudio-planner") == "${CLAUDIO_IMAGE:-claudio/agent:latest}"
    generated = idx.ComposeIndex(gen.render_for_executors(2))
    assert generated.image("claudio-executor-2") == "${CLAUDIO_EXECUTOR_IMAGE:-claudio/agent-exec:latest}"
    assert generated.image("claudio-ui") == "${CLAUDIO_WEB_IMAGE:-claudio/web:latest}"
    assert generated.image("planner") is None  # comes from the anchor
    assert generated.merged_service("planner")["image"] == "clams:local"


# --------------------------------------------------------------------------
# anchors, merges, environment
# --------------------------------------------------------------------------
def test_anchors_and_merged_environment() -> None:
    index = idx.ComposeIndex(SNIPPET)
    assert "clams" in index.anchors
    assert index.environment("planner") == {}
    merged = index.merged_environment("planner")
    assert merged["CLAMS_REDIS_URL"] == "redis://redis:6379/0"
    assert merged["GIT_CONFIG_GLOBAL"] == "/etc/clams-gitconfig"
    assert index.merged_service("planner")["restart"] == "unless-stopped"
    assert index.merged_service("planner")["command"] == ["planner"]


def test_generated_runner_environment_is_self_contained() -> None:
    index = idx.ComposeIndex(gen.render_for_executors(2))
    env = index.environment("executor-runner-2")
    # a local ``environment:`` replaces the anchor wholesale, so every key the
    # runner needs must be restated - and the executor URL must be its own.
    assert env["CLAMS_CLAUDIO_EXECUTOR_URL"] == "http://claudio-executor-2:8100"
    assert env["CLAMS_CLAUDIO_PLANNER_URL"] == "http://claudio-planner:8100"
    assert env["CLAMS_REDIS_URL"] == "redis://redis:6379/0"
    assert env["GIT_CONFIG_GLOBAL"] == "/etc/clams-gitconfig"
    assert env["CLAMS_EXECUTOR_ID"] == "executor-2"
    assert index.merged_environment("executor-runner-2") == env


def test_service_labels_are_strings() -> None:
    labels = idx.service_labels(gen.render_for_executors(2), "claudio-ui")
    assert labels["clams.ui.backend.count"] == "3"
    assert labels["clams.ui.backend.executor-2.label"] == "Executor 2"


# --------------------------------------------------------------------------
# robustness of the scanner itself
# --------------------------------------------------------------------------
def test_comments_and_blank_lines_are_ignored() -> None:
    commented = "# leading comment\n\n" + SNIPPET.replace(
        "  redis:\n", "  # a comment about redis\n  redis:\n"
    )
    assert idx.services(commented) == idx.services(SNIPPET)


def test_multi_line_flow_sequence_is_joined() -> None:
    index = idx.ComposeIndex(gen.render_for_executors(1))
    test = index.service("claudio-planner")["healthcheck"]["test"]
    assert isinstance(test, list)
    assert test[0] == "CMD"
    assert any("healthz" in str(part) for part in test)


def test_hash_inside_a_value_is_not_a_comment() -> None:
    text = 'services:\n  a:\n    image: "repo:tag#1"\n    command: [echo, "x#y"]\n'
    index = idx.ComposeIndex(text)
    assert index.image("a") == "repo:tag#1"
    assert index.service("a")["command"] == ["echo", "x#y"]


def test_index_round_trips_generated_documents_for_many_n() -> None:
    for count in range(1, 6):
        text = gen.render_for_executors(count)
        index = idx.ComposeIndex(text)
        assert len(index.services()) == 5 + 2 * count
        assert index.image("redis") == "redis:7-alpine"
        assert index.depends_on(gen.runner_service_name(count))["claudio-executor"
                                                            + ("" if count == 1 else f"-{count}")] \
            == "service_healthy"
