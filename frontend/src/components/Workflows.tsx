import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import { Badge, Card, Empty, ErrorNote, Loading, shortId, timeAgo } from './ui';

const STATUSES = [
  'intake',
  'planning',
  'running',
  'review',
  'done',
  'failed',
  'blocked',
  'paused',
  'cancelled',
];

export function WorkflowsView({ onOpen }: { onOpen: (workflowId: string) => void }) {
  const [status, setStatus] = useState('');
  const { data, error, isLoading } = useQuery({
    queryKey: ['workflows', status],
    queryFn: () => api.workflows(status || undefined),
  });

  return (
    <Card>
      <div className="flex items-center justify-between gap-3 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
        <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-200">Workflows</h2>
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          className="rounded-md border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
        >
          <option value="">All statuses</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>

      {isLoading ? (
        <Loading label="Loading workflows…" />
      ) : error ? (
        <div className="p-4">
          <ErrorNote error={error} />
        </div>
      ) : (data ?? []).length === 0 ? (
        <Empty>No workflows.</Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-left text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-400 dark:border-slate-800">
                <th className="px-4 py-2 font-medium">Workflow</th>
                <th className="px-4 py-2 font-medium">Goal</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">Source</th>
                <th className="px-4 py-2 font-medium">Issue</th>
                <th className="px-4 py-2 font-medium">Replans</th>
                <th className="px-4 py-2 font-medium">Updated</th>
              </tr>
            </thead>
            <tbody>
              {(data ?? []).map((w) => (
                <tr
                  key={w.workflow_id}
                  onClick={() => onOpen(w.workflow_id)}
                  className="cursor-pointer border-b border-slate-100 hover:bg-slate-50 last:border-0 dark:border-slate-800/60 dark:hover:bg-slate-800/40"
                >
                  <td className="px-4 py-2.5 font-mono text-xs text-sky-700 dark:text-sky-300">
                    {shortId(w.workflow_id)}
                  </td>
                  <td className="max-w-[380px] truncate px-4 py-2.5 text-slate-700 dark:text-slate-200">
                    {w.goal}
                  </td>
                  <td className="px-4 py-2.5">
                    <Badge status={w.status} />
                  </td>
                  <td className="px-4 py-2.5 text-slate-500 dark:text-slate-400">{w.source}</td>
                  <td className="px-4 py-2.5 text-slate-500 dark:text-slate-400">
                    {w.gh_issue_number ? `#${w.gh_issue_number}` : '—'}
                  </td>
                  <td className="px-4 py-2.5 text-slate-500 dark:text-slate-400">{w.replans}</td>
                  <td className="px-4 py-2.5 text-slate-500 dark:text-slate-400">
                    {timeAgo(w.updated_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
