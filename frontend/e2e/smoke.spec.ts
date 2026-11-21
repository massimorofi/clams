/**
 * WP8-live smoke tests (issue #132) against the running Clams stack.
 *
 * Prereqs: `./clamsctl start` healthy, CLAMS_API_TOKEN exported, and
 * (for the question test) docker access to the planner container.
 *
 * Note: the planner drains commands only at the start of a cycle, and a
 * cycle can be busy in a multi-minute planning LLM call — command
 * acknowledgement is therefore polled with a generous timeout.
 */
import { test, expect, request as pwRequest } from '@playwright/test';
import { execFileSync } from 'node:child_process';

const TOKEN = process.env.CLAMS_API_TOKEN ?? '';
const API = process.env.BASE_URL ?? 'http://127.0.0.1:8200';

let ctx: Awaited<ReturnType<typeof pwRequest.newContext>>;

async function api(path: string, init?: Record<string, unknown>) {
  const headers = {
    Authorization: `Bearer ${TOKEN}`,
    ...(init?.headers as Record<string, string> | undefined),
  };
  return ctx.fetch(`${API}/api/v1${path}`, { ...init, headers });
}

async function command(kind: string, target?: string,
                       payload: Record<string, unknown> = {}) {
  const r = await api('/commands', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    data: JSON.stringify({ kind, target, payload }),
  });
  expect(r.ok(), `command ${kind} rejected: ${await r.text()}`).toBeTruthy();
  return r.json();
}

/** Wait until the planner has applied a command (drain can lag behind LLM work). */
async function waitCommand(commandId: number) {
  await expect
    .poll(async () => (await api(`/commands/${commandId}`)).json().status,
          { timeout: 120_000, message: `command ${commandId} not applied` })
    .toBe('applied');
}

async function setPaused(paused: boolean) {
  const ack = await command(paused ? 'pause' : 'resume');
  await waitCommand(ack.command_id);
}

function sql(statement: string): string {
  return execFileSync('docker', ['exec', 'clams-planner-1', 'sqlite3',
                                 '/data/clams.db', statement],
                      { encoding: 'utf8' }).trim();
}

async function authedPage(page: import('@playwright/test').Page) {
  await page.addInitScript(([token]) => {
    try { localStorage.setItem('clams_api_token', token); } catch { /* */ }
  }, [TOKEN]);
  await page.goto('/');
}

test.beforeAll(async () => {
  if (!TOKEN) throw new Error('CLAMS_API_TOKEN must be set for the e2e suite');
  ctx = await pwRequest.newContext();
  await setPaused(false); // known starting state
});

test.afterAll(async () => {
  try { await setPaused(false); } catch { /* best effort */ }
  await ctx?.dispose();
});

