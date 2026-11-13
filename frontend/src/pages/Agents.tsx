import { useEffect, useState } from 'react'
import { api } from '../api'
import type { StatusPayload } from '../types'
import { useApp } from '../app-state'

interface AgentRow {
  name: string
  alive: boolean
  age: string
  task: string
  queue: string
}

function fmtAge(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  if (seconds < 1) return 'just now'
  if (seconds < 60) return `${Math.round(seconds)}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  return `${Math.round(seconds / 3600)} hr ago`
}

export default function AgentsPage() {
  const { status, sseOk, wsOk, toast, reconnectWs } = useApp()
  const [live, setLive] = useState<StatusPayload | null>(status)

  useEffect(() => {
    setLive(status)
  }, [status])

  const heartbeats = live?.heartbeats ?? {}
  const names = Object.keys(heartbeats)
  const aliveCount = names.filter((n) => heartbeats[n]?.alive).length
  const unacked = live?.unacked_alerts ?? 0

  const rows: AgentRow[] = names.map((n) => {
    const hb = heartbeats[n] ?? { alive: false, age_seconds: null }
    return {
      name: n,
      alive: hb.alive,
      age: fmtAge(hb.age_seconds),
      task: hb.alive ? 'Active' : 'Idle',
      queue: hb.alive ? '1 active' : '—',
    }
  })

  function reconnectSse() {
    // Force a fresh telemetry read; the EventSource reconnects on its own.
    api
      .getStatus()
      .then((s) => {
        setLive(s)
        toast('Telemetry refreshed.')
      })
      .catch(() => toast('Telemetry refresh failed.'))
  }

  function reconnectWebsocket() {
    reconnectWs()
    toast('Reconnecting WebSocket…')
  }

  const stateLabel = live?.state ?? 'Idle'

  return (
    <section className="page active" id="page-agents">
      <div className="heading">
        <div>
          <div className="eyebrow">Operations</div>
          <h1>Agents &amp; health</h1>
          <p className="sub">Live heartbeat details and transport connection states.</p>
        </div>
        <div className="actions">
          <button className="btn" onClick={reconnectSse}>
            Refresh telemetry
          </button>
          <button className="btn" onClick={reconnectWebsocket}>
            Reconnect WebSocket
          </button>
        </div>
      </div>

      <div className="grid4">
        <article className="card metric">
          <div className="metrichead">Overall state</div>
          <strong style={{ fontSize: 20 }} id="agent-state-metric">
            {stateLabel}
          </strong>
          <span className="trend">Derived from work items</span>
        </article>
        <article className="card metric">
          <div className="metrichead">Alive heartbeats</div>
          <strong id="alive-count">
            {aliveCount} / {names.length}
          </strong>
          <span className="trend">Heartbeat age shown below</span>
        </article>
        <article className="card metric">
          <div className="metrichead">Unacknowledged alerts</div>
          <strong id="unacked-count">{unacked}</strong>
          <span className="trend">Diagnostic queue</span>
        </article>
        <article className="card metric">
          <div className="metrichead">Event transport</div>
          <strong style={{ fontSize: 17 }} id="transport-metric">
            {sseOk || wsOk ? 'Connected' : 'Offline'}
          </strong>
          <span className="trend">SSE + WebSocket</span>
        </article>
      </div>

      <section className="card">
        <div className="panelhead">
          <div>
            <h2>Agent heartbeat matrix</h2>
            <p className="sub">Alive status, last beat age and current assignment</p>
          </div>
          <span className="pill" id="last-beat">
            {live ? `Updated ${fmtAge(
              live.timestamp
                ? (Date.now() - new Date(live.timestamp).getTime()) / 1000
                : null,
            )}` : 'No data'}
          </span>
        </div>
        <div className="tablewrap">
          <table className="table">
            <thead>
              <tr>
                <th>Agent</th>
                <th>Status</th>
                <th>Last heartbeat</th>
                <th>Assignment</th>
                <th>Queue</th>
              </tr>
            </thead>
            <tbody>
              {rows.length ? (
                rows.map((a) => (
                  <tr key={a.name}>
                    <td>
                      <b>{a.name}</b>
                    </td>
                    <td>
                      <span className={`status ${a.alive ? '' : 'err'}`}>
                        <i className="dot" />
                        {a.alive ? 'Alive' : 'Stale / no heartbeat'}
                      </span>
                    </td>
                    <td>{a.age}</td>
                    <td>{a.task}</td>
                    <td className="cellmuted">{a.queue}</td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={5} className="muted">
                    No agent heartbeats received yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card" style={{ marginTop: 15 }}>
        <div className="panelhead">
          <div>
            <h2>Transport diagnostics</h2>
            <p className="sub">SSE telemetry and planner WebSocket status</p>
          </div>
        </div>
        <div className="settingrow">
          <span>
            Telemetry SSE
            <small>status, logs and retry behavior</small>
          </span>
          <span className={`status ${sseOk ? 'blue' : 'err'}`} id="sse-detail">
            {sseOk ? 'Connected' : 'Offline / reconnecting'}
          </span>
        </div>
        <div className="settingrow">
          <span>
            Planner WebSocket
            <small>chat reply channel</small>
          </span>
          <span className={`status ${wsOk ? 'blue' : 'err'}`} id="ws-detail">
            {wsOk ? 'Connected' : 'Offline / reconnecting'}
          </span>
        </div>
      </section>
    </section>
  )
}
