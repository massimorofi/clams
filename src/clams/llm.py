"""Direct OpenAI-compatible LLM caller.

The planner takes an ``llm_call`` callable, so it can be driven either by a
Claudio instance (``claudio_client.ClaudioClient``) or, when that is not
reachable, straight by any OpenAI-compatible endpoint (halogen / GUFO /
llama.cpp served behind strixper).
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Callable, Coroutine, Optional

import httpx


class LlmError(Exception):
    pass


# ---- sync -> async bridge ---------------------------------------------
#
# The planner takes a *synchronous* ``llm_call`` callable, but the orchestrator
# invokes it from inside its own running asyncio loop. ``asyncio.run`` cannot be
# nested inside a live loop, so a blocking caller needs somewhere else to run the
# coroutine. We keep one dedicated background loop on its own thread and hand
# coroutines to it. This works whether or not the calling thread already has a
# running loop.

_bridge_loop: Optional[asyncio.AbstractEventLoop] = None
_bridge_thread: Optional[threading.Thread] = None
_bridge_lock = threading.Lock()


def _ensure_bridge_loop() -> asyncio.AbstractEventLoop:
    global _bridge_loop, _bridge_thread
    with _bridge_lock:
        if _bridge_loop is None or _bridge_loop.is_closed():
            _bridge_loop = asyncio.new_event_loop()
            _bridge_thread = threading.Thread(
                target=_bridge_loop.run_forever,
                name="clams-llm-bridge",
                daemon=True,
            )
            _bridge_thread.start()
        return _bridge_loop


def run_sync(coro_factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    """Run an async callable to completion from synchronous code.

    ``coro_factory`` is a zero-argument callable that returns a fresh coroutine
    each time (e.g. ``lambda: client.chat(prompt)``). When no event loop is
    running in the current thread we use ``asyncio.run`` directly; when one is
    already running (the orchestrator case) we submit the coroutine to a
    dedicated background loop and block on its result.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    loop = _ensure_bridge_loop()
    future = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
    return future.result()


class OpenAILlmClient:
    def __init__(self, base_url: str, api_key: str = "", model: str = "",
                 timeout: float = 300.0, temperature: float = 0.0,
                 max_tokens: int = 4096, enable_thinking: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        # Default output budget for the ``__call__`` path. A 3-7 task plan is a
        # few thousand characters of JSON; 2048 truncated it mid-structure and
        # every plan then failed JSON validation (issue #18 planning step).
        self.max_tokens = max_tokens
        # Reasoning/thinking models (qwen3 et al.) spend the token budget on a
        # hidden chain-of-thought before answering. With thinking on, that chain
        # ate ~7000 tokens and the JSON answer was cut off at ``length`` every
        # time. Disabling it (``enable_thinking: false`` via chat_template_kwargs)
        # makes the completion return the full JSON.
        self.enable_thinking = enable_thinking

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def complete(self, prompt: str, max_tokens: Optional[int] = None) -> str:
        """Blocking completion — usable directly as a planner ``llm_call``.

        Safe to call from inside a running asyncio loop: the coroutine is run on
        the module's background loop (see :func:`run_sync`).
        """
        mt = self.max_tokens if max_tokens is None else max_tokens
        return run_sync(lambda: self.complete_async(prompt, mt))

    async def complete_async(self, prompt: str,
                            max_tokens: Optional[int] = None) -> str:
        mt = self.max_tokens if max_tokens is None else max_tokens
        payload: dict[str, Any] = {
            "model": self.model or "local",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": mt,
            "temperature": self.temperature,
            # Disable the model's thinking mode so the budget goes to the JSON
            # answer instead of a chain-of-thought that truncates it.
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
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
        return self.complete(prompt, max_tokens=self.max_tokens)


def make_planner_llm(settings) -> "OpenAILlmClient":
    """Build the planner LLM caller from settings.

    Prefers the configured strixper/engine base URL, which speaks the
    OpenAI-compatible protocol directly.
    """
    base = getattr(settings.llm, "base_url", "") if hasattr(settings, "llm") else ""
    key = getattr(settings.llm, "api_key", "") if hasattr(settings, "llm") else ""
    model = getattr(settings.llm, "model", "") if hasattr(settings, "llm") else ""
    return OpenAILlmClient(base or settings.strixper.base_url, key, model)
