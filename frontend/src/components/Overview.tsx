import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import { Badge, Card, Empty, ErrorNote, Loading, SectionTitle, timeAgo } from './ui';

function CountChips({ counts }: { counts: Record<string, number> }) {
  const entries = Object.entries(counts ?? {}).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) {
    return <div className="text-sm text-slate-400 dark:text-slate-500">none</div>;
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {entries.map(([status, n]) => (
        <span key={status} className="inline-flex items-center gap-1.5">
          <Badge status={status} />
          <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">{n}</span>
        </span>
      ))}
    </div>
  );
}

function rateColor(rate: number): string {
  if (rate >= 0.9) return 'text-emerald-600 dark:text-emerald-400';
  if (rate >= 0.6) return 'text-amber-600 dark:text-amber-400';
  return 'text-rose-600 dark:text-rose-400';
}

export function OverviewView() {
  const { data, error, isLoading } = useQuery({
    queryKey: ['overview'],
    queryFn: api.overview,
    refetchInterval: 30000,
  });

  if (isLoading) return <Loading label="Loading overview…" />;
  if (error) return <ErrorNote error={error} />;
  if (!data) return null;

  const totalWorkflows = Object.values(data.workflows ?? {}).reduce((a, b) => a + b, 0);
  const totalTasks = Object.values(data.tasks ?? {}).reduce((a, b) => a + b, 0);
  const rate =
    data.success_rate > 1 ? data.success_rate : Math.round(data.success_rate * 1000) / 10;
  const openAlerts = (data.alerts ?? []).filter((a) => a.status === 'open');

  return (
    <div className="space-y-4">
      {data.paused && (
        <div className="flex items-center gap-2 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
          <span className="font-semibold">Orchestrator is paused.</span>
          {data.paused_by ? <span>Paused by {data.paused_by}.</span> : null}
          <span>Use Resume in the header to continue.</span>
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Card className="p-4">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Workflows <span className="normal-case">({totalWorkflows})</span>
          </div>
          <CountChips counts={data.workflows} />
        </Card>
        <Card className="p-4">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Tasks <span className="normal-case">({totalTasks})</span>
          </div>
          <CountChips counts={data.tasks} />
        </Card>
        <Card className="p-4">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Success rate
          </div>
          <div className={`text-3xl font-bold ${rateColor(data.success_rate)}`}>
            {Number.isFinite(rate) ? rate : '—'}%
          </div>
        </Card>
        <Card className="p-4">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Outbox
          </div>
          <CountChips counts={data.outbox} />
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <SectionTitle right={<span className="text-xs text-slate-400">heartbeat &lt; 60s = live</span>}>
            Executors ({(data.executors ?? []).length})
          </SectionTitle>
          {(data.executors ?? []).length === 0 ? (
            <Empty>No executors registered.</Empty>
          ) : (
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-400 dark:border-slate-800">
                  <th className="px-4 py-2 font-medium">Executor</th>
                  <th className="px-4 py-2 font-medium">Version</th>
                  <th className="px-4 py-2 font-medium">Capacity</th>
                  <th className="px-4 py-2 font-medium">Last seen</th>
                </tr>
              </thead>
              <tbody>
                {(data.executors ?? []).map((ex) => (
                  <tr
                    key={ex.executor_id}
                    className="border-b border-slate-100 last:border-0 dark:border-slate-800/60"
                  >
                    <td className="px-4 py-2 font-mono text-xs">{ex.executor_id}</td>
                    <td className="px-4 py-2">{ex.version ?? '—'}</td>
                    <td className="px-4 py-2">{ex.capacity ?? '—'}</td>
                    <td className="px-4 py-2 text-slate-500 dark:text-slate-400">
                      {timeAgo(ex.last_seen)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card>
          <SectionTitle
            right={
              <span className="text-xs text-slate-400">
                {openAlerts.length} open / {(data.alerts ?? []).length} total
              </span>
            }
          >
            Alerts
          </SectionTitle>
          {(data.alerts ?? []).length === 0 ? (
            <Empty>No alerts. All quiet.</Empty>
          ) : (
            <ul className="divide-y divide-slate-100 dark:divide-slate-800/60">
              {(data.alerts ?? []).slice(0, 20).map((a) => (
                <li key={String(a.alert_id)} className="flex items-start gap-3 px-4 py-2.5">
                  <Badge status={a.severity} />
                  <div className="min-w-0 flex-1">
                    <div className="break-words text-sm text-slate-700 dark:text-slate-200">
                      {a.message}
                    </div>
                    <div className="text-xs text-slate-400">
                      {a.probe ? `${a.probe} · ` : ''}
                      {a.status} · {timeAgo(a.created_at)}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