test.describe('Clams v2 SPA smoke', () => {
  test('overview loads with live data', async ({ page }) => {
    await authedPage(page);
    await expect(page.getByRole('button', { name: 'Workflows' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Questions' })).toBeVisible();
    await expect(page.locator('main')).toContainText(/workflow/i,
                                                     { timeout: 15_000 });
  });

  test('pause and resume round-trip', async ({ page }) => {
    await authedPage(page);
    const pauseBtn = page.getByRole('button', { name: 'Pause', exact: true });
    await expect(pauseBtn).toBeVisible({ timeout: 15_000 });
    await pauseBtn.click();
    const resumeBtn = page.getByRole('button', { name: 'Resume', exact: true });
    await expect(resumeBtn).toBeVisible({ timeout: 30_000 }); // SSE refresh
    await expect(page.locator('body')).toContainText(/paused/i);
    await resumeBtn.click();
    await expect(pauseBtn).toBeVisible({ timeout: 30_000 });
    // authoritative check, not just the UI
    const ov = await (await api('/overview')).json();
    expect(ov.paused).toBeFalsy();
  });

  test('ad-hoc modal submits a goal and the workflow appears', async ({ page }) => {
    await authedPage(page);
    await page.getByRole('button', { name: '+ Ad-hoc' }).click();
    const goal = `E2E playwright smoke goal ${Date.now()} (no-op)`;
    await page.getByPlaceholder('What should the orchestrator achieve?')
              .fill(goal);
    await page.getByPlaceholder('owner/name').fill('massimorofi/claudio');
    await page.getByRole('button', { name: 'Start workflow' }).click();
    await expect(page.getByPlaceholder('What should the orchestrator achieve?'))
           .toHaveCount(0, { timeout: 15_000 });
    let wfId = '';
    await expect
      .poll(async () => {
        const wfs = await (await api('/workflows')).json();
        const wf = wfs.find((w: { goal: string }) => w.goal === goal);
        if (wf) wfId = wf.workflow_id;
        return !!wfId;
      }, { timeout: 30_000 })
      .toBeTruthy();
    // cancel it so the planner does not spend LLM budget on a no-op goal
    const ack = await command('cancel', wfId);
    await waitCommand(ack.command_id);
  });

  test('question inbox: answering resumes the task', async ({ page }) => {
    test.skip(!process.env.E2E_SEED_QUESTION,
              'set E2E_SEED_QUESTION=1 (needs docker exec on the planner)');
    const stamp = Date.now();
    const QK = `e2e-q-${stamp}`;
    const wfId = `e2e-q-wf-${stamp}`;
    const tId = `e2e-q-task-${stamp}`;
    sql(`INSERT INTO workflows(workflow_id,project_id,source,goal,status)
         VALUES('${wfId}',1,'adhoc','e2e seeded question','running');
         INSERT INTO tasks(task_id,workflow_id,sequence_index,depends_on,
           title,objective,acceptance,brief_json,status,attempt,max_attempts)
         VALUES('${tId}','${wfId}',1,'[]','e2e question task','o','a',
           '{"objective":"o","acceptance":"a","run":["echo x"],
             "writable":["*.py"],"repo":"e2e/nonexistent","max_wall_s":60}',
           'waiting_input',1,3);
         INSERT INTO questions(task_id,attempt,question,options,status,
           idempotency_key)
         VALUES('${tId}',1,'E2E-${stamp}: pick a color','["red","blue"]',
           'open','${QK}');`);
    try {
      await authedPage(page);
      await page.getByRole('button', { name: 'Questions' }).click();
      // scope to OUR card (other open questions may exist on the live board)
      const card = page.locator('div', { hasText: `E2E-${stamp}: pick a color` })
                       .locator('form').last();
      await expect(card).toBeVisible({ timeout: 15_000 });
      await card.getByPlaceholder('Type an answer…').fill('blue');
      await card.getByRole('button', { name: 'Answer' }).click();
      // cancel the seeded workflow immediately so the now-ready task is
      // never dispatched to a real executor
      const ack = await command('cancel', wfId);
      await waitCommand(ack.command_id);
      const rows = sql(
        `SELECT status||':'||COALESCE(answer,'') FROM questions
           WHERE idempotency_key='${QK}';
         SELECT status FROM tasks WHERE task_id='${tId}';`)
        .split('\n');
      expect(rows[0]).toBe('answered:blue');
      expect(['ready', 'cancelled', 'dispatched']).toContain(rows[1]);
    } finally {
      sql(`DELETE FROM questions WHERE idempotency_key='${QK}';
           DELETE FROM tasks WHERE task_id='${tId}';
           DELETE FROM workflows WHERE workflow_id='${wfId}';`);
    }
  });

  test('SSE resumes from Last-Event-ID', async () => {
    // max_events bounds the stream so the request completes
    const r = await fetch(
      `${API}/api/v1/events?token=${encodeURIComponent(TOKEN)}&max_events=3`,
      { headers: { 'Last-Event-ID': '1' } });
    expect(r.status).toBe(200);
    const body = await r.text();
    const ids = [...body.matchAll(/^id: (\d+)$/gm)].map((m) => Number(m[1]));
    expect(ids.length).toBeGreaterThan(0);
    expect(Math.min(...ids)).toBeGreaterThan(1);
  });
});
