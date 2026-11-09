"""Tests for issue #13: the LLM probe target must be container-aware.

A loopback default (127.0.0.1) is correct natively and wrong inside a
container, where it points at the container itself instead of the host engine.
"""
from __future__ import annotations

import pytest

from clams.config import (
    LlmSettings,
    _is_loopback_url,
    resolve_llm_base_url,
)


def test_loopback_detection():
    assert _is_loopback_url("http://127.0.0.1:8731")
    assert _is_loopback_url("http://localhost:8731")
    assert _is_loopback_url("http://[::1]:8731")
    assert not _is_loopback_url("http://host.docker.internal:8731")
    assert not _is_loopback_url("http://192.168.1.10:8731")


def test_native_keeps_loopback():
    url, rewrote = resolve_llm_base_url(
        "http://127.0.0.1:8731", inside_container=False
    )
    assert url == "http://127.0.0.1:8731"
    assert rewrote is False


def test_container_rewrites_loopback():
    url, rewrote = resolve_llm_base_url(
        "http://127.0.0.1:8731", inside_container=True
    )
    assert url == "http://host.docker.internal:8731"
    assert rewrote is True


def test_container_keeps_explicit_non_loopback():
    url, rewrote = resolve_llm_base_url(
        "http://10.0.0.5:8731", inside_container=True
    )
    assert url == "http://10.0.0.5:8731"
    assert rewrote is False


def test_custom_container_base_url():
    url, rewrote = resolve_llm_base_url(
        "http://localhost:8731",
        inside_container=True,
        container_base_url="http://engine.internal:9000",
    )
    assert url == "http://engine.internal:9000"
    assert rewrote is True


def test_llm_settings_resolved_property(monkeypatch):
    monkeypatch.setattr("clams.config.in_container", lambda: True)
    s = LlmSettings()
    assert s.resolved_base_url == "http://host.docker.internal:8731"


def test_llm_settings_rejects_non_url():
    with pytest.raises(ValueError, match="absolute http"):
        LlmSettings(base_url="not a url")


def test_watchdog_uses_resolved_url(tmp_path, monkeypatch):
    """The watchdog must probe the resolved target, not a hard-coded loopback."""
    from clams.config import Settings
    from clams.db import open_store
    from clams.watchdog import WatchdogService

    monkeypatch.setenv("CLAMS__LLM__BASE_URL", "http://127.0.0.1:8731")
    monkeypatch.setattr("clams.config.in_container", lambda: True)
    s = Settings()
    store = open_store(str(tmp_path / "t.db"))
    svc = WatchdogService(store, s)
    assert svc.llm_base_url == "http://host.docker.internal:8731"
    store.db.close()


def test_watchdog_explicit_override_wins(tmp_path):
    from clams.config import Settings
    from clams.db import open_store
    from clams.watchdog import WatchdogService

    s = Settings()
    store = open_store(str(tmp_path / "t.db"))
    svc = WatchdogService(store, s, llm_base_url="http://elsewhere:1234")
    assert svc.llm_base_url == "http://elsewhere:1234"
    store.db.close()
