import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useApp } from '../app-state'

interface Msg {
  id: number
  role: 'You' | 'Planner' | 'System'
  text: string
  time: string
}

let seq = 0

export default function ChatPage() {
  const { wsOk, sendPlanner, onPlannerReply, toast } = useApp()
  const [messages, setMessages] = useState<Msg[]>([
    {
      id: ++seq,
      role: 'System',
      text: 'Planner console ready.',
      time: new Date().toLocaleTimeString(),
    },
  ])
  const [draft, setDraft] = useState('')
  const [requirement, setRequirement] = useState('')
  const logRef = useRef<HTMLDivElement | null>(null)

  function append(role: Msg['role'], text: string) {
    setMessages((m) => [
      ...m,
      { id: ++seq, role, text, time: new Date().toLocaleTimeString() },
    ])
  }

  useEffect(() => {
    const off = onPlannerReply((text) => append('Planner', text))
    return off
  }, [onPlannerReply])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [messages])

  async function send() {
    const text = draft.trim()
    if (!text) return
    append('You', text)
    setDraft('')
    const reqNum = requirement ? Number(requirement) : undefined
    try {
      const reply = await sendPlanner(text, reqNum)
      // If the socket was down, sendPlanner returns the REST reply directly.
      if (reply) append('Planner', reply)
    } catch (e) {
      append('System', `Send failed: ${e instanceof Error ? e.message : 'unknown error'}`)
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      send()
    }
  }

  async function quick(action: string, label: string, needsConfirm: boolean) {
    if (needsConfirm && !window.confirm(`Confirm action: ${label}?`)) return
    try {
      const reqNum = requirement ? Number(requirement) : undefined
      await api.plannerAction({ action, requirement_number: reqNum })
      append('System', `Action recorded: ${action}.`)
      toast(`${label} sent.`)
    } catch (e) {
      append('System', `Action failed: ${e instanceof Error ? e.message : 'unknown error'}`)
    }
  }

  return (
    <section className="page active" id="page-chat">
      <div className="heading">
        <div>
          <div className="eyebrow">Operations</div>
          <h1>Planner chat</h1>
          <p className="sub">Talk to the planner and issue quick interventions.</p>
        </div>
        <span className={`status ${wsOk ? 'blue' : 'warn'}`}>
          {wsOk ? 'WS connected' : 'REST fallback'}
        </span>
      </div>

      <div className="layout" style={{ gridTemplateColumns: 'minmax(0,1fr) 265px' }}>
        <section
          className="card"
          style={{ display: 'flex', flexDirection: 'column', minHeight: 510 }}
        >
          <div className="panelhead">
            <div>
              <h2>Planner conversation</h2>
              <p className="sub">Transport state is shown in the top bar.</p>
            </div>
            <span className={`status ${wsOk ? 'blue' : 'warn'}`} id="chat-transport">
              {wsOk ? 'WS live' : 'REST fallback'}
            </span>
          </div>
          <div className="chatlog" ref={logRef} aria-live="polite">
            {messages.map((m) => (
              <div
                key={m.id}
                className={`message${m.role === 'You' ? ' user' : m.role === 'System' ? ' system' : ''}`}
              >
                <small>
                  {m.role} · {m.time}
                </small>
                {m.text}
              </div>
            ))}
          </div>
          <div className="pad">
            <div className="fieldgroup">
              <label htmlFor="requirement">Requirement number (optional)</label>
              <input
                className="input"
                id="requirement"
                type="number"
                placeholder="e.g. 42"
                value={requirement}
                onChange={(e) => setRequirement(e.target.value)}
              />
            </div>
            <div className="inlinebar" style={{ marginTop: 9 }}>
              <textarea
                id="chat-draft"
                className="input"
                rows={2}
                style={{ flex: 1, resize: 'vertical' }}
                placeholder="Message the planner — Use @agent or #task-id"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={onKeyDown}
              />
              <button className="btn primary" onClick={send}>
                Send
              </button>
            </div>
            <p className="tiny">Ctrl/Cmd + Enter to send.</p>
          </div>
        </section>

        <aside className="card pad">
          <h2 style={{ fontSize: 13, marginTop: 0 }}>Quick interventions</h2>
          <p className="sub">Use confirmation for consequential changes.</p>
          <div className="stack" style={{ marginTop: 14 }}>
            <button className="btn" onClick={() => quick('retry', 'Retry failed task', false)}>
              Retry failed task
            </button>
            <button className="btn" onClick={() => quick('pause', 'Pause planner', true)}>
              Pause planner
            </button>
            <button className="btn" onClick={() => quick('resume', 'Resume planner', false)}>
              Resume planner
            </button>
            <button className="btn" onClick={() => quick('approve', 'Approve plan', true)}>
              Approve plan
            </button>
            <button className="btn" onClick={() => quick('reject', 'Reject plan', true)}>
              Reject plan
            </button>
          </div>
          <div className="notice">
            Action requests carry the requirement number when provided and are recorded with a
            timestamp.
          </div>
        </aside>
      </div>
    </section>
  )
}
