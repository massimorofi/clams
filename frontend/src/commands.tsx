import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from './api';
import type { Command } from './types';

export type CommandKind =
  | 'pause'
  | 'resume'
  | 'cancel'
  | 'replan'
  | 'retry'
  | 'answer'
  | 'adhoc'
  | 'config_patch'
  | 'settings_patch';

interface SubmitOptions {
  target?: string | null;
  payload?: unknown;
  label?: string;
}

interface CommandApi {
  submit: (kind: CommandKind, opts?: SubmitOptions) => Promise<void>;
  pendingCount: number;
}

const CommandContext = createContext<CommandApi | null>(null);

export function useCommand(): CommandApi {
  const ctx = useContext(CommandContext);
  if (!ctx) throw new Error('useCommand must be used inside <CommandProvider>');
  return ctx;
}

interface PendingCommand {
  id: string;
  kind: CommandKind;
  label: string;
}

interface Toast {
  id: number;
  kind: 'applied' | 'rejected' | 'error';
  text: string;
}

function describeResult(result: unknown): string {
  if (result === null || result === undefined || result === '') return 'no reason given';
  if (typeof result === 'string') return result;
  try {
    const json = JSON.stringify(result);
    return json.length > 180 ? `${json.slice(0, 177)}…` : json;
  } catch {
    return String(result);
  }
}

export function CommandProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [pending, setPending] = useState<PendingCommand[]>([]);
  const [toasts, setToasts] = useState<Toast[]>([]);

  const pushToast = useCallback((kind: Toast['kind'], text: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, kind, text }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000);
  }, []);

  const submit = useCallback(
    async (kind: CommandKind, opts?: SubmitOptions) => {
      try {
        const ack = await api.sendCommand({
          kind,
          target: opts?.target ?? null,
          payload: opts?.payload ?? {},
          issued_by: 'web-ui',
        });
        setPending((p) => [
          ...p,
          { id: String(ack.command_id), kind, label: opts?.label ?? kind },
        ]);
      } catch (e) {
        pushToast('error', e instanceof Error ? e.message : String(e));
      }
    },
    [pushToast],
  );

  const ids = useMemo(() => pending.map((p) => p.id), [pending]);

  const { data: settled } = useQuery({
    queryKey: ['pending-commands', ids],
    queryFn: async () => {
      const results = await Promise.all(
        ids.map((id) => api.command(id).catch(() => null)),
      );
      return results.filter((c): c is Command => c !== null);
    },
    enabled: ids.length > 0,
    refetchInterval: 2000,
  });

  useEffect(() => {
    if (!settled || settled.length === 0) return;
    const done = settled.filter((c) => c.status !== 'new');
    if (done.length === 0) return;
    const doneIds = new Set(done.map((c) => String(c.command_id)));
    setPending((p) => p.filter((x) => !doneIds.has(x.id)));
    for (const c of done) {
      const label = pending.find((p) => p.id === String(c.command_id))?.label ?? c.kind;
      if (c.status === 'applied') {
        pushToast('applied', `${label}: applied`);
        qc.invalidateQueries();
      } else {
        pushToast('rejected', `${label}: rejected — ${describeResult(c.result)}`);
      }
    }
    // `pending` is read only for the toast label; the settled guard prevents loops.
  }, [settled]);

  const apiValue = useMemo<CommandApi>(
    () => ({ submit, pendingCount: pending.length }),
    [submit, pending.length],
  );

  return (
    <CommandContext.Provider value={apiValue}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[100] flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={
              t.kind === 'applied'
                ? 'pointer-events-auto rounded-lg border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-800 shadow-lg dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-200'
                : 'pointer-events-auto rounded-lg border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-800 shadow-lg dark:border-rose-800 dark:bg-rose-950 dark:text-rose-200'
            }
          >
            {t.text}
          </div>
        ))}
      </div>
    </CommandContext.Provider>
  );
}
