"""Render the env files of a deployment: ``.env`` plus one per agent container.

Three documents come out of this module:

``.env``
    every ``CLAMS_*`` setting of :class:`~clams_admin.model.ClamsSettings`,
    plus the generated ``CLAMS_API_TOKEN`` and the two agent bearer tokens
    ``CLAMS_CLAUDIO_PLANNER_TOKEN`` / ``CLAMS_CLAUDIO_EXECUTOR_TOKEN``.
``.env.claudio-planner``
    the planner container's own environment: ``CLAUDIO__SERVER__AUTH_TOKEN``
    (equal to ``CLAMS_CLAUDIO_PLANNER_TOKEN``) plus the role values
    (approval mode, allowed workspace roots, LLM overrides, git identity).
``.env.claudio-executor`` / ``.env.claudio-executor-N``
    the same shape per executor, with ``CLAUDIO__SERVER__AUTH_TOKEN`` equal to
    ``CLAMS_CLAUDIO_EXECUTOR_TOKEN``. Instance 1 keeps the unsuffixed name
    because that is what ``compose_gen`` mounts (see
    :meth:`~clams_admin.compose_gen.ComposeOptions.env_file_for`).

The tokens are generated **once** per run by :class:`TokenBundle` and then
written to both sides of each pair, so the value Clams presents as a bearer
token is by construction the value the agent listens for. An existing token in
the deployment is reused rather than rotated, which makes re-rendering
idempotent.

Every file this module produces carries a secret, so it is always written with
mode ``0o600`` by :mod:`clams_admin.writer`.

Stdlib-only.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from .envfile import EnvFile, EnvLine
from .model import AgentRoleSettings, Deployment
from .secrets import generate_secret, register_secret
from .writer import WritePlan, plan as new_plan

__all__ = [
    "TokenBundle",
    "ROOT_ENV_NAME",
    "PLANNER_ENV_NAME",
    "EXECUTOR_ENV_NAME",
    "DEFAULT_WORKSPACE_ROOTS",
    "root_env_name",
    "planner_env_name",
    "executor_env_name",
    "render_root_env",
    "render_planner_env",
    "render_executor_env",
    "plan",
    "apply",
]

ROOT_ENV_NAME = ".env"
PLANNER_ENV_NAME = ".env.claudio-planner"
EXECUTOR_ENV_NAME = ".env.claudio-executor"

#: the roots an agent may touch inside its container; ``/work`` is where the
#: Clams worktrees volume is mounted, so an agent that cannot see it cannot
#: edit a task.
DEFAULT_WORKSPACE_ROOTS: tuple[str, ...] = ("/workspace", "/work", "/sessions")


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------
@dataclass
class TokenBundle:
    """The three bearer tokens a deployment needs, generated exactly once."""

    api_token: str
    planner_token: str
    executor_token: str

    @classmethod
    def generate(cls, *, nbytes: int = 32) -> "TokenBundle":
        return cls(
            api_token=generate_secret(nbytes),
            planner_token=generate_secret(nbytes),
            executor_token=generate_secret(nbytes),
        )

    @classmethod
    def for_deployment(cls, deployment: Deployment, *, nbytes: int = 32) -> "TokenBundle":
        """Existing tokens are reused; missing ones are generated.

        All three are registered with :mod:`clams_admin.secrets` so that any
        summary, diff or traceback printed later is masked automatically.
        """
        clams = deployment.clams

        def existing(secret: object) -> Optional[str]:
            if secret is None:
                return None
            value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
            return value or None

        bundle = cls(
            api_token=existing(clams.api_token) or generate_secret(nbytes),
            planner_token=existing(clams.claudio_planner_token) or generate_secret(nbytes),
            executor_token=existing(clams.claudio_executor_token) or generate_secret(nbytes),
        )
        for value in bundle.as_dict().values():
            register_secret(value)
        return bundle

    def as_dict(self) -> dict[str, str]:
        """``env key -> plaintext`` for the three token variables."""
        return {
            "CLAMS_API_TOKEN": self.api_token,
            "CLAMS_CLAUDIO_PLANNER_TOKEN": self.planner_token,
            "CLAMS_CLAUDIO_EXECUTOR_TOKEN": self.executor_token,
        }

    def redacted(self) -> dict[str, str]:
        from .secrets import MASK

        return {key: MASK for key in self.as_dict()}


# --------------------------------------------------------------------------
# file naming (mirrors compose_gen so the files match what compose mounts)
# --------------------------------------------------------------------------
def root_env_name(deployment: Optional[Deployment] = None) -> str:
    if deployment is not None and deployment.admin.env_file:
        return Path(deployment.admin.env_file).name
    return ROOT_ENV_NAME


def planner_env_name(deployment: Optional[Deployment] = None) -> str:
    if deployment is not None and deployment.admin.planner_env_file:
        return Path(deployment.admin.planner_env_file).name
    return PLANNER_ENV_NAME


def executor_env_name(index: int, deployment: Optional[Deployment] = None) -> str:
    """Instance 1 -> ``.env.claudio-executor``, instances 2..N -> ``-N`` suffix."""
    base = EXECUTOR_ENV_NAME
    if deployment is not None and deployment.admin.executor_env_file:
        base = Path(deployment.admin.executor_env_file).name
    return base if index <= 1 else f"{base}-{index}"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _with_token(
    role: AgentRoleSettings,
    *,
    approval_mode: Optional[str],
    workspace_roots: Sequence[str],
) -> AgentRoleSettings:
    """Copy of ``role`` with the bearer token and the role values forced."""
    roots = list(role.workspace_allowed_roots.split(",")) if role.workspace_allowed_roots else []
    merged = [r.strip() for r in roots if r.strip()]
    for root in workspace_roots:
        if root not in merged:
            merged.append(root)
    return dataclasses.replace(
        role,
        auth_token=None,
        approval_mode=role.approval_mode or approval_mode or "off",
        workspace_allowed_roots=",".join(merged),
    )


def _render_env(
    mapping: Mapping[str, object],
    *,
    base: Optional[str] = None,
    header: Sequence[str] = (),
) -> str:
    """Serialise ``mapping`` as a ``.env`` document.

    ``base`` (the current file text) is updated in place so operator comments,
    section headers and key order survive a re-render; without it a fresh,
    commented document is produced.
    """
    if base is not None:
        doc = EnvFile.parse(base)
    else:
        doc = EnvFile(lines=[EnvLine(kind="comment", raw=text) for text in header])
    doc.update(dict(mapping))
    return doc.text()


# --------------------------------------------------------------------------
# renderers
# --------------------------------------------------------------------------
def root_env_mapping(deployment: Deployment, tokens: TokenBundle) -> dict[str, str]:
    """The ``CLAMS_*`` body of ``.env``: every setting plus the token trio."""
    body = deployment.clams.to_env()
    body.update(tokens.as_dict())
    if deployment.clams.role is None:
        body.setdefault("CLAMS_ROLE", "planner")
    return body


def planner_env_mapping(deployment: Deployment, tokens: TokenBundle) -> dict[str, str]:
    """The planner container's env (``CLAUDIO__*`` plus git identity)."""
    role = _with_token(
        deployment.planner,
        approval_mode="off",
        workspace_roots=DEFAULT_WORKSPACE_ROOTS,
    )
    body = role.to_env()
    body["CLAUDIO__SERVER__AUTH_TOKEN"] = tokens.planner_token
    return body


