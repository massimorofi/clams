import { useCallback, useEffect, useState } from 'react'
import { api, subscribeEvents } from './api'
import type { BadgeState, StatusPayload } from './types'
import ProjectConfigTab from './tabs/ProjectConfig'
import CliRunnerTab from './tabs/CliRunner'
import MonitoringTab from './tabs/Monitoring'
import PlannerChatTab from './tabs/PlannerChat'

const TABS = [
  { id: 'project', label: 'Project & Config' },
  { id: 'cli', label: 'CLI Runner' },
  { id: 'monitor', label: 'Activity & Agents' },
  { id: 'chat', label: 'Planner Chat' },
] as const

type TabId = (typeof TABS)[number]['id']

const BADGE: Record<BadgeState, string> = {
  Idle: 'bg-ink-700 text-slate-300 border-ink-600',
  Executing: 'bg-blue-950 text-blue-300 border-blue-700',
  Paused: 'bg-amber-950 text-amber-300 border-amber-700',
  Error: 'bg-red-950 text-red-300 border-red-700',
}

export default function App() {
  const [tab, setTab] = useState<TabId>('project')
  const [status, setStatus] = useState<StatusPayload | null>(null)
  const [live, setLive] = useState(false)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.getStatus())
    } catch {
      /* header degrades gracefully if the API is briefly unavailable */
    }
  }, [])

  useEffect(() => {
    refresh()
    const off = subscribeEvents({
      'telemetry:update': (d) => {
        setStatus(d as StatusPayload)
        setLive(true)
      },
    })
    return off
  }, [refresh])

  async function control(action: 'pause' | 'resume') {
    setBusy(true)
    try {
      await api.plannerAction({ action })
      await refresh()
    } catch (e) {
      console.error(e)
    } finally {
      setBusy(false)
    }
  }

  async function emergencyStop() {
    setBusy(true)
    try {
      await api.plannerAction({ action: 'pause', detail: 'emergency stop' })
      await refresh()
    } catch (e) {
      console.error(e)
    } finally {
      setBusy(false)
    }
  }

  const state: BadgeState = status?.state ?? 'Idle'

  return (
    <div className="min-h-screen flex flex-col bg-ink-950">
      <header className="border-b border-ink-700 bg-ink-900 px-4 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2 mr-2">
            <span className="text-lg font-semibold tracking-tight text-white">Clams</span>
            <span className="text-xs text-slate-500 hidden sm:inline">Multi-Agent System</span>
          </div>

          <span
            className={`px-3 py-1 rounded-full text-xs font-medium border ${BADGE[state]}`}
            data-testid="state-badge"
          >
            {state}
          </span>

          <a
            href={`https://github.com/${status?.repository ?? ''}`}
            target="_blank"
            rel="noreferrer"
            className="text-xs text-accent hover:underline"
          >
            {status?.repository ?? '—'}
          </a>

          <span className="text-xs text-slate-400">
            <span className="text-slate-200 font-medium">{status?.active_agents ?? 0}</span> active
            agents
          </span>

          <span
            className={`text-xs ${live ? 'text-ok' : 'text-slate-500'}`}
            title={live ? 'receiving SSE telemetry' : 'no telemetry yet'}
          >
            ● {live ? 'live' : 'offline'}
          </span>

          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={() => control('pause')}
              disabled={busy}
              className="px-3 py-2 rounded-md bg-ink-700 hover:bg-ink-600 text-xs font-medium disabled:opacity-50"
            >
              Pause
            </button>
            <button
              onClick={() => control('resume')}
              disabled={busy}
              className="px-3 py-2 rounded-md bg-ink-700 hover:bg-ink-600 text-xs font-medium disabled:opacity-50"
            >
              Resume
            </button>
            <button
              onClick={emergencyStop}
              disabled={busy}
              className="px-3 py-2 rounded-md bg-red-900 hover:bg-red-800 text-red-100 text-xs font-semibold disabled:opacity-50"
            >
              Emergency Stop
            </button>
          </div>
        </div>

        <nav className="flex gap-1 mt-3 overflow-x-auto">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              aria-current={tab === t.id ? 'page' : undefined}
              className={`px-4 py-2 rounded-t-md text-sm whitespace-nowrap transition-colors ${
                tab === t.id
                  ? 'bg-ink-950 text-white border-b-2 border-accent'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>

      <main className="flex-1 p-4 overflow-auto">
        {tab === 'project' && <ProjectConfigTab />}
        {tab === 'cli' && <CliRunnerTab />}
        {tab === 'monitor' && <MonitoringTab />}
        {tab === 'chat' && <PlannerChatTab />}
      </main>
    </div>
  )
}
