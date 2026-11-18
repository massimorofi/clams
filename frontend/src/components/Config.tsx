import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import { useCommand } from '../commands';
import { Card, Empty, ErrorNote, Loading, SectionTitle, fmtDateTime } from './ui';
import type { Project } from '../types';

function enabledLabel(v: unknown): string {
  if (v === true || v === 1) return 'enabled';
  if (v === false || v === 0) return 'disabled';
  return '—';
}

function isEnabled(p: Project): boolean {
  return !(p['enabled'] === false || p['enabled'] === 0);
}

const inputCls =
  'w-full rounded-md border border-slate-300 bg-white px-3 py-2 font-mono text-sm ' +
  'text-slate-800 placeholder:text-slate-400 dark:border-slate-700 dark:bg-slate-800 ' +
  'dark:text-slate-100';

function Field({
  label,
  value,
  onChange,
  placeholder,
  autoFocus,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  autoFocus?: boolean;
}) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium text-slate-700 dark:text-slate-200">
        {label}
      </span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoFocus={autoFocus}
        className={inputCls}
      />
    </label>
  );
}

function AddProjectModal({ onClose }: { onClose: () => void }) {
  const { submit } = useCommand();
  const [repo, setRepo] = useState('');
  const [nodeId, setNodeId] = useState('');
  const [statusField, setStatusField] = useState('');
  const [readyOption, setReadyOption] = useState('');
  const [sending, setSending] = useState(false);

  const repoOk = /^\S+\/\S+$/.test(repo.trim());

  async function send() {
    if (!repoOk || sending) return;
    setSending(true);
    try {
      const entry: Record<string, unknown> = { repo: repo.trim(), enabled: true };
      if (nodeId.trim()) entry.gh_project_node_id = nodeId.trim();
      if (statusField.trim()) entry.status_field_id = statusField.trim();
      if (readyOption.trim()) entry.ready_option_id = readyOption.trim();
      await submit('config_patch', {
        payload: { projects: [entry] },
        label: `add ${repo.trim()}`,
      });
      onClose();
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} aria-hidden="true" />
      <div className="relative w-full max-w-lg rounded-xl border border-slate-200 bg-white p-5 shadow-2xl dark:border-slate-800 dark:bg-slate-900">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold text-slate-800 dark:text-slate-100">
            Add GitHub project to Clams
          </h2>
          <button
            onClick={onClose}
            aria-label="Close"
            className="rounded-md px-2 py-1 text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800"
          >
            ✕
          </button>
        </div>

        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <Field
            label="Repo (owner/name)"
            value={repo}
            onChange={setRepo}
            placeholder="octocat/hello-world"
            autoFocus
          />
          <p className="-mt-2 text-xs text-slate-400">
            Clams starts reading issues labelled{' '}
            <code className="font-mono">status:Ready</code> on the next planner cycle.
            The GitHub token must have access to this repo.
          </p>
          <details className="text-sm">
            <summary className="cursor-pointer text-slate-500 dark:text-slate-400">
              Optional: GitHub Projects V2 board
            </summary>
            <div className="mt-3 space-y-3">
              <Field
                label="Project node ID"
                value={nodeId}
                onChange={setNodeId}
                placeholder="PVT_xxx"
              />
              <Field
                label="Status field ID"
                value={statusField}
                onChange={setStatusField}
                placeholder="PVT_field_xxx"
              />
              <Field
                label="Ready option ID"
                value={readyOption}
                onChange={setReadyOption}
                placeholder="opt_xxx"
              />
            </div>
          </details>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!repoOk || sending}
              className="rounded-md bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {sending ? 'Submitting…' : 'Add project'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export function ConfigView() {
  const { data, error, isLoading } = useQuery({
    queryKey: ['projects'],
    queryFn: api.projects,
  });
  const { submit, pendingCount } = useCommand();
  const [addOpen, setAddOpen] = useState(false);
  const busy = pendingCount > 0;

  if (isLoading) return <Loading label="Loading projects…" />;
  if (error) return <ErrorNote error={error} />;

  const projects = data ?? [];

  function toggle(p: Project) {
    const repo = String(p['repo']);
    const on = isEnabled(p);
    void submit('config_patch', {
      payload: { projects: [{ repo, enabled: !on }] },
      label: `${on ? 'stop' : 'start'} ${repo}`,
    });
  }

  function remove(p: Project) {
    const repo = String(p['repo']);
    if (
      !window.confirm(
        `Remove ${repo} from Clams?\n\n` +
          'Intake stops and the registration is deleted. This is refused while ' +
          'the project has any workflows.',
      )
    )
      return;
    void submit('config_patch', {
      payload: { projects: [{ repo, remove: true }] },
      label: `remove ${repo}`,
    });
  }

  const rowBtn =
    'rounded-md border px-2 py-1 text-xs font-medium disabled:cursor-not-allowed ' +
    'disabled:opacity-40';

  return (
    <Card>
      <SectionTitle
        right={
          <button
            onClick={() => setAddOpen(true)}
            disabled={busy}
            className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            + Add project
          </button>
        }
      >
        Projects ({projects.length})
      </SectionTitle>
      {projects.length === 0 ? (
        <Empty>No projects registered — add one to start reading issues.</Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-left text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-400 dark:border-slate-800">
                <th className="px-4 py-2 font-medium">ID</th>
                <th className="px-4 py-2 font-medium">Repo</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">GitHub project</th>
                <th className="px-4 py-2 font-medium">Created</th>
                <th className="px-4 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {projects.map((p, i) => (
                <tr
                  key={String(p['project_id'] ?? i)}
                  className="border-b border-slate-100 last:border-0 dark:border-slate-800/60"
                >
                  <td className="px-4 py-2 font-mono text-xs text-slate-500 dark:text-slate-400">
                    {String(p['project_id'] ?? '—')}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs text-slate-700 dark:text-slate-200">
                    {p['repo'] ? String(p['repo']) : '—'}
                  </td>
                  <td className="px-4 py-2">
                    <span
                      className={
                        isEnabled(p)
                          ? 'text-emerald-600 dark:text-emerald-400'
                          : 'text-slate-400'
                      }
                    >
                      {enabledLabel(p['enabled'])}
                    </span>
                  </td>
                  <td className="px-4 py-2 font-mono text-xs text-slate-500 dark:text-slate-400">
                    {p['gh_project_node_id'] ? String(p['gh_project_node_id']) : '—'}
                  </td>
                  <td className="px-4 py-2 text-slate-500 dark:text-slate-400">
                    {fmtDateTime(p['created_at'] ? String(p['created_at']) : null)}
                  </td>
                  <td className="px-4 py-2">
                    <div className="flex justify-end gap-2">
                      <button
                        onClick={() => toggle(p)}
                        disabled={busy}
                        title={
                          isEnabled(p)
                            ? 'Stop reading issues from this project'
                            : 'Resume reading issues from this project'
                        }
                        className={
                          rowBtn +
                          ' border-slate-300 text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800'
                        }
                      >
                        {isEnabled(p) ? 'Stop' : 'Start'}
                      </button>
                      <button
                        onClick={() => remove(p)}
                        disabled={busy}
                        title="Remove this project from Clams"
                        className={
                          rowBtn +
                          ' border-rose-300 text-rose-600 hover:bg-rose-50 dark:border-rose-800 dark:text-rose-400 dark:hover:bg-rose-950'
                        }
                      >
                        Remove
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {addOpen && <AddProjectModal onClose={() => setAddOpen(false)} />}
    </Card>
  );
}
