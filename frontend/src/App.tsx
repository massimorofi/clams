import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, getToken, setToken } from './api';
import { useCommand } from './commands';
import { useEventStream } from './useSse';
import { OverviewView } from './components/Overview';
import { WorkflowsView } from './components/Workflows';
import { WorkflowDetailView } from './components/WorkflowDetail';
import { QuestionsView } from './components/Questions';
import { ConfigView } from './components/Config';
import { TaskDrawer } from './components/TaskDrawer';
import { AdhocModal } from './components/AdhocModal';
import { Spinner } from './components/ui';

type View = 'overview' | 'workflows' | 'questions' | 'config';

const NAV: { id: View; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'workflows', label: 'Workflows' },
  { id: 'questions', label: 'Questions' },
  { id: 'config', label: 'Config' },
];

const THEME_KEY = 'clams_theme';

function initialTheme(): 'light' | 'dark' {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === 'light' || stored === 'dark') return stored;
  } catch {
    /* storage unavailable */
  }
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function SettingsPopover({
  token,
  onSaved,
  onClose,
}: {
  token: string;
  onSaved: (t: string) => void;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState(token);
  return (
    <>
      <div className="fixed inset-0 z-40" onClick={onClose} aria-hidden="true" />
      <div className="absolute right-0 top-full z-50 mt-2 w-80 rounded-lg border border-slate-200 bg-white p-4 shadow-xl dark:border-slate-700 dark:bg-slate-900">
        <h3 className="mb-1 text-sm font-semibold text-slate-800 dark:text-slate-100">
          API bearer token
        </h3>
        <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
          Stored in localStorage under <code>clams_api_token</code> and sent as{' '}
          <code>Authorization: Bearer …</code> on every request.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setToken(draft.trim());
            onSaved(draft.trim());
            onClose();
          }}
          className="space-y-2"
        >
          <input
            type="password"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="paste token…"
            autoFocus
            className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 font-mono text-sm text-slate-800 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
          />
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 dark:border-slate-700 dark:text-slate-300"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="rounded-md bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-700"
            >
              Save
            </button>
          </div>
        </form>
      </div>
    </>
  );
}

export default function App() {
  const qc = useQueryClient();
  const { submit, pendingCount } = useCommand();

  const [view, setView] = useState<View>('overview');
  const [workflowId, setWorkflowId] = useState<string | null>(null);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [adhocOpen, setAdhocOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [token, setTokenState] = useState(getToken());
  const [theme, setTheme] = useState<'light' | 'dark'>(initialTheme);

  const connected = useEventStream(token);
  const { data: overview } = useQuery({
    queryKey: ['overview'],
    queryFn: api.overview,
    refetchInterval: 30000,
  });

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark');
  }, [theme]);

  // Follow the OS preference until the user picks one explicitly.
  useEffect(() => {
    let stored: string | null = null;
    try {
      stored = localStorage.getItem(THEME_KEY);
    } catch {
      /* ignore */
    }
    if (stored) return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = (e: MediaQueryListEvent) => setTheme(e.matches ? 'dark' : 'light');
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  function toggleTheme() {
    const next = theme === 'dark' ? 'light' : 'dark';
    setTheme(next);
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      /* ignore */
    }
  }

  const paused = overview?.paused ?? false;

  const iconBtn =
    'rounded-md border border-slate-300 p-2 text-slate-500 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800';

  return (
    <div className="min-h-screen bg-slate-100 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/90 backdrop-blur dark:border-slate-800 dark:bg-slate-900/90">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-2 px-4 py-2.5">
          <div className="mr-2 flex items-center gap-2 font-bold tracking-tight">
            <span className="grid h-7 w-7 place-items-center rounded-lg bg-emerald-600 text-sm text-white">
              ◈
            </span>
            Clams <span className="text-xs font-normal text-slate-400">v2</span>
          </div>

          <nav className="flex items-center gap-1">
            {NAV.map((n) => (
              <button
                key={n.id}
                onClick={() => {
                  setView(n.id);
                  setWorkflowId(null);
                }}
                className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                  view === n.id
                    ? 'bg-slate-200 text-slate-900 dark:bg-slate-800 dark:text-slate-100'
                    : 'text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800/60'
                }`}
              >
                {n.label}
              </button>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-2">
            <span
              title={connected ? 'Live event stream connected' : 'Live event stream offline'}
              className={`h-2.5 w-2.5 rounded-full ${connected ? 'bg-emerald-500' : 'bg-slate-400'}`}
            />
            {pendingCount > 0 ? (
              <span className="flex items-center gap-1 text-xs text-slate-500 dark:text-slate-400">
                <Spinner /> {pendingCount} pending
              </span>
            ) : null}

            <button
              onClick={() => setAdhocOpen(true)}
              className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700"
            >
              + Ad-hoc
            </button>

            <button
              onClick={() =>
                void submit(paused ? 'resume' : 'pause', { label: paused ? 'Resume' : 'Pause' })
              }
              title={paused && overview?.paused_by ? `Paused by ${overview.paused_by}` : undefined}
              className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                paused
                  ? 'bg-emerald-600 text-white hover:bg-emerald-700'
                  : 'border border-amber-400 bg-amber-50 text-amber-800 hover:bg-amber-100 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-200'
              }`}
            >
              {paused ? 'Resume' : 'Pause'}
            </button>

            <button onClick={toggleTheme} aria-label="Toggle dark mode" className={iconBtn}>
              {theme === 'dark' ? (
                <svg
                  className="h-4 w-4"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  aria-hidden="true"
                >
                  <circle cx="12" cy="12" r="4" />
                  <path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
                </svg>
              ) : (
                <svg
                  className="h-4 w-4"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
                </svg>
              )}
            </button>

            <div className="relative">
              <button onClick={() => setSettingsOpen((v) => !v)} aria-label="Settings" className={iconBtn}>
                <svg
                  className="h-4 w-4"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <circle cx="12" cy="12" r="3" />
                  <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
                </svg>
              </button>
              {settingsOpen ? (
                <SettingsPopover
                  token={token}
                  onClose={() => setSettingsOpen(false)}
                  onSaved={(t) => {
                    setTokenState(t);
                    qc.invalidateQueries();
                  }}
                />
              ) : null}
            </div>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-4 py-5">
        {view === 'overview' && <OverviewView />}
        {view === 'workflows' &&
          (workflowId ? (
            <WorkflowDetailView
              id={workflowId}
              onBack={() => setWorkflowId(null)}
              onOpenTask={setTaskId}
            />
          ) : (
            <WorkflowsView onOpen={setWorkflowId} />
          ))}
        {view === 'questions' && <QuestionsView onOpenTask={setTaskId} />}
        {view === 'config' && <ConfigView />}
      </main>

      {taskId ? <TaskDrawer taskId={taskId} onClose={() => setTaskId(null)} /> : null}
      {adhocOpen ? <AdhocModal onClose={() => setAdhocOpen(false)} /> : null}
    </div>
  );
}
