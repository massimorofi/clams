import { useEffect, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { api } from '../api'

interface CommandSpec {
  id: string
  label: string
  args: { name: string; label: string; type: 'text' | 'number' | 'flag'; placeholder?: string }[]
}

const COMMANDS: CommandSpec[] = [
  { id: 'status', label: 'clams status', args: [] },
  { id: 'intake', label: 'clams intake', args: [] },
  {
    id: 'plan',
    label: 'clams plan',
    args: [{ name: 'requirement', label: 'Requirement #', type: 'number', placeholder: '42' }],
  },
  { id: 'dispatch', label: 'clams dispatch', args: [] },
  { id: 'execute', label: 'clams execute', args: [] },
  { id: 'worker', label: 'clams worker (daemon)', args: [] },
  {
    id: 'review',
    label: 'clams review',
    args: [{ name: 'requirement', label: 'Requirement #', type: 'number', placeholder: '42' }],
  },
  { id: 'doctor', label: 'clams doctor', args: [] },
  {
    id: 'healthcheck',
    label: 'clams healthcheck',
    args: [
      { name: 'service', label: 'Service', type: 'text', placeholder: 'orchestrator' },
      { name: 'max-age', label: 'Max age (s)', type: 'number', placeholder: '90' },
    ],
  },
  { id: 'init-db', label: 'clams init-db', args: [] },
]

export default function CliRunnerTab() {
  const [cmdId, setCmdId] = useState('status')
  const [values, setValues] = useState<Record<string, string>>({})
  const [running, setRunning] = useState(false)
  const [search, setSearch] = useState('')
  const termRef = useRef<HTMLDivElement>(null)
  const term = useRef<Terminal | null>(null)
  const fit = useRef<FitAddon | null>(null)
  const raw = useRef('')

  useEffect(() => {
    if (!termRef.current) return
    const t = new Terminal({
      convertEol: true,
      theme: {
        background: '#0a0e14',
        foreground: '#d7dee8',
        cursor: '#4ea1ff',
      },
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: 12.5,
      scrollback: 5000,
    })
    const f = new FitAddon()
    t.loadAddon(f)
    t.open(termRef.current)
    f.fit()
    term.current = t
    fit.current = f
    const onResize = () => f.fit()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      t.dispose()
    }
  }, [])

  const spec = COMMANDS.find((c) => c.id === cmdId) ?? COMMANDS[0]

  async function run() {
    if (running) return
    setRunning(true)
    term.current?.clear()
    raw.current = ''
    const args: string[] = []
    for (const a of spec.args) {
      const v = (values[`${spec.id}.${a.name}`] ?? '').trim()
      if (v) args.push(v)
    }
    term.current?.writeln(`$ clams ${[spec.id, ...args].join(' ')}`)
    try {
      const r = await api.cliExec(spec.id, args)
      const out = [r.stdout, r.stderr].filter(Boolean).join('\n')
      raw.current = out
      term.current?.writeln(out || '(no output)')
      term.current?.writeln(
        `\n${r.ok ? '✓ exit 0' : `✗ exit ${r.exit_code}`}`,
      )
    } catch (e: any) {
      raw.current = String(e)
      term.current?.writeln(`error: ${e.message}`)
    } finally {
      setRunning(false)
    }
  }

  function exportLog() {
    const blob = new Blob([raw.current], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `clams-${spec.id}-${Date.now()}.log`
    a.click()
    URL.revokeObjectURL(url)
  }

  function copyOut() {
    navigator.clipboard?.writeText(raw.current)
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
      <section className="bg-ink-900 border border-ink-700 rounded-lg p-4">
        <h2 className="text-sm font-semibold text-white mb-3">Command Selector</h2>
        <div className="grid grid-cols-2 gap-2 mb-4">
          {COMMANDS.map((c) => (
            <button
              key={c.id}
              onClick={() => setCmdId(c.id)}
              className={`px-2 py-2 rounded-md text-xs font-medium ${
                cmdId === c.id
                  ? 'bg-blue-800 text-white'
                  : 'bg-ink-800 text-slate-400 hover:bg-ink-700'
              }`}
            >
              {c.label}
            </button>
          ))}
        </div>

        <h3 className="text-xs font-semibold text-slate-400 mb-2">Parameters</h3>
        {spec.args.length === 0 ? (
          <p className="text-xs text-slate-500">This command takes no parameters.</p>
        ) : (
          <div className="space-y-3">
            {spec.args.map((a) => (
              <div key={a.name}>
                <label className="block text-xs text-slate-400 mb-1">{a.label}</label>
                <input
                  type={a.type === 'number' ? 'number' : 'text'}
                  placeholder={a.placeholder}
                  value={values[`${spec.id}.${a.name}`] ?? ''}
                  onChange={(e) =>
                    setValues((v) => ({ ...v, [`${spec.id}.${a.name}`]: e.target.value }))
                  }
                  className="w-full bg-ink-800 border border-ink-600 rounded px-3 text-xs"
                />
              </div>
            ))}
          </div>
        )}

        <button
          onClick={run}
          disabled={running}
          className="mt-4 w-full px-4 py-2 rounded-md bg-blue-700 hover:bg-blue-600 text-white text-sm font-semibold disabled:opacity-40"
        >
          {running ? 'Running…' : 'Run'}
        </button>
      </section>

      <section className="bg-ink-900 border border-ink-700 rounded-lg p-3 flex flex-col min-h-[60vh]">
        <div className="flex items-center gap-2 mb-2">
          <h2 className="text-sm font-semibold text-white">Terminal Output</h2>
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search output…"
            className="ml-auto w-52 bg-ink-800 border border-ink-600 rounded px-2 text-xs"
          />
          <button
            onClick={copyOut}
            className="px-3 py-2 rounded-md bg-ink-700 hover:bg-ink-600 text-xs"
          >
            Copy
          </button>
          <button
            onClick={exportLog}
            className="px-3 py-2 rounded-md bg-ink-700 hover:bg-ink-600 text-xs"
          >
            Export
          </button>
        </div>
        <div ref={termRef} className="flex-1 rounded overflow-hidden bg-ink-950" />
        {search && (
          <p className="text-xs text-slate-500 mt-2">
            {raw.current.split('\n').filter((l) => l.includes(search)).length} matching line(s)
          </p>
        )}
      </section>
    </div>
  )
}
