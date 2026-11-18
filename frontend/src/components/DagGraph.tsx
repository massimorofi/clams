import { useMemo } from 'react';
import type { TaskSummary } from '../types';

const NODE_W = 136;
const NODE_H = 42;
const GAP_X = 56;
const GAP_Y = 14;
const PAD = 10;

const STATUS_HEX: Record<string, string> = {
  success: '#10b981',
  done: '#10b981',
  failed: '#ef4444',
  error: '#ef4444',
  running: '#0ea5e9',
  dispatched: '#6366f1',
  review: '#8b5cf6',
  waiting_input: '#f59e0b',
  blocked: '#f97316',
  paused: '#f59e0b',
  planning: '#f59e0b',
  pending: '#94a3b8',
  ready: '#94a3b8',
  intake: '#94a3b8',
  cancelled: '#64748b',
};

export function statusHex(status: string): string {
  return STATUS_HEX[status.toLowerCase()] ?? '#94a3b8';
}

interface Layout {
  positions: Map<string, { x: number; y: number }>;
  bySeq: Map<number, TaskSummary>;
  width: number;
  height: number;
}

function layoutTasks(tasks: TaskSummary[]): Layout {
  const bySeq = new Map<number, TaskSummary>();
  for (const t of tasks) bySeq.set(t.sequence_index, t);

  const depth = new Map<number, number>();
  const calc = (seq: number, seen: Set<number>): number => {
    const cached = depth.get(seq);
    if (cached !== undefined) return cached;
    if (seen.has(seq)) return 0; // cycle guard
    seen.add(seq);
    const deps = (bySeq.get(seq)?.depends_on ?? []).filter((d) => bySeq.has(d));
    const d = deps.length === 0 ? 0 : 1 + Math.max(...deps.map((x) => calc(x, seen)));
    depth.set(seq, d);
    return d;
  };
  for (const t of tasks) calc(t.sequence_index, new Set());

  const cols = new Map<number, TaskSummary[]>();
  for (const t of tasks) {
    const d = depth.get(t.sequence_index) ?? 0;
    const arr = cols.get(d) ?? [];
    arr.push(t);
    cols.set(d, arr);
  }
  const colList = [...cols.entries()].sort((a, b) => a[0] - b[0]);
  const maxRows = Math.max(1, ...colList.map(([, arr]) => arr.length));

  const positions = new Map<string, { x: number; y: number }>();
  colList.forEach(([, arr], ci) => {
    arr.forEach((t, ri) => {
      positions.set(t.task_id, { x: PAD + ci * (NODE_W + GAP_X), y: PAD + ri * (NODE_H + GAP_Y) });
    });
  });

  return {
    positions,
    bySeq,
    width: PAD * 2 + Math.max(1, colList.length) * (NODE_W + GAP_X) - GAP_X,
    height: PAD * 2 + maxRows * (NODE_H + GAP_Y) - GAP_Y,
  };
}

export function DagGraph({
  tasks,
  onSelect,
}: {
  tasks: TaskSummary[];
  onSelect: (taskId: string) => void;
}) {
  const layout = useMemo(() => layoutTasks(tasks), [tasks]);
  if (tasks.length === 0) return null;

  const edges: { from: { x: number; y: number }; to: { x: number; y: number } }[] = [];
  for (const t of tasks) {
    const to = layout.positions.get(t.task_id);
    if (!to) continue;
    for (const dep of t.depends_on ?? []) {
      const depTask = layout.bySeq.get(dep);
      const from = depTask ? layout.positions.get(depTask.task_id) : undefined;
      if (from) edges.push({ from, to });
    }
  }

  return (
    <div className="overflow-x-auto">
      <svg
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        width={layout.width}
        height={layout.height}
        className="h-auto max-w-full"
        role="img"
        aria-label="Task dependency graph"
      >
        {edges.map((e, i) => {
          const x1 = e.from.x + NODE_W;
          const y1 = e.from.y + NODE_H / 2;
          const x2 = e.to.x;
          const y2 = e.to.y + NODE_H / 2;
          const mid = (x1 + x2) / 2;
          return (
            <path
              key={i}
              d={`M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`}
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              className="text-slate-300 dark:text-slate-600"
            />
          );
        })}
        {tasks.map((t) => {
          const pos = layout.positions.get(t.task_id);
          if (!pos) return null;
          const hex = statusHex(t.status);
          const title = t.title.length > 15 ? `${t.title.slice(0, 14)}…` : t.title;
          return (
            <g
              key={t.task_id}
              onClick={() => onSelect(t.task_id)}
              className="cursor-pointer"
            >
              <rect
                x={pos.x}
                y={pos.y}
                width={NODE_W}
                height={NODE_H}
                rx={7}
                fill={hex}
                fillOpacity={0.14}
                stroke={hex}
                strokeWidth={1.5}
              />
              <text
                x={pos.x + 8}
                y={pos.y + 16}
                fontSize={11}
                fontWeight={700}
                fill={hex}
              >
                {`#${t.sequence_index} ${t.status}`}
              </text>
              <text x={pos.x + 8} y={pos.y + 31} fontSize={10} className="fill-slate-600 dark:fill-slate-300">
                {title}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
