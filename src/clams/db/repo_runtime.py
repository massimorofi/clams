"""runtime_state helpers: paused / paused_by / heartbeat / schema_version."""
from __future__ import annotations

import sqlite3
from typing import Optional

from .conn import get_runtime_state, set_runtime_state, utcnow_s

KEY_PAUSED = "paused"
KEY_PAUSED_BY = "paused_by"
KEY_HEARTBEAT = "heartbeat"


def is_paused(conn: sqlite3.Connection) -> bool:
    return get_runtime_state(conn, KEY_PAUSED, "0") == "1"


def paused_by(conn: sqlite3.Connection) -> Optional[str]:
    return get_runtime_state(conn, KEY_PAUSED_BY)


def pause(conn: sqlite3.Connection, by: str) -> None:
    """by: 'operator' | 'watchdog'. Operator pause is never auto-cleared."""
    set_runtime_state(conn, KEY_PAUSED, "1")
    set_runtime_state(conn, KEY_PAUSED_BY, by)


def resume(conn: sqlite3.Connection, by: str) -> bool:
    """Returns True if the pause was cleared. A watchdog resume cannot clear
    an operator pause."""
    if not is_paused(conn):
        return True
    if by == "watchdog" and paused_by(conn) == "operator":
        return False
    set_runtime_state(conn, KEY_PAUSED, "0")
    set_runtime_state(conn, KEY_PAUSED_BY, "")
    return True


def heartbeat(conn: sqlite3.Connection) -> None:
    set_runtime_state(conn, KEY_HEARTBEAT, utcnow_s())


def heartbeat_age_s(conn: sqlite3.Connection) -> Optional[float]:
    import time
    from .conn import iso_to_epoch
    ts = get_runtime_state(conn, KEY_HEARTBEAT)
    if not ts:
        return None
    return max(0.0, time.time() - iso_to_epoch(ts))
