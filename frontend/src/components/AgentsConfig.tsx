import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { AgentRole, AgentRow, AgentsResponse } from '../types'
import { useApp } from '../app-state'

const ROLE_LABELS: Record<AgentRole, string> = {
  planner: 'Planner',
  executor: 'Executor',
  orchestrator: 'Orchestrator',
  watchdog: 'Watchdog',
}

const CLAUDIO_ROLES = new Set<AgentRole>(['planner', 'executor'])

interface DraftAgent {
  id: string
  role: AgentRole
  base_url: string
  auth_token: string
  enabled: boolean
  request_timeout: number
}

function emptyDraft(defaults: AgentsResponse['defaults']): DraftAgent {
  return {
    id: '',
    role: 'executor',
    base_url: `http://${defaults.host}:${defaults.executor_port}/api/v1`,
    auth_token: '',
    enabled: true,
    request_timeout: defaults.request_timeout ?? 120,
  }
}

function toDraft(a: AgentRow): DraftAgent {
  return {
    id: a.id,
    role: a.role,
    base_url: a.base_url,
    // The token is never sent to the UI; an empty field means "leave unchanged".
    auth_token: '',
    enabled: a.enabled,
    request_timeout: a.request_timeout,
  }
}

/**
 * Agent registry editor (issue #19).
 *
 * Each agent binds a role to an address. Planner and executor are Claudio agent
 * instances, so their address must carry the `/api/v1` suffix — the backend
 * normalises it, and the hint below tells the operator before they save.
 */
