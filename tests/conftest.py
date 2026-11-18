"""Shared fixtures for the Clams v2 test suite."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clams.db import connect, migrate  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    """A migrated, empty v2 database."""
    path = tmp_path / "clams.db"
    conn = connect(str(path))
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "clams.db")


def redis_available() -> bool:
    url = os.environ.get("CLAMS_REDIS_URL", "redis://127.0.0.1:6379/0")
    try:
        import redis
        r = redis.Redis.from_url(url, socket_connect_timeout=1)
        r.ping()
        return True
    except Exception:
        return False
