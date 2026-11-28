"""Generate per-container Claudio agent configs from the tracked templates.

The seed is always the tracked file - ``config/agents/planner/config.yaml`` or
``config/agents/executor/config.yaml`` - and the generated copy lands under
``<root>/config/agents/generated/{planner,executor-N}/config.yaml`` so nothing
tracked is ever modified.

Why patch text instead of round-tripping YAML
--------------------------------------------
The templates are hand-tuned: they carry the comments that explain *why* a value
is what it is (the ``approval_mode`` note about ``off`` being YAML 1.1 boolean
``False``, the reason ``host_execution_enabled`` stays false, ...). A
``load -> dump`` cycle throws all of that away, and PyYAML would additionally
re-render ``"off"`` as ``'off'`` - a different literal than the one the runtime
needs. So this module edits the template line by line: keys it owns are
replaced in place, keys it does not touch survive verbatim.

Values this module owns
-----------------------
``server.auth_token``                 the agent's bearer token (quoted)
``server.cors_origins``               template origins + the UI origin
``agent.name``                        ``Planner`` / ``Executor-N``
``agent.max_turns``                   from the deployment
``agent.approval_mode``               executors only: the literal ``"off"``
``agent.workspace_allowed_roots``     guaranteed to contain ``/work``
``llm.{provider,base_url,model,api_key,temperature,max_tokens,timeout_seconds,retry_attempts}``

The generated file holds a token and (usually) an LLM api key, so it is written
with mode ``0o600`` by :mod:`clams_admin.writer`.

Stdlib-only: no YAML library is imported, at import time or later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from .writer import WritePlan, plan as new_plan

__all__ = [
    "GENERATED_DIR",
    "TEMPLATE_RELPATHS",
    "WORKSPACE_ROOT",
    "DEFAULT_WORKSPACE_ROOTS",
    "LlmSettings",
    "AgentConfigSpec",
    "generated_relpath",
    "template_relpath",
    "ui_origins",
    "spec_for_planner",
    "spec_for_executor",
    "render",
    "plan",
    "apply",
    "set_scalar",
    "set_list",
    "get_scalar",
]

#: generated configs live here, next to (never inside) the tracked templates.
GENERATED_DIR = "config/agents/generated"
#: the tracked seed for each role.
TEMPLATE_RELPATHS = {
    "planner": "config/agents/planner/config.yaml",
    "executor": "config/agents/executor/config.yaml",
}
#: where the Clams worktrees volume is mounted inside an agent container.
WORKSPACE_ROOT = "/work"
#: roots an agent may edit; ``/work`` is mandatory or a task cannot be worked on.
DEFAULT_WORKSPACE_ROOTS: tuple[str, ...] = ("/workspace", WORKSPACE_ROOT, "/sessions")
#: first line of the banner :func:`render` prepends; used to strip an old one.
BANNER_FIRST_LINE = "# GENERATED FILE - do not edit by hand; your changes are overwritten."

_KEY_RE = re.compile(r"^(?P<indent>[ ]*)(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*):(?P<rest>.*)$")
_YAML_BOOLS = {
    "true", "false", "yes", "no", "on", "off", "y", "n", "null", "~", "none",
}


# --------------------------------------------------------------------------
# line scanning
# --------------------------------------------------------------------------
def _lines(text: str) -> list[str]:
    body = text[:-1] if text.endswith("\n") else text
    return body.split("\n")


def _join(lines: Sequence[str]) -> str:
    return "\n".join(lines) + "\n"


def _entry(line: str) -> Optional[tuple[int, str, str]]:
    """``(indent, key, rest)`` for a ``key: value`` line, else ``None``."""
    if not line.strip() or line.lstrip().startswith("#") or line.lstrip().startswith("- "):
        return None
    match = _KEY_RE.match(line)
    if not match:
        return None
    return len(match.group("indent")), match.group("key"), match.group("rest").strip()


def _split_inline_comment(rest: str) -> tuple[str, str]:
    """Split ``value   # comment`` into its two parts, respecting quotes."""
    quote: Optional[str] = None
    for index, char in enumerate(rest):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            continue
        if char == "#" and index > 0 and rest[index - 1] in " \t":
            return rest[:index].rstrip(), rest[index:]
    return rest.rstrip(), ""


def _find(lines: Sequence[str], key: str, start: int, end: int, indent: int) -> Optional[int]:
    """Index of ``key:`` at exactly ``indent`` inside ``[start, end)``."""
    for index in range(start, end):
        entry = _entry(lines[index])
        if entry and entry[0] == indent and entry[1] == key:
            return index
    return None