def executor_env_mapping(
    deployment: Deployment, tokens: TokenBundle, index: int
) -> dict[str, str]:
    """One executor container's env: defaults overlaid with that instance's own."""
    position = index - 1
    base = deployment.executor_defaults.agent
    if 0 <= position < len(deployment.executors):
        instance = deployment.executors[position]
        base = dataclasses.replace(
            base,
            **{
                f.name: getattr(instance.agent, f.name)
                for f in dataclasses.fields(instance.agent)
                if f.name not in {"role", "extra"}
                and getattr(instance.agent, f.name) is not None
            },
            extra={**base.extra, **instance.agent.extra},
        )
    role = _with_token(
        base,
        approval_mode=deployment.executor_defaults.approval_mode or "off",
        workspace_roots=DEFAULT_WORKSPACE_ROOTS,
    )
    body = role.to_env()
    body["CLAUDIO__SERVER__AUTH_TOKEN"] = tokens.executor_token
    body["CLAUDIO__AGENT__APPROVAL_MODE"] = role.approval_mode or "off"
    return body


def render_root_env(
    deployment: Deployment,
    tokens: TokenBundle,
    *,
    base: Optional[str] = None,
) -> str:
    """Full text of ``.env``."""
    header = [
        f"# CLAMS runtime environment for the "
        f"\"{deployment.name or 'clams'}\" deployment.",
        "# Generated by clams-admin (render_env). Contains secrets: mode 0o600.",
        "# The three token keys below are mirrored into the .env.claudio-* files.",
    ]
    return _render_env(root_env_mapping(deployment, tokens), base=base, header=header)


