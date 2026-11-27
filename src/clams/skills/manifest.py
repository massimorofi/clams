"""Skill manifest: the contract a skill artifact must satisfy before it can be
proposed at all (stdlib only: hashlib, json, dataclasses, re).

A manifest is a *description of limits*, not a place to ask for capabilities.
So validation is deny-by-default on structure: an unknown top-level key is
rejected rather than ignored, because "ignored" is exactly how a manifest would
smuggle in ``auto_approve: true`` or a permissions block.

Rejections always carry an explicit machine-readable ``code`` plus a human
``reason`` naming the offending path, so an operator fixing a manifest is not
guessing which line was refused.

Hard rejections (see ``REJECTION_CODES``):
  permission_grant      the manifest grants/requests privileges or tools outside
                        the declared tool allowlist
  approval_bypass       anything that lets the skill skip or self-serve human
                        approval / activation gates
  scope_weakening       empty, wildcard or "unrestricted" project/repo scope
  evidence_weakening    gates, checks or evidence requirements turned off
  secret_exposure       credential-shaped material anywhere in the document
  external_side_effects authorization to cause side effects outside the repo
                        (network egress, merges, deploys, outbound messages)
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# ------------------------------------------------------------------ vocabulary

SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{2,63}$")
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z]+(?:[.\-][0-9A-Za-z]+)*)?"
    r"(?:\+[0-9A-Za-z]+(?:[.\-][0-9A-Za-z]+)*)?$")

#: the only top-level keys a manifest may contain
MANIFEST_FIELDS = (
    "skill_id", "version", "description", "role", "activation", "scope",
    "tools", "non_goals", "provenance",
)

ROLES = ("planner", "executor", "reviewer", "postmortem", "operator")

#: the closed tool allowlist. A skill may declare a subset of these; anything
#: else (including "*", "all", "shell_root") is a permission grant.
KNOWN_TOOLS = (
    "read_file", "list_dir", "grep", "write_file", "edit_file",
    "run_tests", "run_lint", "git_read",
)

#: sources allowed to author a manifest (mirrors skill_versions.source)
SOURCES = ("operator", "postmortem", "import", "system")

REJECTION_CODES = (
    "missing_field", "bad_skill_id", "bad_version", "bad_role",
    "bad_description", "unknown_field", "bad_activation", "bad_scope",
    "bad_tools", "bad_non_goals", "bad_provenance",
    "permission_grant", "approval_bypass", "scope_weakening",
    "evidence_weakening", "secret_exposure", "external_side_effects",
)

# ------------------------------------------------------------- deny-lists

#: keys anywhere in the document that mean "give this skill privilege"
PERMISSION_KEYS = {
    "permissions", "permission", "grants", "grant", "privileges", "privileged",
    "capabilities", "sudo", "root", "admin", "allow_all", "allowall",
    "unrestricted", "escalate", "impersonate", "service_account",
}

#: keys anywhere that mean "skip the human / skip the gate"
APPROVAL_BYPASS_KEYS = {
    "auto_approve", "autoapprove", "self_approve", "bypass_approval",
    "skip_approval", "no_approval", "auto_activate", "autoactivate",
    "auto_deploy", "autoactivate_on_write", "bypass", "skip_review",
    "require_approval", "operator_override",
}

#: keys that mean "turn a check off"
EVIDENCE_WEAKENING_KEYS = {
    "skip_evidence", "no_evidence", "evidence_required", "skip_gate",
    "skip_checks", "disable_checks", "ignore_failures", "force_pass",
    "max_wall_time_override", "skip_tests", "bypass_verification",
}

#: keys/values that mean "talk to the outside world"
SIDE_EFFECT_KEYS = {
    "side_effects", "sideeffects", "external_effects", "network", "egress",
    "webhook", "webhooks", "http", "httpx", "fetch", "curl", "auto_merge",
    "automerge", "merge_pr", "deploy", "deploy_production", "publish",
    "send_email", "send_message", "post_comment", "create_pr", "create_issue",
}

#: substrings in a *value* that mean the same (e.g. tools: ["git_push"])
SIDE_EFFECT_VALUE_RE = re.compile(
    r"(?i)\b(git_push|force_push|push_to_main|net_fetch|net_post|"
    r"external_api|prod_deploy|release_publish|send_slack|send_email)\b")

#: credential shapes. Deliberately broad: a false positive costs one edit.
SECRET_VALUE_RES = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|credential)"
               r"\w*\s*[:=]\s*['\"]?[^\s'\"]{6,}"),
)

#: key names whose mere presence with a non-empty value is secret exposure
SECRET_KEY_NAMES = {"secret", "token", "password", "passwd", "api_key",
                    "apikey", "access_key", "private_key", "credential",
                    "credentials", "client_secret"}

#: scope values that mean "everything" and are therefore refused
WILDCARD_SCOPE = {"*", "all", "any", "**", "everything", "global", "any_repo"}


# ------------------------------------------------------------------- errors

class ManifestError(ValueError):
    """A manifest was refused. ``code`` is machine-readable, ``reason`` names
    the offending path so the author can fix the right line."""

    def __init__(self, code: str, reason: str):
        if code not in REJECTION_CODES:  # pragma: no cover - programmer error
            raise AssertionError(f"unknown rejection code: {code}")
        super().__init__(f"[{code}] {reason}")
        self.code = code
        self.reason = reason


# --------------------------------------------------------------- canonical form

def canonical_json(obj: Any) -> str:
    """Stable serialization used for hashing: sorted keys, no whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def content_hash(payload: Any) -> str:
    """sha256 of the canonical JSON of ``payload`` (the artifact's identity)."""
    return "sha256:" + hashlib.sha256(
        canonical_json(payload).encode("utf-8")).hexdigest()


