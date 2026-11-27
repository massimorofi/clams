"""GitHub webhook intake primitives.

Stdlib only (``hmac``, ``hashlib``, ``json``, ``dataclasses``) so the
security-critical path can be unit-tested without FastAPI, the DB or the
network. ``re`` is deliberately avoided: the redaction rules are plain string
operations.

Design rules (each one covered by tests/unit/test_gh_webhooks.py):

1. **Authentication is the HMAC-SHA256 signature.** GitHub cannot present a
   bearer token, so ``X-Hub-Signature-256`` *is* the authenticator.
   :func:`verify_webhook_signature` uses ``hmac.compare_digest`` and does the
   same amount of work on every failure path (missing header, wrong scheme,
   non-hex, wrong length, wrong digest): one HMAC over the body plus one
   constant-time comparison against a same-length buffer. No early return
   leaks whether the header was well formed.
2. **The raw payload is never persisted.** :func:`normalize_event` projects a
   delivery onto a small, bounded, redacted record.
3. **Explicit allowlist of (event, action) pairs.** Anything else raises
   :class:`UnsupportedEventError` so the caller answers 4xx and enqueues
   nothing.
4. **Bounded everything.** Every string that reaches the record is
   length-bounded and redacted; free-text bodies are dropped outright.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "SUPPORTED_ACTIONS", "DEFAULT_MAX_BODY_BYTES", "MAX_SUMMARY_CHARS",
    "WebhookError", "SignatureMissingError", "SignatureInvalidError",
    "BodyTooLargeError", "PayloadInvalidError", "UnsupportedEventError",
    "RepoNotAllowedError", "WebhookNotConfiguredError", "NormalizedEvent",
    "compute_signature", "verify_webhook_signature", "enforce_size_limit",
    "byte_size", "parse_payload", "infer_event", "normalize_event",
    "redact", "repo_is_allowed", "process_webhook_body",
]

# --- bounds -----------------------------------------------------------------
SIGNATURE_SCHEME = "sha256="
_SHA256_HEX_LEN = 64
#: GitHub deliveries are chunky (check_suite payloads carry every check run);
#: 1 MiB is generous and still a hard ceiling on what we will buffer.
DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024
MAX_SUMMARY_CHARS = 240
MAX_REF_CHARS = 255
MAX_SHA_CHARS = 64
MAX_ID_CHARS = 64
MAX_REPO_CHARS = 200
MAX_DELIVERY_ID_CHARS = 64

#: The only (event, action) pairs this deployment reacts to.
SUPPORTED_ACTIONS: Mapping[str, frozenset] = {
    "pull_request": frozenset(
        {"opened", "reopened", "synchronize", "ready_for_review", "closed"}),
    "pull_request_review": frozenset({"submitted"}),
    "check_suite": frozenset({"completed"}),
    "check_run": frozenset({"completed"}),
}

_REPO_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_HEX_CHARS = set("0123456789abcdefABCDEF")


# --- errors -----------------------------------------------------------------
class WebhookError(Exception):
    """Rejected delivery. ``status_code`` is what the endpoint answers."""

    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def as_dict(self) -> dict:
        return {"error": type(self).__name__, "detail": self.message}


class WebhookNotConfiguredError(WebhookError):
    """No webhook secret: fail closed rather than accept unsigned deliveries."""
    status_code = 503


class SignatureMissingError(WebhookError):
    status_code = 401


class SignatureInvalidError(WebhookError):
    status_code = 401


class BodyTooLargeError(WebhookError):
    status_code = 413


class PayloadInvalidError(WebhookError):
    status_code = 400


class UnsupportedEventError(WebhookError):
    status_code = 422


class RepoNotAllowedError(WebhookError):
    status_code = 403


# --- signature --------------------------------------------------------------
def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError(f"expected str/bytes, got {type(value).__name__}")


def _digest(secret: Any, raw_body: Any) -> bytes:
    return hmac.new(_as_bytes(secret), _as_bytes(raw_body),
                    hashlib.sha256).digest()


def compute_signature(secret: Any, raw_body: Any) -> str:
    """The value GitHub sends in ``X-Hub-Signature-256`` (also a test helper)."""
    return SIGNATURE_SCHEME + hmac.new(
        _as_bytes(secret), _as_bytes(raw_body), hashlib.sha256).hexdigest()


def _presented_digest(signature_header: Any) -> Optional[bytes]:
    """Parse the header into a raw digest, or ``None`` if it is unusable.

    Never raises: a malformed header is a *verification failure*, not a parse
    error, so the caller keeps the same work profile on every path.
    """
    if isinstance(signature_header, (bytes, bytearray, memoryview)):
        signature_header = _as_bytes(signature_header).decode("latin-1")
    if not isinstance(signature_header, str):
        return None
    scheme, sep, hexpart = signature_header.strip().partition("=")
    if not sep or scheme.strip().lower() != "sha256":
        return None
    hexpart = hexpart.strip()
    if len(hexpart) != _SHA256_HEX_LEN:
        return None
    if not all(c in _HEX_CHARS for c in hexpart):
        return None
    try:
        return bytes.fromhex(hexpart)
    except ValueError:
        return None


def verify_webhook_signature(secret: Any, raw_body: Any,
                             signature_header: Any) -> bool:
    """Constant-time HMAC-SHA256 verification of a webhook delivery.

    ``True`` only when ``signature_header`` is a well formed ``sha256=<hex>``
    digest of ``raw_body`` under ``secret``. Every failure path performs
    exactly one HMAC and one ``hmac.compare_digest`` over two equal-length
    buffers, so timing never reveals *why* verification failed.
    """
    expected = _digest(secret, raw_body)
    presented = _presented_digest(signature_header)
    if presented is None:
        # Same-length stand-in: the compare costs the same as the "parsed fine
        # but did not match" case, so a missing/garbage header is not faster.
        presented = bytes(len(expected))
    return hmac.compare_digest(expected, presented)


# --- size limit -------------------------------------------------------------
def byte_size(raw_body: Any) -> int:
    """Length of ``raw_body`` in bytes (never trusts ``len()`` on a str)."""
    if raw_body is None:
        return 0
    if isinstance(raw_body, str):
        return len(raw_body.encode("utf-8"))
    return len(raw_body)


def enforce_size_limit(raw_body: Any,
                       max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> int:
    """Reject oversized bodies *before* hashing or parsing them.

    Returns the accepted size. Raises :class:`BodyTooLargeError` (413) when the
    body exceeds ``max_bytes``, and ``ValueError`` on a nonsensical limit.
    """
    if max_bytes is None:
        max_bytes = DEFAULT_MAX_BODY_BYTES
    max_bytes = int(max_bytes)
    if max_bytes < 1:
        raise ValueError("max_bytes must be >= 1")
    size = byte_size(raw_body)
    if size > max_bytes:
        raise BodyTooLargeError(
            f"webhook body of {size} bytes exceeds the {max_bytes}-byte limit")
    return size


# --- payload ----------------------------------------------------------------
def parse_payload(raw_body: Any) -> dict:
    """Decode the delivery body into a dict or raise ``PayloadInvalidError``."""
    if isinstance(raw_body, (bytes, bytearray, memoryview)):
        try:
            raw_body = _as_bytes(raw_body).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PayloadInvalidError(f"body is not valid UTF-8: {exc}") from exc
    if isinstance(raw_body, str):
        try:
            payload = json.loads(raw_body)
        except (ValueError, TypeError) as exc:
            raise PayloadInvalidError(f"body is not valid JSON: {exc}") from exc
    elif isinstance(raw_body, dict):
        payload = raw_body
    else:
        raise PayloadInvalidError("payload must be a JSON object")
    if not isinstance(payload, dict):
        raise PayloadInvalidError(
            f"payload must be a JSON object, not {type(payload).__name__}")
    return payload


def infer_event(payload: Mapping[str, Any]) -> str:
    """Derive the event name from the payload shape.

    GitHub sends the name in the ``X-GitHub-Event`` header; the body carries
    distinguishing keys, so normalization works from the payload alone and the
    header stays an optional cross-check.
    """
    if "check_run" in payload:
        return "check_run"
    if "check_suite" in payload:
        return "check_suite"
    if "review" in payload:
        return "pull_request_review"
    if "pull_request" in payload:
        return "pull_request"
    raise UnsupportedEventError("cannot determine the GitHub event from the payload")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    return str(value)


# --- redaction --------------------------------------------------------------
#: Only unambiguous credential shapes are masked, so a legitimate title such as
#: "add secret scanner" survives while a leaked token does not.
_SECRET_MARKERS = (
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_", "gtok",
    "xoxb-", "xoxp-", "xoxa-", "akia", "-----begin",
    "api_key=", "apikey=", "access_token=", "password=", "passwd=",
    "secret=", "client_secret=", "bearer ",
)
_REDACTION = "[redacted]"


def _mask_credentials(text: str) -> str:
    """Mask ``scheme://user:pass@host`` credentials and token-shaped blobs."""
    out = text
    # scheme://user:pass@host -> scheme://[redacted]@host
    cursor = 0
    while True:
        idx = out.lower().find("://", cursor)
        if idx < 0:
            break
        end = len(out)
        for stop in (" ", "\t", "\n", "\"", "'", ")", "]"):
            found = out.find(stop, idx)
            if 0 <= found < end:
                end = found
        authority = out[idx + 3:end]
        if "@" in authority:
            host = authority.rsplit("@", 1)[1]
            out = out[:idx + 3] + _REDACTION + "@" + host + out[end:]
        cursor = idx + 3
    # token-shaped blobs: the marker plus whatever follows it is dropped
    for marker in _SECRET_MARKERS:
        start = 0
        while True:
            idx = out.lower().find(marker, start)
            if idx < 0:
                break
            out = out[:idx] + _REDACTION + out[idx + 40:]
            start = idx + len(_REDACTION)
    return out


