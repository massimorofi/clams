import { useEffect, useState } from 'react'
import { api } from '../api'
import type { AlertRow } from '../types'
import { useApp } from '../app-state'

function sevClass(sev: string): string {
  if (sev === 'critical') return 'err'
  if (sev === 'warning') return 'warn'
  return 'blue'
}

export default function ActivityPage() {
  const { toast } = useApp()
  const [alerts, setAlerts] = useState<AlertRow[]>([])
  const [severity, setSeverity] = useState('all')
  const [busy, setBusy] = useState(false)

  const load = async () => {
    const sev = severity === 'all' ? undefined : severity
    const r = await api.getAlerts(200, sev)
    setAlerts(r.alerts)
  }

  useEffect(() => {
    load().catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [severity])

  const unacked = alerts.filter((a) => !a.acknowledged).length

  async function ackOne(id: number) {
    setBusy(true)
    try {
      await api.ackAlert(id)
      await load()
      toast('Alert acknowledged.')
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not acknowledge alert.')
    } finally {
      setBusy(false)
    }
  }

  async function ackAll() {
    setBusy(true)
    try {
      const sev = severity === 'all' ? undefined : severity
      const r = await api.ackAllAlerts(sev)
      await load()
      toast(`${r.acknowledged ?? 0} alert(s) acknowledged.`)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not acknowledge alerts.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="page active" id="page-activity">
      <div className="heading">
        <div>
          <div className="eyebrow">Operations</div>
          <h1>Activity &amp; alerts</h1>
          <p className="sub">Severity-filtered diagnostic feed.</p>
        </div>
        <div className="actions">
          <select
            className="input"
            aria-label="Severity filter"
            value={severity}
            onChange={(e) => setSeverity(e.target.value)}
          >
            <option value="all">All severities</option>
            <option value="info">Info</option>
            <option value="warning">Warning</option>
            <option value="critical">Critical</option>
          </select>
          <button className="btn" onClick={ackAll} disabled={busy}>
            Acknowledge filtered
          </button>
        </div>
      </div>

      <section className="card">
        <div className="panelhead">
          <div>
            <h2>Alert &amp; diagnostic feed</h2>
            <p className="sub">
              <span id="alert-count">{unacked}</span> unacknowledged
            </p>
          </div>
          <span className="pill">{alerts.length} shown</span>
        </div>
        <div className="tablewrap">
          <table className="table">
            <thead>
              <tr>
                <th>Severity</th>
                <th>Time</th>
                <th>Source</th>
                <th>Message</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {alerts.length ? (
                alerts.map((a) => (
                  <tr key={a.id}>
                    <td>
                      <span className={`status ${sevClass(a.severity)}`}>
                        {a.acknowledged ? 'ACKED' : a.severity.toUpperCase()}
                      </span>
                    </td>
                    <td className="cellmuted">{a.created_at ?? '—'}</td>
                    <td>{a.source}</td>
                    <td>{a.message}</td>
                    <td>
                      {a.acknowledged ? (
                        <span className="cellmuted">Acknowledged</span>
                      ) : (
                        <button
                          className="textlink"
                          disabled={busy}
                          onClick={() => ackOne(a.id)}
                        >
                          Acknowledge
                        </button>
                      )}
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={5} className="muted">
                    No alerts at this severity.
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
