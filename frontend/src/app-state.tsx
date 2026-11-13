import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from 'react'
import type { ReactNode } from 'react'
import { api } from './api'
import type { StatusPayload } from './types'

export interface FeedEvent {
  id: number
  level: string
  source: string
  message: string
  timestamp: string
}

export interface AppContextValue {
  status: StatusPayload | null
  sseOk: boolean
  wsOk: boolean
  refresh: () => void
  toast: (msg: string) => void
  /** Rolling list of recent log/alert events from the SSE stream. */
  events: FeedEvent[]
  /** Force-close the planner WebSocket (it auto-reconnects). */
  reconnectWs: () => void
  /** Send a planner chat message; resolves to the planner's reply text. */
  sendPlanner: (message: string, requirementNumber?: number) => Promise<string>
  /** Register a listener for planner replies arriving over the socket. */
  onPlannerReply: (fn: (text: string) => void) => () => void
}

const noop = () => {}
const defaultValue: AppContextValue = {
  status: null,
  sseOk: false,
  wsOk: false,
  refresh: noop,
  toast: noop,
  events: [],
  reconnectWs: noop,
  sendPlanner: async () => '',
  onPlannerReply: () => noop,
}

export const AppCtx = createContext<AppContextValue>(defaultValue)

export function useApp(): AppContextValue {
  return useContext(AppCtx)
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<StatusPayload | null>(null)
  const [sseOk, setSseOk] = useState(false)
  const [wsOk, setWsOk] = useState(false)
  const [toastMsg, setToastMsg] = useState<{ id: number; text: string } | null>(null)
  const [events, setEvents] = useState<FeedEvent[]>([])
  const eventSeq = useRef(0)

  const toast = useCallback((msg: string) => {
    setToastMsg({ id: Date.now(), text: msg })
    window.setTimeout(() => {
      setToastMsg((cur) => (cur && cur.text === msg ? null : cur))
    }, 2700)
  }, [])

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.getStatus())
    } catch {
      /* header degrades gracefully if the API is briefly unavailable */
    }
  }, [])

  // ---- SSE telemetry stream with connection tracking ----
  useEffect(() => {
    let closed = false
    let es: EventSource | null = null
    try {
      es = new EventSource('/api/v1/events')
      es.addEventListener('connected', () => {
        if (!closed) setSseOk(true)
      })
      es.addEventListener('telemetry:update', (ev: MessageEvent) => {
        if (closed) return
        try {
          setStatus(JSON.parse(ev.data) as StatusPayload)
        } catch {
          /* ignore malformed frame */
        }
        setSseOk(true)
      })
      es.addEventListener('logs:stream', (ev: MessageEvent) => {
        if (closed) return
        try {
          const d = JSON.parse(ev.data)
          const evt: FeedEvent = {
            id: ++eventSeq.current,
            level: String(d.level ?? 'info'),
            source: String(d.source ?? 'system'),
            message: String(d.message ?? ''),
            timestamp: String(d.timestamp ?? new Date().toISOString()),
          }
          setEvents((cur) => [evt, ...cur].slice(0, 50))
        } catch {
          /* ignore malformed frame */
        }
      })
      es.onopen = () => {
        if (!closed) setSseOk(true)
      }
      es.onerror = () => {
        // EventSource auto-reconnects; just reflect the degraded state.
        if (!closed) setSseOk(false)
      }
    } catch {
      setSseOk(false)
    }
    return () => {
      closed = true
      try {
        es?.close()
      } catch {
        /* already closed */
      }
    }
  }, [])

  // ---- Planner WebSocket with reconnect + REST fallback ----
  const wsRef = useRef<WebSocket | null>(null)
  const replyListeners = useRef<Set<(text: string) => void>>(new Set())
  const reconnectTimer = useRef<number | undefined>(undefined)
  const mounted = useRef(true)

  const connectWs = useCallback(() => {
    let ws: WebSocket
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    try {
      ws = new WebSocket(`${proto}://${window.location.host}/ws`)
    } catch {
      return
    }
    wsRef.current = ws
    ws.onopen = () => {
      if (mounted.current) setWsOk(true)
    }
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data)
        if (msg.event === 'planner:reply') {
          const reply = String(msg.data?.reply ?? '')
          replyListeners.current.forEach((fn) => fn(reply))
        }
      } catch {
        replyListeners.current.forEach((fn) => fn(String(ev.data)))
      }
    }
    ws.onclose = () => {
      if (!mounted.current) return
      setWsOk(false)
      // reconnect with a small backoff
      window.clearTimeout(reconnectTimer.current)
      reconnectTimer.current = window.setTimeout(() => {
        if (mounted.current) connectWs()
      }, 2000)
    }
    ws.onerror = () => {
      if (mounted.current) setWsOk(false)
    }
  }, [])

  useEffect(() => {
    mounted.current = true
    connectWs()
    return () => {
      mounted.current = false
      window.clearTimeout(reconnectTimer.current)
      try {
        wsRef.current?.close()
      } catch {
        /* already closed */
      }
    }
  }, [connectWs])

  const sendPlanner = useCallback(
    async (message: string, requirementNumber?: number): Promise<string> => {
      const ws = wsRef.current
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(
          JSON.stringify({
            type: 'planner:message',
            message,
            requirement_number: requirementNumber,
          }),
        )
        // Reply arrives asynchronously via onPlannerReply listeners.
        return ''
      }
      // REST fallback so chat works even without a live socket.
      const r = await api.plannerMessage(message, requirementNumber)
      return r.reply
    },
    [],
  )

  const onPlannerReply = useCallback(
    (fn: (text: string) => void) => {
      replyListeners.current.add(fn)
      return () => {
        replyListeners.current.delete(fn)
      }
    },
    [],
  )

  const reconnectWs = useCallback(() => {
    try {
      wsRef.current?.close()
    } catch {
      /* already closed; the reconnect timer will start a new one */
    }
  }, [])

  const value: AppContextValue = {
    status,
    sseOk,
    wsOk,
    refresh,
    toast,
    events,
    reconnectWs,
    sendPlanner,
    onPlannerReply,
  }

  return (
    <AppCtx.Provider value={value}>
      {children}
      <div
        className={`toast${toastMsg ? ' show' : ''}`}
        role="status"
        aria-live="polite"
        id="toast"
      >
        {toastMsg?.text ?? ''}
      </div>
    </AppCtx.Provider>
  )
}
