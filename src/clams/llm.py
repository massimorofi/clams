"""Direct OpenAI-compatible LLM caller.

The planner takes an ``llm_call`` callable, so it can be driven either by a
Claudio instance (``claudio_client.ClaudioClient``) or, when that is not
reachable, straight by any OpenAI-compatible endpoint (halogen / GUFO /
llama.cpp served behind strixper).
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx


class LlmError(Exception):
    pass


class OpenAILlmClient:
    def __init__(self, base_url: str, api_key: str = "", model: str = "",
                 timeout: float = 300.0, temperature: float = 0.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def complete(self, prompt: str, max_tokens: int = 2048) -> str:
        """Blocking completion — usable directly as a planner ``llm_call``."""
        import asyncio
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.complete_async(prompt, max_tokens))
        raise LlmError("complete() cannot be called inside a running loop; "
                      "use complete_async()")

    async def complete_async(self, prompt: str, max_tokens: int = 2048) -> str:
        payload: dict[str, Any] = {
            "model": self.model or "local",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": self.temperature,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(f"{self.base_url}/v1/chat/completions",
                                     json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise LlmError(f"LLM endpoint unreachable: {exc}") from exc
        if r.status_code >= 400:
            raise LlmError(f"LLM endpoint returned {r.status_code}: {r.text[:200]}")
        data = r.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise LlmError(f"unexpected LLM response shape: {str(data)[:200]}") from exc

    def __call__(self, prompt: str) -> str:
        return self.complete(prompt)


def make_planner_llm(settings) -> "OpenAILlmClient":
    """Build the planner LLM caller from settings.

    Prefers the configured strixper/engine base URL, which speaks the
    OpenAI-compatible protocol directly.
    """
    base = getattr(settings.llm, "base_url", "") if hasattr(settings, "llm") else ""
    key = getattr(settings.llm, "api_key", "") if hasattr(settings, "llm") else ""
    model = getattr(settings.llm, "model", "") if hasattr(settings, "llm") else ""
    return OpenAILlmClient(base or settings.strixper.base_url, key, model)