def _walk(obj: Any, path: str = "$") -> Iterable[tuple[str, str, Any]]:
    """Yield (path, key, value) for every mapping entry, recursively."""
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            key = str(k)
            yield path, key, v
            yield from _walk(v, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


# ----------------------------------------------------------------- manifest

@dataclass(frozen=True)
class SkillManifest:
    """A validated manifest. Construct only via :func:`parse_manifest`."""

    skill_id: str
    version: str
    description: str
    role: str
    activation: dict
    scope: dict
    tools: tuple[str, ...]
    non_goals: tuple[str, ...]
    provenance: dict
    source: str = "operator"
    raw: dict = None  # type: ignore[assignment]

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "description": self.description,
            "role": self.role,
            "activation": dict(self.activation),
            "scope": {"projects": list(self.scope.get("projects", [])),
                      "repos": list(self.scope.get("repos", []))},
            "tools": list(self.tools),
            "non_goals": list(self.non_goals),
            "provenance": dict(self.provenance),
        }

    def content_hash(self) -> str:
        """Hash of the manifest document itself (identity of the *contract*)."""
        return content_hash(self.to_dict())

    def permitted_repos(self) -> tuple[str, ...]:
        return tuple(self.scope.get("repos", ()))

    def permitted_projects(self) -> tuple[str, ...]:
        return tuple(self.scope.get("projects", ()))

    def allows_repo(self, repo: str) -> bool:
        """Scope check is exact-match only: no globs, no prefixes. A skill that
        wants another repo needs a new manifest, not a wildcard."""
        return repo in self.permitted_repos()


def _require_str(doc: Mapping, field: str, code: str, *, maxlen: int = 500,
                 path: str = "$") -> str:
    if field not in doc or doc[field] is None:
        raise ManifestError("missing_field", f"{path}.{field} is required")
    val = doc[field]
    if not isinstance(val, str) or not val.strip():
        raise ManifestError(code, f"{path}.{field} must be a non-empty string")
    if len(val) > maxlen:
        raise ManifestError(code, f"{path}.{field} longer than {maxlen} chars")
    return val.strip()


