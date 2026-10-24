import { test, expect, type Page } from '@playwright/test';

/**
 * The chat widget's streamed turn (`POST /api/v2/chat/stream`, SSE over fetch).
 *
 * Part 1 drives the widget against a FAKE stream the test controls byte by byte (an init script replaces `fetch` for the stream URL only; every
 * other request reaches the real engine), so what the shopper sees at each moment is deterministic. No model is involved.
 *
 * Part 2 (skipped unless CV3_FAKE_PROVIDER_URL is set) runs the REAL engine against a local fake OpenAI-compatible provider that streams slowly and proves
 * the first visible text arrives before the provider has finished writing. It is not a live-model test: the model is the fake.
 */

const NORTHFIELD = '/northfield';

async function openChat(page: Page) {
  await page.goto(NORTHFIELD);
  await page.locator('.chatlaunch').waitFor({ timeout: 15000 });
  await page.locator('.chatlaunch').click();
  await expect(page.locator('.chatwidget')).toBeVisible();
}

async function send(page: Page, message: string) {
  await page.getByPlaceholder('Type a message').fill(message);
  await page.getByRole('button', { name: 'Send' }).click();
}

const FINAL = {
  reply: 'Final authoritative answer.',
  used_model: true,
  products: [],
  choices: [],
  actions: [],
  proposals: [],
  evidence: [],
};

/** Installs `window.__ctl`: scripted control of the stream endpoint. `attempts` records each POST (headers + body). */
async function installFakeStream(page: Page, opts: { status?: number } = {}) {
  await page.addInitScript((o) => {
    const w = window as unknown as Record<string, unknown>;
    const real = window.fetch.bind(window);
    const enc = new TextEncoder();
    const attempts: { lastEventId: string | null; requestId: string | null; auth: string | null }[] = [];
    const controllers: ReadableStreamDefaultController<Uint8Array>[] = [];
    let aborted = 0;
    w.__ctl = {
      attempts,
      get aborted() {
        return aborted;
      },
      push(text: string, attempt = -1) {
        const c = attempt < 0 ? controllers[controllers.length - 1] : controllers[attempt];
        c.enqueue(enc.encode(text));
      },
      close(attempt = -1) {
        const c = attempt < 0 ? controllers[controllers.length - 1] : controllers[attempt];
        c.close();
      },
    };
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
      if (!url.includes('/api/v2/chat/stream')) return real(input, init);
      const h = new Headers(init?.headers);
      const requestId = (() => {
        try {
          return JSON.parse(String(init?.body)).request_id as string;
        } catch {
          return null;
        }
      })();
      attempts.push({ lastEventId: h.get('Last-Event-ID'), requestId, auth: h.get('Authorization') });
      if (o.status) return Promise.resolve(new Response(JSON.stringify({ detail: 'nope' }), { status: o.status }));
      const stream = new ReadableStream<Uint8Array>({
        start(c) {
          controllers.push(c);
        },
        cancel() {
          aborted++;
        },
      });
      // A real fetch rejects the pending body read when its signal aborts; the fake must do the same.
      init?.signal?.addEventListener('abort', () => {
        aborted++;
        try {
          controllers[controllers.length - 1].error(new DOMException('aborted', 'AbortError'));
        } catch {
          // already closed
        }
      });
      return Promise.resolve(new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }));
    };
  }, opts);
}