export default function AgentsConfig() {
  const { toast } = useApp()
  const [agents, setAgents] = useState<AgentRow[]>([])
  const [roles, setRoles] = useState<AgentRole[]>([
    'planner',
    'executor',
    'orchestrator',
    'watchdog',
  ])
  const [defaults, setDefaults] = useState<AgentsResponse['defaults']>({
    host: '127.0.0.1',
    planner_port: 1072,
    executor_port: 1073,
    request_timeout: 120,
  })
  const [drafts, setDrafts] = useState<Record<string, DraftAgent>>({})
  const [creating, setCreating] = useState<DraftAgent>(emptyDraft(defaults))
  const [showAdd, setShowAdd] = useState(false)
  const [busy, setBusy] = useState(false)
  const [probes, setProbes] = useState<Record<string, string>>({})
  const [msg, setMsg] = useState('Agent changes are saved immediately.')

  const load = useCallback(async () => {
    const res = await api.listAgents()
    setAgents(res.agents)
    setRoles(res.roles)
    setDefaults(res.defaults)
    const map: Record<string, DraftAgent> = {}
    for (const a of res.agents) map[a.id] = toDraft(a)
    setDrafts(map)
    return res
  }, [])

  useEffect(() => {
    load().catch((e) => setMsg(`Failed to load agents: ${String(e)}`))
  }, [load])

  function patch(id: string, changes: Partial<DraftAgent>) {
    setDrafts((d) => ({ ...d, [id]: { ...d[id], ...changes } }))
  }

  async function save(a: DraftAgent) {
    setBusy(true)
    try {
      const body: Record<string, unknown> = {
        role: a.role,
        base_url: a.base_url,
        enabled: a.enabled,
        request_timeout: Number(a.request_timeout),
      }
      // Only send a token when the operator typed one; an empty field means
      // "keep what the backend already has".
      if (a.auth_token.trim()) body.auth_token = a.auth_token.trim()
      const res = await api.updateAgent(a.id, body as never)
      setAgents(res.agents)
      const map: Record<string, DraftAgent> = {}
      for (const row of res.agents) map[row.id] = toDraft(row)
      setDrafts(map)
      toast(`Agent "${a.id}" updated.`)
      setMsg(`Saved ${a.id} → ${a.base_url}`)
    } catch (e) {
      const m = e instanceof Error ? e.message : 'Update failed.'
      setMsg(`Failed: ${m}`)
      toast(m)
    } finally {
      setBusy(false)
    }
  }

  async function add() {
    if (!creating.id.trim()) {
      toast('Agent id is required.')
      return
    }
    setBusy(true)
    try {
      const body: Record<string, unknown> = {
        id: creating.id.trim().toLowerCase(),
        role: creating.role,
        base_url: creating.base_url,
        enabled: creating.enabled,
        request_timeout: Number(creating.request_timeout),
      }
      if (creating.auth_token.trim()) body.auth_token = creating.auth_token.trim()
      const res = await api.createAgent(body as never)
      setAgents(res.agents)
      const map: Record<string, DraftAgent> = {}
      for (const row of res.agents) map[row.id] = toDraft(row)
      setDrafts(map)
      setCreating(emptyDraft(defaults))
      setShowAdd(false)
      toast(`Agent "${creating.id}" added.`)
    } catch (e) {
      const m = e instanceof Error ? e.message : 'Add failed.'
      setMsg(`Failed: ${m}`)
      toast(m)
    } finally {
      setBusy(false)
    }
  }

  async function remove(a: AgentRow) {
    if (!window.confirm(`Remove agent "${a.id}" (${a.role})?`)) return
    setBusy(true)
    try {
      const res = await api.deleteAgent(a.id)
      setAgents(res.agents)
      const map: Record<string, DraftAgent> = {}
      for (const row of res.agents) map[row.id] = toDraft(row)
      setDrafts(map)
      toast(`Agent "${a.id}" removed.`)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Delete failed.')
    } finally {
      setBusy(false)
    }
  }

  async function probe(a: AgentRow) {
    setProbes((p) => ({ ...p, [a.id]: 'probing…' }))
    try {
      const r = await api.probeAgent(a.id)
      setProbes((p) => ({
        ...p,
        [a.id]: r.ok ? `online (${r.status})` : `unreachable — ${r.detail}`,
      }))
    } catch (e) {
      setProbes((p) => ({
        ...p,
        [a.id]: `probe failed — ${e instanceof Error ? e.message : 'error'}`,
      }))
    }
  }

  return (
    <section className="card" style={{ marginTop: 15 }}>
      <div className="panelhead">
        <div>
          <h2>Agent registry</h2>
          <p className="sub">
            Each role is bound to an address. Planner and executor are Claudio
            agent instances and must end with <code>/api/v1</code>.
          </p>
        </div>
        <div className="tools">
          <button
            className="btn"
            onClick={() => {
              setCreating(emptyDraft(defaults))
              setShowAdd((v) => !v)
            }}
          >
            {showAdd ? 'Cancel' : '+ Add agent'}
          </button>
        </div>
      </div>

      <div className="tablewrap">
        <table className="table">
          <thead>
            <tr>
              <th>Agent</th>
              <th>Role</th>
              <th>Address</th>
              <th>Enabled</th>
              <th>Timeout (s)</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((a) => {
              const d = drafts[a.id] ?? toDraft(a)
              return (
                <tr key={a.id}>
                  <td>
                    <b>{a.id}</b>
                  </td>
                  <td>
                    <select
                      className="input"
                      aria-label={`Role for ${a.id}`}
                      value={d.role}
                      onChange={(e) =>
                        patch(a.id, { role: e.target.value as AgentRole })
                      }
                    >
                      {roles.map((r) => (
                        <option key={r} value={r}>
                          {ROLE_LABELS[r] ?? r}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <input
                      className="input"
                      aria-label={`Address for ${a.id}`}
                      value={d.base_url}
                      placeholder="http://127.0.0.1:1072/api/v1"
                      onChange={(e) => patch(a.id, { base_url: e.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      style={{ width: 20, height: 20, accentColor: 'var(--green)' }}
                      aria-label={`Enabled for ${a.id}`}
                      checked={d.enabled}
                      onChange={(e) => patch(a.id, { enabled: e.target.checked })}
                    />
                  </td>
                  <td>
                    <input
                      className="input"
                      type="number"
                      min={1}
                      max={3600}
                      aria-label={`Timeout for ${a.id}`}
                      value={d.request_timeout}
                      onChange={(e) =>
                        patch(a.id, { request_timeout: Number(e.target.value) })
                      }
                    />
                  </td>
                  <td>
                    <div className="inlinebar">
                      <button
                        className="btn primary"
                        disabled={busy}
                        onClick={() => save(d)}
                      >
                        Save
                      </button>
                      <button className="btn" onClick={() => probe(a)}>
                        Probe
                      </button>
                      <button className="btn danger" disabled={busy} onClick={() => remove(a)}>
                        Remove
                      </button>
                    </div>
                    {probes[a.id] && (
                      <span className="tiny">{probes[a.id]}</span>
                    )}
                  </td>
                </tr>
              )
            })}
            {!agents.length && (
              <tr>
                <td colSpan={6} className="muted">
                  No agents configured.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {showAdd && (
        <div className="pad" style={{ marginTop: 13 }}>
          <h3 style={{ fontSize: 12 }}>New agent</h3>
          <div className="formrow">
            <label htmlFor="new-agent-id">Agent id</label>
            <input
              id="new-agent-id"
              className="input"
              value={creating.id}
              placeholder="executor-2"
              onChange={(e) => setCreating({ ...creating, id: e.target.value })}
            />
          </div>
          <div className="formrow">
            <label htmlFor="new-agent-role">Role</label>
            <select
              id="new-agent-role"
              className="input"
              value={creating.role}
              onChange={(e) =>
                setCreating({ ...creating, role: e.target.value as AgentRole })
              }
            >
              {roles.map((r) => (
                <option key={r} value={r}>
                  {ROLE_LABELS[r] ?? r}
                </option>
              ))}
            </select>
          </div>
          <div className="formrow">
            <label htmlFor="new-agent-url">
              Address
              {CLAUDIO_ROLES.has(creating.role) && (
                <small>Must include /api/v1</small>
              )}
            </label>
            <input
              id="new-agent-url"
              className="input"
              value={creating.base_url}
              onChange={(e) =>
                setCreating({ ...creating, base_url: e.target.value })
              }
            />
          </div>
          <div className="formrow">
            <label htmlFor="new-agent-token">
              Auth token
              <small>Must match the agent&apos;s CLAUDIO__SERVER__AUTH_TOKEN</small>
            </label>
            <input
              id="new-agent-token"
              className="input"
              type="password"
              value={creating.auth_token}
              onChange={(e) =>
                setCreating({ ...creating, auth_token: e.target.value })
              }
            />
          </div>
          <div className="inlinebar" style={{ marginTop: 12 }}>
            <button className="btn primary" disabled={busy} onClick={add}>
              Add agent
            </button>
          </div>
        </div>
      )}

      <div className="pad">
        <span className="tiny" aria-live="polite">
          {msg}
        </span>
      </div>
    </section>
  )
}
