import { test, expect, request as pwRequest, type APIRequestContext, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');

/**
 * Browser verification of Merchant -> Shopper follow-ups.
 *
 * Real engine, real pipeline, no model: a hand-over is created the way a shopper
 * creates one - a declined card on Northfield, which cannot recover a payment, so the
 * engine passes it to a person. The shopper's own conversation is then read back to
 * prove the merchant's note really reaches them (or that nothing is sent).
 *
 * The only mocked responses are the ones that cannot be produced any other way (a slow
 * load, a failed load); they live in this file and nowhere in the app.
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
const bearer = (key: string) => ({ Authorization: `Bearer ${key}` });

interface Handover {
  caseId: string;
  sessionId: string;
  username: string;
  email: string;
}

/** A real hand-over on Northfield: a signed-in shopper whose card is declined. */
async function makeHandover(shopper: APIRequestContext): Promise<Handover> {
  const tag = `pwfu_${Math.random().toString(36).slice(2, 8)}`;
  const up = await shopper.post(`${ENGINE}/api/account/signup`, {
    headers: bearer(PK_DEMO),
    data: { connection_id: 'conn_demo', username: tag, password: 'password123', email: `${tag}@example.com`, guest_session: null, guest_cart: null },
  });
  expect(up.ok()).toBeTruthy();
  const cart = await (await shopper.post(`${ENGINE}/api/shop/conn_demo/cart`, { headers: bearer(PK_DEMO) })).json();
  await shopper.post(`${ENGINE}/api/shop/conn_demo/cart/${cart.cart_id}/lines`, {
    headers: bearer(PK_DEMO),
    data: { product_id: 'P1003', quantity: 1 },
  });
  const sessionId = `pwfu-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  const pay = await shopper.post(`${ENGINE}/api/chat/pay`, {
    headers: bearer(PK_DEMO),
    data: { connection_id: 'conn_demo', session_id: sessionId, cart_id: cart.cart_id, card_last4: '0004' },
  });
  expect(pay.ok()).toBeTruthy();
  const reply = await pay.json();
  expect(reply.case_id).toBeTruthy();
  return { caseId: reply.case_id, sessionId, username: tag, email: `${tag}@example.com` };
}

async function openTotal(api: APIRequestContext): Promise<number> {
  const r = await api.get(`${ENGINE}/api/handovers/conn_demo?limit=1`, { headers: bearer(SK_DEMO) });
  expect(r.ok()).toBeTruthy();
  return (await r.json()).total;
}

async function closeAllOpen(api: APIRequestContext) {
  const r = await api.get(`${ENGINE}/api/handovers/conn_demo?limit=200`, { headers: bearer(SK_DEMO) });
  for (const f of (await r.json()).follow_ups) {
    await api.post(`${ENGINE}/api/handovers/conn_demo/${f.case_id}/close`, { headers: bearer(SK_DEMO), data: {} });
  }
}

async function transcript(shopper: APIRequestContext, sessionId: string): Promise<{ speaker: string; text: string }[]> {
  const r = await shopper.get(`${ENGINE}/api/chat/conn_demo/${sessionId}`, { headers: bearer(PK_DEMO) });
  expect(r.ok()).toBeTruthy();
  const body = await r.json();
  return body.turns ?? body;
}

async function signIn(page: Page) {
  await page.goto('/northfield/merchant', { waitUntil: 'networkidle' });
  await page.getByLabel('Merchant key').fill(SK_DEMO);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('navigation', { name: 'Merchant sections' })).toBeVisible();
}

test.describe.configure({ mode: 'serial' });

test.describe('Merchant Shopper follow-ups (Northfield)', () => {
  let api: APIRequestContext;

  test.beforeAll(async () => {
    api = await pwRequest.newContext();
    await closeAllOpen(api);
  });

  test.afterAll(async () => {
    await closeAllOpen(api);
    await api.dispose();
  });

  test('zero data, then a real hand-over appears in the badge and Overview without a reload', async ({ page }) => {
    const shopper = await pwRequest.newContext();
    await signIn(page);

    await expect(page.getByTestId('follow-ups-badge')).toHaveCount(0);
    await page.getByRole('button', { name: 'Shopper follow-ups' }).click();
    await expect(page.getByText('No shoppers are waiting for a follow-up')).toBeVisible({ timeout: 30000 });

    await page.getByRole('button', { name: 'Overview' }).click();
    await makeHandover(shopper);

    const badge = page.getByTestId('follow-ups-badge');
    await expect(badge).toBeVisible({ timeout: 50000 });
    const n = Number((await badge.textContent())?.trim());
    expect(n).toBe(await openTotal(api));
    await expect(page.getByText(`${n} ${n === 1 ? 'shopper is' : 'shoppers are'} waiting for a follow-up`)).toBeVisible();

    await shopper.dispose();
    await closeAllOpen(api);
  });

  test('view the conversation and contact, then close with a message the shopper really receives', async ({ page }) => {
    const shopper = await pwRequest.newContext();
    const h = await makeHandover(shopper);

    await signIn(page);
    await page.getByRole('button', { name: 'Shopper follow-ups' }).click();
    const card = page.getByTestId('follow-up-card').first();
    await expect(card).toBeVisible({ timeout: 30000 });
    await expect(page.getByRole('tab', { name: /Waiting for a follow-up \(1\)/ })).toBeVisible();
    await expect(card).toContainText(/waiting \d+ min/);
    await expect(card).toContainText('The shopper was told');
    await expect(card).not.toContainText(/late|overdue/i);

    // The conversation and who the shopper is: real, from the engine's own records.
    await card.getByRole('button', { name: 'Show conversation and contact' }).click();
    await expect(card.getByText(`${h.username} - ${h.email}`, { exact: false })).toBeVisible({ timeout: 30000 });
    await expect(card.locator('.fu-turns')).toContainText('card ending 0004');

    // Closing needs a message for the shopper, or an explicit "already contacted" confirmation.
    await card.getByRole('button', { name: 'Mark as handled' }).click();
    const close = card.getByRole('button', { name: 'Close follow-up' });
    await expect(close).toBeDisabled();
    await card.getByLabel('Message to the shopper').fill('We have emailed you a payment link.');
    await expect(close).toBeEnabled();
    await card.getByLabel('Message to the shopper').fill('');
    await expect(close).toBeDisabled();
    await card.getByLabel(/already contacted the shopper directly/).check();
    await expect(close).toBeEnabled();
    await card.getByLabel(/already contacted the shopper directly/).uncheck();
    await expect(close).toBeDisabled();

    await card.getByLabel('Message to the shopper').fill('We have emailed you a payment link.');
    await close.click();

    await expect(page.locator('.appr-recent')).toContainText('your message was sent to the shopper', { timeout: 30000 });
    await expect(page.getByRole('tab', { name: /Waiting for a follow-up \(0\)/ })).toBeVisible({ timeout: 30000 });
    await expect(page.getByTestId('follow-ups-badge')).toHaveCount(0);

    // The shopper's own conversation now holds it, word for word.
    const turns = await transcript(shopper, h.sessionId);
    expect(turns.some((t) => t.speaker === 'assistant' && t.text === 'An update from the shop: We have emailed you a payment link.')).toBeTruthy();

    // The record: who, when, and what the shopper was told.
    await page.getByRole('tab', { name: 'Already handled' }).click();
    const row = page.getByTestId('handled-row').first();
    await expect(row).toBeVisible({ timeout: 30000 });
    await expect(row).toContainText(/by secret:/);
    await expect(row).toContainText('Told the shopper: We have emailed you a payment link.');
    await shopper.dispose();
  });

  test('confirming direct contact closes it and sends the shopper nothing', async ({ page }) => {
    const shopper = await pwRequest.newContext();
    const h = await makeHandover(shopper);
    const before = (await transcript(shopper, h.sessionId)).length;

    await signIn(page);
    await page.getByRole('button', { name: 'Shopper follow-ups' }).click();
    const card = page.getByTestId('follow-up-card').first();
    await expect(card).toBeVisible({ timeout: 30000 });
    await card.getByRole('button', { name: 'Mark as handled' }).click();
    await card.getByLabel(/already contacted the shopper directly/).check();
    await card.getByRole('button', { name: 'Close follow-up' }).click();

    await expect(page.locator('.appr-recent')).toContainText('no message was sent', { timeout: 30000 });
    const turns = await transcript(shopper, h.sessionId);
    expect(turns.length).toBe(before);
    expect(turns.some((t) => t.text.startsWith('An update from the shop'))).toBeFalsy();

    await page.getByRole('tab', { name: 'Already handled' }).click();
    await expect(page.getByTestId('handled-row').first()).toContainText('No message was sent to the shopper.', { timeout: 30000 });
    await shopper.dispose();
  });

  test('a slow load says loading, a failed first load says it failed - neither says nobody is waiting', async ({ page }) => {
    await signIn(page);
    const listUrl = /\/api\/handovers\/conn_demo\?handled=false&limit=25/;

    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    await page.route(listUrl, async (route) => {
      await gate;
      await route.continue();
    });
    await page.getByRole('button', { name: 'Shopper follow-ups' }).click();
    await expect(page.getByText('Loading…')).toBeVisible();
    await expect(page.getByText('No shoppers are waiting for a follow-up')).toHaveCount(0);
    release();
    await expect(page.getByText('No shoppers are waiting for a follow-up')).toBeVisible({ timeout: 30000 });
    await page.unroute(listUrl);

    // A first load that cannot complete, on a fresh page.
    await page.route(listUrl, (route) => route.abort());
    await page.reload({ waitUntil: 'networkidle' });
    await expect(page.getByRole('navigation', { name: 'Merchant sections' })).toBeVisible();
    await page.getByRole('button', { name: 'Shopper follow-ups' }).click();
    await expect(page.getByText(/Could not load your follow-ups/)).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('No shoppers are waiting for a follow-up')).toHaveCount(0);
  });

  test('a failed refresh keeps the last list and labels it stale', async ({ page }) => {
    const shopper = await pwRequest.newContext();
    await makeHandover(shopper);
    await signIn(page);
    await page.getByRole('button', { name: 'Shopper follow-ups' }).click();
    await expect(page.getByTestId('follow-up-card')).toHaveCount(1, { timeout: 30000 });

    await page.route(/\/api\/handovers\/conn_demo\?handled=false&limit=25/, (route) => route.abort());
    // The open page polls its list every 20 seconds.
    await expect(page.getByText(/could not refresh/)).toBeVisible({ timeout: 45000 });
    await expect(page.getByTestId('follow-up-card')).toHaveCount(1);
    await shopper.dispose();
  });
});
