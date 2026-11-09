import type {
  CliResult,
  ConfigPutResponse,
  ConfigResponse,
  ProjectInfo,
  StatusPayload,
  AlertRow,
  WorkItemRow,
} from './types'

const BASE = '/api/v1'

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body?.detail) detail = String(body.detail)
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail)
  }
  return (await res.json()) as T
}

export const api = {
  getStatus: () => req<StatusPayload>('/status'),
  getProject: () => req<ProjectInfo>('/project'),
  pullProject: () => req<ProjectInfo>('/project/pull', { method: 'POST' }),
  getConfig: () => req<ConfigResponse>('/config'),
  putConfig: (values: Record<string, unknown>) =>
    req<ConfigPutResponse>('/config', {
      method: 'PUT',
      body: JSON.stringify({ values }),
    }),
  cliExec: (command: string, args: string[] = [], timeout = 300) =>
    req<CliResult>('/cli/exec', {
      method: 'POST',
      body: JSON.stringify({ command, args, timeout }),
    }),
  plannerAction: (body: {
    action: string
    requirement_number?: number
    task_number?: number
    detail?: string
  }) => req<{ ok: boolean }>('/planner/action', { method: 'POST', body: JSON.stringify(body) }),
  plannerMessage: (message: string, requirement_number?: number) =>
    req<{ reply: string }>('/planner/message', {
      method: 'POST',
      body: JSON.stringify({ message, requirement_number }),
    }),
  getAlerts: (limit = 100, severity?: string) =>
    req<{ alerts: AlertRow[] }>(
      `/alerts?limit=${limit}${severity ? `&severity=${severity}` : ''}`,
    ),
  getTasks: () => req<{ tasks: WorkItemRow[] }>('/tasks'),
}

/**
 * Subscribe to the SSE telemetry stream. Returns a disposer that closes the
 * underlying EventSource.
 */
export function subscribeEvents(
  handlers: Record<string, (data: any) => void>,
): () => void {
  const es = new EventSource('/api/v1/events')
  for (const [name, fn] of Object.entries(handlers)) {
    es.addEventListener(name, (ev: MessageEvent) => {
      try {
        fn(JSON.parse(ev.data))
      } catch {
        fn(ev.data)
      }
    })
  }
  return () => es.close()
}

/**
 * Planner chat over WebSocket, with a REST fallback: if the socket cannot be
 * opened or dies, sends still succeed via POST so chat never becomes a dead end.
 */
export class PlannerSocket {
  private ws: WebSocket | null = null
  private open = false

  connect(onReply: (text: string) => void, onSystem?: (text: string) => void) {
    try {
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      this.ws = new WebSocket(`${proto}://${window.location.host}/ws`)
      this.ws.onopen = () => {
        this.open = true
        onSystem?.('connected to planner over websocket')
      }
      this.ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data)
          if (msg.event === 'planner:reply') onReply(String(msg.data?.reply ?? ''))
          else if (msg.event === 'error') onSystem?.(String(msg.data?.message ?? 'error'))
        } catch {
          onReply(String(ev.data))
        }
      }
      this.ws.onclose = () => {
        this.open = false
        onSystem?.('websocket closed, falling back to REST')
      }
      this.ws.onerror = () => {
        this.open = false
      }
    } catch {
      onSystem?.('websocket unavailable, using REST')
    }
  }

  async send(message: string, requirement_number?: number) {
    if (this.open && this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'planner:message', message, requirement_number }))
      return
    }
    // Fallback so chat works even without a live socket.
    const r = await api.plannerMessage(message, requirement_number)
    return r.reply
  }

  close() {
    try {
      this.ws?.close()
    } catch {
      /* already closed */
    }
    this.ws = null
    this.open = false
  }
}
