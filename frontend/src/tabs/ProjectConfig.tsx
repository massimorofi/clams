import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { ConfigResponse, ProjectInfo } from '../types'

function getIn(obj: any, path: string): any {
  return path.split('.').reduce((acc, k) => (acc == null ? undefined : acc[k]), obj)
}

function setIn(obj: any, path: string, value: any): any {
  const parts = path.split('.')
  const root = { ...obj }
  let cur = root
  for (const p of parts.slice(0, -1)) {
    cur[p] = Array.isArray(cur[p]) ? [...cur[p]] : { ...(cur[p] ?? {}) }
    cur = cur[p]
  }
  cur[parts[parts.length - 1]] = value
  return root
}

export default function ProjectConfigTab() {
  const [project, setProject] = useState<ProjectInfo | null>(null)
  const [cfg, setCfg] = useState<ConfigResponse | null>(null)
  const [draft, setDraft] = useState<Record<string, any>>({})
  const [mode, setMode] = useState<'form' | 'raw'>('form')
  const [raw, setRaw] = useState('')
  const [rawError, setRawError] = useState('')
  const [showDiff, setShowDiff] = useState(false)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  const load = async () => {
    const [p, c] = await Promise.all([api.getProject(), api.getConfig()])
    setProject(p)
    setCfg(c)
    setDraft(JSON.parse(JSON.stringify(c.settings ?? {})))
    setRaw(JSON.stringify(c.settings ?? {}, null, 2))
  }

  useEffect(() => {
    load().catch((e) => setMsg(String(e)))
  }, [])

  const changedKeys = useMemo(() => {
    const before = cfg?.settings ?? {}
    const keys: string[] = []
    for (const f of cfg?.editable ?? []) {
      if (JSON.stringify(getIn(before, f.path)) !== JSON.stringify(getIn(draft, f.path))) {
        keys.push(f.path)
      }
    }
    return keys
  }, [cfg, draft])

  function onRawChange(text: string) {
    setRaw(text)
    try {
      const parsed = JSON.parse(text)
      setDraft(parsed)
      setRawError('')
    } catch (e: any) {
      setRawError(`invalid JSON: ${e.message}`)
    }
  }

  async function apply() {
    setBusy(true)
    setMsg('')
    try {
      // Send only the flat dotted paths the backend declared editable.
      const values: Record<string, unknown> = {}
      for (const f of cfg?.editable ?? []) {
        values[f.path] = getIn(draft, f.path)
      }
      const res = await api.putConfig(values)
      setCfg((c) => (c ? { ...c, settings: res.settings } : c))
      setDraft(JSON.parse(JSON.stringify(res.settings ?? {})))
      setRaw(JSON.stringify(res.settings ?? {}, null, 2))
      setMsg(`applied ${Object.keys(res.changed ?? {}).length} change(s), hot-applied live`)
    } catch (e: any) {
      setMsg(`failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  async function pull() {
    setBusy(true)
    try {
      const r: any = await api.pullProject()
      setMsg(r.ok ? `pulled, now at ${r.commit}` : `pull failed: ${r.stderr}`)
      setProject(await api.getProject())
    } catch (e: any) {
      setMsg(`pull error: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <section className="bg-ink-900 border border-ink-700 rounded-lg p-4">
        <h2 className="text-sm font-semibold text-white mb-3">GitHub Integration</h2>
        <dl className="grid grid-cols-3 gap-y-2 text-xs">
          <dt className="text-slate-500">Repository</dt>
          <dd className="col-span-2 text-slate-200">{project?.full_name ?? '—'}</dd>
          <dt className="text-slate-500">Branch</dt>
          <dd className="col-span-2 text-slate-200">{project?.branch ?? '—'}</dd>
          <dt className="text-slate-500">Commit</dt>
          <dd className="col-span-2 font-mono text-slate-200">{project?.commit ?? '—'}</dd>
          <dt className="text-slate-500">Worktree</dt>
          <dd className="col-span-2 font-mono text-slate-200">{project?.worktree_dir ?? '—'}</dd>
          <dt className="text-slate-500">Tree state</dt>
          <dd className="col-span-2">
            <span className={project?.clean ? 'text-ok' : 'text-warn'}>
              {project?.clean ? 'clean' : 'dirty (uncommitted changes)'}
            </span>
          </dd>
        </dl>
        <div className="flex gap-2 mt-4">
          <button
            onClick={pull}
            disabled={busy}
            className="px-3 py-2 rounded-md bg-ink-700 hover:bg-ink-600 text-xs font-medium disabled:opacity-50"
          >
            Pull updates
          </button>
          <button
            onClick={load}
            className="px-3 py-2 rounded-md bg-ink-700 hover:bg-ink-600 text-xs font-medium"
          >
            Sync context
          </button>
        </div>
      </section>

      <section className="bg-ink-900 border border-ink-700 rounded-lg p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-white">Live Configuration Editor</h2>
          <div className="flex gap-1">
            {(['form', 'raw'] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`px-3 py-2 rounded-md text-xs font-medium ${
                  mode === m ? 'bg-ink-600 text-white' : 'bg-ink-800 text-slate-400'
                }`}
              >
                {m === 'form' ? 'Form' : 'JSON'}
              </button>
            ))}
          </div>
        </div>

        {mode === 'form' ? (
          <div className="space-y-3 max-h-[52vh] overflow-auto pr-1">
            {(cfg?.editable ?? []).map((f) => (
              <div key={f.path} className="flex items-center justify-between gap-3">
                <label className="text-xs text-slate-400 flex-1">{f.label}</label>
                {f.type === 'boolean' ? (
                  <input
                    type="checkbox"
                    checked={Boolean(getIn(draft, f.path))}
                    onChange={(e) => setDraft(setIn(draft, f.path, e.target.checked))}
                    className="h-5 w-5 accent-blue-500"
                  />
                ) : f.type === 'number' ? (
                  <div className="flex items-center gap-2 w-56">
                    <input
                      type="range"
                      min={f.min}
                      max={f.max}
                      step={f.step}
                      value={Number(getIn(draft, f.path) ?? 0)}
                      onChange={(e) => setDraft(setIn(draft, f.path, Number(e.target.value)))}
                      className="flex-1 accent-blue-500"
                    />
                    <input
                      type="number"
                      value={Number(getIn(draft, f.path) ?? 0)}
                      onChange={(e) => setDraft(setIn(draft, f.path, Number(e.target.value)))}
                      className="w-20 bg-ink-800 border border-ink-600 rounded px-2 text-xs"
                    />
                  </div>
                ) : (
                  <input
                    type="text"
                    value={String(getIn(draft, f.path) ?? '')}
                    onChange={(e) => setDraft(setIn(draft, f.path, e.target.value))}
                    className="w-56 bg-ink-800 border border-ink-600 rounded px-2 text-xs"
                  />
                )}
              </div>
            ))}
          </div>
        ) : (
          <div>
            <textarea
              value={raw}
              onChange={(e) => onRawChange(e.target.value)}
              spellCheck={false}
              className="term w-full h-[52vh] bg-ink-950 border border-ink-600 rounded p-3 text-slate-200"
            />
            {rawError && <p className="text-xs text-err mt-2">{rawError}</p>}
          </div>
        )}

        <div className="flex items-center gap-2 mt-4">
          <button
            onClick={() => setShowDiff((s) => !s)}
            className="px-3 py-2 rounded-md bg-ink-700 hover:bg-ink-600 text-xs font-medium"
          >
            {showDiff ? 'Hide diff' : 'Review diff'} ({changedKeys.length})
          </button>
          <button
            onClick={apply}
            disabled={busy || changedKeys.length === 0 || !!rawError}
            className="px-4 py-2 rounded-md bg-blue-700 hover:bg-blue-600 text-white text-xs font-semibold disabled:opacity-40"
          >
            Hot-Apply
          </button>
          {msg && <span className="text-xs text-slate-300">{msg}</span>}
        </div>

        {showDiff && (
          <div className="mt-3 bg-ink-950 border border-ink-700 rounded p-3 max-h-56 overflow-auto">
            {changedKeys.length === 0 ? (
              <p className="text-xs text-slate-500">No changes.</p>
            ) : (
              changedKeys.map((k) => (
                <div key={k} className="text-xs font-mono mb-1">
                  <span className="text-slate-500">{k}: </span>
                  <span className="text-err line-through">
                    {JSON.stringify(getIn(cfg?.settings ?? {}, k))}
                  </span>{' '}
                  <span className="text-ok">
                    {JSON.stringify(getIn(draft, k))}
                  </span>
                </div>
              ))
            )}
          </div>
        )}
      </section>
    </div>
  )
}
