import { useEffect, useState } from 'react'
import { api } from '../api'
import type { ProjectRow } from '../types'
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

export default function ProjectsPage() {
  const { toast } = useApp()
  const [projects, setProjects] = useState<ProjectRow[]>([])
  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [actionBusy, setActionBusy] = useState<number | null>(null)

  const load = async () => {
    const r = await api.listProjects()
    setProjects(r.projects)
  }

  useEffect(() => {
    load().catch(() => {})
  }, [])

  async function doSync(p: ProjectRow) {
    setActionBusy(p.id)
    try {
      await api.syncProject(p.id)
      await load()
      toast(`Synced ${p.name}.`)
    } catch (err) {
      toast(err instanceof Error ? err.message : 'Sync failed.')
    } finally {
      setActionBusy(null)
    }
  }

  async function togglePause(p: ProjectRow) {
    setActionBusy(p.id)
    try {
      if (p.paused) {
        await api.resumeProject(p.id)
      } else {
        await api.pauseProject(p.id)
      }
      await load()
    } catch (err) {
      toast(err instanceof Error ? err.message : 'Action failed.')
    } finally {
      setActionBusy(null)
    }
  }

  function openForm() {
    setShowForm(true)
  }

  function cancel() {
    setShowForm(false)
    setName('')
    setUrl('')
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    try {
      await api.createProject(name, url)
      await load()
      cancel()
      toast('Project registered.')
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Could not add project.'
      toast(msg)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="page active" id="page-projects">
      <div className="heading">
        <div>
          <div className="eyebrow">Workspace</div>
          <h1>Projects</h1>
          <p className="sub">Add a GitHub repository to begin, then manage its Clams configuration.</p>
        </div>
        <div className="actions">
          <button className="btn primary" onClick={openForm}>
            + Add project
          </button>
        </div>
      </div>

      <section className="card">
        <div className="panelhead">
          <div>
            <h2>Project workspace</h2>
            <p className="sub">Start with a project name and GitHub repository URL.</p>
          </div>
          <span className="pill">{projects.length} registered</span>
        </div>

        {showForm && (
          <div
            className="pad"
            style={{ borderTop: '1px solid var(--line)', background: '#fbfdfb' }}
          >
            <form onSubmit={submit}>
              <div className="layout" style={{ gridTemplateColumns: '1fr 1.35fr', alignItems: 'end' }}>
                <div className="fieldgroup">
                  <label htmlFor="new-project-name">Project name</label>
                  <input
                    className="input"
                    id="new-project-name"
                    name="name"
                    placeholder="e.g. Website refresh"
                    required
                    maxLength={80}
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                  />
                </div>
                <div className="fieldgroup">
                  <label htmlFor="new-project-url">GitHub repository URL</label>
                  <input
                    className="input"
                    id="new-project-url"
                    name="url"
                    type="url"
                    placeholder="https://github.com/owner/repository"
                    required
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                  />
                </div>
              </div>
              <div className="inlinebar" style={{ marginTop: 12 }}>
                <button className="btn primary" type="submit" disabled={busy}>
                  Add project
                </button>
                <button className="btn" type="button" onClick={cancel}>
                  Cancel
                </button>
              </div>
            </form>
          </div>
        )}

        <div className="tablewrap">
          <table className="table">
            <thead>
              <tr>
                <th>Project</th>
                <th>Status</th>
                <th>Progress</th>
                <th>Working tree</th>
                <th>Updated</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {projects.length ? (
                projects.map((p) => (
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
                    <td className="cellmuted">
                      {p.error ? (
                        <span className="err" title={p.error}>
                          ⚠ {p.error}
                        </span>
                      ) : p.worktree_path ? (
                        <code style={{ fontSize: 12 }}>{p.worktree_path}</code>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className="cellmuted">{p.updated ?? '—'}</td>
                    <td>
                      <div className="inlinebar" style={{ gap: 6 }}>
                        <button
                          className="btn"
                          type="button"
                          disabled={actionBusy === p.id}
                          onClick={() => doSync(p)}
                        >
                          Sync
                        </button>
                        <button
                          className="btn"
                          type="button"
                          disabled={actionBusy === p.id}
                          onClick={() => togglePause(p)}
                        >
                          {p.paused ? 'Resume' : 'Pause'}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={6} className="muted">
                    No projects yet. Add one to get started.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </section>
  )
}
