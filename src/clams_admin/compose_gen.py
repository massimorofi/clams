"""Render the **complete effective ``docker-compose.yml``** from a deployment.

Stdlib-only: the document is produced from a plain string template (no PyYAML,
no docker CLI) seeded from the tracked ``docker-compose.yml`` in this repo, so
what ``clams-admin`` writes is byte-comparable with what a human reads.

Topology (single source of truth: this template)
-----------------------------------------------
* ``name: clams`` plus the ``x-clams: &clams`` anchor that ``planner``,
  ``executor-runner`` and ``api`` merge with ``<<: *clams``. The anchor carries
  the in-container wiring (``CLAMS_*``, ``GIT_CONFIG_GLOBAL``), the
  ``restart: unless-stopped`` policy and ``networks: [clams]``.
* one ``redis``, one ``claudio-planner``, one ``claudio-ui``, one ``planner``,
  one ``api`` — and **N** executor pairs.
* Executor instance 1 keeps the historical names ``claudio-executor`` /
  ``executor-runner`` (``clamsctl`` greps those names); instances 2..N are
  ``claudio-executor-N`` / ``executor-runner-N``.
* Every ``executor-runner-N`` talks to exactly one agent:
  ``CLAMS_CLAUDIO_EXECUTOR_URL: http://claudio-executor-N:8100`` and
  ``depends_on: {claudio-executor-N: {condition: service_healthy}}``.
* Host ports (published on the configured exposure address): planner 1072,
  executor 1 -> 1073, executor i>=2 -> 1077+(i-2), UI 1076, API 1075. All
  distinct by construction. ``publish_agent_debug_ports=False`` publishes no
  agent port at all; the in-container port stays 8100 either way.
* Exactly one ``claudio-ui``, on ``ui-planner``, whose default backend is the
  planner through the network alias ``claudio-agent``. A generated registry
  (labels + env) lists the planner and every executor with human labels.

Managed region
--------------
Everything this module *adds* on top of the single-executor default is wrapped
in ``# BEGIN CLAMS-ADMIN GENERATED`` / ``# END CLAMS-ADMIN GENERATED`` markers
so a human can tell at a glance which lines are machine-owned, and so
``clams-admin compose write`` can splice a new region into an existing file.
The markers are always emitted (the UI backend registry is always generated).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "MARKER_BEGIN",
    "MARKER_END",
    "DEFAULT_EXPOSURE_ADDRESS",
    "AGENT_CONTAINER_PORT",
    "UI_CONTAINER_PORT",
    "API_CONTAINER_PORT",
    "PLANNER_HOST_PORT",
    "API_HOST_PORT",
    "UI_HOST_PORT",
    "FIRST_EXECUTOR_HOST_PORT",
    "EXTRA_EXECUTOR_HOST_PORT_BASE",
    "ComposeGenError",
    "ComposeOptions",
    "ExecutorPlacement",
    "agent_service_name",
    "runner_service_name",
    "executor_host_port",
    "placements",
    "port_plan",
    "options_from_config",
    "render",
    "render_for_executors",
    "default_config",
    "managed_region",
    "split_managed_region",
]


class ComposeGenError(ValueError):
    """The deployment cannot be rendered into a compose document."""


# --------------------------------------------------------------------------
# marker strings (kept as plain literals: ``grep 'CLAMS-ADMIN GENERATED'`` is
# the acceptance check that this module owns a managed region)
# --------------------------------------------------------------------------
MARKER_BEGIN = "# BEGIN CLAMS-ADMIN GENERATED"
MARKER_END = "# END CLAMS-ADMIN GENERATED"


def _begin(what: str = "") -> str:
    return f"{MARKER_BEGIN}: {what}" if what else MARKER_BEGIN


def _end(what: str = "") -> str:
    return f"{MARKER_END}: {what}" if what else MARKER_END


# --------------------------------------------------------------------------
# port plan
# --------------------------------------------------------------------------
#: every Claudio agent listens on 8100 inside its own container
AGENT_CONTAINER_PORT = 8100
#: the claudio/web UI listens on 8080
UI_CONTAINER_PORT = 8080
#: the clams API listens on 8200 inside the api container
API_CONTAINER_PORT = 8200

PLANNER_HOST_PORT = 1072
API_HOST_PORT = 1075
UI_HOST_PORT = 1076
FIRST_EXECUTOR_HOST_PORT = 1073
#: executor instance ``i`` (``i >= 2``) is published on ``1077 + (i - 2)``
EXTRA_EXECUTOR_HOST_PORT_BASE = 1077

DEFAULT_EXPOSURE_ADDRESS = "127.0.0.1"
"""Loopback by default: the published ports are operator debug ports."""


def executor_host_port(index: int, configured: Optional[int] = None) -> int:
    """Host debug port of executor *index* (1-based).

    ``configured`` (``ExecutorInstance.debug_port``) always wins; otherwise
    instance 1 is 1073 and instance ``i >= 2`` is ``1077 + (i - 2)`` so the
    generated block never collides with 1072/1073/1075/1076.
    """
    if index < 1:
        raise ComposeGenError(f"executor index must be >= 1, got {index}")
    if configured:
        return int(configured)
    if index == 1:
        return FIRST_EXECUTOR_HOST_PORT
    return EXTRA_EXECUTOR_HOST_PORT_BASE + (index - 2)


# --------------------------------------------------------------------------
# options
# --------------------------------------------------------------------------
@dataclass
class ComposeOptions:
    """Everything that is a *rendering* choice rather than a runtime setting.

    ``exposure_address`` prefixes every published port (``"127.0.0.1"`` ->
    ``"127.0.0.1:1072:8100"``; ``""`` publishes on every interface exactly
    like the bare ``"1072:8100"`` form). ``publish_agent_debug_ports=False``
    publishes **no** agent port at all - planner and executors stay reachable
    only from the ``clams`` network, on container port 8100.
    """

    exposure_address: str = DEFAULT_EXPOSURE_ADDRESS
    publish_agent_debug_ports: bool = True
    executor_count: Optional[int] = None
    planner_host_port: int = PLANNER_HOST_PORT
    api_host_port: int = API_HOST_PORT
    ui_host_port: int = UI_HOST_PORT
    ui_container_port: int = UI_CONTAINER_PORT
    clams_image: str = "clams:local"
    clams_build: str = "."
    root_env_file: str = ".env"
    planner_env_file: str = ".env.claudio-planner"
    executor_env_file: str = ".env.claudio-executor"
    planner_config_dir: str = "./config/agents/planner"
    executor_config_dir: str = "./config/agents/executor"
    generated_config_dir: str = "./config/agents/generated"
    gitconfig_mount: str = "./config/clams-gitconfig:/etc/clams-gitconfig:ro"
    planner_image: str = "${CLAUDIO_IMAGE:-claudio/agent:latest}"
    executor_image: str = "${CLAUDIO_EXECUTOR_IMAGE:-claudio/agent-exec:latest}"
    ui_image: str = "${CLAUDIO_WEB_IMAGE:-claudio/web:latest}"
    redis_image: str = "redis:7-alpine"
    redis_url: str = "redis://redis:6379/0"
    planner_url: str = "http://claudio-planner:8100"
    #: extra ``KEY: value`` lines appended to the ``x-clams`` environment block
    extra_environment: dict[str, str] = field(default_factory=dict)

    # -- derived helpers -----------------------------------------------------
    def address_prefix(self) -> str:
        """``"127.0.0.1:"`` or ``""`` (never a bare ``":``)."""
        addr = (self.exposure_address or "").strip()
        return f"{addr}:" if addr else ""

    def published(self, host_port: int, container_port: int) -> str:
        return f'"{self.address_prefix()}{host_port}:{container_port}"'

    def env_file_for(self, index: int) -> str:
        """Instance 1 keeps ``.env.claudio-executor``; 2..N get a ``-N`` suffix."""
        if index <= 1:
            return self.executor_env_file
        return f"{self.executor_env_file}-{index}"

    def config_dir_for(self, index: int) -> str:
        """Tracked dir for instance 1, ``generated/executor-N`` for 2..N."""
        if index <= 1:
            return self.executor_config_dir
        return f"{self.generated_config_dir}/executor-{index}"


@dataclass
class ExecutorPlacement:
    """One executor pair: service names, port, files and volumes."""

    index: int
    agent_service: str
    runner_service: str
    executor_id: str
    host_port: int
    env_file: str
    config_dir: str
    data_volume: str

    @property
    def agent_url(self) -> str:
        """The URL *only* this pair's runner may use (never 127.0.0.1)."""
        return f"http://{self.agent_service}:{AGENT_CONTAINER_PORT}"


def agent_service_name(index: int) -> str:
    """``claudio-executor`` for instance 1 (clamsctl compatibility), else ``-N``."""
    return "claudio-executor" if index <= 1 else f"claudio-executor-{index}"


def runner_service_name(index: int) -> str:
    """``executor-runner`` for instance 1, else ``executor-runner-N``."""
    return "executor-runner" if index <= 1 else f"executor-runner-{index}"


def placements(options: ComposeOptions,
               executor_ports: Sequence[Optional[int]] = (),
               executor_ids: Sequence[Optional[str]] = ()) -> list[ExecutorPlacement]:
    """The N executor pairs implied by *options*, in instance order."""
    count = max(1, int(options.executor_count or 1))
    out: list[ExecutorPlacement] = []
    for index in range(1, count + 1):
        configured_port = executor_ports[index - 1] if index - 1 < len(executor_ports) else None
        configured_id = executor_ids[index - 1] if index - 1 < len(executor_ids) else None
        out.append(
            ExecutorPlacement(
                index=index,
                agent_service=agent_service_name(index),
                runner_service=runner_service_name(index),
                executor_id=configured_id or f"executor-{index}",
                host_port=executor_host_port(index, configured_port),
                env_file=options.env_file_for(index),
                config_dir=options.config_dir_for(index),
                data_volume=("claudio-executor-data" if index <= 1
                             else f"claudio-executor-{index}-data"),
            )
        )
    return out


def port_plan(options: ComposeOptions,
              executor_ports: Sequence[Optional[int]] = (),
              executor_ids: Sequence[Optional[str]] = ()) -> dict[str, int]:
    """``service -> host port`` for every published port.

    Agent debug ports are omitted entirely when
    ``publish_agent_debug_ports`` is off.
    """
    plan: dict[str, int] = {}
    if options.publish_agent_debug_ports:
        plan["claudio-planner"] = options.planner_host_port
    plan["claudio-ui"] = options.ui_host_port
    plan["api"] = options.api_host_port
    if options.publish_agent_debug_ports:
        for place in placements(options, executor_ports, executor_ids):
            plan[place.agent_service] = place.host_port
    return plan


# --------------------------------------------------------------------------
# the template
# --------------------------------------------------------------------------
#: A line whose content is exactly this sentinel is removed after substitution.
#: It is how an optional line (a published debug port that is switched off, an
#: empty generated region) disappears instead of leaving a blank line behind.
DROP = "@@DROP@@"

_DROP = DROP

# NOTE: ``%%NAME%%`` tokens (not ``string.Template``) because compose bodies are
# full of ``$`` (``${CLAUDIO_IMAGE:-...}``) and ``{}`` (flow mappings).
_TEMPLATE = """# Effective docker-compose.yml for the "clams" deployment.
# Generated by clams-admin (compose_gen). The single-executor shape below is
# seeded from the tracked docker-compose.yml; everything clams-admin *adds* for
# extra executors and for the UI backend registry sits inside
# # BEGIN CLAMS-ADMIN GENERATED / # END CLAMS-ADMIN GENERATED markers.
name: clams

x-clams: &clams
  image: %%CLAMS_IMAGE%%
  build: %%CLAMS_BUILD%%
  env_file: %%ROOT_ENV%%
  environment:
    # In-container wiring; overrides .env (which keeps host-dev defaults
    # and secrets). Volumes: clams-db -> /data, worktrees -> /work.
    CLAMS_DATA_DIR: /data
    CLAMS_DB_PATH: /data/clams.db
    CLAMS_WORK_ROOT: /work
    CLAMS_CLAUDIO_WORKSPACE_ROOT: /work
    CLAMS_REDIS_URL: %%REDIS_URL%%
    # Must bind all interfaces inside the container, or docker's port
    # forwarding can't reach it; the host mapping keeps it loopback-only.
    CLAMS_API_HOST: 0.0.0.0
    CLAMS_CLAUDIO_PLANNER_URL: %%PLANNER_URL%%
    CLAMS_CLAUDIO_EXECUTOR_URL: http://claudio-executor:8100
    # git credential helper for task-branch pushes (executor-runner mounts
    # config/clams-gitconfig); harmless where no git runs.
    GIT_CONFIG_GLOBAL: /etc/clams-gitconfig
%%EXTRA_ENV%%
  restart: unless-stopped
  networks: [clams]

services:
  redis:
    image: %%REDIS_IMAGE%%
    command: ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec"]
    volumes: [redis-data:/data]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      retries: 5
    networks: [clams]
    restart: unless-stopped

  claudio-planner:
    image: %%PLANNER_IMAGE%%
    # locally built from the claudio repo - not on any registry, so
    # `clamsctl rebuild`'s pull step must skip it
    pull_policy: never
%%PLANNER_PORTS%%
    env_file: %%PLANNER_ENV%%
    volumes:
      - claudio-sessions:/sessions
      # transcripts live here - without this a rebuild wipes every session
      - claudio-planner-data:/srv/claudio/claudio-backend/data
      # image config wins over env vars; bind our copy (raised run deadline).
      # Mount the DIRECTORY, not the file: claudio persists UI config edits
      # (e.g. agent name) by rename() over config.yaml, which fails EBUSY
      # when the file itself is a bind mount.
      - %%PLANNER_CONFIG%%:/srv/claudio/claudio-backend/config
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8100/healthz').status==200 else 1)"]
      interval: 15s
      retries: 5
    networks:
      clams: {}
      # alias so the stock claudio/web nginx upstream (claudio-agent:8100)
      # resolves to THIS backend - the single UI's default (planner) backend
      ui-planner: { aliases: [claudio-agent] }
    restart: unless-stopped

  claudio-executor:
    # executor-side agent: derived image with pytest for evidence-gate runs
    image: %%EXECUTOR_IMAGE%%
    pull_policy: never
%%EXEC1_PORTS%%
    env_file: %%EXEC1_ENV%%
    volumes:
      - claudio-sessions:/sessions
      # transcripts live here - without this a rebuild wipes every session
      - claudio-executor-data:/srv/claudio/claudio-backend/data
      - worktrees:/work
      # image config wins over env vars; bind our copy so /work is an allowed
      # root. Directory mount - see planner note re: rename()/EBUSY.
      - %%EXEC1_CONFIG%%:/srv/claudio/claudio-backend/config
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8100/healthz').status==200 else 1)"]
      interval: 15s
      retries: 5
    networks:
      clams: {}
    restart: unless-stopped

  # Single Claudio web UI. Its nginx proxies same-origin /api to claudio-agent
  # (alias -> claudio-planner), which is therefore the default backend. The
  # registry below names every agent the operator can switch the UI to.
  claudio-ui:
    image: %%UI_IMAGE%%
    pull_policy: never
%%UI_PORTS%%
    depends_on:
      claudio-planner: {condition: service_healthy}
%%UI_REGISTRY%%
    networks: [ui-planner]
    restart: unless-stopped

  planner:
    <<: *clams
    command: ["planner"]
    volumes: [clams-db:/data]
    depends_on:
      redis: {condition: service_healthy}
      claudio-planner: {condition: service_healthy}
    healthcheck:
      test: ["CMD", "clams", "healthcheck", "planner"]
      interval: 20s
      retries: 3

  executor-runner:
    <<: *clams
    command: ["executor"]
    env_file:
      - %%ROOT_ENV%%
      - %%EXEC1_ENV%%
    environment:
%%EXEC1_RUNNER_ENV%%
    volumes:
      - worktrees:/work
      # git credential helper for pushing task branches (GIT_CONFIG_GLOBAL)
      - %%GITCONFIG_MOUNT%%
    depends_on:
      redis: {condition: service_healthy}
      claudio-executor: {condition: service_healthy}
    healthcheck:
      test: ["CMD", "clams", "healthcheck", "executor"]
      interval: 20s
      retries: 3
%%GENERATED_PAIRS%%
  api:
    <<: *clams
    command: ["api"]
    # rw on clams-db is required: the API's only write is INSERT into commands
    volumes: [clams-db:/data, claudio-sessions:/sessions:ro]
%%API_PORTS%%
    depends_on:
      planner: {condition: service_healthy}

networks: {clams: {}, ui-planner: {}}
volumes: %%VOLUMES%%
"""


# --------------------------------------------------------------------------
# block builders
# --------------------------------------------------------------------------
def _fill(template: str, values: Mapping[str, str]) -> str:
    """Substitute ``%%NAME%%`` tokens, then drop ``@@DROP@@`` placeholder lines."""
    out = template
    for name, value in values.items():
        out = out.replace(f"%%{name}%%", value)
    kept = [line for line in out.split("\n") if line.strip() != _DROP]
    return "\n".join(kept)


def _quoted(value: str) -> str:
    """Quote a YAML scalar only when it must be quoted.

    Mirrors the tracked ``docker-compose.yml`` style: ``redis://redis:6379/0``
    stays bare (a ``:`` that is not followed by a space is not a mapping
    separator), while ``Executor 1`` is quoted because it contains a space and
    could otherwise be read as a number or a boolean.
    """
    text = str(value)
    if text == "" or text != text.strip():
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    lowered = text.lower()
    ambiguous = {
        "true", "false", "yes", "no", "on", "off", "null", "none", "~",
        "y", "n", "-", "+",
    }
    needs_quotes = (
        ": " in text or " #" in text or text.startswith("#")
        or text[0] in "-?:,[]{}#&*!|>'\"%@`"
        or lowered in ambiguous
    )
    if not needs_quotes:
        try:
            int(text)
            needs_quotes = True
        except ValueError:
            try:
                float(text)
                needs_quotes = True
            except ValueError:
                needs_quotes = False
    if needs_quotes:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _label(value: Any) -> str:
    """Docker labels must be strings, so numbers get quoted even when YAML
    would otherwise read them as an int."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _anchor_environment(options: ComposeOptions) -> dict[str, str]:
    """The ``x-clams`` environment mapping, in order (the anchor's own keys)."""
    env: dict[str, str] = {
        "CLAMS_DATA_DIR": "/data",
        "CLAMS_DB_PATH": "/data/clams.db",
        "CLAMS_WORK_ROOT": "/work",
        "CLAMS_CLAUDIO_WORKSPACE_ROOT": "/work",
        "CLAMS_REDIS_URL": options.redis_url,
        # bind all interfaces *inside* the container; the host mapping decides
        # who can reach it.
        "CLAMS_API_HOST": "0.0.0.0",
        "CLAMS_CLAUDIO_PLANNER_URL": options.planner_url,
        "CLAMS_CLAUDIO_EXECUTOR_URL": "http://claudio-executor:8100",
        "GIT_CONFIG_GLOBAL": "/etc/clams-gitconfig",
    }
    env.update({str(k): str(v) for k, v in (options.extra_environment or {}).items()})
    return env


def _runner_environment(options: ComposeOptions, place: ExecutorPlacement, indent: str = "      ") -> str:
    """Full ``environment:`` body for one runner.

    YAML merge keys are *shallow*: a local ``environment:`` map replaces the
    anchor's wholesale, so a runner that overrides one key must restate them
    all. That is why every runner block is self-contained - and why each one
    names exactly its own agent in ``CLAMS_CLAUDIO_EXECUTOR_URL``.
    """
    env = _anchor_environment(options)
    env["CLAMS_CLAUDIO_EXECUTOR_URL"] = place.agent_url
    env["CLAMS_EXECUTOR_ID"] = place.executor_id
    return "\n".join(f"{indent}{key}: {_quoted(value)}" for key, value in env.items())


_AGENT_HEALTHCHECK = """      test: ["CMD", "python", "-c",
             "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8100/healthz').status==200 else 1)"]
      interval: 15s
      retries: 5"""

_RUNNER_HEALTHCHECK = """      test: ["CMD", "clams", "healthcheck", "executor"]
      interval: 20s
      retries: 3"""


def _generated_pair(options: ComposeOptions, place: ExecutorPlacement) -> str:
    """One ``claudio-executor-N`` + ``executor-runner-N`` pair, marker-wrapped.

    Instance 1 is rendered by the template itself (tracked names, no markers);
    this builds instances 2..N.
    """
    ports_line = (f"    ports: [{options.published(place.host_port, AGENT_CONTAINER_PORT)}]"
                  if options.publish_agent_debug_ports else _DROP)
    return f"""  {_begin(f'executor pair {place.index}')}
  {place.agent_service}:
    # executor-side agent {place.index}: derived image with pytest for
    # evidence-gate runs
    image: {options.executor_image}
    pull_policy: never
{ports_line}
    env_file: {place.env_file}
    volumes:
      - claudio-sessions:/sessions
      - {place.data_volume}:/srv/claudio/claudio-backend/data
      - worktrees:/work
      # generated per-executor config dir (directory mount, see planner note)
      - {place.config_dir}:/srv/claudio/claudio-backend/config
    healthcheck:
{_AGENT_HEALTHCHECK}
    networks:
      clams: {{}}
    restart: unless-stopped

  {place.runner_service}:
    <<: *clams
    command: ["executor"]
    env_file:
      - {options.root_env_file}
      - {place.env_file}
    environment:
{_runner_environment(options, place)}
    volumes:
      - worktrees:/work
      # git credential helper for pushing task branches (GIT_CONFIG_GLOBAL)
      - {options.gitconfig_mount}
    depends_on:
      redis: {{condition: service_healthy}}
      {place.agent_service}: {{condition: service_healthy}}
    healthcheck:
{_RUNNER_HEALTHCHECK}
  {_end(f'executor pair {place.index}')}"""


def _ui_registry(options: ComposeOptions, places: Sequence[ExecutorPlacement]) -> str:
    """The generated backend registry for the single ``claudio-ui``.

    Labels *and* env both list the planner plus every executor, with the human
    labels ``Planner`` / ``Executor N``. The default backend is the planner via
    the ``claudio-agent`` network alias.
    """
    backends: list[tuple[str, str, str]] = [("planner", "Planner", "http://claudio-agent:8100")]
    for place in places:
        backends.append((place.executor_id, f"Executor {place.index}", place.agent_url))

    labels = [
        f"      clams.ui.backend.count: {_label(len(backends))}",
        "      clams.ui.backend.default: claudio-agent",
    ]
    env = [f"      CLAMS_UI_BACKEND_DEFAULT: http://claudio-agent:{AGENT_CONTAINER_PORT}"]
    env.append("      CLAMS_UI_BACKEND_IDS: " + _quoted(",".join(b[0] for b in backends)))
    env.append("      CLAMS_UI_BACKEND_LABELS: " + _quoted(",".join(b[1] for b in backends)))
    for bid, label, url in backends:
        labels.append(f"      clams.ui.backend.{bid}.label: {_quoted(label)}")
        labels.append(f"      clams.ui.backend.{bid}.url: {url}")
        env.append(f"      CLAMS_UI_BACKEND_{bid.upper().replace('-', '_')}_LABEL: {_quoted(label)}")
        env.append(f"      CLAMS_UI_BACKEND_{bid.upper().replace('-', '_')}_URL: {url}")

    lines = [f"    {_begin('UI backend registry')}", "    labels:"]
    lines += labels
    lines.append("    environment:")
    lines += env
    lines.append(f"    {_end('UI backend registry')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# config -> options
# --------------------------------------------------------------------------
_OPTION_KEYS = (
    "exposure_address",
    "publish_agent_debug_ports",
    "expose_agent_debug_ports",
    "executor_count",
    "planner_host_port",
    "api_host_port",
    "ui_host_port",
    "ui_container_port",
    "clams_image",
    "clams_build",
    "root_env_file",
    "planner_env_file",
    "executor_env_file",
    "planner_config_dir",
    "executor_config_dir",
    "generated_config_dir",
    "gitconfig_mount",
    "planner_image",
    "executor_image",
    "ui_image",
    "redis_image",
    "extra_environment",
)


def _as_deployment(config: Any):
    """Coerce *config* (Deployment | mapping | None) into a ``Deployment``."""
    from .model import Deployment

    if config is None:
        return Deployment.from_dict({})
    if isinstance(config, Deployment):
        return config
    if isinstance(config, Mapping):
        return Deployment.from_dict(dict(config))
    if hasattr(config, "executors") and hasattr(config, "ui"):
        return config
    raise ComposeGenError(
        f"cannot render compose from {type(config).__name__}: "
        "pass a Deployment, a mapping, or None for the default"
    )


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    raise ComposeGenError(f"not a boolean: {value!r}")


def options_from_config(config: Any) -> tuple[ComposeOptions, list[Optional[int]], list[Optional[str]]]:
    """Derive ``(options, executor_debug_ports, executor_ids)`` from *config*.

    Overrides are looked up in order: a top-level key of a mapping config, its
    ``compose`` section, ``deployment.extra``, ``deployment.admin.extra``.
    """
    deployment = _as_deployment(config)

    sources: list[Mapping[str, Any]] = []
    if isinstance(config, Mapping):
        sources.append(dict(config))
        nested = config.get("compose")
        if isinstance(nested, Mapping):
            sources.append(dict(nested))
    extra = getattr(deployment, "extra", None)
    if isinstance(extra, Mapping):
        sources.append(extra)
    admin = getattr(deployment, "admin", None)
    if admin is not None and isinstance(getattr(admin, "extra", None), Mapping):
        sources.append(dict(admin.extra))

    def pick(key: str, default: Any = None) -> Any:
        for source in sources:
            if key in source and source[key] is not None:
                return source[key]
        return default

    clams = deployment.clams
    ui = deployment.ui
    admin_cfg = deployment.admin

    executors = list(getattr(deployment, "executors", []) or [])
    declared = getattr(admin_cfg, "executor_count", None) if admin_cfg else None
    count = max(len(executors), int(declared or 0), 1)
    if pick("executor_count") is not None:
        count = max(1, int(pick("executor_count")))

    ui_image = None
    if getattr(ui, "image", None):
        ui_image = f"${{CLAUDIO_WEB_IMAGE:-{ui.image}}}"

    options = ComposeOptions(
        exposure_address=str(pick("exposure_address", DEFAULT_EXPOSURE_ADDRESS)),
        publish_agent_debug_ports=_bool(
            pick("publish_agent_debug_ports", pick("expose_agent_debug_ports", None)), True
        ),
        executor_count=count,
        planner_host_port=int(getattr(deployment, "planner_port", None) or pick("planner_host_port", PLANNER_HOST_PORT)),
        api_host_port=int(getattr(deployment, "api_port", None) or pick("api_host_port", API_HOST_PORT)),
        ui_host_port=int(getattr(ui, "port", None) or pick("ui_host_port", UI_HOST_PORT)),
        ui_container_port=int(getattr(ui, "container_port", None) or pick("ui_container_port", UI_CONTAINER_PORT)),
        clams_image=str(pick("clams_image", "clams:local")),
        clams_build=str(pick("clams_build", ".")),
        root_env_file=str(getattr(admin_cfg, "env_file", None) or pick("root_env_file", ".env")),
        planner_env_file=str(getattr(admin_cfg, "planner_env_file", None)
                             or pick("planner_env_file", ".env.claudio-planner")),
        executor_env_file=str(getattr(admin_cfg, "executor_env_file", None)
                              or pick("executor_env_file", ".env.claudio-executor")),
        planner_config_dir=str(pick("planner_config_dir", "./config/agents/planner")),
        executor_config_dir=str(pick("executor_config_dir", "./config/agents/executor")),
        generated_config_dir=str(pick("generated_config_dir", "./config/agents/generated")),
        gitconfig_mount=str(pick("gitconfig_mount", "./config/clams-gitconfig:/etc/clams-gitconfig:ro")),
        planner_image=str(pick("planner_image", "${CLAUDIO_IMAGE:-claudio/agent:latest}")),
        executor_image=str(pick("executor_image", "${CLAUDIO_EXECUTOR_IMAGE:-claudio/agent-exec:latest}")),
        ui_image=str(ui_image or pick("ui_image", "${CLAUDIO_WEB_IMAGE:-claudio/web:latest}")),
        redis_image=str(pick("redis_image", "redis:7-alpine")),
        redis_url=str(getattr(clams, "redis_url", None) or pick("redis_url", "redis://redis:6379/0")),
        planner_url=str(getattr(clams, "claudio_planner_url", None)
                        or pick("planner_url", "http://claudio-planner:8100")),
        extra_environment={str(k): str(v) for k, v in (pick("extra_environment") or {}).items()},
    )
    ports = [getattr(e, "debug_port", None) for e in executors]
    ids = [getattr(e, "executor_id", None) for e in executors]
    return options, ports, ids


def default_config() -> Any:
    """The default deployment (the tracked compose topology)."""
    from .adminconfig import default_deployment

    return default_deployment()


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------
def render(config: Any = None, *, options: Optional[ComposeOptions] = None) -> str:
    """The complete effective ``docker-compose.yml`` for *config*.

    ``config`` may be a :class:`clams_admin.model.Deployment`, a plain mapping
    (the ``.clams-admin/config.json`` document), or ``None`` for the default
    single-executor topology. ``options`` overrides whatever the config
    implies. The result always ends with a newline and always contains the
    ``# BEGIN CLAMS-ADMIN GENERATED`` / ``# END CLAMS-ADMIN GENERATED`` markers.
    """
    derived_options, exec_ports, exec_ids = options_from_config(config)
    if options is not None:
        derived_options = options
        if derived_options.executor_count is None:
            derived_options.executor_count = max(1, len(exec_ports))
    places = placements(derived_options, exec_ports, exec_ids)

    opts = derived_options
    pairs = [_generated_pair(opts, place) for place in places if place.index >= 2]
    volumes = ["clams-db: {}", "redis-data: {}", "claudio-sessions: {}", "claudio-planner-data: {}",
               "claudio-executor-data: {}"]
    volumes += [f"{place.data_volume}: {{}}" for place in places if place.index >= 2]
    volumes.append("worktrees: {}")

    anchor_env = _anchor_environment(opts)
    extra_env_lines = [f"    {k}: {_quoted(v)}" for k, v in opts.extra_environment.items()]
    first = places[0]

    values = {
        "CLAMS_IMAGE": opts.clams_image,
        "CLAMS_BUILD": opts.clams_build,
        "ROOT_ENV": opts.root_env_file,
        "REDIS_URL": opts.redis_url,
        "REDIS_IMAGE": opts.redis_image,
        "PLANNER_URL": opts.planner_url,
        "EXTRA_ENV": "\n".join(extra_env_lines) if extra_env_lines else _DROP,
        "PLANNER_IMAGE": opts.planner_image,
        "PLANNER_ENV": opts.planner_env_file,
        "PLANNER_CONFIG": opts.planner_config_dir,
        "PLANNER_PORTS": (f"    ports: [{opts.published(opts.planner_host_port, AGENT_CONTAINER_PORT)}]"
                          if opts.publish_agent_debug_ports else _DROP),
        "EXECUTOR_IMAGE": opts.executor_image,
        "EXEC1_PORTS": (f"    ports: [{opts.published(first.host_port, AGENT_CONTAINER_PORT)}]"
                        if opts.publish_agent_debug_ports else _DROP),
        "EXEC1_ENV": first.env_file,
        "EXEC1_CONFIG": first.config_dir,
        "EXEC1_RUNNER_ENV": _runner_environment(opts, first),
        "UI_IMAGE": opts.ui_image,
        "UI_PORTS": f"    ports: [{opts.published(opts.ui_host_port, opts.ui_container_port)}]",
        "UI_REGISTRY": _ui_registry(opts, places),
        "GITCONFIG_MOUNT": opts.gitconfig_mount,
        "GENERATED_PAIRS": ("\n\n".join(pairs) + "\n") if pairs else "",
        "API_PORTS": f"    ports: [{opts.published(opts.api_host_port, API_CONTAINER_PORT)}]",
        "VOLUMES": "{" + ", ".join(volumes) + "}",
    }
    text = _fill(_TEMPLATE, values)
    # collapse any accidental triple blank lines
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


def render_for_executors(count: int, **overrides: Any) -> str:
    """Convenience: render the default topology with exactly *count* executors."""
    options = ComposeOptions(executor_count=count, **overrides)
    return render(None, options=options)


# --------------------------------------------------------------------------
# managed-region helpers (for ``compose write`` splicing)
# --------------------------------------------------------------------------
def split_managed_region(text: str) -> tuple[str, str, str]:
    """``(before, region, after)`` around the first managed region."""
    lines = text.splitlines()
    start = end = -1
    for index, line in enumerate(lines):
        stripped = line.strip()
        if start < 0 and stripped.startswith(MARKER_BEGIN):
            start = index
        elif start >= 0 and stripped.startswith(MARKER_END):
            end = index
            break
    if start < 0 or end < 0:
        return text, "", ""
    return "\n".join(lines[:start]), "\n".join(lines[start:end + 1]), "\n".join(lines[end + 1:])


def managed_region(text: str) -> str:
    """The managed region of *text* (``""`` when there is none)."""
    return split_managed_region(text)[1]