def redact(value: Any, limit: int = MAX_SUMMARY_CHARS) -> str:
    """Bounded, single-line, credential-free rendering of one field.

    Control characters and newlines collapse to spaces (no log or terminal
    injection, no multi-line blobs), credential shapes are masked, and the
    result is hard-truncated to ``limit`` characters.
    """
    text = _text(value)
    if not text:
        return ""
    cleaned = "".join(ch if ch >= " " else " " for ch in text)
    cleaned = " ".join(cleaned.split())
    cleaned = _mask_credentials(cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[:max(0, limit - 1)].rstrip() + "\u2026"
    return cleaned


def _opt_text(value: Any, limit: int) -> Optional[str]:
    text = redact(value, limit)
    return text or None


def _sha(value: Any) -> Optional[str]:
    text = redact(value, MAX_SHA_CHARS)
    if not text or len(text) < 7 or not all(c in _HEX_CHARS for c in text):
        return None
    return text.lower()


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _repo_full_name(payload: Mapping[str, Any]) -> str:
    repo = _mapping(payload.get("repository"))
    name = redact(repo.get("full_name"), MAX_REPO_CHARS)
    if not name or name.count("/") != 1:
        raise PayloadInvalidError("payload has no repository full_name")
    owner, _, leaf = name.partition("/")
    owner, leaf = owner.strip(), leaf.strip()
    if not owner or not leaf:
        raise PayloadInvalidError(f"malformed repository full_name {name!r}")
    if any(c not in _REPO_CHARS for c in owner + leaf):
        raise PayloadInvalidError(
            f"repository full_name {name!r} has unsupported characters")
    return f"{owner}/{leaf}"


# --- normalized record ------------------------------------------------------
@dataclass(frozen=True)
class NormalizedEvent:
    """The ONLY thing kept from a delivery: bounded, redacted, no raw body."""

    delivery_id: str
    event: str
    action: str
    repo: str
    pr_number: Optional[int] = None
    head_ref: Optional[str] = None
    head_sha: Optional[str] = None
    check_id: Optional[str] = None
    summary: str = ""

    @property
    def event_action(self) -> str:
        return f"{self.event}.{self.action}"

    def as_row(self) -> dict:
        """Columns for ``github_events`` (migrations/013_github_events.sql)."""
        return {
            "delivery_id": self.delivery_id,
            "repo": self.repo,
            "event_action": self.event_action,
            "pr_number": self.pr_number,
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
            "check_id": self.check_id,
            "summary": self.summary,
        }

    def as_command_payload(self) -> dict:
        """Bounded command-inbox payload — never the raw delivery."""
        payload = self.as_row()
        payload["event"] = self.event
        payload["action"] = self.action
        payload["source"] = "github_webhook"
        return payload


def _pr_number(payload: Mapping[str, Any]) -> Optional[int]:
    number = _positive_int(payload.get("number"))
    if number is None:
        number = _positive_int(
            _mapping(payload.get("pull_request")).get("number"))
    if number is None:
        for key in ("check_suite", "check_run"):
            for pr in _mapping(payload.get(key)).get("pull_requests") or []:
                number = _positive_int(_mapping(pr).get("number"))
                if number is not None:
                    break
            if number is not None:
                break
    return number


def _head(payload: Mapping[str, Any]) -> tuple:
    """(head_ref, head_sha) for the delivery, from its own blocks."""
    pr = _mapping(payload.get("pull_request"))
    suite = _mapping(payload.get("check_suite"))
    run = _mapping(payload.get("check_run"))
    head = _mapping(pr.get("head"))
    ref = (head.get("ref") or suite.get("head_branch")
           or _mapping(run.get("check_suite")).get("head_branch")
           or run.get("head_branch"))
    sha = (head.get("sha") or run.get("head_sha") or suite.get("head_sha")
           or _mapping(payload.get("head_commit")).get("id"))
    return _opt_text(ref, MAX_REF_CHARS), _sha(sha)


def _summary(payload: Mapping[str, Any], event: str, action: str,
             pr_number: Optional[int]) -> str:
    """One bounded, redacted line. Free-text bodies are dropped, never stored."""
    bits: list = []
    if event in ("pull_request", "pull_request_review"):
        pr = _mapping(payload.get("pull_request"))
        bits.append(f"PR #{pr_number if pr_number is not None else '?'} {action}")
        if pr.get("title"):
            bits.append(redact(pr.get("title"), 120))
        if pr.get("base") and _mapping(pr.get("base")).get("ref"):
            bits.append(f"-> {redact(_mapping(pr.get('base')).get('ref'), 80)}")
        author = _mapping(pr.get("user")).get("login")
        if event == "pull_request_review":
            review = _mapping(payload.get("review"))
            bits.append(f"review {redact(review.get('state'), 40)}")
            author = author or _mapping(review.get("user")).get("login")
        if author:
            bits.append(f"by {redact(author, 60)}")
    elif event == "check_suite":
        suite = _mapping(payload.get("check_suite"))
        bits.append(f"check_suite {action}")
        bits.append(f"conclusion={redact(suite.get('conclusion'), 40)}")
        bits.append(f"runs={redact(suite.get('check_runs_count'), 10)}")
    else:
        run = _mapping(payload.get("check_run"))
        bits.append(f"check_run {action}")
        bits.append(f"name={redact(run.get('name'), 80)}")
        bits.append(f"conclusion={redact(run.get('conclusion'), 40)}")
    if pr_number is not None and event.startswith("check"):
        bits.append(f"PR #{pr_number}")
    return redact(" ".join(str(b) for b in bits if b), MAX_SUMMARY_CHARS)


def normalize_event(payload: Any, delivery_id: Any,
                    event: Optional[str] = None) -> NormalizedEvent:
    """Project a delivery onto a :class:`NormalizedEvent`.

    Raises :class:`UnsupportedEventError` for any (event, action) pair outside
    :data:`SUPPORTED_ACTIONS` and :class:`PayloadInvalidError` when the payload
    is not a usable object.
    """
    if not isinstance(payload, Mapping):
        raise PayloadInvalidError("payload must be a mapping")
    name = redact(event, 80) if event else ""
    if not name:
        name = infer_event(payload)
    if name not in SUPPORTED_ACTIONS:
        raise UnsupportedEventError(f"unsupported event {name!r}")
    action = redact(payload.get("action"), 80)
    if not action:
        raise UnsupportedEventError(f"event {name!r} carries no action")
    if action not in SUPPORTED_ACTIONS[name]:
        raise UnsupportedEventError(
            f"unsupported action {action!r} for event {name!r}")
    delivery = redact(delivery_id, MAX_DELIVERY_ID_CHARS)
    if not delivery:
        raise PayloadInvalidError("missing delivery id")
    repo = _repo_full_name(payload)
    number = _pr_number(payload)
    head_ref, head_sha = _head(payload)
    check_id = None
    if name == "check_suite":
        check_id = _opt_text(_mapping(payload.get("check_suite")).get("id"),
                             MAX_ID_CHARS)
    elif name == "check_run":
        check_id = _opt_text(_mapping(payload.get("check_run")).get("id"),
                             MAX_ID_CHARS)
    return NormalizedEvent(
        delivery_id=delivery, event=name, action=action, repo=repo,
        pr_number=number, head_ref=head_ref, head_sha=head_sha,
        check_id=check_id, summary=_summary(payload, name, action, number))


# --- allowlist + pipeline ---------------------------------------------------
def repo_is_allowed(repo: Any, allowed_repos: Optional[Iterable[str]]) -> bool:
    """Exact ``owner/repo`` or ``owner/*`` match, case-insensitive.

    An empty or missing allowlist denies everything: a webhook that can enqueue
    work must be pointed at known repositories on purpose (fail closed).
    """
    name = _text(repo).strip().lower()
    if not name:
        return False
    for pattern in allowed_repos or ():
        pat = _text(pattern).strip().lower()
        if not pat:
            continue
        if pat == "*":
            return True
        if pat.endswith("/*"):
            if name.startswith(pat[:-1]) and "/" in name:
                return True
        elif pat == name:
            return True
    return False


def process_webhook_body(secret: Any, raw_body: Any, signature_header: Any,
                         delivery_id: Any, event: Optional[str] = None, *,
                         max_bytes: int = DEFAULT_MAX_BODY_BYTES,
                         allowed_repos: Optional[Iterable[str]] = None,
                         ) -> NormalizedEvent:
    """Authenticate, bound, validate and normalize one delivery.

    The whole security contract in one stdlib-only call, so the endpoint stays
    thin and the tests can prove a rejected delivery yields *nothing*: this
    function raises before any record exists, so nothing can be persisted or
    enqueued.

    Order matters: size bound -> signature -> parse -> (event, action)
    allowlist -> repository allowlist.
    """
    if not secret:
        raise WebhookNotConfiguredError(
            "github webhook secret is not configured (deployment env only)")
    enforce_size_limit(raw_body, max_bytes)
    if signature_header is None or not _text(signature_header).strip():
        # Still run the verification (constant work), then report it as missing.
        verify_webhook_signature(secret, raw_body, signature_header)
        raise SignatureMissingError("missing X-Hub-Signature-256 header")
    if not verify_webhook_signature(secret, raw_body, signature_header):
        raise SignatureInvalidError("invalid X-Hub-Signature-256 signature")
    payload = parse_payload(raw_body)
    record = normalize_event(payload, delivery_id, event)
    if not repo_is_allowed(record.repo, allowed_repos):
        raise RepoNotAllowedError(
            f"repository {record.repo!r} is not in the webhook allowlist")
    return record