const ev = (id: number, event: string, data: unknown) => `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
type Ctl = { attempts: { lastEventId: string | null; requestId: string | null; auth: string | null }[]; aborted: number } & Record<string, (...a: unknown[]) => void>;
const ctl = (page: Page, fn: string, ...args: unknown[]) => page.evaluate(([f, a]) => (window as unknown as { __ctl: Ctl }).__ctl[f as string](...(a as unknown[])), [fn, args] as const);
const attempts = (page: Page) => page.evaluate(() => (window as unknown as { __ctl: Ctl }).__ctl.attempts);

function trackPay(page: Page) {
  const hits: string[] = [];
  page.on('request', (r) => {
    if (/\/api\/chat\/pay|\/api\/chat\/proposals\/.*\/accept/.test(r.url())) hits.push(r.url());
  });
  return hits;
}

test.describe.configure({ mode: 'serial' });

test.describe('streamed turn, fake stream', () => {
  test('typing indicator, then incremental draft, then the final reply replaces it (one bubble)', async ({ page }) => {
    const pay = trackPay(page);
    await installFakeStream(page);
    await openChat(page);
    await send(page, 'compare the two trail shoes');

    await expect(page.getByTestId('typing-indicator')).toBeVisible();
    await expect(page.getByTestId('draft-bubble')).toHaveCount(0);
    await expect(page.getByTestId('stop-button')).toBeVisible();

    await ctl(page, 'push', ev(1, 'start', { run_id: 'r' }) + ev(2, 'status', { stage: 'compare', label: 'Comparing the options' }));
    await expect(page.getByTestId('typing-indicator')).toContainText('Comparing the options');

    await ctl(page, 'push', ev(3, 'delta', { segment: 0, text: 'The first ' }));
    await expect(page.getByTestId('draft-bubble')).toContainText('The first');
    await expect(page.getByTestId('typing-indicator')).toHaveCount(0);

    // The draft is visible while the stream is still open: that is the incremental part.
    await ctl(page, 'push', ev(4, 'delta', { segment: 0, text: 'words arrive.' }));
    await expect(page.getByTestId('draft-bubble')).toContainText('The first words arrive.');
    await expect(page.locator('.bubble.assistant', { hasText: 'Final authoritative answer.' })).toHaveCount(0);

    await ctl(page, 'push', ev(5, 'message', FINAL) + ev(6, 'done', {}));
    await expect(page.locator('.bubble.assistant', { hasText: 'Final authoritative answer.' })).toHaveCount(1);
    await expect(page.getByTestId('draft-bubble')).toHaveCount(0);
    await expect(page.getByTestId('typing-indicator')).toHaveCount(0);
    await expect(page.getByTestId('stop-button')).toHaveCount(0);
    expect(await page.locator('.bubble.assistant', { hasText: 'The first words' }).count()).toBe(0);
    expect(pay).toEqual([]);
  });

  test('a reset discards the draft; the final reply is what stays', async ({ page }) => {
    await installFakeStream(page);
    await openChat(page);
    await send(page, 'compare them');
    await ctl(page, 'push', ev(1, 'start', {}) + ev(2, 'delta', { segment: 0, text: 'Ungrounded claim here.' }));
    await expect(page.getByTestId('draft-bubble')).toContainText('Ungrounded claim here.');
    await ctl(page, 'push', ev(3, 'reset', { segment: 0, reason: 'ungrounded' }));
    await expect(page.getByTestId('draft-bubble')).toHaveCount(0);
    await ctl(page, 'push', ev(4, 'message', FINAL) + ev(5, 'done', {}));
    await expect(page.locator('.bubble.assistant', { hasText: 'Final authoritative answer.' })).toHaveCount(1);
    await expect(page.locator('.bubble', { hasText: 'Ungrounded claim here.' })).toHaveCount(0);
  });

  test('an error event ends in a clear message, no draft and no stuck indicator', async ({ page }) => {
    await installFakeStream(page);
    await openChat(page);
    await send(page, 'compare them');
    await ctl(page, 'push', ev(1, 'start', {}) + ev(2, 'delta', { segment: 0, text: 'Half a thou' }));
    await ctl(page, 'push', ev(3, 'error', { code: 'TURN_FAILED', message: 'internal detail that must not show', retryable: true }) + ev(4, 'done', {}));
    await expect(page.locator('.bubble.assistant', { hasText: 'Something went wrong with that message' })).toHaveCount(1);
    await expect(page.locator('.bubble', { hasText: 'internal detail' })).toHaveCount(0);
    await expect(page.getByTestId('typing-indicator')).toHaveCount(0);
    await expect(page.getByTestId('draft-bubble')).toHaveCount(0);
    await expect(page.getByTestId('stop-button')).toHaveCount(0);
  });

  test('malformed, duplicate, out-of-order and unknown events are ignored', async ({ page }) => {
    await installFakeStream(page);
    await openChat(page);
    await send(page, 'compare them');
    await ctl(page, 'push', ev(1, 'delta', { segment: 0, text: 'Once. ' }));
    await ctl(page, 'push', ev(1, 'delta', { segment: 0, text: 'Once. ' })); // duplicate id
    await ctl(page, 'push', 'id: 2\nevent: delta\ndata: {not json\n\n'); // malformed
    await ctl(page, 'push', ev(3, 'mystery', { x: 1 })); // unknown type
    await ctl(page, 'push', ev(1, 'delta', { segment: 0, text: 'Stale. ' })); // out of order
    await ctl(page, 'push', ': ping\n\n' + ev(4, 'delta', { segment: 0, text: 'Twice.' }));
    await expect(page.getByTestId('draft-bubble')).toHaveText(/Once\. Twice\./);
    await expect(page.getByTestId('draft-bubble')).not.toContainText('Stale');
    await ctl(page, 'push', ev(5, 'message', FINAL) + ev(6, 'done', {}));
    await expect(page.locator('.bubble.assistant', { hasText: 'Final authoritative answer.' })).toHaveCount(1);
  });

  test('a dropped connection re-attaches with the same request id and Last-Event-ID, without a duplicate bubble', async ({ page }) => {
    await installFakeStream(page);
    await openChat(page);
    await send(page, 'compare them');
    await ctl(page, 'push', ev(1, 'start', {}) + ev(2, 'delta', { segment: 0, text: 'Before the drop. ' }));
    await expect(page.getByTestId('draft-bubble')).toContainText('Before the drop.');
    await ctl(page, 'close'); // ends without `done`
    await expect.poll(async () => (await attempts(page)).length, { timeout: 8000 }).toBe(2);
    const a = await attempts(page);
    expect(a[1].lastEventId).toBe('2');
    expect(a[1].requestId).toBe(a[0].requestId);
    expect(a[0].requestId).toBeTruthy();
    expect(a[1].auth).toMatch(/^Bearer /);
    await ctl(page, 'push', ev(2, 'delta', { segment: 0, text: 'Before the drop. ' }) + ev(3, 'delta', { segment: 0, text: 'After.' })); // replay of 2 is ignored
    await expect(page.getByTestId('draft-bubble')).toHaveText(/Before the drop\. After\./);
    await ctl(page, 'push', ev(4, 'message', FINAL) + ev(5, 'done', {}));
    await expect(page.locator('.bubble.assistant', { hasText: 'Final authoritative answer.' })).toHaveCount(1);
    expect((await attempts(page)).length).toBe(2);
  });

  test('Stop ends the wait with an honest message and does not pay or accept anything', async ({ page }) => {
    const pay = trackPay(page);
    await installFakeStream(page);
    await openChat(page);
    await send(page, 'compare them');
    await ctl(page, 'push', ev(1, 'start', {}) + ev(2, 'delta', { segment: 0, text: 'Partial text' }));
    await expect(page.getByTestId('draft-bubble')).toBeVisible();
    await page.getByTestId('stop-button').click();
    await expect(page.getByTestId('draft-bubble')).toHaveCount(0);
    await expect(page.getByTestId('typing-indicator')).toHaveCount(0);
    await expect(page.locator('.bubble.assistant', { hasText: 'Stopped.' })).toHaveCount(1);
    expect(await page.evaluate(() => (window as unknown as { __ctl: Ctl }).__ctl.aborted)).toBeGreaterThan(0);
    expect((await attempts(page)).length).toBe(1); // no retry after a deliberate stop
    expect(pay).toEqual([]);
  });

  test('an engine without the stream endpoint falls back to the plain route', async ({ page }) => {
    await installFakeStream(page, { status: 404 });
    let plain = 0;
    await page.route('**/api/v2/chat', async (route) => {
      plain++;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(FINAL) });
    });
    await openChat(page);
    await send(page, 'compare them');
    await expect(page.locator('.bubble.assistant', { hasText: 'Final authoritative answer.' })).toHaveCount(1);
    expect(plain).toBe(1);
  });

  test('a rejection before the stream (HTTP error body) shows the mapped message, not a fallback', async ({ page }) => {
    await installFakeStream(page);
    await page.addInitScript(() => {
      const prev = window.fetch;
      window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
        if (url.includes('/api/v2/chat/stream')) {
          return Promise.resolve(new Response(JSON.stringify({ detail: { code: 'TURN_IN_PROGRESS', message: 'raw', retryable: true } }), { status: 409 }));
        }
        return prev(input, init);
      };
    });
    let plain = 0;
    await page.route('**/api/v2/chat', async (route) => {
      plain++;
      await route.abort();
    });
    await openChat(page);
    await send(page, 'compare them');
    await expect(page.locator('.bubble.assistant', { hasText: 'still answering your last message' })).toHaveCount(1);
    expect(plain).toBe(0);
  });

  test('the transcript poll does not add a second bubble while a stream is open', async ({ page }) => {
    await installFakeStream(page);
    let polls = 0;
    page.on('request', (r) => {
      if (/\/transcript/.test(r.url())) polls++;
    });
    await openChat(page);
    await send(page, 'compare them');
    await ctl(page, 'push', ev(1, 'start', {}) + ev(2, 'delta', { segment: 0, text: 'Streaming.' }));
    const before = polls;
    await page.waitForTimeout(6000);
    expect(polls).toBe(before);
    await ctl(page, 'push', ev(3, 'message', FINAL) + ev(4, 'done', {}));
    await expect(page.locator('.bubble.assistant', { hasText: 'Final authoritative answer.' })).toHaveCount(1);
  });
});

const PROVIDER = process.env.CV3_FAKE_PROVIDER_URL;

test.describe('real engine + fake slow provider', () => {
  test.skip(!PROVIDER, 'needs the stack started with the fake provider (stackrun_fake); set CV3_FAKE_PROVIDER_URL');

  test('first visible text arrives before the provider has finished generating', async ({ page, request }) => {
    const claim = (over: Record<string, unknown>) => ({
      capability: 'discover', context_action: 'continue', question_type: 'retrieve', reference_scope: 'none', reference_kind: 'none',
      conversational_repair: 'none', cart_operation: 'none', rationale: 'scripted', ...over,
    });
    await request.post(`${PROVIDER}/__script`, { data: { reset: true, delay: 0.4, mode: 'normal' } });
    await openChat(page);

    // The understanding step's claims are queued by the test, so the only model behaviour here is the fake provider's.
    await request.post(`${PROVIDER}/__script`, { data: { claims: [claim({ context_action: 'push' })] } });
    await send(page, 'show me running shoes');
    await expect(page.locator('.chatproduct').first()).toBeVisible({ timeout: 30000 });

    await request.post(`${PROVIDER}/__script`, {
      data: { claims: [claim({ capability: 'compare', reference_scope: 'last_rendered', reference_kind: 'ordinal', reference_positions: [1, 2] })] },
    });
    await send(page, 'compare the first two');

    const draft = page.getByTestId('draft-bubble');
    await expect(draft).toBeVisible({ timeout: 20000 });
    const firstVisible = Date.now() / 1000;
    const state = await (await request.get(`${PROVIDER}/__state`)).json();
    expect(state.stream_started_at).not.toBeNull();
    // The provider is still writing at the moment the shopper first sees text.
    expect(state.stream_finished_at === null || state.stream_finished_at > firstVisible - 0.05).toBeTruthy();

    await expect(page.getByTestId('stop-button')).toHaveCount(0, { timeout: 30000 });
    const after = await (await request.get(`${PROVIDER}/__state`)).json();
    expect(after.stream_finished_at).toBeGreaterThan(firstVisible);
    await expect(draft).toHaveCount(0);
  });
});
