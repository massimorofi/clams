"""SSE tail of the events table (§10). No history replay unless
Last-Event-ID is supplied; resume from that id."""
from __future__ import annotations

import json
import time
from typing import Iterator

from ..db import repo_events


def event_stream(conn, last_event_id: int = 0,
                 poll_s: float = 1.0, max_events: int = 0) -> Iterator[str]:
    """Yield SSE frames. ``max_events`` bounds the stream (tests); 0 = forever."""
    after = last_event_id
    sent = 0
    while True:
        rows = repo_events.tail_events(conn, after, limit=100)
        for r in rows:
            after = r["event_id"]
            data = {
                "event_id": r["event_id"], "scope": r["scope"], "ref": r["ref"],
                "level": r["level"], "message": r["message"],
                "ts": r["ts"],
                "data": json.loads(r["data"]) if r["data"] else None,
            }
            yield f"id: {r['event_id']}\ndata: {json.dumps(data)}\n\n"
            sent += 1
            if max_events and sent >= max_events:
                return
        time.sleep(poll_s)
