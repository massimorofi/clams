import { useEffect, useRef, useState } from 'react'
import { PlannerSocket } from '../api'
import type { ChatMessage } from '../types'

const QUICK = [
  { label: 'Retry failed', action: 'retry' },
  { label: 'Pause', action: 'pause' },
  { label: 'Resume', action: 'resume' },
  { label: 'Approve plan', action: 'approve' },
  { label: 'Reject plan', action: 'reject' },
]

export default function PlannerChatTab() {
  const [msgs, setMsgs] = useState<ChatMessage[]>([
    {
      id: 'intro',
      role: 'system',
      text: 'Planner chat console. Messages go to the planner Claudio instance over websocket; if the socket is unavailable they fall back to REST.',
      ts: new Date().toISOString(),
    },
  ])
  const [draft, setDraft] = useState('')
  const [reqNum, setReqNum] = useState('')
  const [sending, setSending] = useState(false)
  const sock = useRef<PlannerSocket | null>(null)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const s = new PlannerSocket()
    s.connect(
      (text) =>
        setMsgs((m) => [
          ...m,
          { id: `${Date.now()}-p`, role: 'planner', text, ts: new Date().toISOString() },
        ]),
      (text) =>
        setMsgs((m) => [
          ...m,
          { id: `${Date.now()}-s`, role: 'system', text, ts: new Date().toISOString() },
        ]),
    )
    sock.current = s
    return () => s.close()
  }, [])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [msgs])

  async function send() {
    const text = draft.trim()
    if (!text || sending) return
    setMsgs((m) => [
      ...m,
      { id: `${Date.now()}-u`, role: 'user', text, ts: new Date().toISOString() },
    ])
    setDraft('')
    setSending(true)
    try {
      const n = reqNum.trim() ? Number(reqNum.trim()) : undefined
      const reply = await sock.current?.send(text, n)
      // REST fallback returns the reply directly; the websocket path delivers it
      // via onmessage, so only append here when we actually used REST.
      if (reply) {
        setMsgs((m) => [
          ...m,
          { id: `${Date.now()}-r`, role: 'planner', text: reply, ts: new Date().toISOString() },
        ])
      }
    } catch (e: any) {
      setMsgs((m) => [
        ...m,
        { id: `${Date.now()}-e`, role: 'system', text: `error: ${e.message}`, ts: new Date().toISOString() },
      ])
    } finally {
      setSending(false)
    }
  }

  async function quick(action: string) {
    setSending(true)
    try {
      const n = reqNum.trim() ? Number(reqNum.trim()) : undefined
      const { api } = await import('../api')
      await api.plannerAction({ action, requirement_number: n })
      setMsgs((m) => [
        ...m,
        { id: `${Date.now()}-q`, role: 'system', text: `sent action: ${action}`, ts: new Date().toISOString() },
      ])
    } catch (e: any) {
      setMsgs((m) => [
        ...m,
        { id: `${Date.now()}-qe`, role: 'system', text: `action failed: ${e.message}`, ts: new Date().toISOString() },
      ])
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_280px] h-[calc(100vh-180px)]">
      <section className="bg-ink-900 border border-ink-700 rounded-lg flex flex-col min-h-0">
        <div className="flex-1 overflow-auto p-4 space-y-3">
          {msgs.map((m) => (
            <div
              key={m.id}
              className={`max-w-[80%] rounded-lg px-3 py-2 text-sm ${
                m.role === 'user'
                  ? 'ml-auto bg-blue-900 text-blue-50'
                  : m.role === 'planner'
                    ? 'bg-ink-800 text-slate-100'
                    : 'bg-ink-950 text-slate-400 text-xs italic'
              }`}
            >
              <div className="text-[10px] uppercase tracking-wide opacity-60 mb-1">
                {m.role} · {new Date(m.ts).toLocaleTimeString()}
              </div>
              <div className="whitespace-pre-wrap break-words">{m.text}</div>
            </div>
          ))}
          <div ref={endRef} />
        </div>

        <div className="border-t border-ink-700 p-3">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                e.preventDefault()
                send()
              }
            }}
            placeholder="Message the planner… markdown and code blocks supported. Use @agent, #task-id, /retry"
            rows={3}
            className="w-full bg-ink-800 border border-ink-600 rounded p-3 text-sm resize-none"
          />
          <div className="flex items-center gap-2 mt-2">
            <input
              type="number"
              value={reqNum}
              onChange={(e) => setReqNum(e.target.value)}
              placeholder="requirement # (optional)"
              className="w-52 bg-ink-800 border border-ink-600 rounded px-3 text-xs"
            />
            <button
              onClick={send}
              disabled={sending || !draft.trim()}
              className="ml-auto px-5 py-2 rounded-md bg-blue-700 hover:bg-blue-600 text-white text-sm font-semibold disabled:opacity-40"
            >
              {sending ? 'Sending…' : 'Send'}
            </button>
          </div>
          <p className="text-[11px] text-slate-500 mt-1">⌘/Ctrl + Enter to send</p>
        </div>
      </section>

      <section className="bg-ink-900 border border-ink-700 rounded-lg p-4">
        <h2 className="text-sm font-semibold text-white mb-3">Interventions</h2>
        <div className="space-y-2">
          {QUICK.map((q) => (
            <button
              key={q.action}
              onClick={() => quick(q.action)}
              disabled={sending}
              className="w-full px-3 py-2 rounded-md bg-ink-800 hover:bg-ink-700 text-xs font-medium text-left disabled:opacity-50"
            >
              {q.label}
            </button>
          ))}
        </div>
        <h2 className="text-sm font-semibold text-white mt-5 mb-2">Context tags</h2>
        <ul className="text-xs text-slate-400 space-y-1">
          <li><code className="text-accent">@agent</code> — address a specific agent</li>
          <li><code className="text-accent">#123</code> — reference a task id</li>
          <li><code className="text-accent">/retry</code> — retry the last failed task</li>
        </ul>
      </section>
    </div>
  )
}
