import { test, expect, type Page, type Route } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');

/**
 * A basket whose payment outcome could not be confirmed is HELD: every payment control for it is off, says why, and only the
 * server's own answer lifts the hold. The engine refuses the same payments server-side regardless (payment_guard.py and its
 * tests); this proves the shopper's screen matches that guard and fails closed.
 *
 * Real stack, real signup, real cart. A genuine timeout cannot be produced by the demo merchants, so the failures this suite
 * needs - the engine saying "held", a payment request that dies, a cart read that fails - are injected at the browser's own
 * network layer, in this file only. No payment is ever sent to a provider; the one real payment is a declined demo card.
 */

test.describe.configure({ mode: 'serial' });

const NORTHFIELD = '/northfield';
const CART_GET = /\/api\/shop\/conn_demo\/cart\/[^/]+$/;
const CHECKOUT = /\/api\/shop\/conn_demo\/cart\/[^/]+\/checkout$/;
const CHAT_PAY = /\/api\/chat\/pay$/;
const CHAT_V2 = /\/api\/v2\/chat$/;

type ServerState = 'unknown' | 'in_flight' | null | 'unreadable';

async function addFirstProduct(page: Page) {
  const addButton = page.locator('button', { hasText: 'Add to cart' }).first();
  await addButton.waitFor({ timeout: 15000 });
  await page.waitForTimeout(1500);
  for (let i = 0; i < 6; i++) {
    await addButton.click();
    await page.waitForTimeout(1800);
    try {
      await page.locator('.panel .line').first().waitFor({ timeout: 3000 });
      return;
    } catch {
      // not ready yet
    }
  }
  throw new Error('could not get an item into the cart');
}

/** A signed-in shopper with one line in the basket, on the cart panel with the card picker showing. */
async function readyShopper(page: Page): Promise<string> {
  await page.goto(NORTHFIELD, { waitUntil: 'networkidle' });
  await page.locator('.panel', { hasText: 'Nothing here yet' }).waitFor({ timeout: 15000 });
  await addFirstProduct(page);
  await page.getByRole('link', { name: 'Create an account' }).click();
  const stamp = Date.now();
  await page.getByLabel('Username').fill(`hold_${stamp}`);
  await page.getByLabel('Password').fill('password123');
  await page.getByLabel('Email').fill(`hold_${stamp}@example.com`);
  await page.getByRole('button', { name: 'Create account' }).click();
  await expect(page.locator('select.card-picker')).toBeVisible({ timeout: 15000 });
  const id = await page.evaluate(() => sessionStorage.getItem('cv3_cart_conn_demo'));
  expect(id, 'a cart id is stored').toBeTruthy();
  return id as string;
}

/** What the engine's cart read says about the basket. 'unreadable' = the read itself fails. Changeable mid-test. */
function serverSays(page: Page) {
  const box: { state: ServerState | 'garbage' } = { state: null };
  return page.route(CART_GET, async (route: Route) => {
    if (route.request().method() !== 'GET') return route.continue();
    // Read the state ONCE, when the request arrives: a test that changes it while the real answer is in flight must not
    // leak the new value into this reply.
    const state = box.state;
    if (state === 'unreadable') return route.abort();
    const res = await route.fetch();
    const body = await res.json();
    body.payment_state = state === 'garbage' ? 'SOMETHING-NEW' : state;
    return route.fulfill({ response: res, json: body });
  }).then(() => box);
}

function counter(page: Page, pattern: RegExp) {
  const n = { posts: 0 };
  page.on('request', (req) => {
    if (req.method() === 'POST' && pattern.test(req.url())) n.posts++;
  });
  return n;
}

const hold = (page: Page) => page.getByTestId('payment-hold');
const payNow = (page: Page) => page.getByTestId('pay-now');