def _block(lines: Sequence[str], key_line: int, indent: int) -> tuple[int, int, int]:
    """The value block of ``key_line``: ``(first_child, stop, child_indent)``.

    A sequence written at the *same* indent as its key (``key:`` then
    ``- item``) is part of the block, which is how the planner template lays
    out ``cors_origins``.
    """
    stop = len(lines)
    for index in range(key_line + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            continue
        line_indent = len(lines[index]) - len(lines[index].lstrip(" "))
        if line_indent < indent:
            stop = index
            break
        if line_indent == indent and not stripped.startswith("- "):
            stop = index
            break
    child_indent = indent + 2
    for index in range(key_line + 1, stop):
        if lines[index].strip():
            child_indent = len(lines[index]) - len(lines[index].lstrip(" "))
            break
    return key_line + 1, stop, child_indent


def _locate(text: str, path: Sequence[str]) -> tuple[list[str], Optional[int], int, int, int, int]:
    """Locate ``path`` (section keys + final key) inside ``text``.

    Returns ``(lines, key_line, start, end, indent, child_indent)``. ``key_line``
    is ``None`` when the key is absent, in which case ``start``/``end``/``indent``
    describe the section block it should be appended to.
    """
    lines = _lines(text)
    start, end, indent = 0, len(lines), 0
    for depth, key in enumerate(path):
        found = _find(lines, key, start, end, indent)
        if found is None:
            return lines, None, start, end, indent, indent + 2
        if depth == len(path) - 1:
            return lines, found, start, end, indent, indent + 2
        start, end, child = _block(lines, found, indent)
        indent = child
    return lines, None, start, end, indent, indent + 2  # pragma: no cover


# --------------------------------------------------------------------------
# scalar rendering
# --------------------------------------------------------------------------
def _needs_quotes(text: str) -> bool:
    if text == "" or text != text.strip():
        return True
    if text.lower() in _YAML_BOOLS:
        return True
    if text[0] in "-?:,[]{}#&*!|>'\"%@`":
        return True
    if ": " in text or text.endswith(":") or " #" in text:
        return True
    try:
        float(text)
        return True
    except ValueError:
        pass
    return any(char in text for char in "\n\t\"\\")


def render_scalar(value: object, *, quote: bool = False) -> str:
    """One YAML scalar, double-quoted whenever the bare form could be misread."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if quote or _needs_quotes(text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return text


def _rest_of(text: str, key_line: int) -> str:
    entry = _entry(_lines(text)[key_line])
    return entry[2] if entry else ""


def get_scalar(text: str, path: Sequence[str]) -> Optional[str]:
    """The raw value text at ``path`` (quotes stripped), or ``None`` when absent."""
    lines, key_line, _, _, _, _ = _locate(text, path)
    if key_line is None:
        return None
    value, _ = _split_inline_comment(_rest_of(text, key_line))
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def set_scalar(text: str, path: Sequence[str], value: object, *, quote: bool = False) -> str:
    """Set ``path`` to ``value``, keeping comments and everything else intact."""
    lines, key_line, start, end, indent, _ = _locate(text, path)
    rendered = render_scalar(value, quote=quote)
    key = path[-1]
    if key_line is None:
        insert_at = end
        while insert_at > start and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, f"{' ' * indent}{key}: {rendered}")
        return _join(lines)
    _, comment = _split_inline_comment(_rest_of(text, key_line))
    suffix = f" {comment}" if comment else ""
    lines[key_line] = f"{' ' * indent}{key}: {rendered}{suffix}"
    return _join(lines)


def set_list(text: str, path: Sequence[str], values: Sequence[str]) -> str:
    """Replace (or insert) a block sequence at ``path``."""
    lines, key_line, start, end, indent, child_indent = _locate(text, path)
    key = path[-1]
    if key_line is None:
        insert_at = end
        while insert_at > start and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, f"{' ' * indent}{key}:")
        for offset, value in enumerate(values):
            lines.insert(insert_at + 1 + offset, f"{' ' * (indent + 2)}- {render_scalar(value)}")
        return _join(lines)
    _, stop, child = _block(lines, key_line, indent)
    _, comment = _split_inline_comment(_rest_of(text, key_line))
    header = f"{' ' * indent}{key}:" + (f" {comment}" if comment else "")
    item_indent = child if values else indent + 2
    body = [f"{' ' * item_indent}- {render_scalar(value)}" for value in values]
    lines[key_line:stop] = [f"{header} []"] if not body else [header] + body
    return _join(lines)


# --------------------------------------------------------------------------
# specs
# --------------------------------------------------------------------------
@dataclass
class LlmSettings:
    """The ``llm:`` values this module may override. ``None`` keeps the template."""

    provider: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    timeout_seconds: Optional[int] = None
    retry_attempts: Optional[int] = None

    #: (field name, yaml key) in the order they are written.
    KEYS: tuple[tuple[str, str], ...] = (
        ("provider", "provider"),
        ("base_url", "base_url"),
        ("model", "model"),
        ("api_key", "api_key"),
        ("temperature", "temperature"),
        ("max_tokens", "max_tokens"),
        ("timeout_seconds", "timeout_seconds"),
        ("retry_attempts", "retry_attempts"),
    )


@dataclass
class AgentConfigSpec:
    """Everything that makes one generated agent config differ from its seed."""

    role: str = "planner"
    index: int = 1
    name: str = "Planner"
    auth_token: str = ""
    max_turns: Optional[int] = None
    cors_origins: tuple[str, ...] = ()
    workspace_allowed_roots: tuple[str, ...] = (WORKSPACE_ROOT,)
    approval_mode: Optional[str] = None
    """Executors pin this to ``off``; the planner leaves it as the template has it."""
    llm: LlmSettings = field(default_factory=LlmSettings)

    @property
    def label(self) -> str:
        return self.role if self.role == "planner" else f"{self.role}-{self.index}"


def generated_relpath(role: str, index: int = 1) -> str:
    """``config/agents/generated/planner/config.yaml`` (or ``executor-N``)."""
    folder = "planner" if role == "planner" else f"executor-{index}"
    return f"{GENERATED_DIR}/{folder}/config.yaml"


def template_relpath(role: str) -> str:
    return TEMPLATE_RELPATHS[role]


def ui_origins(deployment=None, *, port: Optional[int] = None, extra: Sequence[str] = ()) -> tuple[str, ...]:
    """Every origin the UI may call an agent from (deduplicated, order kept)."""
    if port is None:
        port = getattr(getattr(deployment, "ui", None), "port", None) or 1076
    origins = [f"http://localhost:{port}", f"http://127.0.0.1:{port}", *extra]
    seen: list[str] = []
    for origin in origins:
        if origin and origin not in seen:
            seen.append(origin)
    return tuple(seen)


def _llm_from_role(deployment, role_settings) -> LlmSettings:
    """LLM overrides implied by one agent role (unset fields keep template values)."""

    def reveal(value: object) -> Optional[str]:
        if value is None:
            return None
        text = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
        return text or None

    return LlmSettings(
        base_url=reveal(role_settings.llm_base_url) or reveal(deployment.clams.model_server_url),
        model=reveal(role_settings.llm_model),
        api_key=reveal(role_settings.llm_api_key),
    )


def spec_for_planner(deployment, tokens, *, port: Optional[int] = None) -> AgentConfigSpec:
    """The planner spec: token from the bundle, origins covering the UI."""
    return AgentConfigSpec(
        role="planner",
        index=1,
        name="Planner",
        auth_token=tokens.planner_token,
        max_turns=deployment.clams.max_turns,
        cors_origins=ui_origins(deployment, port=port),
        workspace_allowed_roots=DEFAULT_WORKSPACE_ROOTS,
        approval_mode=None,
        llm=_llm_from_role(deployment, deployment.planner),
    )


def spec_for_executor(deployment, tokens, index: int) -> AgentConfigSpec:
    """One executor spec: pinned ``approval_mode: "off"`` and a writable ``/work``."""
    position = index - 1
    instance = (
        deployment.executors[position] if 0 <= position < len(deployment.executors) else None
    )
    role_settings = instance.agent if instance is not None else deployment.executor_defaults.agent
    return AgentConfigSpec(
        role="executor",
        index=index,
        name=f"Executor-{index}",
        auth_token=tokens.executor_token,
        max_turns=deployment.executor_defaults.max_turns or deployment.clams.max_turns,
        cors_origins=ui_origins(deployment),
        workspace_allowed_roots=DEFAULT_WORKSPACE_ROOTS,
        approval_mode="off",
        llm=_llm_from_role(deployment, role_settings),
    )


def get_list(text: str, path: Sequence[str]) -> list[str]:
    """The block (or flow) sequence at ``path``; ``[]`` when absent."""
    lines, key_line, _, _, indent, _ = _locate(text, path)
    if key_line is None:
        return []
    rest = _split_inline_comment(_rest_of(text, key_line))[0]
    if rest.startswith("[") and rest.endswith("]"):
        inner = rest[1:-1].strip()
        return [part.strip().strip("\"'") for part in inner.split(",") if part.strip()]
    first, stop, _child = _block(lines, key_line, indent)
    values: list[str] = []
    for index in range(first, stop):
        stripped = lines[index].strip()
        if stripped.startswith("- "):
            values.append(stripped[2:].strip().strip("\"'"))
    return values


def _merge(keep: Sequence[str], seed: Sequence[str]) -> list[str]:
    """``keep`` first (deduplicated), then anything the seed had that is new."""
    out: list[str] = []
    for value in [*keep, *seed]:
        if value and value not in out:
            out.append(value)
    return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def render(
    spec: AgentConfigSpec,
    template_text: str,
    *,
    template_name: str = "",
) -> str:
    """Patch ``template_text`` into the generated config described by ``spec``."""
    text = template_text

    text = set_scalar(text, ("server", "auth_token"), spec.auth_token, quote=True)
    text = set_list(
        text,
        ("server", "cors_origins"),
        _merge(spec.cors_origins, get_list(text, ("server", "cors_origins"))),
    )

    text = set_scalar(text, ("agent", "name"), spec.name)
    if spec.max_turns is not None:
        text = set_scalar(text, ("agent", "max_turns"), int(spec.max_turns))
    text = set_list(
        text,
        ("agent", "workspace_allowed_roots"),
        _merge([*spec.workspace_allowed_roots, WORKSPACE_ROOT],
               get_list(text, ("agent", "workspace_allowed_roots"))),
    )
    if spec.approval_mode is not None:
        # quoted on purpose: bare `off` is YAML 1.1 boolean False, which fails
        # pydantic string validation and blanks the whole config.
        text = set_scalar(text, ("agent", "approval_mode"), spec.approval_mode, quote=True)

    for attribute, key in spec.llm.KEYS:
        value = getattr(spec.llm, attribute)
        if value is not None:
            text = set_scalar(text, ("llm", key), value, quote=(key == "api_key"))

    banner = [
        "# GENERATED FILE - do not edit by hand; your changes are overwritten.",
        f"# Seed template: {template_name or template_relpath(spec.role)}",
        "# Edit the tracked template instead, then re-run `clams-admin` render.",
        "# Contains secrets (server.auth_token, llm.api_key): keep mode 0o600.",
    ]
    # re-rendering a generated file must not stack banners
    if text.startswith(BANNER_FIRST_LINE + "\n"):
        text = "\n".join(text.split("\n")[len(banner):])
    return "".join(f"{line}\n" for line in banner) + text


# --------------------------------------------------------------------------
# plan / apply
# --------------------------------------------------------------------------
def read_template(role: str, *, template_root: Path | str, index: int = 1) -> tuple[str, Path]:
    """The seed text for ``role`` plus the path it came from."""
    path = Path(template_root) / template_relpath(role)
    if not path.is_file():
        from .errors import ConfigFileError

        raise ConfigFileError(
            f"agent config template not found: {path}",
            file=str(path),
            corrective_action=(
                f"restore the tracked template for the {role} role "
                f"({template_relpath(role)}) or pass --template-root"
            ),
        )
    return path.read_text(encoding="utf-8"), path


def specs_for_deployment(deployment, tokens) -> list[AgentConfigSpec]:
    """One spec per agent container: the planner, then executors 1..N."""
    from .render_env import executor_count

    specs = [spec_for_planner(deployment, tokens)]
    for index in range(1, executor_count(deployment) + 1):
        specs.append(spec_for_executor(deployment, tokens, index))
    return specs


def plan(
    deployment,
    root: Path | str,
    *,
    tokens=None,
    template_root: Optional[Path | str] = None,
) -> tuple[WritePlan, object]:
    """Build (but do not run) the generated agent-config write plan."""
    from .render_env import TokenBundle

    root = Path(root)
    bundle = tokens or TokenBundle.for_deployment(deployment)
    seed_root = Path(template_root) if template_root is not None else root
    write_plan = new_plan(root)
    for spec in specs_for_deployment(deployment, bundle):
        template_text, template_path = read_template(
            spec.role, template_root=seed_root, index=spec.index
        )
        write_plan.add(
            generated_relpath(spec.role, spec.index),
            render(spec, template_text, template_name=str(template_path)),
            secret=True,
            label=f"{spec.label} config",
        )
    return write_plan, bundle


def apply(
    deployment,
    root: Path | str,
    *,
    tokens=None,
    template_root: Optional[Path | str] = None,
    overwrite: bool = False,
    backup: bool = True,
    dry_run: bool = False,
    fail_on: Optional[Path | str] = None,
) -> tuple[object, object]:
    """Write every generated agent config; returns ``(WriteReport, tokens)``."""
    from .writer import apply as apply_plan

    write_plan, bundle = plan(deployment, root, tokens=tokens, template_root=template_root)
    report = apply_plan(
        write_plan, overwrite=overwrite, backup=backup, dry_run=dry_run, fail_on=fail_on
    )
    return report, bundle