def _scan_hazards(doc: Mapping) -> None:
    """Walk the whole document and refuse privilege/bypass/weakening/secret/
    side-effect material anywhere in it, including nested dicts the schema does
    not know about."""
    for path, key, value in _walk(doc):
        nk = _norm_key(key)

        if nk in {_norm_key(k) for k in PERMISSION_KEYS}:
            raise ManifestError(
                "permission_grant",
                f"{path}.{key}: manifests may not grant privileges; declare a "
                f"subset of tools={list(KNOWN_TOOLS)} instead")

        if nk in {_norm_key(k) for k in APPROVAL_BYPASS_KEYS}:
            raise ManifestError(
                "approval_bypass",
                f"{path}.{key}: activation may never be automated or bypassed; "
                f"activation.requires_human_approval must stay true")

        if nk in {_norm_key(k) for k in EVIDENCE_WEAKENING_KEYS}:
            raise ManifestError(
                "evidence_weakening",
                f"{path}.{key}: gates and evidence checks may not be disabled "
                f"by a manifest")

        if nk in SECRET_KEY_NAMES and value not in (None, "", [], {}):
            raise ManifestError(
                "secret_exposure",
                f"{path}.{key}: credential-shaped material in a manifest; "
                f"reference the secret by name, never by value")

        if nk in {_norm_key(k) for k in SIDE_EFFECT_KEYS}:
            raise ManifestError(
                "external_side_effects",
                f"{path}.{key}: a skill may not be authorized to cause side "
                f"effects outside the declared repo scope")

        if isinstance(value, str):
            for rx in SECRET_VALUE_RES:
                if rx.search(value):
                    raise ManifestError(
                        "secret_exposure",
                        f"{path}.{key}: value matches a credential pattern")
            if SIDE_EFFECT_VALUE_RE.search(value):
                raise ManifestError(
                    "external_side_effects",
                    f"{path}.{key}: value '{value}' names an external side "
                    f"effect")
        if value is True and nk in {"unrestricted", "all", "everything"}:
            raise ManifestError(
                "scope_weakening", f"{path}.{key}: unrestricted scope refused")


def _validate_activation(doc: Mapping) -> dict:
    act = doc.get("activation")
    if not isinstance(act, Mapping):
        raise ManifestError("bad_activation",
                            "$.activation must be an object of criteria")
    if act.get("requires_human_approval") is not True:
        raise ManifestError(
            "approval_bypass",
            "$.activation.requires_human_approval must be exactly true: no "
            "skill activates without a named human approval row")
    crit = act.get("criteria")
    if not isinstance(crit, (list, tuple)) or not crit:
        raise ManifestError(
            "bad_activation",
            "$.activation.criteria must be a non-empty list of observable "
            "conditions that justify activation")
    for i, c in enumerate(crit):
        if not isinstance(c, str) or not c.strip():
            raise ManifestError("bad_activation",
                                f"$.activation.criteria[{i}] must be non-empty")
    rate = act.get("min_pass_rate", 1.0)
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) \
            or not (0 < float(rate) <= 1):
        raise ManifestError("bad_activation",
                            "$.activation.min_pass_rate must be in (0, 1]")
    if not str(act.get("eval_set_version", "")).strip():
        raise ManifestError(
            "bad_activation",
            "$.activation.eval_set_version is required: activation must name "
            "the evaluation set it was measured against")
    return {"requires_human_approval": True,
            "criteria": [str(c).strip() for c in crit],
            "min_pass_rate": float(rate),
            "eval_set_version": str(act["eval_set_version"]).strip()}


def _validate_scope(doc: Mapping) -> dict:
    scope = doc.get("scope")
    if not isinstance(scope, Mapping):
        raise ManifestError("bad_scope",
                            "$.scope must be an object listing permitted "
                            "projects and repos")
    out: dict[str, list[str]] = {}
    for field in ("projects", "repos"):
        vals = scope.get(field, [])
        if not isinstance(vals, (list, tuple)):
            raise ManifestError("bad_scope", f"$.scope.{field} must be a list")
        cleaned = []
        for i, v in enumerate(vals):
            s = str(v).strip()
            if s.lower() in WILDCARD_SCOPE:
                raise ManifestError(
                    "scope_weakening",
                    f"$.scope.{field}[{i}]='{v}': wildcards are refused; a "
                    f"skill must name the exact projects/repos it may touch")
            if not s:
                raise ManifestError("bad_scope",
                                    f"$.scope.{field}[{i}] is empty")
            cleaned.append(s)
        out[field] = cleaned
    if not out["projects"] and not out["repos"]:
        raise ManifestError(
            "scope_weakening",
            "$.scope must name at least one project or repo; an empty scope "
            "means 'everything' and is refused")
    return out


