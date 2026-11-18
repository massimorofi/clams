import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import { useCommand } from '../commands';
import type { Question } from '../types';
import { Badge, Card, Empty, ErrorNote, Loading, fmtDateTime, shortId, timeAgo } from './ui';

function parseOptions(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const v: unknown = JSON.parse(raw);
    if (Array.isArray(v)) {
      return v.map((x) =>
        typeof x === 'string'
          ? x
          : String(
              (x as Record<string, unknown>)?.['label'] ??
                (x as Record<string, unknown>)?.['value'] ??
                JSON.stringify(x),
            ),
      );
    }
    if (typeof v === 'string') return [v];
  } catch {
    /* fall through to line-splitting */
  }
  return raw
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean);
}

function QuestionCard({
  q,
  onOpenTask,
}: {
  q: Question;
  onOpenTask: (taskId: string) => void;
}) {
  const { submit } = useCommand();
  const [answer, setAnswer] = useState('');
  const [sending, setSending] = useState(false);
  const options = parseOptions(q.options);

  const expired = q.expires_at ? Date.parse(q.expires_at) < Date.now() : false;

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || sending) return;
    setSending(true);
    try {
      await submit('answer', {
        target: q.task_id,
        payload: { question_id: q.question_id, answer: trimmed },
        label: 'Answer',
      });
      setAnswer('');
    } finally {
      setSending(false);
    }
  }

  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm font-medium text-slate-800 dark:text-slate-100">{q.question}</p>
        <Badge status={q.status} />
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-400">
        <button
          onClick={() => onOpenTask(q.task_id)}
          className="font-mono text-sky-700 hover:underline dark:text-sky-300"
        >
          task {shortId(q.task_id, 10)}
        </button>
        <span>attempt {q.attempt}</span>
        <span>asked {timeAgo(q.asked_at)}</span>
        {q.expires_at ? (
          <span className={expired ? 'text-rose-500' : ''}>
            expires {fmtDateTime(q.expires_at)}
          </span>
        ) : null}
      </div>

      {options.length > 0 ? (
        <div className="mt-3 flex flex-wrap gap-2">
          {options.map((opt, i) => (
            <button
              key={i}
              onClick={() => setAnswer(opt)}
              className="rounded-full border border-slate-300 px-3 py-1 text-xs text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              {opt}
            </button>
          ))}
        </div>
      ) : null}

      <form
        className="mt-3 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void send(answer);
        }}
      >
        <input
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
          placeholder="Type an answer…"
          className="min-w-0 flex-1 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-800 placeholder:text-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
        />
        <button
          type="submit"
          disabled={!answer.trim() || sending}
          className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {sending ? 'Sending…' : 'Answer'}
        </button>
      </form>
    </Card>
  );
}

export function QuestionsView({ onOpenTask }: { onOpenTask: (taskId: string) => void }) {
  const { data, error, isLoading } = useQuery({
    queryKey: ['questions'],
    queryFn: () => api.questions('open'),
  });

  if (isLoading) return <Loading label="Loading questions…" />;
  if (error) return <ErrorNote error={error} />;

  const questions = data ?? [];
  if (questions.length === 0) {
    return (
      <Card>
        <Empty>No open questions. The executors have everything they need.</Empty>
      </Card>
    );
  }

  return (
    <div className="space-y-3">
      {questions.map((q) => (
        <QuestionCard key={String(q.question_id)} q={q} onOpenTask={onOpenTask} />
      ))}
    </div>
  );
}