test('the server says the basket is held: payment is off, the reason is shown, and a forced click pays nothing', async ({ page }) => {
  test.setTimeout(120000);
  await readyShopper(page);
  const box = await serverSays(page);
  const checkouts = counter(page, CHECKOUT);

  box.state = 'unknown';
  await page.reload({ waitUntil: 'networkidle' });

  await expect(hold(page)).toBeVisible({ timeout: 15000 });
  await expect(hold(page)).toContainText("don't pay again");
  await expect(payNow(page)).toBeDisabled();
  await expect(payNow(page)).toContainText('Payment on hold');
  await expect(page.locator('select.card-picker')).toBeDisabled();

  await payNow(page).click({ force: true, trial: false }).catch(() => {});
  await page.waitForTimeout(800);
  expect(checkouts.posts, 'a held basket never reaches the checkout route').toBe(0);

  // a payment really in flight reads differently
  box.state = 'in_flight';
  await page.getByTestId('payment-hold-recheck').click();
  await expect(hold(page)).toContainText('going through now', { timeout: 10000 });
  await expect(payNow(page)).toBeDisabled();
});

test('a payment state this page does not recognise fails closed, never free', async ({ page }) => {
  test.setTimeout(120000);
  await readyShopper(page);
  const box = await serverSays(page);
  box.state = 'garbage';
  await page.reload({ waitUntil: 'networkidle' });
  await expect(hold(page)).toBeVisible({ timeout: 15000 });
  await expect(payNow(page)).toBeDisabled();
  box.state = null;
  await page.getByTestId('payment-hold-recheck').click();
  await expect(hold(page)).toHaveCount(0, { timeout: 10000 });
  await expect(payNow(page)).toBeEnabled();
});

test('only the server lifts the hold: Check again re-reads, and re-enables payment only when the basket is free', async ({ page }) => {
  test.setTimeout(120000);
  await readyShopper(page);
  const box = await serverSays(page);
  box.state = 'unknown';
  await page.reload({ waitUntil: 'networkidle' });
  await expect(hold(page)).toBeVisible({ timeout: 15000 });

  await page.getByTestId('payment-hold-recheck').click();
  await page.waitForTimeout(800);
  await expect(hold(page), 'still held while the server still says so').toBeVisible();
  await expect(payNow(page)).toBeDisabled();

  box.state = null;
  await page.getByTestId('payment-hold-recheck').click();
  await expect(hold(page)).toHaveCount(0, { timeout: 10000 });
  await expect(payNow(page)).toBeEnabled();
  await expect(payNow(page)).toContainText('Pay now');
});

test('the engine answers 409 PAYMENT_OUTCOME_UNKNOWN: the basket is held at once and the message is the fixed one', async ({ page }) => {
  test.setTimeout(120000);
  await readyShopper(page);
  const box = await serverSays(page);
  const checkouts = counter(page, CHECKOUT);
  await page.route(CHECKOUT, (route) =>
    route.fulfill({
      status: 409,
      contentType: 'application/json',
      body: JSON.stringify({ detail: { code: 'PAYMENT_OUTCOME_UNKNOWN', message: 'RAW-ENGINE-TEXT-MUST-NOT-SHOW', retryable: false } }),
    }),
  );

  box.state = null;
  await payNow(page).click();

  await expect(hold(page)).toBeVisible({ timeout: 10000 });
  await expect(payNow(page)).toBeDisabled();
  await expect(page.locator('body')).not.toContainText('RAW-ENGINE-TEXT-MUST-NOT-SHOW');
  expect(checkouts.posts, 'exactly one payment request was made').toBe(1);

  await payNow(page).click({ force: true }).catch(() => {});
  await page.waitForTimeout(600);
  expect(checkouts.posts, 'and a second press cannot make another').toBe(1);
});

