import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { ConfigResponse, EditableField } from '../types'
import { useApp } from '../app-state'

function getIn(obj: unknown, path: string): unknown {
  return path
    .split('.')
    .reduce<unknown>(
      (acc, k) =>
        acc != null && typeof acc === 'object'
          ? (acc as Record<string, unknown>)[k]
          : undefined,
      obj,
    )
}

export default function ConfigPage() {
  const { toast } = useApp()
  const [cfg, setCfg] = useState<ConfigResponse | null>(null)
  const [baseline, setBaseline] = useState<Record<string, unknown>>({})
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [showDiff, setShowDiff] = useState(false)
  const [msg, setMsg] = useState('Changes are local until you hot-apply them.')
  const [busy, setBusy] = useState(false)

  const load = async () => {
    const c = await api.getConfig()
    setCfg(c)
    const flat: Record<string, unknown> = {}
    for (const f of c.editable) {
      flat[f.path] = getIn(c.settings, f.path)
    }
    setBaseline(flat)
    setDraft({ ...flat })
  }

  useEffect(() => {
    load().catch((e) => setMsg(String(e)))
  }, [])

  const changedKeys = useMemo(() => {
    return Object.keys(draft).filter(
      (k) => JSON.stringify(draft[k]) !== JSON.stringify(baseline[k]),
    )
  }, [draft, baseline])

  function onField(f: EditableField, value: unknown) {
    setDraft((d) => ({ ...d, [f.path]: value }))
  }

  function reset() {
    setDraft({ ...baseline })
    setMsg('Draft reset to current live values.')
  }

  async function apply() {
    setBusy(true)
    try {
      const res = await api.putConfig(draft)
      const flat: Record<string, unknown> = {}
      for (const f of cfg?.editable ?? []) {
        flat[f.path] = getIn(res.settings, f.path)
      }
      setBaseline(flat)
      setDraft({ ...flat })
      setCfg((c) => (c ? { ...c, settings: res.settings } : c))
      setMsg(`Applied ${Object.keys(res.changed ?? {}).length} change(s), hot-applied live.`)
      toast('Configuration applied.')
    } catch (e) {
      const m = e instanceof Error ? e.message : 'Apply failed.'
      setMsg(`Failed: ${m}`)
      toast(m)
    } finally {
      setBusy(false)
    }
  }

  const diffText = changedKeys.length
    ? changedKeys
        .map(
          (k) =>
            `- ${k}: ${JSON.stringify(baseline[k])}\n\n+ ${k}: ${JSON.stringify(draft[k])}`,
        )
        .join('\n\n')
    : 'No configuration changes.'

  return (
    <section className="page active" id="page-config">
      <div className="heading">
        <div>
          <div className="eyebrow">System</div>
          <h1>Configuration</h1>
          <p className="sub">Review proposed edits before a hot-apply.</p>
        </div>
        <div className="actions">
          <span className="pill" id="dirty-pill">
            {changedKeys.length ? `${changedKeys.length} pending change(s)` : 'No pending edits'}
          </span>
          <button className="btn" onClick={reset}>
            Reset
          </button>
          <button className="btn primary" onClick={apply} disabled={!changedKeys.length || busy}>
            Hot-apply changes
          </button>
        </div>
      </div>

      <div className="layout" style={{ marginTop: 15 }}>
        <section className="card">
          <div className="panelhead">
            <div>
              <h2>Live configuration editor</h2>
              <p className="sub">Fields come from the backend editable schema</p>
            </div>
          </div>
          <div className="pad">
            {(cfg?.editable ?? []).map((f) => (
              <div className="formrow" key={f.path}>
                <label htmlFor={`cfg-${f.path}`}>
                  {f.label}
                  <small>{f.path}</small>
                </label>
                {f.type === 'boolean' ? (
                  <input
                    id={`cfg-${f.path}`}
                    type="checkbox"
                    style={{ width: 20, height: 20, accentColor: 'var(--green)' }}
                    checked={Boolean(draft[f.path])}
                    onChange={(e) => onField(f, e.target.checked)}
                  />
                ) : f.type === 'number' ? (
                  <input
                    id={`cfg-${f.path}`}
                    type="number"
                    className="input"
                    min={f.min}
                    max={f.max}
                    step={f.step}
                    value={Number(draft[f.path] ?? 0)}
                    onChange={(e) => onField(f, Number(e.target.value))}
                  />
                ) : (
                  <input
                    id={`cfg-${f.path}`}
                    type="text"
                    className="input"
                    value={String(draft[f.path] ?? '')}
                    onChange={(e) => onField(f, e.target.value)}
                  />
                )}
              </div>
            ))}
            <div className="inlinebar" style={{ marginTop: 15 }}>
              <button
                className="btn"
                onClick={() => setShowDiff((v) => !v)}
              >
                Review diff
              </button>
              <span className="tiny" id="config-msg" aria-live="polite">
                {msg}
              </span>
            </div>
            {showDiff && (
              <div style={{ marginTop: 13 }}>
                <h3 style={{ fontSize: 12 }}>Proposed configuration diff</h3>
                <pre className="diff" id="diff">
                  {diffText}
                </pre>
              </div>
            )}
          </div>
        </section>

        <section className="card">
          <div className="panelhead">
            <div>
              <h2>Raw JSON preview</h2>
              <p className="sub">Read-only view generated from form values</p>
            </div>
          </div>
          <div className="pad">
            <pre className="code" id="json-preview" style={{ minHeight: 250 }}>
              {JSON.stringify(draft, null, 2)}
            </pre>
            <p className="tiny">
              The backend validates the patch and rejects out-of-range values before applying.
            </p>
          </div>
        </section>
      </div>
    </section>
  )
}
