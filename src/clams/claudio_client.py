"""Sync HTTP client for an unmodified Claudio agent backend.

Contract (WP0-verified live against ``claudio/agent:latest``, port 8100):
  GET  /healthz -> {"status": "ok"}                       (no prefix)
  POST /api/v1/chat   {message, session_id?, approval_mode?, workspace_dir?, files?}
                    -> {run_id, session_id, final_output, usage}
  POST /api/v1/chat/runs/{run_id}/cancel -> {cancelled: true}
  GET  /api/v1/sessions/{session_id}/messages?limit=N -> {messages: [...]}
Errors: {"error": {"code", "message", "retryable", "details"}}.
Auth: ``Authorization: Bearer <token>`` (server.auth_token).
approval_mode values: off | on_request | always.
session_ref == Claudio session_id (uuid); a client-supplied session_id is
honoured for new sessions, and the session keeps its bound workspace on resume.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import httpx


class ClaudioError(Exception):
    def __init__(self, message: str, status: int = 0, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class ClaudioUnavailable(ClaudioError):
    pass


@dataclass
class ChatResponse:
    run_id: str
    session_id: str
    final_output: str
    usage: dict[str, Any] = field(default_factory=dict)


class ClaudioClient:
    def __init__(self, base_url: str, auth_token: str = "",
                 timeout: float = 120.0,
                 transport: Optional[httpx.BaseTransport] = None):
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout = timeout
        self._transport = transport

    def _client(self, timeout: Optional[float] = None) -> httpx.Client:
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return httpx.Client(base_url=self.base_url, headers=headers,
                            timeout=timeout if timeout is not None else self.timeout,
                            transport=self._transport)

    def health(self) -> bool:
        try:
            with self._client(timeout=5.0) as c:
                return c.get("/healthz").status_code == 200
        except Exception:
            return False

    def chat(self, message: str, session_id: Optional[str] = None,
             approval_mode: Optional[str] = None,
             workspace_dir: Optional[str] = None,
             timeout: Optional[float] = None) -> ChatResponse:
        payload: dict[str, Any] = {"message": message}
        if session_id:
            payload["session_id"] = session_id
        if approval_mode:
            payload["approval_mode"] = approval_mode
        if workspace_dir:
            payload["workspace_dir"] = workspace_dir
        try:
            with self._client(timeout=timeout) as c:
                r = c.post("/api/v1/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise ClaudioUnavailable(f"claudio chat timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ClaudioUnavailable(f"claudio chat unreachable: {exc}") from exc
        if r.status_code >= 400:
            raise ClaudioError(
                f"claudio chat failed {r.status_code}: {r.text[:300]}",
                status=r.status_code, payload=r.text[:2000])
        d = r.json()
        return ChatResponse(run_id=d.get("run_id", ""),
                            session_id=d.get("session_id", ""),
                            final_output=d.get("final_output", "") or "",
                            usage=d.get("usage", {}) or {})

    def cancel(self, run_id: str) -> bool:
        try:
            with self._client(timeout=15.0) as c:
                r = c.post(f"/api/v1/chat/runs/{run_id}/cancel")
            return r.status_code < 400
        except httpx.HTTPError:
            return False

    def session_messages(self, session_id: str, limit: int = 200) -> list[dict]:
        try:
            with self._client(timeout=15.0) as c:
                r = c.get(f"/api/v1/sessions/{session_id}/messages",
                          params={"limit": limit})
                r.raise_for_status()
                data = r.json()
                return data.get("messages", data if isinstance(data, list) else [])
        except httpx.HTTPError as exc:
            raise ClaudioUnavailable(f"claudio session fetch failed: {exc}") from exc
