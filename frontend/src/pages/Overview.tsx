import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { ProjectRow, WorkItemRow } from '../types'
import { useApp } from '../app-state'

const STAT_CLASS: Record<string, string> = {
  Running: '',
  'In review': 'warn',
  Queued: 'blue',
  Paused: 'err',
  Done: '',
}

function badge(s: string) {
  return (
    <span className={`status ${STAT_CLASS[s] ?? ''}`}>
      <i className="dot" />
      {s}
    </span>
  )
}

export default function OverviewPage({
  go,
}: {
  go: (id: 'overview' | 'projects' | 'activity' | 'cli' | 'config' | 'chat' | 'agents') => void
}) {
  const { status, refresh, events } = useApp()
  const [projects, setProjects] = useState<ProjectRow[]>([])
  const [tasks, setTasks] = useState<WorkItemRow[]>([])
  const [q, setQ] = useState('')
  const [filter, setFilter] = useState('All status')

  const load = async () => {
    const [p, t] = await Promise.all([api.listProjects(), api.getTasks()])
    setProjects(p.projects)
    setTasks(t.tasks)
  }

  useEffect(() => {
    load().catch(() => {})
  }, [])

  const filtered = useMemo(() => {
    const needle = q.toLowerCase()
    return projects.filter((p) => {
      const hay = `${p.name} ${p.url}`.toLowerCase()
      const matchesQ = hay.includes(needle)
      const matchesF = filter === 'All status' || p.status === filter
      return matchesQ && matchesF
    })
  }, [projects, q, filter])

  const running = tasks.filter((t) => t.status === 'in_progress').length
  const inReview = tasks.filter((t) => t.status === 'review').length
  const done = tasks.filter((t) => t.status === 'done').length
  const ready = tasks.filter((t) => t.status === 'pending' || t.status === 'planned').length

  return (
    <section className="page active" id="page-overview">
      <div className="heading">
        <div>
          <div className="eyebrow">Workspace overview</div>
          <h1>Control room</h1>
          <p className="sub">A calm view of your projects, agents and operational work.</p>
        </div>
        <div className="actions">
          <button
            className="btn"
            onClick={() => {
              refresh()
              load().catch(() => {})
            }}
          >
            Refresh
          </button>
          <button className="btn primary" onClick={() => go('cli')}>
            Open CLI runner →
          </button>
        </div>
      </div>

      <div className="grid4">
        <article className="card metric">
          <div className="metrichead">
            Active projects <span className="metricico">◇</span>
          </div>
          <strong>{projects.length}</strong>
          <span className="trend">Registered repositories</span>
        </article>
        <article className="card metric">
          <div className="metrichead">
            Running tasks <span className="metricico">↗</span>
          </div>
          <strong>{running}</strong>
          <span className="trend">In progress now</span>
        </article>
        <article className="card metric">
          <div className="metrichead">
            Awaiting review <span className="metricico">▯</span>
          </div>
          <strong>{inReview}</strong>
          <span className="trend">Needs attention</span>
        </article>
        <article className="card metric">
          <div className="metrichead">
            Active agents <span className="metricico">◉</span>
          </div>
          <strong>{status?.active_agents ?? 0}</strong>
          <span className="trend">Live heartbeat</span>
        </article>
      </div>

      <div className="layout">
        <div className="stack">
          <section className="card">
            <div className="panelhead">
              <div>
                <h2>Projects</h2>
                <p className="sub">Work registered across the workspace</p>
              </div>
              <div className="tools">
                <input
                  className="input"
                  aria-label="Search projects"
                  placeholder="Search projects"
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                />
                <select
                  className="input"
                  aria-label="Filter project status"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                >
                  <option>All status</option>
                  <option>Running</option>
                  <option>In review</option>
                  <option>Queued</option>
                  <option>Paused</option>
                </select>
              </div>
            </div>
            <div className="tablewrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Project</th>
                    <th>Status</th>
                    <th>Progress</th>
                    <th>Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.length ? (
                    filtered.map((p) => (
                      <tr key={p.id}>
                        <td>
                          <div className="projectname">
                            <span className="projecticon">📦</span>
                            <span>
                              <b>{p.name}</b>
                              <small>{p.url}</small>
                            </span>
                          </div>
                        </td>
                        <td>{badge(p.status)}</td>
                        <td>
                          <span className="progress">
                            <i style={{ width: `${p.progress}%` }} />
                          </span>{' '}
                          {p.progress}%
                        </td>
                        <td className="cellmuted">{p.updated ?? '—'}</td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={4} className="muted">
                        No projects match.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section className="card">
            <div className="panelhead">
              <div>
                <h2>Pipeline snapshot</h2>
                <p className="sub">Work-item counts by stage</p>
              </div>
              <button className="textlink" onClick={() => go('activity')}>
                Explore activity →
              </button>
            </div>
            <div className="pad">
              <div className="inlinebar">
                <span className="pill">Ready · {ready}</span>
                <span className="pill">In progress · {running}</span>
                <span className="pill">Review · {inReview}</span>
                <span className="pill">Done · {done}</span>
              </div>
            </div>
          </section>
        </div>

        <div className="stack">
          <section className="card">
            <div className="panelhead">
              <div>
                <h2>Recent activity</h2>
                <p className="sub">Live event stream</p>
              </div>
              <button className="textlink" onClick={() => go('activity')}>
                View all →
              </button>
            </div>
            {events.length ? (
              events.slice(0, 8).map((e) => (
                <div className="event" key={e.id}>
                  <span
                    className={`eventmark${
                      e.level === 'warning' || e.level === 'critical' ? ' warn' : ''
                    }`}
                  >
                    ●
                  </span>
                  <div className="eventtext">
                    <b>{e.source}</b> {e.message}
                    <time>{e.timestamp}</time>
                  </div>
                </div>
              ))
            ) : (
              <div className="event">
                <div className="eventtext">
                  <span className="muted">No events yet.</span>
                </div>
              </div>
            )}
          </section>

          <section className="card">
            <div className="panelhead">
              <div>
                <h2>System health</h2>
                <p className="sub">Live connection states</p>
              </div>
              <button className="textlink" onClick={() => go('agents')}>
                Details →
              </button>
            </div>
            <div className="healthrow">
              <span>Clams engine</span>
              <span className="status">
                <i className="dot" />
                {status ? 'Online' : 'No telemetry'}
              </span>
            </div>
            <div className="healthrow">
              <span>Planner socket</span>
              <span className={`status ${status && status.heartbeats?.planner?.alive ? '' : 'warn'}`}>
                {status?.heartbeats?.planner?.alive ? 'Alive' : 'No heartbeat'}
              </span>
            </div>
            <div className="healthrow">
              <span>Telemetry stream</span>
              <span className={`status ${status ? '' : 'err'}`}>
                {status ? 'Streaming' : 'Offline'}
              </span>
            </div>
          </section>
        </div>
      </div>
    </section>
  )
}