def _validate_tools(doc: Mapping) -> tuple[str, ...]:
    tools = doc.get("tools")
    if not isinstance(tools, (list, tuple)):
        raise ManifestError("bad_tools", "$.tools must be a list of tool names")
    out = []
    for i, t in enumerate(tools):
        s = str(t).strip()
        if s.lower() in WILDCARD_SCOPE:
            raise ManifestError(
                "permission_grant",
                f"$.tools[{i}]='{t}': a skill may not declare every tool")
        if s not in KNOWN_TOOLS:
            raise ManifestError(
                "permission_grant",
                f"$.tools[{i}]='{s}' is not in the allowlist {list(KNOWN_TOOLS)}")
        if s not in out:
            out.append(s)
    return tuple(out)


def parse_manifest(doc: Any, *, source: str = "operator") -> SkillManifest:
    """Validate ``doc`` and return a frozen :class:`SkillManifest`.

    Raises :class:`ManifestError` (with an explicit code + reason) on the first
    problem. Order matters: hazards are scanned *before* field validation so a
    manifest that both smuggles a permissions block and has a typo cannot be
    reported as merely malformed.
    """
    if isinstance(doc, SkillManifest):
        return doc
    if not isinstance(doc, Mapping):
        raise ManifestError("missing_field", "manifest must be a JSON object")

    _scan_hazards(doc)

    unknown = [k for k in doc if k not in MANIFEST_FIELDS]
    if unknown:
        raise ManifestError(
            "unknown_field",
            f"unknown manifest keys {sorted(unknown)}; allowed={list(MANIFEST_FIELDS)}")

    skill_id = _require_str(doc, "skill_id", "bad_skill_id", maxlen=64)
    if not SKILL_ID_RE.match(skill_id):
        raise ManifestError(
            "bad_skill_id",
            f"skill_id '{skill_id}' must match {SKILL_ID_RE.pattern}: a stable "
            f"lower-case id that is never reused across different skills")

    version = _require_str(doc, "version", "bad_version", maxlen=40)
    if not SEMVER_RE.match(version):
        raise ManifestError(
            "bad_version",
            f"version '{version}' is not semver MAJOR.MINOR.PATCH (optional "
            f"-prerelease/+build); a content change requires a new version")

    description = _require_str(doc, "description", "bad_description")

    role = _require_str(doc, "role", "bad_role", maxlen=32)
    if role not in ROLES:
        raise ManifestError("bad_role", f"role '{role}' not in {list(ROLES)}")

    non_goals = doc.get("non_goals")
    if not isinstance(non_goals, (list, tuple)) or not non_goals:
        raise ManifestError(
            "bad_non_goals",
            "$.non_goals must be a non-empty list: a skill with no stated "
            "boundaries will drift into whatever the prompt happens to ask")
    ng = []
    for i, g in enumerate(non_goals):
        if not isinstance(g, str) or not g.strip():
            raise ManifestError("bad_non_goals",
                                f"$.non_goals[{i}] must be a non-empty string")
        ng.append(g.strip())

    prov = doc.get("provenance")
    if not isinstance(prov, Mapping):
        raise ManifestError(
            "bad_provenance",
            "$.provenance must record where the artifact came from")
    for req in ("authored_by", "origin"):
        if not str(prov.get(req, "")).strip():
            raise ManifestError(
                "bad_provenance",
                f"$.provenance.{req} is required for attribution in "
                f"post-mortems")
    provenance = {str(k): v for k, v in prov.items()}
    provenance.setdefault("manifest_source", source)

    return SkillManifest(
        skill_id=skill_id,
        version=version,
        description=description,
        role=role,
        activation=_validate_activation(doc),
        scope=_validate_scope(doc),
        tools=_validate_tools(doc),
        non_goals=tuple(ng),
        provenance=provenance,
        source=source,
        raw=dict(doc),
    )


def validate_manifest(doc: Any, *, source: str = "operator") -> dict:
    """Non-raising wrapper: ``{"ok": bool, "code": str, "reason": str,
    "manifest": SkillManifest|None}``. Used by the CLI to print one clear line
    instead of a traceback."""
    try:
        m = parse_manifest(doc, source=source)
    except ManifestError as exc:
        return {"ok": False, "code": exc.code, "reason": exc.reason,
                "manifest": None}
    return {"ok": True, "code": "", "reason": "", "manifest": m}
