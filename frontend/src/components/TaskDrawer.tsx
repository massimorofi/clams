import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import type { EvidenceCheck, TaskDetail } from '../types';
import { useCommand } from '../commands';

const TERMINAL_TASK = new Set(['success', 'failed', 'cancelled']);
import {
  Badge,
  Card,
  Empty,
  ErrorNote,
  KV,
  Loading,
  SectionTitle,
  Spinner,
  fmtDateTime,
  shortId,
  timeAgo,
} from './ui';

function checkName(c: EvidenceCheck): string {
  for (const k of ['name', 'check', 'label', 'key', 'id']) {
    if (typeof c[k] === 'string') return c[k] as string;
  }
  return JSON.stringify(c).slice(0, 80);
}

function checkPassed(c: EvidenceCheck): boolean | null {
  for (const k of ['passed', 'ok', 'pass', 'success']) {
    if (typeof c[k] === 'boolean') return c[k] as boolean;
  }
  if (typeof c['status'] === 'string') {
    const s = (c['status'] as string).toLowerCase();
    if (['pass', 'passed', 'ok', 'success'].includes(s)) return true;
    if (['fail', 'failed', 'error'].includes(s)) return false;
  }
  return null;
}

function checkDetail(c: EvidenceCheck): string {
  for (const k of ['detail', 'message', 'reason', 'error', 'output', 'value']) {
    const v = c[k];
    if (typeof v === 'string') return v;
    if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  }
  return '';
}

