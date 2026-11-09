import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, subscribeEvents } from '../api'
import type { AlertRow, StatusPayload, WorkItemRow } from '../types'

const ORDER = ['status:Ready', 'status:In Progress', 'status:Review', 'status:Done']

function fmtAge(s: number | null | undefined): string {
  if (s == null) return '—'
  if (s < 60) return `${Math.round(s)}s`
  if (s < 3600) return `${Math.round(s / 60)}m`
  return `${(s / 3600).toFixed(1)}h`
}

export default function MonitoringTab() {
  const [status, setStatus] = useState<StatusPayload | null>(null)
  const [tasks, setTasks] = useState<WorkItemRow[]>([])
  const [alerts, setAlerts] = useState<AlertRow[]>([])
  const [sev, setSev] = useState<string>('')

  const refresh = useCallback(async () => {
    const [s, t, a] = await Promise.all([
      api.getStatus(),
      api.getTasks(),
      api.getAlerts(200, sev || undefined),
    ])
    setStatus(s)
    setTasks(t.tasks ?? [])
    setAlerts(a.alerts ?? [])
  }, [sev])

  useEffect(() => {
    refresh().catch(() => {})
    const off = subscribeEvents({
      'telemetry:update': (d) => setStatus(d as StatusPayload),
      'logs:stream': (d) => setAlerts((prev) => [{ ...(d as AlertRow) }, ...prev].slice(0, 300)),
    })
    return off
  }, [refresh])

  const byStatus = useMemo(() => {
    const g: Record<string, WorkItemRow[]> = {}
    for (const t of tasks) {
      const k = String(t.status ?? 'unknown')
      ;(g[k] ??= []).push(t)
    }
    return g
  }, [tasks])

  const stages = useMemo(() => {
    const all = [...ORDER, ...Object.keys(byStatus)]
    const keys = Array.from(new Set(all)).filter((k) => k !== 'unknown')
    const total = tasks.length || 1
    let cum = 0
    return keys.map((k) => {
      const n = (byStatus[k] ?? []).length
      const start = (cum / total) * 100
      cum += n
      return { key: k, count: n, start, width: (n / total) * 100 }
    })
  }, [byStatus, tasks.length])

  return (
    <div className="space-y-4">
      <section className="bg-ink-900 border border-ink-700 rounded-lg p-4">
        <h2 className="text-sm font-semibold text-white mb-3">Plan Pipeline</h2>
        {tasks.length === 0 ? (
          <p className="text-xs text-slate-500">
            No work items tracked yet. Run <span className="font-mono">clams intake</span> from the
            CLI tab to pull Ready issues.
          </p>
        ) : (
          <>
            <div className="flex h-3 w-full rounded-full overflow-hidden bg-ink-800 mb-2">
              {stages.map((s) => (
                <div
                  key={s.key}
                  title={`${s.key}: ${s.count}`}
                  style={{ width: `${s.width}%` }}
                  className={
                    s.key === 'status:Done'
                      ? 'bg-ok'
                      : s.key === 'status:In Progress'
                        ? 'bg-accent'
                        : s.key === 'status:Review'
                          ? 'bg-warn'
                          : 'bg-ink-600'
                  }
                />
              ))}
            </div>
            <div className="flex flex-wrap gap-4 text-xs text-slate-400">
              {stages.map((s) => (
                <span key={s.key}>
                  {s.key.replace('status:', '')}{' '}
                  <span className="text-slate-200 font-medium">{s.count}</span>
                </span>
              ))}
            </div>
          </>
        )}
      </section>

      <section className="bg-ink-900 border border-ink-700 rounded-lg p-4">
        <h2 className="text-sm font-semibold text-white mb-3">Agent Matrix</h2>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {Object.entries(status?.heartbeats ?? {}).map(([name, hb]) => (
            <div key={name} className="bg-ink-800 border border-ink-700 rounded-lg p-3">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-slate-200">{name}</span>
                <span
                  className={`w-2.5 h-2.5 rounded-full ${hb.alive ? 'bg-ok' : 'bg-err'}`}
                  aria-label={hb.alive ? 'alive' : 'dead'}
                />
              </div>
              <dl className="mt-2 text-xs text-slate-400 space-y-1">
                <div className="flex justify-between">
                  <dt>state</dt>
                  <dd className={hb.alive ? 'text-ok' : 'text-err'}>
                    {hb.alive ? 'alive' : 'no heartbeat'}
                  </dd>
                </div>
                <div className="flex justify-between">
                  <dt>last beat</dt>
                  <dd className="font-mono">{fmtAge(hb.age_seconds)}</dd>
                </div>
              </dl>
            </div>
          ))}
        </div>
      </section>

      <section className="bg-ink-900 border border-ink-700 rounded-lg p-4">
        <div className="flex items-center gap-2 mb-3">
          <h2 className="text-sm font-semibold text-white">Alert &amp; Diagnostic Feed</h2>
          <select
            value={sev}
            onChange={(e) => setSev(e.target.value)}
            className="ml-auto bg-ink-800 border border-ink-600 rounded px-2 text-xs"
          >
            <option value="">All severities</option>
            <option value="info">INFO</option>
            <option value="warning">WARNING</option>
            <option value="critical">ERROR / CRITICAL</option>
          </select>
          <button
            onClick={() => refresh()}
            className="px-3 py-2 rounded-md bg-ink-700 hover:bg-ink-600 text-xs"
          >
            Refresh
          </button>
        </div>
        <div className="overflow-auto max-h-[46vh]">
          <table className="w-full text-xs">
            <thead className="text-slate-500 text-left sticky top-0 bg-ink-900">
              <tr>
                <th className="py-2 pr-3">Severity</th>
                <th className="py-2 pr-3">Source</th>
                <th className="py-2">Message</th>
              </tr>
            </thead>
            <tbody>
              {alerts.length === 0 ? (
                <tr>
                  <td colSpan={3} className="py-4 text-slate-500">
                    No alerts.
                  </td>
                </tr>
              ) : (
                alerts.map((a, i) => (
                  <tr key={a.id ?? i} className="border-t border-ink-800 align-top">
                    <td className="py-2 pr-3">
                      <span
                        className={
                          a.severity === 'critical'
                            ? 'text-err'
                            : a.severity === 'warning'
                              ? 'text-warn'
                              : 'text-slate-400'
                        }
                      >
                        {String(a.severity ?? 'info').toUpperCase()}
                      </span>
                    </td>
                    <td className="py-2 pr-3 text-slate-400">{a.source ?? '—'}</td>
                    <td className="py-2 text-slate-300">{a.message}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}
