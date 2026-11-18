import type { ReactNode } from 'react';

export function Card({
  children,
  className = '',
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`rounded-lg border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900 ${className}`}
    >
      {children}
    </div>
  );
}

export function SectionTitle({
  children,
  right,
}: {
  children: ReactNode;
  right?: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
      <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-200">{children}</h2>
      {right}
    </div>
  );
}

const STATUS_CLASSES: Record<string, string> = {
  // green
  success: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  done: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  applied: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  sent: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  answered: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  cleared: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300',
  // red
  failed: 'bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300',
  rejected: 'bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300',
  dead: 'bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300',
  critical: 'bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300',
  error: 'bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300',
  // blue / indigo
  running: 'bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300',
  dispatched: 'bg-indigo-100 text-indigo-800 dark:bg-indigo-950 dark:text-indigo-300',
  review: 'bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-300',
  info: 'bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300',
  // amber
  waiting_input: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  blocked: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  paused: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  warning: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  inflight: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  planning: 'bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
  // slate
  pending: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  ready: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  intake: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  new: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  open: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  cancelled: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-400',
  expired: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-400',
};

export function statusClass(status: string): string {
  return (
    STATUS_CLASSES[status.toLowerCase()] ??
    'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300'
  );
}

export function Badge({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${statusClass(status)}`}
    >
      {status}
    </span>
  );
}

export function Spinner({ className = '' }: { className?: string }) {
  return (
    <svg
      className={`inline h-4 w-4 animate-spin ${className}`}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity="0.25" strokeWidth="4" />
      <path
        d="M12 2a10 10 0 0 1 10 10"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 p-6 text-sm text-slate-500 dark:text-slate-400">
      <Spinner /> {label}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="rounded-lg border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-800 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-200">
      {msg}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="px-4 py-8 text-center text-sm text-slate-400 dark:text-slate-500">
      {children}
    </div>
  );
}

export function KV({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 py-1 text-sm">
      <span className="shrink-0 text-slate-500 dark:text-slate-400">{k}</span>
      <span className="min-w-0 break-words text-right font-medium text-slate-800 dark:text-slate-100">
        {v}
      </span>
    </div>
  );
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  return new Date(t).toLocaleString();
}

export function shortId(id: string | number | null | undefined, len = 8): string {
  if (id === null || id === undefined) return '—';
  const s = String(id);
  return s.length > len ? `${s.slice(0, len)}…` : s;
}
