import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import { Badge, Card, Empty, ErrorNote, Loading, SectionTitle, fmtDateTime, shortId } from './ui';
import { DagGraph, statusHex } from './DagGraph';

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

  if (isLoading) return <Loading label="Loading workflow…" />;
  if (error) return <ErrorNote error={error} />;
  if (!data) return null;

  const tasks = [...(data.tasks ?? [])].sort((a, b) => a.sequence_index - b.sequence_index);

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
      </div>

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
    </div>
  );
}