def render_planner_env(
    deployment: Deployment,
    tokens: TokenBundle,
    *,
    base: Optional[str] = None,
) -> str:
    """Full text of ``.env.claudio-planner``."""
    header = [
        "# claudio-planner container environment.",
        "# Generated by clams-admin (render_env). Contains secrets: mode 0o600.",
        "# CLAUDIO__SERVER__AUTH_TOKEN == CLAMS_CLAUDIO_PLANNER_TOKEN in .env.",
    ]
    return _render_env(planner_env_mapping(deployment, tokens), base=base, header=header)


def render_executor_env(
    deployment: Deployment,
    tokens: TokenBundle,
    index: int,
    *,
    base: Optional[str] = None,
) -> str:
    """Full text of ``.env.claudio-executor`` (index 1) or ``.env.claudio-executor-N``."""
    header = [
        f"# {executor_env_name(index)} - claudio-executor-{index} container environment.",
        "# Generated by clams-admin (render_env). Contains secrets: mode 0o600.",
        "# CLAUDIO__SERVER__AUTH_TOKEN == CLAMS_CLAUDIO_EXECUTOR_TOKEN in .env.",
        "# approval_mode is pinned to off: nobody is there to approve a gate.",
    ]
    return _render_env(
        executor_env_mapping(deployment, tokens, index), base=base, header=header
    )


# --------------------------------------------------------------------------
# plan / apply
# --------------------------------------------------------------------------
def executor_count(deployment: Deployment) -> int:
    """How many executor env files to write (``admin.executor_count`` wins)."""
    count = deployment.admin.executor_count or len(deployment.executors) or 1
    return max(1, int(count))


def plan(
    deployment: Deployment,
    root: Path | str,
    *,
    tokens: Optional[TokenBundle] = None,
    existing: Optional[Mapping[str, str]] = None,
) -> tuple[WritePlan, TokenBundle]:
    """Build (but do not run) the env write plan for ``deployment``.

    ``existing`` optionally maps file name -> current text, so a re-render keeps
    the operator's comments. Every item is marked ``secret=True`` and therefore
    lands with mode ``0o600``.
    """
    root = Path(root)
    bundle = tokens or TokenBundle.for_deployment(deployment)
    seen = existing or {}
    write_plan = new_plan(root)
    write_plan.add(
        root_env_name(deployment),
        render_root_env(deployment, bundle, base=seen.get(root_env_name(deployment))),
        secret=True,
        label="root env",
    )
    write_plan.add(
        planner_env_name(deployment),
        render_planner_env(deployment, bundle, base=seen.get(planner_env_name(deployment))),
        secret=True,
        label="planner env",
    )
    for index in range(1, executor_count(deployment) + 1):
        name = executor_env_name(index, deployment)
        write_plan.add(
            name,
            render_executor_env(deployment, bundle, index, base=seen.get(name)),
            secret=True,
            label=f"executor-{index} env",
        )
    return write_plan, bundle


def apply(
    deployment: Deployment,
    root: Path | str,
    *,
    tokens: Optional[TokenBundle] = None,
    overwrite: bool = False,
    backup: bool = True,
    dry_run: bool = False,
    fail_on: Optional[Path | str] = None,
) -> tuple[object, TokenBundle]:
    """Write every env file for ``deployment``; returns ``(WriteReport, tokens)``."""
    write_plan, bundle = plan(deployment, root, tokens=tokens)
    from .writer import apply as apply_plan

    report = apply_plan(
        write_plan, overwrite=overwrite, backup=backup, dry_run=dry_run, fail_on=fail_on
    )
    return report, bundle