function EvidenceTable({ task }: { task: TaskDetail }) {
  const checks = task.result?.evidence?.checks ?? [];
  if (checks.length === 0) return null;
  return (
    <Card>
      <SectionTitle
        right={
          task.result?.evidence?.passed === false ? (
            <Badge status="failed" />
          ) : task.result?.evidence?.passed === true ? (
            <Badge status="success" />
          ) : null
        }
      >
        Evidence checks
      </SectionTitle>
      {task.result?.evidence?.reason ? (
        <div className="px-4 pt-2 text-xs text-slate-500 dark:text-slate-400">
          {task.result.evidence.reason}
        </div>
      ) : null}
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-400 dark:border-slate-800">
            <th className="px-4 py-2 font-medium">Check</th>
            <th className="px-4 py-2 font-medium">Result</th>
            <th className="px-4 py-2 font-medium">Detail</th>
          </tr>
        </thead>
        <tbody>
          {checks.map((c, i) => {
            const passed = checkPassed(c);
            return (
              <tr key={i} className="border-b border-slate-100 last:border-0 dark:border-slate-800/60">
                <td className="px-4 py-2 font-mono text-xs text-slate-700 dark:text-slate-200">
                  {checkName(c)}
                </td>
                <td className="px-4 py-2">
                  {passed === true ? (
                    <span className="font-medium text-emerald-600 dark:text-emerald-400">pass</span>
                  ) : passed === false ? (
                    <span className="font-medium text-rose-600 dark:text-rose-400">fail</span>
                  ) : (
                    <span className="text-slate-400">—</span>
                  )}
                </td>
                <td className="max-w-[260px] break-words px-4 py-2 text-xs text-slate-500 dark:text-slate-400">
                  {checkDetail(c) || '—'}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Card>
  );
}

function TranscriptSection({ taskId }: { taskId: string }) {
  const [load, setLoad] = useState(false);
  const { data, error, isLoading } = useQuery({
    queryKey: ['transcript', taskId],
    queryFn: () => api.transcript(taskId),
    enabled: load,
  });

  if (!load) {
    return (
      <button
        onClick={() => setLoad(true)}
        className="w-full rounded-md border border-dashed border-slate-300 px-4 py-2 text-sm text-slate-500 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-400 dark:hover:bg-slate-800/40"
      >
        Load transcript
      </button>
    );
  }
  if (isLoading)
    return (
      <div className="flex items-center gap-2 p-4 text-sm text-slate-500">
        <Spinner /> Loading transcript…
      </div>
    );
  if (error) return <ErrorNote error={error} />;
  const messages = data?.messages ?? [];
  if (messages.length === 0) return <Empty>No transcript messages.</Empty>;
  return (
    <ul className="max-h-96 space-y-2 overflow-y-auto p-4">
      {messages.map((m, i) => {
        const role = String(m['role'] ?? m['type'] ?? 'message');
        const raw = m['content'] ?? m['text'] ?? m['message'];
        const text =
          typeof raw === 'string' ? raw : raw === undefined ? JSON.stringify(m) : JSON.stringify(raw);
        return (
          <li key={i} className="rounded-md bg-slate-50 p-2 text-xs dark:bg-slate-800/60">
            <span className="mr-2 font-semibold uppercase tracking-wide text-slate-400">
              {role}
            </span>
            <span className="whitespace-pre-wrap break-words text-slate-700 dark:text-slate-200">
              {text}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function ListBlock({ title, items }: { title: string; items: string[] | null | undefined }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="py-1 text-sm">
      <div className="text-slate-500 dark:text-slate-400">{title}</div>
      <ul className="mt-1 space-y-0.5">
        {items.map((it, i) => (
          <li key={i} className="break-all font-mono text-xs text-slate-700 dark:text-slate-200">
            {it}
          </li>
        ))}
      </ul>
    </div>
  );
}

function MetaRow({ label, value, mono }: { label: string; value: string | null | undefined; mono?: boolean }) {
  if (!value) return null;
  return (
    <div className="flex items-center gap-2 py-0.5 text-xs text-slate-500 dark:text-slate-400">
      <span>{label}</span>
      <span className={mono ? 'break-all font-mono text-slate-700 dark:text-slate-200' : ''}>
        {value}
      </span>
    </div>
  );
}

export function TaskDrawer({ taskId, onClose }: { taskId: string; onClose: () => void }) {
  const { data, error, isLoading } = useQuery({
    queryKey: ['task', taskId],
    queryFn: () => api.task(taskId),
  });
  const { submit } = useCommand();

  return (
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} aria-hidden="true" />
      <aside className="absolute inset-y-0 right-0 flex w-full max-w-xl flex-col overflow-y-auto border-l border-slate-200 bg-slate-50 shadow-2xl dark:border-slate-800 dark:bg-slate-950">
        <div className="sticky top-0 z-10 flex items-center justify-between gap-3 border-b border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-800 dark:bg-slate-950">
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-slate-800 dark:text-slate-100">
              {isLoading ? 'Loading task…' : (data?.title ?? 'Task')}
            </div>
            <div className="font-mono text-xs text-slate-400">{shortId(taskId, 14)}</div>
          </div>
          <div className="flex items-center gap-2">
            {data ? <Badge status={data.status} /> : null}
            {data && !TERMINAL_TASK.has(data.status) ? (
              <button
                onClick={() => {
                  if (
                    window.confirm(
                      `Cancel this task?\n\nA running agent is stopped ` +
                        `best-effort; dependents will be blocked.`,
                    )
                  ) {
                    void submit('cancel', {
                      target: taskId,
                      label: `cancel task ${shortId(taskId, 8)}`,
                    });
                  }
                }}
                className="rounded-md border border-rose-300 px-2.5 py-1 text-sm text-rose-600 hover:bg-rose-50 dark:border-rose-800 dark:text-rose-400 dark:hover:bg-rose-950"
              >
                Cancel
              </button>
            ) : null}
            <button
              onClick={onClose}
              aria-label="Close task panel"
              className="rounded-md border border-slate-300 px-2.5 py-1 text-sm text-slate-500 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              ✕
            </button>
          </div>
        </div>

        <div className="space-y-4 p-4">
          {isLoading ? (
            <Loading label="Loading task…" />
          ) : error ? (
            <ErrorNote error={error} />
          ) : !data ? null : (
            <>
              <Card className="p-4">
                <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
                  <span>seq #{data.sequence_index}</span>
                  <span>attempt {data.attempt}</span>
                  {data.depends_on?.length ? (
                    <span>depends on {data.depends_on.map((d) => `#${d}`).join(', ')}</span>
                  ) : null}
                  {data.gh_issue_number ? <span>issue #{data.gh_issue_number}</span> : null}
                </div>
                <MetaRow label="commit" value={data.commit_sha} mono />
                <MetaRow label="branch" value={data.branch} mono />
              </Card>

              {data.brief ? (
                <Card className="p-4">
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                    Brief
                  </h3>
                  <KV k="Objective" v={data.brief.objective ?? '—'} />
                  <KV k="Acceptance" v={data.brief.acceptance ?? '—'} />
                  <KV k="Repo" v={data.brief.repo ?? '—'} />
                  <KV k="Base ref" v={data.brief.base_ref ?? '—'} />
                  <KV
                    k="Limits"
                    v={`${data.brief.max_turns ?? '—'} turns / ${data.brief.max_wall_s ?? '—'} s`}
                  />
                  {data.brief.answer ? <KV k="Answer" v={data.brief.answer} /> : null}
                  {data.brief.session_ref ? <KV k="Session" v={data.brief.session_ref} /> : null}
                  <ListBlock title="Run" items={data.brief.run} />
                  <ListBlock title="Writable" items={data.brief.writable} />
                  <ListBlock title="Forbidden" items={data.brief.forbidden} />
                </Card>
              ) : null}

              <Card className="p-4">
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  Lease
                </h3>
                <KV k="Executor" v={data.executor_id ?? '—'} />
                <KV
                  k="Expires"
                  v={
                    data.lease_expires_at
                      ? `${fmtDateTime(data.lease_expires_at)} (${timeAgo(data.lease_expires_at)})`
                      : '—'
                  }
                />
                <KV k="Lease token" v={data.lease_token ? shortId(data.lease_token, 12) : '—'} />
                <KV k="Session ref" v={data.session_ref ?? data.result?.session_ref ?? '—'} />
                {data.error_log ? (
                  <div className="mt-2">
                    <div className="text-xs text-slate-500 dark:text-slate-400">Error log</div>
                    <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-md bg-rose-50 p-2 font-mono text-xs text-rose-800 dark:bg-rose-950/50 dark:text-rose-200">
                      {data.error_log}
                    </pre>
                  </div>
                ) : null}
              </Card>

              {data.result ? (
                <Card className="p-4">
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                    Result
                  </h3>
                  <KV k="Outcome" v={data.result.outcome ?? '—'} />
                  <KV k="Commit" v={data.result.commit_sha ?? '—'} />
                  <KV k="Branch" v={data.result.branch ?? '—'} />
                  {data.result.changed_files?.length ? (
                    <ListBlock title="Changed files" items={data.result.changed_files} />
                  ) : null}
                </Card>
              ) : null}

              <EvidenceTable task={data} />

              {data.result?.log_tail ? (
                <Card>
                  <SectionTitle>Log tail</SectionTitle>
                  <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-all p-4 font-mono text-xs text-slate-700 dark:text-slate-300">
                    {data.result.log_tail}
                  </pre>
                </Card>
              ) : null}

              <Card>
                <SectionTitle>Transcript</SectionTitle>
                <TranscriptSection taskId={taskId} />
              </Card>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}
