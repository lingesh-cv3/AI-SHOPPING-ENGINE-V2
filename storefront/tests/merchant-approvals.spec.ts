import { test, expect, request as pwRequest, type APIRequestContext, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');

/**
 * Browser verification of the Merchant Approvals section - the one place a
 * merchant decides everything the engine is holding for them.
 *
 * Real engine, real pipeline, no model: a non-payment approval is created the way
 * a shopper would create one (a tap on "add to cart" while the merchant is in
 * Cautious mode), so this exercises a type that the old Payments panel never
 * showed, on a platform (Northfield) with no payment recovery at all. The policy
 * is restored afterwards whatever happens.
 *
 * The only mocked responses are the ones that cannot be produced any other way
 * (a slow load, a failed load, an approval the engine cannot run); they live in
 * this file and nowhere in the app.
 */

const ENGINE = 'http://127.0.0.1:8000';

function readKey(name: string): string {
  const text = readFileSync(path.join(REPO_ROOT, '.env.keys'), 'utf8');
  const line = text.split('\n').find((l) => l.startsWith(`${name}=`));
  if (!line) throw new Error(`missing ${name} in .env.keys`);
  return line.split('=')[1].trim();
}

const SK_DEMO = readKey('CV3_SECRET_CONN_DEMO');
const PK_DEMO = readKey('CV3_PUBLISHABLE_CONN_DEMO');
const SK_KETTLE = readKey('CV3_SECRET_CONN_KETTLE');

const bearer = (key: string) => ({ Authorization: `Bearer ${key}` });

async function getPolicy(api: APIRequestContext) {
  const r = await api.get(`${ENGINE}/api/policy/conn_demo`, { headers: bearer(SK_DEMO) });
  expect(r.ok()).toBeTruthy();
  return r.json();
}

async function putPolicy(api: APIRequestContext, p: { mode: string; auto_allowed: string[]; blocked: string[]; holdout_percent: number }) {
  const r = await api.put(`${ENGINE}/api/policy/conn_demo`, {
    headers: bearer(SK_DEMO),
    data: { mode: p.mode, auto_allowed: p.auto_allowed, blocked: p.blocked, holdout_percent: p.holdout_percent },
  });
  expect(r.ok()).toBeTruthy();
}

/** One held approval on Northfield, made by a shopper's own tap. Needs Cautious mode. */
async function tapAddToCart(shopper: APIRequestContext) {
  const cart = await (
    await shopper.post(`${ENGINE}/api/shop/conn_demo/cart`, { headers: bearer(PK_DEMO) })
  ).json();
  const r = await shopper.post(`${ENGINE}/api/chat/act`, {
    headers: bearer(PK_DEMO),
    data: {
      connection_id: 'conn_demo',
      session_id: `pw-appr-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      product_id: 'P1003',
      cart_id: cart.cart_id,
      said: 'Add that',
    },
  });
  expect(r.ok()).toBeTruthy();
}

async function pendingTotal(api: APIRequestContext, key = SK_DEMO, conn = 'conn_demo'): Promise<number> {
  const r = await api.get(`${ENGINE}/api/approvals/${conn}?limit=1`, { headers: bearer(key) });
  expect(r.ok()).toBeTruthy();
  return (await r.json()).total;
}

async function rejectAllPending(api: APIRequestContext) {
  const r = await api.get(`${ENGINE}/api/approvals/conn_demo?limit=200`, { headers: bearer(SK_DEMO) });
  for (const a of (await r.json()).approvals) {
    await api.post(`${ENGINE}/api/approvals/conn_demo/${a.approval_id}`, {
      headers: bearer(SK_DEMO),
      data: { approved: false, decided_by: 'playwright-cleanup', note: 'test cleanup' },
    });
  }
}

async function signIn(page: Page, prefix: string, key: string) {
  await page.goto(`/${prefix}/merchant`, { waitUntil: 'networkidle' });
  await page.getByLabel('Merchant key').fill(key);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('navigation', { name: 'Merchant sections' })).toBeVisible();
}

test.describe.configure({ mode: 'serial' });

test.describe('Merchant Approvals (Northfield, Cautious mode)', () => {
  let api: APIRequestContext;
  let shopper: APIRequestContext;
  let original: { mode: string; auto_allowed: string[]; blocked: string[]; holdout_percent: number };

  test.beforeAll(async () => {
    api = await pwRequest.newContext();
    shopper = await pwRequest.newContext();
    original = await getPolicy(api);
    await rejectAllPending(api);
    await putPolicy(api, { ...original, mode: 'CAUTIOUS' });
  });

  test.afterAll(async () => {
    await rejectAllPending(api);
    await putPolicy(api, original);
    await api.dispose();
    await shopper.dispose();
  });

  test('zero data, then a held approval appears in the badge without a reload', async ({ page }) => {
    await signIn(page, 'northfield', SK_DEMO);

    // Nothing pending: an honest, stated empty state - after loading, never instead of it.
    await expect(page.getByTestId('approvals-badge')).toHaveCount(0);
    await page.getByRole('button', { name: 'Approvals' }).click();
    await expect(page.getByText('Nothing is waiting for your decision')).toBeVisible({ timeout: 30000 });

    // Leave the section, so the badge (not the open list) is what must update.
    await page.getByRole('button', { name: 'Overview' }).click();
    await tapAddToCart(shopper);

    // Polling is every 20s and the engine can be slow while Overview's heavier report
    // calls are still running; allow a full cycle plus generous slack.
    const badge = page.getByTestId('approvals-badge');
    await expect(badge).toBeVisible({ timeout: 50000 });
    const n = Number((await badge.textContent())?.trim());
    expect(n).toBeGreaterThanOrEqual(1);
    expect(n).toBe(await pendingTotal(api));

    // One count everywhere: Overview's attention list and the Copilot banner.
    const plural = n === 1 ? 'decision' : 'decisions';
    await expect(page.getByText(`${n} ${plural} waiting on you`)).toBeVisible();
    await expect(page.getByText(`${n} ${plural === 'decision' ? 'decision is' : 'decisions are'} waiting for your approval`)).toBeVisible();

    await rejectAllPending(api);
  });

  test('a non-payment approval can be approved and another rejected with a note, from Merchant', async ({ page }) => {
    await tapAddToCart(shopper);
    await tapAddToCart(shopper);
    expect(await pendingTotal(api)).toBe(2);

    await signIn(page, 'northfield', SK_DEMO);
    await page.getByRole('button', { name: 'Approvals' }).click();

    await expect(page.getByRole('tab', { name: /Waiting for you \(2\)/ })).toBeVisible({ timeout: 30000 });
    const cards = page.getByTestId('approval-card').filter({ hasText: 'add to cart' });
    await expect(cards).toHaveCount(2, { timeout: 30000 });

    // The card carries what an approver needs: the rule in plain words and the clock.
    // (A shopper's own tap has no diagnosis or shopper reply, so those blocks are absent
    // rather than shown empty.)
    const first = cards.first();
    await expect(first.getByText(/Held because:/)).toBeVisible();
    await expect(first.getByText(/before it times out/)).toBeVisible();

    // Approve the first: it really runs (reversible cart add on Northfield).
    await first.getByRole('button', { name: 'Approve and run' }).click();
    const recent = page.locator('.appr-recent .qresult').first();
    await expect(recent).toContainText(/approved/i, { timeout: 20000 });
    await expect(recent).not.toHaveClass(/bad/);
    await expect(page.getByRole('tab', { name: /Waiting for you \(1\)/ })).toBeVisible();

    // Reject the second, with a reason.
    const second = page.getByTestId('approval-card').first();
    await second.getByRole('button', { name: 'Reject' }).click();
    await second.getByLabel('Reason for rejecting').fill('not this week');
    await second.getByRole('button', { name: 'Reject', exact: true }).click();
    await expect(page.getByRole('tab', { name: /Waiting for you \(0\)/ })).toBeVisible({ timeout: 20000 });
    await expect(page.getByText('Nothing is waiting for your decision')).toBeVisible();
    await expect(page.getByTestId('approvals-badge')).toHaveCount(0);

    // History shows both, with the real outcome and the note.
    await page.getByRole('tab', { name: 'Already decided' }).click();
    // Newest first, so the two decisions just made are the top two rows (the dev
    // database already holds older history, which is why this is not a count).
    const rows = page.getByTestId('history-row');
    await expect(rows.first()).toBeVisible({ timeout: 30000 });
    const top = [await rows.nth(0).innerText(), await rows.nth(1).innerText()];
    const rejectedRow = top.find((r) => /rejected/i.test(r));
    const approvedRow = top.find((r) => /approved/i.test(r));
    expect(rejectedRow).toContain('not this week');
    expect(approvedRow).toBeTruthy();
    expect(approvedRow).not.toContain('did not run');
    expect(await pendingTotal(api)).toBe(0);
  });

  test('a slow load says loading, and a failed load says it failed - neither says nothing is waiting', async ({ page }) => {
    await signIn(page, 'northfield', SK_DEMO);

    // Slow: hold the first list request.
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    await page.route(/\/api\/approvals\/conn_demo\?limit=25/, async (route) => {
      await gate;
      await route.continue();
    });
    await page.getByRole('button', { name: 'Approvals' }).click();
    await expect(page.getByText('Loading…')).toBeVisible();
    await expect(page.getByText('Nothing is waiting for your decision')).toHaveCount(0);
    release();
    await expect(page.getByText('Nothing is waiting for your decision')).toBeVisible({ timeout: 30000 });
    await page.unroute(/\/api\/approvals\/conn_demo\?limit=25/);

    // Failed: a fresh page, where the very first load cannot complete. (Once a list has
    // loaded, a failed refresh deliberately keeps the last data and labels it stale
    // rather than blanking the page - so this has to be a first load.)
    await page.route(/\/api\/approvals\/conn_demo\?limit=25/, (route) => route.abort());
    await page.reload({ waitUntil: 'networkidle' });
    await expect(page.getByRole('navigation', { name: 'Merchant sections' })).toBeVisible();
    await page.getByRole('button', { name: 'Approvals' }).click();
    await expect(page.getByText(/Could not load your approvals/)).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('Nothing is waiting for your decision')).toHaveCount(0);
  });

  test('an approval the engine cannot run offers no Approve button, only Reject', async ({ page }) => {
    await signIn(page, 'northfield', SK_DEMO);
    const now = new Date();
    const item = {
      approval_id: 'apr_mock_notify',
      case_id: 'case_mock',
      action_type: 'NOTIFY_BACK_IN_STOCK',
      risk_rule: 'IRREVERSIBLE_ALWAYS_HUMAN',
      requested_at: now.toISOString(),
      waiting_minutes: 1,
      expires_at: new Date(now.getTime() + 10 * 60000).toISOString(),
      minutes_left: 10,
      friction_type: 'PRODUCT_UNAVAILABLE',
      diagnosis: 'test fixture',
      evidence: [],
      used_model: false,
      shopper_reply: null,
      model_reply: null,
      selection_reason: null,
      rejected: [],
      financial: false,
      order_id: null,
      query: null,
      executable: { can_run: false, reason: 'Back-in-stock alerts need an email service, which is not built yet, so approving this would run nothing.' },
    };
    await page.route(/\/api\/approvals\/conn_demo\?limit=25/, (route) =>
      route.fulfill({ json: { approvals: [item], total: 1, limit: 25, offset: 0 } }),
    );
    await page.getByRole('button', { name: 'Approvals' }).click();
    const card = page.getByTestId('approval-card');
    await expect(card).toHaveCount(1, { timeout: 30000 });
    await expect(card.getByRole('note')).toContainText('Cannot be run');
    await expect(card.getByRole('button', { name: 'Approve and run' })).toBeDisabled();
    await expect(card.getByRole('button', { name: 'Reject', exact: true })).toBeEnabled();
  });
});

const PK_KETTLE = readKey('CV3_PUBLISHABLE_CONN_KETTLE');

/** A declined-card recovery approval on Kettle (which supports recovery), made through the real pipeline. */
async function makeKettleRecoveryApproval(shopper: APIRequestContext) {
  const tag = `pw_${Math.random().toString(36).slice(2, 8)}`;
  const up = await shopper.post(`${ENGINE}/api/account/signup`, {
    headers: bearer(PK_KETTLE),
    data: { connection_id: 'conn_kettle', username: tag, password: 'password123', email: `${tag}@example.com`, guest_session: null, guest_cart: null },
  });
  expect(up.ok()).toBeTruthy();
  const cart = await (await shopper.post(`${ENGINE}/api/shop/conn_kettle/cart`, { headers: bearer(PK_KETTLE) })).json();
  await shopper.post(`${ENGINE}/api/shop/conn_kettle/cart/${cart.cart_id}/lines`, {
    headers: bearer(PK_KETTLE),
    data: { product_id: 'KB-COL-02', variant_id: 'KB-COL-02::250g whole bean', quantity: 1 },
  });
  const paid = await (
    await shopper.post(`${ENGINE}/api/shop/conn_kettle/cart/${cart.cart_id}/checkout`, {
      headers: bearer(PK_KETTLE),
      data: { card_last4: '0002' },
    })
  ).json();
  const sim = await shopper.post(`${ENGINE}/api/simulate`, {
    headers: bearer(PK_KETTLE),
    data: { connection_id: 'conn_kettle', friction: 'PAYMENT_DECLINED', order_id: paid.order?.order_id, session_id: `pw-rec-${Date.now()}` },
  });
  expect(sim.ok()).toBeTruthy();
}

async function rejectAllKettle(api: APIRequestContext) {
  const r = await api.get(`${ENGINE}/api/approvals/conn_kettle?limit=200`, { headers: bearer(SK_KETTLE) });
  for (const a of (await r.json()).approvals) {
    await api.post(`${ENGINE}/api/approvals/conn_kettle/${a.approval_id}`, {
      headers: bearer(SK_KETTLE),
      data: { approved: false, decided_by: 'playwright-cleanup', note: 'test cleanup' },
    });
  }
}

test('Kettle: a payment-recovery approval appears in Approvals, and Payments & Checkout counts it but no longer carries approve/reject', async ({ page }) => {
  const api = await pwRequest.newContext();
  const shopper = await pwRequest.newContext();
  try {
    await rejectAllKettle(api);
    // The situation goes through the real reasoning step, which uses the live model,
    // and a model can answer a declined card with something that is not a payment
    // action (so nothing is held). Try a few times; the API-level suite covers the
    // payment-recovery types deterministically, so this is skipped, not faked, if the
    // model never produces one.
    let total = 0;
    for (let attempt = 0; attempt < 4 && total < 1; attempt++) {
      await makeKettleRecoveryApproval(shopper);
      total = await pendingTotal(api, SK_KETTLE, 'conn_kettle');
    }
    test.skip(total < 1, 'the model did not propose a payment-recovery action in 4 attempts');

    await signIn(page, 'kettle', SK_KETTLE);

    // Payments & Checkout: the shared count, and no decision controls of its own.
    await page.getByRole('button', { name: 'Payments & Checkout' }).click();
    await expect(page.getByText('Decisions waiting')).toBeVisible({ timeout: 30000 });
    await expect(page.getByRole('button', { name: /^Approve/ })).toHaveCount(0);
    const review = page.getByRole('button', { name: new RegExp(`Review ${total} decision`) });
    await expect(review).toBeVisible({ timeout: 30000 });
    await expect(page.getByTestId('approvals-badge')).toHaveText(String(total), { timeout: 30000 });

    // One click from there to the place decisions are made, where it is a money decision.
    await review.click();
    const card = page.getByTestId('approval-card').first();
    await expect(card).toBeVisible({ timeout: 30000 });
    await expect(card).toContainText('moves money');
    await expect(card.getByRole('button', { name: 'Approve and run' })).toBeEnabled();
  } finally {
    await rejectAllKettle(api);
    await api.dispose();
    await shopper.dispose();
  }
});
