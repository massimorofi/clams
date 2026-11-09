"""HTTP client for a Claudio agent backend instance.

Contract (verified against ``claudio-backend/src/claudio_backend/api``):
  POST /chat        {message, session_id?, approval_mode?, files?}
                  -> {run_id, session_id, final_output, usage}
  POST /chat/stream  SSE variant of the same
  GET  /healthz   -> {"status": "ok"}
  GET  /version
  POST /chat/runs/{run_id}/cancel
Auth: ``Authorization: Bearer <token>``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

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
    usage: dict[str, Any]


class ClaudioClient:
    def __init__(self, base_url: str, auth_token: str = "", timeout: float = 120.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout = timeout
        self._transport = transport

    def _client(self, timeout: Optional[float] = None) -> httpx.AsyncClient:
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return httpx.AsyncClient(
            base_url=self.base_url, headers=headers,
            timeout=timeout if timeout is not None else self.timeout,
            transport=self._transport,
        )

    async def health(self) -> bool:
        try:
            async with self._client(timeout=5.0) as c:
                r = await c.get("/healthz")
            return r.status_code == 200
        except Exception:
            return False

    async def version(self) -> dict[str, Any]:
        async with self._client(timeout=10.0) as c:
            r = await c.get("/version")
            r.raise_for_status()
            return r.json()

    async def chat(self, message: str, session_id: Optional[str] = None,
                  approval_mode: Optional[str] = None,
                  files: Optional[list[Any]] = None,
                  timeout: Optional[float] = None) -> ChatResponse:
        payload: dict[str, Any] = {"message": message}
        if session_id:
            payload["session_id"] = session_id
        if approval_mode:
            payload["approval_mode"] = approval_mode
        if files:
            payload["files"] = files
        try:
            async with self._client(timeout=timeout) as c:
                r = await c.post("/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise ClaudioUnavailable(f"claudio chat timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ClaudioUnavailable(f"claudio chat unreachable: {exc}") from exc

        if r.status_code >= 400:
            body = _safe(r)
            raise ClaudioError(
                f"claudio chat failed with {r.status_code}: {_message_of(body)}",
                status=r.status_code, payload=body,
            )
        data = r.json()
        return ChatResponse(
            run_id=data.get("run_id", ""),
            session_id=data.get("session_id", ""),
            final_output=data.get("final_output", "") or "",
            usage=data.get("usage", {}) or {},
        )

    async def cancel(self, run_id: str) -> dict[str, Any]:
        async with self._client(timeout=15.0) as c:
            r = await c.post(f"/chat/runs/{run_id}/cancel")
            if r.status_code >= 400:
                raise ClaudioError(f"cancel failed: {r.status_code}", status=r.status_code)
            return r.json() if r.content else {}

    async def stream(self, message: str, session_id: Optional[str] = None,
                    approval_mode: Optional[str] = None) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed SSE events from /chat/stream."""
        payload: dict[str, Any] = {"message": message}
        if session_id:
            payload["session_id"] = session_id
        if approval_mode:
            payload["approval_mode"] = approval_mode
        try:
            async with self._client() as c:
                async with c.stream("POST", "/chat/stream", json=payload) as r:
                    if r.status_code >= 400:
                        text = await r.aread()
                        raise ClaudioError(f"stream failed {r.status_code}: {text[:200]}",
                                         status=r.status_code)
                    event: dict[str, Any] = {}
                    async for line in r.aiter_lines():
                        if not line:
                            if event:
                                yield event
                                event = {}
                            continue
                        if line.startswith("event:"):
                            event["event"] = line[len("event:"):].strip()
                        elif line.startswith("data:"):
                            raw = line[len("data:"):].strip()
                            try:
                                event["data"] = json.loads(raw)
                            except json.JSONDecodeError:
                                event["data"] = raw
                        elif line.startswith("id:"):
                            event["id"] = line[len("id:"):].strip()
                    if event:
                        yield event
        except httpx.HTTPError as exc:
            raise ClaudioUnavailable(f"claudio stream unreachable: {exc}") from exc


def _safe(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return r.text[:500]


def _message_of(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or body)[:200]
    return str(body)[:200]