test('a payment request that dies without a clean answer fails CLOSED, then follows the server', async ({ page }) => {
  test.setTimeout(150000);
  await readyShopper(page);
  const box = await serverSays(page);
  const checkouts = counter(page, CHECKOUT);
  await page.route(CHECKOUT, (route) => route.abort());

  // the server knows the payment is unresolved
  box.state = 'unknown';
  await payNow(page).click();
  await expect(hold(page)).toBeVisible({ timeout: 10000 });
  await expect(payNow(page)).toBeDisabled();
  expect(checkouts.posts).toBe(1);

  // the server cannot even be asked: payment stays off, with an honest message
  await page.unroute(CHECKOUT);
  await page.route(CHECKOUT, (route) => route.abort());
  box.state = 'unreadable';
  await page.getByTestId('payment-hold-recheck').click();
  await expect(payNow(page)).toBeDisabled();

  // once the server can answer and says the basket is free (a refusal that proved nothing happened), payment comes back
  box.state = null;
  await page.getByTestId('payment-hold-recheck').click();
  await expect(hold(page)).toHaveCount(0, { timeout: 10000 });
  await expect(payNow(page)).toBeEnabled();
});

test('a 5xx on payment and an unreadable cart: payment stays off and says it could not check', async ({ page }) => {
  test.setTimeout(150000);
  await readyShopper(page);
  const box = await serverSays(page);
  await page.route(CHECKOUT, (route) =>
    route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":{"code":"UPSTREAM_ERROR","message":"x","retryable":true}}' }),
  );
  box.state = 'unreadable';
  await payNow(page).click();

  await expect(hold(page)).toBeVisible({ timeout: 10000 });
  await expect(hold(page)).toContainText("couldn't check your payment status");
  await expect(payNow(page)).toBeDisabled();
  await page.waitForTimeout(800);
  await expect(payNow(page), 'it does not quietly re-enable itself').toBeDisabled();
});

test('a clean decline does NOT hold the basket: the shopper can try another card', async ({ page }) => {
  test.setTimeout(120000);
  await readyShopper(page);
  await serverSays(page); // the engine's own answer: free

  await page.locator('select.card-picker').selectOption('0002');
  await payNow(page).click();
  // A decline is a settled non-charge: the order view says so, and the basket is NOT held.
  await expect(page.getByText('Order held')).toBeVisible({ timeout: 20000 });

  await expect(hold(page)).toHaveCount(0);
  await expect(page.getByTestId('chat-payment-hold')).toHaveCount(0);
  await expect(payNow(page)).toBeVisible();
  await expect(payNow(page)).toBeEnabled();
  await expect(page.locator('select.card-picker')).toBeEnabled();
});

/** The assistant's payment picker for the stored basket, built from a mocked checkout turn (the engine's real checkout offer
 *  is covered by test_payment_hold_checkout_offer.py; this needs the picker on screen deterministically). */
async function mockCheckoutOffer(page: Page, cartId: string) {
  // These tests mock the plain `/api/v2/chat` reply; the picker, not the transport, is under test, so this tab uses the plain route
  // (the widget streams by default - see chat-streaming.spec.ts).
  await page.addInitScript(() => sessionStorage.setItem('cv3_chat_stream', 'off'));
  await page.evaluate(() => sessionStorage.setItem('cv3_chat_stream', 'off')).catch(() => {});
  await page.route(CHAT_V2, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reply: 'Your cart is ready. Available in this demo: Card ending 1111.',
        shopping_state: {},
        evidence: { products: [] },
        used_model: false,
        products: [],
        why: null,
        trace: {
          capability: 'checkout',
          checkout_payload: {
            cart_id: cartId,
            item_count: 1,
            grand_total: '₹100',
            subtotal: '₹100',
            lines: [{ title: 'A thing', quantity: 1, total: '₹100' }],
            cards: [
              { last4: '1111', label: 'Card ending 1111' },
              { last4: '0002', label: 'Card ending 0002 (no funds)' },
            ],
          },
        },
      }),
    }),
  );
}

async function openPicker(page: Page, cartId: string) {
  await mockCheckoutOffer(page, cartId);
  await page.locator('.chatlaunch').click();
  await expect(page.locator('.chatwidget')).toBeVisible();
  await page.getByPlaceholder('Type a message').fill("I'm ready to checkout");
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.locator('.payblock')).toBeVisible({ timeout: 15000 });
}

