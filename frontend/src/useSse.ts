import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';

/**
 * Global SSE connection to /api/v1/events. Every frame invalidates all queries
 * so the UI refetches on backend changes. EventSource reconnects automatically
 * and replays missed frames via the Last-Event-ID header (the backend stamps
 * each frame with `id:`).
 *
 * EventSource cannot set request headers, so the bearer token is passed as a
 * `token` query parameter for backends that accept it.
 */
export function useEventStream(token: string): boolean {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let es: EventSource | null = null;
    try {
      const url = token
        ? `/api/v1/events?token=${encodeURIComponent(token)}`
        : '/api/v1/events';
      es = new EventSource(url);
    } catch {
      return;
    }
    const onEvent: EventListener = () => {
      setConnected(true);
      qc.invalidateQueries();
    };
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = onEvent;
    // Also catch named frames in case the backend uses them.
    for (const name of ['connected', 'telemetry:update', 'logs:stream', 'planner:reply']) {
      es.addEventListener(name, onEvent);
    }
    return () => {
      es?.close();
    };
  }, [qc, token]);

  return connected;
}
