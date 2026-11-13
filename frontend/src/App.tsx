import { useCallback, useEffect, useState } from 'react'
import { AppProvider, useApp } from './app-state'
import { api } from './api'
import OverviewPage from './pages/Overview'
import ProjectsPage from './pages/Projects'
import ActivityPage from './pages/Activity'
import CliRunnerPage from './pages/CliRunner'
import ConfigPage from './pages/Config'
import ChatPage from './pages/Chat'
import AgentsPage from './pages/Agents'

type PageId =
  | 'overview'
  | 'projects'
  | 'activity'
  | 'cli'
  | 'config'
  | 'chat'
  | 'agents'

const PAGE_TITLES: Record<PageId, string> = {
  overview: 'Overview',
  projects: 'Projects',
  activity: 'Activity & alerts',
  cli: 'CLI runner',
  config: 'Configuration',
  chat: 'Planner chat',
  agents: 'Agents & health',
}

const VALID: PageId[] = [
  'overview',
  'projects',
  'activity',
  'cli',
  'config',
  'chat',
  'agents',
]

function parseHash(): PageId {
  const raw = window.location.hash.replace(/^#/, '') as PageId
  return VALID.includes(raw) ? raw : 'overview'
}

function Shell() {
  const { status, sseOk, wsOk, refresh, toast } = useApp()
  const [page, setPage] = useState<PageId>(parseHash())
  const [menuOpen, setMenuOpen] = useState(false)
  const [busy, setBusy] = useState(false)

  const go = useCallback((id: PageId) => {
    if (window.location.hash !== `#${id}`) {
      window.location.hash = id
    }
    setPage(id)
    setMenuOpen(false)
  }, [])

  useEffect(() => {
    const onHash = () => setPage(parseHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  async function control(action: 'pause' | 'resume' | 'stop') {
    if (action === 'stop' && !window.confirm('Emergency stop the planner?')) return
    setBusy(true)
    try {
      await api.plannerAction({
        action,
        detail: action === 'stop' ? 'emergency stop' : '',
      })
      await refresh()
      toast(action === 'stop' ? 'Emergency stop issued.' : `${action} issued.`)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Control action failed.')
    } finally {
      setBusy(false)
    }
  }

  const stateLabel = status?.state ?? 'Idle'
  const stateClass =
    stateLabel === 'Error'
      ? 'status err'
      : stateLabel === 'Paused'
        ? 'status warn'
        : 'status'

  const nav = (id: PageId, ico: string, label: string) => (
    <button
      className={`nav${page === id ? ' active' : ''}`}
      onClick={() => go(id)}
      aria-current={page === id ? 'page' : undefined}
    >
      <span className="ico">{ico}</span>
      {label}
    </button>
  )

  return (
    <div className="shell">
      <aside className={`sidebar${menuOpen ? ' open' : ''}`} id="sidebar">
        <div className="brand">
          <span className="logo">◈</span> clams <span className="tiny">control</span>
        </div>
        <div className="workspace">
          <div className="avatar">CL</div>
          <div>
            <div className="worklabel">
              {status?.repository ? status.repository : 'Clams workspace'}
            </div>
            <div className="tiny">
              {sseOk ? 'SSE live' : 'SSE offline'} ·{' '}
              {wsOk ? 'WS live' : 'WS offline'}
            </div>
          </div>
        </div>
        <div className="navtitle">Workspace</div>
        {nav('overview', '▦', 'Overview')}
        {nav('projects', '◇', 'Projects')}
        {nav('activity', '▯', 'Activity & alerts')}
        <div className="navtitle">Operations</div>
        {nav('cli', '⌘', 'CLI runner')}
        {nav('config', '⚙', 'Configuration')}
        {nav('chat', '✧', 'Planner chat')}
        {nav('agents', '◉', 'Agents & health')}
        <div className="sidebottom">
          <div className="livebadge">
            <b>◈ System {stateLabel.toLowerCase()}</b>
            <span>
              {sseOk ? 'SSE live' : 'SSE offline'} ·{' '}
              {wsOk ? 'WS live' : 'WS offline'}
            </span>
          </div>
          <div className="navtitle">Control room</div>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div className="inlinebar">
            <button
              className="btn mobilemenu"
              aria-label="Open navigation"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((v) => !v)}
            >
              ☰
            </button>
            <div className="crumb">
              Clams / <b>{PAGE_TITLES[page]}</b>
            </div>
          </div>
          <div className="topright">
            <span className={stateClass}>
              <i className="dot" />
              {stateLabel}
            </span>
            <span className={`status ${sseOk ? '' : 'err'}`}>
              SSE: {sseOk ? 'connected' : 'offline'}
            </span>
            <span className={`status ${wsOk ? '' : 'err'}`}>
              WS: {wsOk ? 'connected' : 'offline'}
            </span>
            <button className="btn" disabled={busy} onClick={() => control('pause')}>
              Pause
            </button>
            <button className="btn" disabled={busy} onClick={() => control('resume')}>
              Resume
            </button>
            <button className="btn danger" disabled={busy} onClick={() => control('stop')}>
              Emergency stop
            </button>
          </div>
        </header>

        <div className="content">
          {page === 'overview' && <OverviewPage go={go} />}
          {page === 'projects' && <ProjectsPage />}
          {page === 'activity' && <ActivityPage />}
          {page === 'cli' && <CliRunnerPage />}
          {page === 'config' && <ConfigPage />}
          {page === 'chat' && <ChatPage />}
          {page === 'agents' && <AgentsPage />}
        </div>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <AppProvider>
      <Shell />
    </AppProvider>
  )
}