test('the assistant\'s payment picker: a held reply turns the picker off here and in the cart panel (one shared state)', async ({ page }) => {
  test.setTimeout(150000);
  const cartId = await readyShopper(page);
  await serverSays(page);
  const pays = counter(page, CHAT_PAY);
  await page.route(CHAT_PAY, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reply: "We couldn't confirm whether your payment went through, so your basket is on hold to avoid charging you twice. Please don't pay again.",
        session_id: 'x', used_model: false, cart_changed: false, trace: null,
        payment: { unknown: true, hold: true, paid: false, cart_retired: false },
      }),
    }),
  );

  await openPicker(page, cartId);
  const cards = page.locator('.paycard');
  await expect(cards.first()).toBeEnabled();
  await cards.first().click();
  await page.getByRole('button', { name: 'Yes, pay now' }).click();

  await expect(page.getByTestId('chat-payment-hold')).toBeVisible({ timeout: 10000 });
  await expect(page.locator('.paycard').first()).toBeDisabled();
  await expect(hold(page), 'the cart panel shows the same hold').toBeVisible();
  await expect(payNow(page)).toBeDisabled();
  expect(pays.posts).toBe(1);

  await page.locator('.paycard').first().click({ force: true }).catch(() => {});
  await page.waitForTimeout(600);
  expect(pays.posts, 'the picker cannot make a second payment').toBe(1);
});

test('the assistant\'s payment picker fails closed when the payment request dies, and a stale picker learns the basket is held', async ({ page }) => {
  test.setTimeout(150000);
  const cartId = await readyShopper(page);
  const box = await serverSays(page);
  await page.route(CHAT_PAY, (route) => route.abort());

  await openPicker(page, cartId);
  box.state = 'unknown';
  await page.locator('.paycard').first().click();
  await page.getByRole('button', { name: 'Yes, pay now' }).click();

  await expect(page.getByTestId('chat-payment-hold')).toBeVisible({ timeout: 10000 });
  await expect(page.locator('.paycard').first()).toBeDisabled();

  // the server later says free: Check again (in the picker) lifts it, and only then
  box.state = null;
  await page.getByTestId('chat-payment-hold-recheck').click();
  await expect(page.getByTestId('chat-payment-hold')).toHaveCount(0, { timeout: 10000 });
  // (an older turn's card buttons stay off once the conversation has moved on - that is the existing rule - so the usable
  //  control after the hold lifts is the cart panel's, which shares the same state)
  await expect(hold(page)).toHaveCount(0);
  await expect(payNow(page)).toBeEnabled();
});

/* ---- operations: the held-payments figure ---- */

function readKey(name: string): string {
  const text = readFileSync(path.join(REPO_ROOT, '.env.keys'), 'utf8');
  const line = text.split('\n').find((l) => l.startsWith(`${name}=`));
  if (!line) throw new Error(`missing ${name} in .env.keys`);
  return line.split('=')[1].trim();
}

async function opsSignIn(page: Page) {
  await page.goto('/operations', { waitUntil: 'networkidle' });
  await page.getByLabel('Operator key').fill(readKey('CV3_OPERATOR_KEY'));
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('navigation', { name: 'Operations sections' })).toBeVisible();
}

function opsRow(page: Page, label: string) {
  return page.locator('.ops-row').filter({ hasText: label });
}

test('operations: held payments are counted with how long the oldest has waited, and zero is an honest zero', async ({ page }) => {
  test.setTimeout(90000);
  await opsSignIn(page);
  const label = 'Payments whose outcome could not be confirmed';
  await expect(opsRow(page, label)).toBeVisible({ timeout: 15000 });
  await expect(opsRow(page, label)).toContainText('0');

  await page.route(/\/api\/ops\/stats$/, async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    body.unresolved_payments = 3;
    body.oldest_unresolved_payment_minutes = 42;
    return route.fulfill({ response: res, json: body });
  });
  await page.getByRole('button', { name: 'Refresh' }).click();
  await expect(opsRow(page, label)).toContainText('3', { timeout: 10000 });
  await expect(opsRow(page, label)).toContainText('oldest held 42m');
});
