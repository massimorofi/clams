import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import { Badge, Card, Empty, ErrorNote, Loading, SectionTitle, fmtDateTime, shortId } from './ui';
import { DagGraph, statusHex } from './DagGraph';
import { useCommand } from '../commands';
import type { RecoveryPreview, TaskSummary, WorkflowDetail } from '../types';

const TERMINAL_WF = new Set(['done', 'failed', 'cancelled']);

/** Task statuses that still represent work — recovery re-plans exactly these. */
const TAIL_STATUSES = new Set([
  'pending',
  'ready',
  'dispatched',
  'running',
  'waiting_input',
  'failed',
  'blocked',
]);

function num(value: number | null | undefined, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function isPublished(t: TaskSummary): boolean {
  return t.gh_issue_number !== null && t.gh_issue_number !== undefined;
}

/**
 * What a `recover` command would sweep, taken from the server preview when the
 * API sends one and otherwise derived from the task list, so the dialog still
 * renders against older payloads (every preview field is optional).
 */
function buildPreview(data: WorkflowDetail, tasks: TaskSummary[]): RecoveryPreview {
  const preview = data.recovery ?? {};
  const published = tasks.filter(isPublished);
  const tail = tasks.filter((t) => TAIL_STATUSES.has(t.status));
  return {
    orphan_count: num(
      preview.orphan_count ?? data.orphan_subissue_count,
      published.filter((t) => t.status === 'cancelled').length,
    ),
    stale_success_count: num(
      preview.stale_success_count ?? data.stale_success_subissue_count,
      published.filter((t) => t.status === 'success').length,
    ),
    preserved_success_count: num(
      preview.preserved_success_count,
      tasks.filter((t) => t.status === 'success').length,
    ),
    replanned_count: num(
      preview.replanned_count ?? data.replannable_task_count,
      tail.length,
    ),
    noop: preview.noop ?? null,
  };
}

function Line({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5">
      <span className="text-sm text-slate-600 dark:text-slate-300">{label}</span>
      <span className="shrink-0 font-mono text-sm text-slate-800 dark:text-slate-100">
        {value}
      </span>
    </div>
  );
}

/**
 * Confirm dialog for the `recover` command (same overlay pattern as AdhocModal).
 * It spells out the two sub-issue sweeps and the plan tail that gets re-planned
 * before anything is queued.
 */
function RecoveryConfirmDialog({
  workflowId,
  goal,
  tasks,
  preview,
  onClose,
  onSubmitted,
}: {
  workflowId: string;
  goal: string;
  tasks: TaskSummary[];
  preview: RecoveryPreview;
  onClose: () => void;
  onSubmitted: () => void;
}) {
  const { submit } = useCommand();
  const [sending, setSending] = useState(false);

  const tail = tasks.filter((t) => TAIL_STATUSES.has(t.status));
  const stalled = tail.filter((t) => t.status === 'failed' || t.status === 'blocked');
  const orphans = num(preview.orphan_count, 0);
  const staleSuccess = num(preview.stale_success_count, 0);
  const preserved = num(preview.preserved_success_count, 0);

  async function confirm() {
    if (sending) return;
    setSending(true);
    try {
      await submit('recover', {
        target: workflowId,
        label: `recover workflow ${shortId(workflowId, 8)}`,
      });
      onSubmitted();
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        className="absolute inset-0 bg-black/40"
        onClick={sending ? undefined : onClose}
        aria-hidden="true"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Confirm workflow retry"
        className="relative w-full max-w-lg rounded-xl border border-slate-200 bg-white p-5 shadow-2xl dark:border-slate-800 dark:bg-slate-900"
      >
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-slate-800 dark:text-slate-100">
              Retry this workflow?
            </h2>
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
              Queues a <span className="font-mono">recover</span> command on{' '}
              {shortId(workflowId, 12)}. Nothing is deleted.
            </p>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="rounded-md px-2 py-1 text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800"
          >
            ✕
          </button>
        </div>

        <p className="mb-3 line-clamp-2 text-sm text-slate-700 dark:text-slate-200">{goal}</p>

        <div className="space-y-3">
          <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
            <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Open sub-issues to close
            </h3>
            <Line label="Orphans — no live task owns them (superseded)" value={orphans} />
            <Line label="Stale successes — task already succeeded" value={staleSuccess} />
            <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
              Closes are queued on the outbox, so GitHub is still only touched by the relay.
            </p>
          </div>

          <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
            <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Failed / blocked tail to re-plan
            </h3>
            {stalled.length === 0 ? (
              <p className="text-sm text-slate-500 dark:text-slate-400">
                No failed or blocked tasks — the plan tail is rebuilt from the tasks that still
                represent work ({tail.length}).
              </p>
            ) : (
              <ul className="max-h-40 space-y-1 overflow-auto">
                {stalled.map((t) => (
                  <li key={t.task_id} className="flex items-center gap-2 text-sm">
                    <span
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={{ backgroundColor: statusHex(t.status) }}
                    />
                    <span className="w-8 shrink-0 font-mono text-xs text-slate-400">
                      #{t.sequence_index}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-slate-700 dark:text-slate-200">
                      {t.title}
                    </span>
                    <Badge status={t.status} />
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
              {preserved} succeeded task(s) are preserved; the workflow moves planning → running.
            </p>
          </div>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={sending}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void confirm()}
            disabled={sending}
            className="rounded-md bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {sending ? 'Queuing…' : 'Confirm retry'}
          </button>
        </div>
      </div>
    </div>
  );
}

export function WorkflowDetailView({
  id,
  onBack,
  onOpenTask,
}: {
  id: string;
  onBack: () => void;
  onOpenTask: (taskId: string) => void;
}) {
  const { data, error, isLoading } = useQuery({
    queryKey: ['workflow', id],
    queryFn: () => api.workflow(id),
  });
  const { submit, pendingCount } = useCommand();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [recoverQueued, setRecoverQueued] = useState(false);
  const sawPending = useRef(false);

  // The command provider polls queued commands and invalidates the cache once
  // one settles (applied/rejected), so the refreshed `data.status` below is what
  // shows the planning -> running transition. The banner stays up while the
  // recover command is in flight: it clears once we have seen the command go
  // pending and then settle, so a fast settle cannot flicker it away early.
  useEffect(() => {
    if (!recoverQueued) return;
    if (pendingCount > 0) {
      sawPending.current = true;
      return;
    }
    if (sawPending.current) {
      sawPending.current = false;
      setRecoverQueued(false);
    }
  }, [pendingCount, recoverQueued]);

  // Safety net: if the command never showed up in the pending queue (e.g. the
  // POST failed), do not leave the "queued" banner stuck up.
  useEffect(() => {
    if (!recoverQueued) return;
    const t = window.setTimeout(() => setRecoverQueued(false), 30000);
    return () => window.clearTimeout(t);
  }, [recoverQueued]);

  if (isLoading) return <Loading label="Loading workflow…" />;
  if (error) return <ErrorNote error={error} />;
  if (!data) return null;

  const tasks = [...(data.tasks ?? [])].sort((a, b) => a.sequence_index - b.sequence_index);
  const recoverable = data.status === 'blocked' || data.status === 'failed';
  const preview = buildPreview(data, tasks);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={onBack}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
        >
          ← Workflows
        </button>
        <span className="font-mono text-xs text-slate-400">{shortId(id, 12)}</span>
        <Badge status={data.status} />
        <span className="text-xs text-slate-500 dark:text-slate-400">
          {data.source}
          {data.gh_issue_number ? ` · issue #${data.gh_issue_number}` : ''}
          {data.replans ? ` · ${data.replans} replans` : ''}
        </span>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          {recoverable && (
            <button
              disabled={pendingCount > 0}
              onClick={() => setConfirmOpen(true)}
              title="Sweep orphan sub-issues and re-plan the failed tail"
              className="rounded-md border border-emerald-300 px-3 py-1.5 text-sm font-medium text-emerald-700 hover:bg-emerald-50 disabled:opacity-50 dark:border-emerald-800 dark:text-emerald-400 dark:hover:bg-emerald-950"
            >
              Retry workflow
            </button>
          )}
          {!TERMINAL_WF.has(data.status) && (
            <button
              disabled={pendingCount > 0}
              onClick={() => {
                if (
                  window.confirm(
                    'Cancel this ENTIRE workflow?\n\n' +
                      'Running agent work is aborted (best-effort stop of the ' +
                      'Claudio run), all non-terminal tasks become cancelled and ' +
                      'dispatch stops. This is for extreme cases and cannot be ' +
                      'undone.',
                  )
                ) {
                  void submit('cancel', {
                    target: id,
                    label: `cancel workflow ${shortId(id, 8)}`,
                  });
                }
              }}
              className="rounded-md border border-rose-300 px-3 py-1.5 text-sm font-medium text-rose-600 hover:bg-rose-50 disabled:opacity-50 dark:border-rose-800 dark:text-rose-400 dark:hover:bg-rose-950"
            >
              Cancel workflow
            </button>
          )}
        </div>
      </div>

      {recoverQueued && (
        <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
          Recovery command queued (pending) — waiting for the planner to apply it.
        </div>
      )}
      {!recoverQueued && data.status === 'planning' && (
        <div className="rounded-md border border-sky-300 bg-sky-50 px-3 py-2 text-sm text-sky-800 dark:border-sky-800 dark:bg-sky-950 dark:text-sky-200">
          The workflow is back with the planner (status <span className="font-mono">planning</span>)
          and moves to <span className="font-mono">running</span> on its own — no action needed here.
        </div>
      )}

      <Card className="p-4">
        <h1 className="text-lg font-semibold text-slate-800 dark:text-slate-100">{data.goal}</h1>
        <p className="mt-1 text-xs text-slate-400">
          project {String(data.project_id)} · created {fmtDateTime(data.created_at)} · updated{' '}
          {fmtDateTime(data.updated_at)}
        </p>
      </Card>

      <Card>
        <SectionTitle>Task graph</SectionTitle>
        {tasks.length === 0 ? (
          <Empty>No tasks yet.</Empty>
        ) : (
          <div className="p-4">
            <DagGraph tasks={tasks} onSelect={onOpenTask} />
          </div>
        )}
      </Card>

      <Card>
        <SectionTitle right={<span className="text-xs text-slate-400">{tasks.length} tasks</span>}>
          Tasks
        </SectionTitle>
        {tasks.length === 0 ? (
          <Empty>No tasks yet.</Empty>
        ) : (
          <ul className="divide-y divide-slate-100 dark:divide-slate-800/60">
            {tasks.map((t) => (
              <li key={t.task_id}>
                <button
                  onClick={() => onOpenTask(t.task_id)}
                  className="flex w-full items-center gap-3 px-4 py-2.5 text-left hover:bg-slate-50 dark:hover:bg-slate-800/40"
                >
                  <span
                    className="h-2.5 w-2.5 shrink-0 rounded-full"
                    style={{ backgroundColor: statusHex(t.status) }}
                  />
                  <span className="w-8 shrink-0 font-mono text-xs text-slate-400">
                    #{t.sequence_index}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-sm text-slate-700 dark:text-slate-200">
                    {t.title}
                  </span>
                  {t.depends_on?.length ? (
                    <span className="hidden shrink-0 text-xs text-slate-400 sm:inline">
                      after {t.depends_on.map((d) => `#${d}`).join(', ')}
                    </span>
                  ) : null}
                  {t.attempt > 0 ? (
                    <span className="shrink-0 text-xs text-slate-400">a{t.attempt}</span>
                  ) : null}
                  {t.commit_sha ? (
                    <span className="hidden shrink-0 font-mono text-xs text-slate-400 md:inline">
                      {shortId(t.commit_sha, 7)}
                    </span>
                  ) : null}
                  <Badge status={t.status} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {confirmOpen && (
        <RecoveryConfirmDialog
          workflowId={id}
          goal={data.goal}
          tasks={tasks}
          preview={preview}
          onClose={() => setConfirmOpen(false)}
          onSubmitted={() => {
            setConfirmOpen(false);
            setRecoverQueued(true);
          }}
        />
      )}
    </div>
  );
}
