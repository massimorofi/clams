import { useState } from 'react'
import { api } from '../api'
import { useApp } from '../app-state'

interface CmdDef {
  value: string
  label: string
  params: { name: string; label: string; type: 'number' | 'text'; placeholder: string }[]
}

const COMMANDS: CmdDef[] = [
  { value: 'status', label: 'clams status', params: [] },
  { value: 'intake', label: 'clams intake', params: [] },
  {
    value: 'plan',
    label: 'clams plan',
    params: [
      { name: 'requirement', label: 'Requirement number', type: 'number', placeholder: '42' },
    ],
  },
  { value: 'dispatch', label: 'clams dispatch', params: [] },
  { value: 'execute', label: 'clams execute', params: [] },
  { value: 'worker', label: 'clams worker', params: [] },
  {
    value: 'review',
    label: 'clams review',
    params: [
      { name: 'requirement', label: 'Requirement number', type: 'number', placeholder: '42' },
    ],
  },
  { value: 'doctor', label: 'clams doctor', params: [] },
  {
    value: 'healthcheck',
    label: 'clams healthcheck',
    params: [
      { name: 'service', label: 'Service', type: 'text', placeholder: 'orchestrator' },
      { name: 'max_age', label: 'Max age seconds', type: 'number', placeholder: '90' },
    ],
  },
  { value: 'init-db', label: 'clams init-db', params: [] },
]

export default function CliRunnerPage() {
  const { toast } = useApp()
  const [command, setCommand] = useState('status')
  const [values, setValues] = useState<Record<string, string>>({})
  const [output, setOutput] = useState('')
  const [meta, setMeta] = useState('No command run yet.')
  const [search, setSearch] = useState('')
  const [busy, setBusy] = useState(false)

  const def = COMMANDS.find((c) => c.value === command) ?? COMMANDS[0]

  function onCommandChange(v: string) {
    setCommand(v)
    setValues({})
  }

  async function run() {
    setBusy(true)
    const args = def.params
      .map((p) => (values[p.name] ?? '').trim())
      .filter((x) => x.length > 0)
    try {
      const r = await api.cliExec(command, args)
      const text = [r.stdout, r.stderr].filter(Boolean).join('\n')
      setOutput(text || '(no output)')
      setMeta(
        `Last run: ${new Date().toLocaleTimeString()} · exit ${r.exit_code} · ${
          r.ok ? 'ok' : 'failed'
        }`,
      )
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Command failed.'
      setOutput(`Error: ${msg}`)
      setMeta(`Last run: ${new Date().toLocaleTimeString()} · error`)
      toast(msg)
    } finally {
      setBusy(false)
    }
  }

  function copy() {
    navigator.clipboard
      ?.writeText(output)
      .then(() => toast('Terminal text copied.'))
      .catch(() => toast('Clipboard unavailable in this browser context.'))
  }

  function exportLog() {
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([output], { type: 'text/plain' }))
    a.download = 'clams-run.log'
    a.click()
    setTimeout(() => URL.revokeObjectURL(a.href), 1000)
  }

  const matchCount = search
    ? output.split('\n').filter((l) => l.toLowerCase().includes(search.toLowerCase())).length
    : 0

  return (
    <section className="page active" id="page-cli">
      <div className="heading">
        <div>
          <div className="eyebrow">Operations</div>
          <h1>CLI runner</h1>
          <p className="sub">Select a supported command and inspect the terminal session.</p>
        </div>
      </div>

      <div className="layout" style={{ gridTemplateColumns: 'minmax(260px,.75fr) minmax(0,1.5fr)' }}>
        <section className="card pad">
          <div className="fieldgroup">
            <label htmlFor="command">Command</label>
            <select
              className="input"
              id="command"
              value={command}
              onChange={(e) => onCommandChange(e.target.value)}
            >
              {COMMANDS.map((c) => (
                <option key={c.value} value={c.value}>
                  {c.label}
                </option>
              ))}
            </select>
          </div>
          <div style={{ marginTop: 13 }}>
            {def.params.length ? (
              def.params.map((p) => (
                <div className="fieldgroup" key={p.name} style={{ marginTop: 10 }}>
                  <label htmlFor={`arg-${p.name}`}>{p.label}</label>
                  <input
                    className="input"
                    id={`arg-${p.name}`}
                    type={p.type}
                    placeholder={p.placeholder}
                    value={values[p.name] ?? ''}
                    onChange={(e) => setValues((v) => ({ ...v, [p.name]: e.target.value }))}
                  />
                </div>
              ))
            ) : (
              <p className="tiny">No parameters for this command.</p>
            )}
          </div>
          <button
            className="btn primary"
            style={{ marginTop: 15, width: '100%' }}
            onClick={run}
            disabled={busy}
          >
            Run command
          </button>
          <p className="tiny">
            The backend enforces a command allowlist, parameter validation, timeouts and output
            limits.
          </p>
        </section>

        <section className="card">
          <div className="panelhead">
            <div>
              <h2>Terminal output</h2>
              <p className="sub" id="terminal-meta">
                {search ? `${matchCount} matching line(s)` : meta}
              </p>
            </div>
            <div className="tools">
              <input
                className="input"
                placeholder="Search output"
                aria-label="Search terminal output"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <button className="btn" onClick={copy}>
                Copy
              </button>
              <button className="btn" onClick={exportLog}>
                Export
              </button>
            </div>
          </div>
          <pre className="code" id="terminal">
            {output || '$ ready'}
          </pre>
        </section>
      </div>
    </section>
  )
}
