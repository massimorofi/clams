"""Load and save the **non-secret** local config document.

Location: ``<root>/.clams-admin/config.json``.

The document is the admin tool's own memory of a deployment: which env files it
edits, how many executors to render, the host ports, the per-section settings.
Secrets are *not* stored here — they live in ``.env`` / ``.env.claudio-*`` and
are masked by :mod:`clams_admin.secrets` wherever a summary is printed.

Guarantees:

* **unknown-but-supported keys survive** a ``load -> save -> load`` cycle: any
  key the model does not know is kept verbatim in the matching ``extra``
  mapping and re-emitted on save.
* **unset vs explicitly empty survives**: an unset setting is absent from the
  JSON, an explicitly empty one is ``""``.
* writes are atomic (temp file + ``os.replace``) and the previous document is
  backed up next to it as ``config.json.bak``.

Stdlib-only.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Mapping, Optional

from .errors import ConfigFileError
from .model import Deployment

__all__ = [
    "CONFIG_DIRNAME",
    "CONFIG_FILENAME",
    "BACKUP_SUFFIX",
    "config_dir",
    "config_path",
    "exists",
    "load_document",
    "save_document",
    "load_deployment",
    "save_deployment",
    "load_admin_config",
    "save_admin_config",
    "default_deployment",
]

CONFIG_DIRNAME = ".clams-admin"
CONFIG_FILENAME = "config.json"
BACKUP_SUFFIX = ".bak"


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------
def config_dir(root: Path | str) -> Path:
    """``<root>/.clams-admin``."""
    return Path(root) / CONFIG_DIRNAME


def config_path(root: Path | str) -> Path:
    """``<root>/.clams-admin/config.json`` (the document this module owns)."""
    root = Path(root)
    if Path(root).name == CONFIG_FILENAME:
        return Path(root)
    return config_dir(root) / CONFIG_FILENAME


def exists(root: Path | str) -> bool:
    return config_path(root).is_file()


# --------------------------------------------------------------------------
# raw document
# --------------------------------------------------------------------------
def load_document(root: Path | str) -> dict[str, Any]:
    """Read ``config.json`` as a plain dict (``{}`` when it does not exist)."""
    path = config_path(root)
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigFileError(
            f"cannot read {path}: {exc}",
            file=str(path),
            corrective_action="check the file exists and is readable by this user",
        ) from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigFileError(
            f"{path} is not valid JSON: {exc}",
            file=str(path),
            corrective_action=f"fix the JSON syntax in {path} (or delete it to start from defaults)",
        ) from exc
    if not isinstance(data, dict):
        raise ConfigFileError(
            f"{path} must contain a JSON object at the top level",
            file=str(path),
            corrective_action=f"wrap the contents of {path} in {{...}}",
        )
    return data


def save_document(root: Path | str, data: Mapping[str, Any], *, backup: bool = True) -> Path:
    """Atomically write ``data`` as pretty JSON. Returns the written path."""
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    if backup and path.is_file():
        shutil.copy2(path, path.with_suffix(path.suffix + BACKUP_SUFFIX))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------
# typed document
# --------------------------------------------------------------------------
def load_deployment(root: Path | str) -> Deployment:
    """Load the deployment described by ``config.json``.

    Missing file -> a fresh default deployment (nothing is invented: the
    defaults are the values already documented in ``.env.example`` /
    ``docker-compose.yml``).
    """
    return Deployment.from_dict(load_document(root))


def save_deployment(root: Path | str, deployment: Deployment, *, backup: bool = True) -> Path:
    """Save the deployment. Secrets are never written (non-secret document)."""
    return save_document(root, deployment.to_dict(include_secrets=False), backup=backup)


def load_admin_config(root: Path | str):
    """Just the ``admin`` section of the document."""
    return load_deployment(root).admin


def save_admin_config(root: Path | str, admin, *, backup: bool = True) -> Path:
    """Replace the ``admin`` section, leaving the rest of the document intact."""
    deployment = load_deployment(root)
    deployment.admin = admin
    return save_deployment(root, deployment, backup=backup)


def default_deployment(root: Optional[Path | str] = None) -> Deployment:
    """A starting point that mirrors the documented compose topology.

    Ports are the ones ``docker-compose.yml`` publishes: API 1075, planner
    agent 1072, executor agent 1073, UI 1076.
    """
    from .model import (
        AdminConfig,
        AgentRoleSettings,
        ClamsSettings,
        ClaudioUI,
        ExecutorDefaults,
        ExecutorInstance,
    )

    deployment = Deployment(
        name="clams",
        api_port=1075,
        planner_port=1072,
        clams=ClamsSettings(
            redis_url="redis://redis:6379/0",
            api_host="0.0.0.0",
            api_port=8200,
            claudio_planner_url="http://claudio-planner:8100",
            claudio_executor_url="http://claudio-executor:8100",
            github_api_base="https://api.github.com",
            github_graphql_base="https://api.github.com/graphql",
            role="planner",
        ),
        planner=AgentRoleSettings(role="planner", approval_mode="off"),
        executor_defaults=ExecutorDefaults(approval_mode="off", executor_capacity=1),
        executors=[ExecutorInstance(executor_id="executor-1", debug_port=1073, capacity=1)],
        ui=ClaudioUI(port=1076, container_port=8080, image="claudio/web:latest"),
        admin=AdminConfig(name="clams", executor_count=1, root=str(root) if root else None),
    )
    return deployment
