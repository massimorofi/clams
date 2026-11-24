// Minimal, dependency-free Markdown renderer for the prompt preview.
// Supports: fenced code blocks, #/##/### headings, - and 1. lists, **bold**,
// *italic*, `inline code`, [links](url). Everything is HTML-escaped first, so
// the output can only ever contain the tags generated here.

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function inline(s: string): string {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" class="underline" target="_blank" rel="noreferrer">$1</a>',
    );
}

function renderMarkdown(src: string): string {
  const out: string[] = [];
  let list: string[] | null = null;
  let para: string[] = [];
  let inFence = false;
  let fence: string[] = [];

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${para.map(inline).join('<br/>')}</p>`);
      para = [];
    }
  };
  const flushList = () => {
    if (list) {
      out.push(`<ul>${list.map((li) => `<li>${inline(li)}</li>`).join('')}</ul>`);
      list = null;
    }
  };

  for (const raw of escapeHtml(src).split('\n')) {
    if (raw.trim().startsWith('```')) {
      if (inFence) {
        out.push(`<pre class="rounded-md bg-slate-100 p-2 text-xs dark:bg-slate-800"><code>${fence.join('\n')}</code></pre>`);
        fence = [];
        inFence = false;
      } else {
        flushPara();
        flushList();
        inFence = true;
      }
      continue;
    }
    if (inFence) {
      fence.push(raw);
      continue;
    }
    const h = /^(#{1,3})\s+(.*)$/.exec(raw);
    if (h) {
      flushPara();
      flushList();
      const lvl = h[1].length;
      const cls =
        lvl === 1
          ? 'mt-2 text-base font-semibold'
          : lvl === 2
            ? 'mt-2 text-sm font-semibold'
            : 'mt-1 text-sm font-medium';
      out.push(`<h${lvl} class="${cls}">${inline(h[2])}</h${lvl}>`);
      continue;
    }
    const ul = /^[-*]\s+(.*)$/.exec(raw);
    const ol = /^\d+\.\s+(.*)$/.exec(raw);
    if (ul || ol) {
      flushPara();
      list = list ?? [];
      list.push((ul ?? ol)![1]);
      continue;
    }
    if (!raw.trim()) {
      flushPara();
      flushList();
      continue;
    }
    flushList();
    para.push(raw);
  }
  if (inFence && fence.length) {
    out.push(`<pre>${fence.join('\n')}</pre>`);
  }
  flushPara();
  flushList();
  return out.join('\n');
}

export function Markdown({ text }: { text: string }) {
  return (
    <div
      className="prose-sm space-y-1.5 text-sm leading-relaxed text-slate-700 dark:text-slate-300 [&_a]:text-emerald-600 [&_code]:rounded [&_code]:bg-slate-100 [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-[12px] dark:[&_code]:bg-slate-800 [&_h1]:text-slate-900 [&_h2]:text-slate-900 [&_h3]:text-slate-900 dark:[&_h1]:text-slate-100 dark:[&_h2]:text-slate-100 dark:[&_h3]:text-slate-100 [&_li]:ml-5 [&_li]:list-disc [&_pre]:whitespace-pre-wrap [&_pre]:break-words [&_strong]:font-semibold [&_em]:italic"
      dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }}
    />
  );
}
