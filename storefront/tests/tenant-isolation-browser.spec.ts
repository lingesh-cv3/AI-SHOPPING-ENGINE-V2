import { test, expect, type Browser, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Browser-level tenant and shopper isolation for the two demo merchants.
 *
 * Model-free and payment-free. Real engine, real pipeline: every attack below is a real
 * request made from inside a real storefront page (same origin, real visitor cookie), using
 * a publishable key the page itself sent - so the engine sees exactly what a malicious
 * script on the page, or a second shopper holding the same public key, could send.
 *
 * What is asserted is the OUTCOME, never a particular status code:
 *   - the request is refused (not ok) with a client error, never a server fault;
 *   - the body carries none of the other tenant's / shopper's data;
 *   - the victim's state is unchanged afterwards, and a positive control with the right
 *     credentials still succeeds (so a refusal is a real refusal, not an empty result).
 *
 * The one fixture is scripts/make_test_proposal.py (a pending proposal cannot be made any
 * other way without a model). It writes through DATABASE_URL, so this file refuses to run
 * unless DATABASE_URL names an isolated database.
 */

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');
const NORTHFIELD = '/northfield';
const KETTLE = '/kettle';
const DEMO = 'conn_demo';
const KET = 'conn_kettle';

test.describe.configure({ mode: 'serial' });

test.beforeAll(() => {
  const url = process.env.DATABASE_URL ?? '';
  if (!url || /cv3\.db(\?|$)/.test(url)) {
    throw new Error(
      'tenant-isolation-browser: DATABASE_URL must name an isolated database (it is "' +
        url +
        '"). Refusing to run: the proposal fixture writes through it.',
    );
  }
});

interface Probe {
  status: number;
  ok: boolean;
  text: string;
}

/**
 * Records the publishable key the storefront itself sends for a merchant - ONCE, on first
 * sight. It must not keep updating: this suite's own cross-merchant probes also pass through
 * this listener, and a probe that sends merchant A's key to merchant B's URL would otherwise
 * overwrite "B's key" with A's and turn a later probe into a legitimate request.
 */
function captureKeys(page: Page): Record<string, string> {
  const keys: Record<string, string> = {};
  page.on('request', (req) => {
    const m = /\/api\/shop\/(conn_[a-z]+)\//.exec(req.url());
    const auth = req.headers()['authorization'];
    if (m && !keys[m[1]] && auth && auth.startsWith('Bearer ') && auth.length > 'Bearer '.length) {
      keys[m[1]] = auth.slice('Bearer '.length);
    }
  });
  return keys;
}

function watchPageErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
  return errors;
}

async function probe(
  page: Page,
  method: 'GET' | 'POST',
  url: string,
  key: string,
  body?: unknown,
): Promise<Probe> {
  return page.evaluate(
    async ({ method, url, key, body }) => {
      const res = await fetch(url, {
        method,
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${key}` },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await res.text();
      return { status: res.status, ok: res.ok, text: text.slice(0, 4000) };
    },
    { method, url, key, body },
  );
}

/** A refusal is: not ok, a client error (not a server fault), and no foreign data in the body. */
function expectRefused(label: string, p: Probe, forbidden: string[]) {
  console.log(`  [isolation] ${label} -> HTTP ${p.status}`);
  expect(p.ok, `${label}: must be refused, got HTTP ${p.status}`).toBe(false);
  expect(p.status, `${label}: a refusal must be a client error, not a server fault`).toBeGreaterThanOrEqual(400);
  expect(p.status, `${label}: a refusal must be a client error, not a server fault`).toBeLessThan(500);
  for (const f of forbidden) {
    expect(p.text, `${label}: body must not expose "${f}"`).not.toContain(f);
  }
}

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
      // not ready yet; try again
    }
  }
  throw new Error('could not get an item into the cart');
}

async function storage(page: Page, key: string): Promise<string> {
  const v = await page.evaluate((k) => sessionStorage.getItem(k), key);
  expect(v, `sessionStorage ${key}`).toBeTruthy();
  return v as string;
}

async function sendChat(page: Page, message: string) {
  const launcher = page.locator('.chatlaunch');
  if (await launcher.isVisible()) await launcher.click();
  await expect(page.locator('.chatwidget')).toBeVisible();
  await page.getByPlaceholder('Type a message').fill(message);
  await page.getByRole('button', { name: 'Send' }).click();
  // the turn is stored once the engine answered (any answer: this suite is model-free)
  await expect(page.locator('.chatwidget')).toContainText(message, { timeout: 20000 });
  await page.waitForTimeout(2500);
}

async function cartLines(page: Page, conn: string, cartId: string, key: string): Promise<string[]> {
  const p = await probe(page, 'GET', `/api/shop/${conn}/cart/${cartId}`, key);
  expect(p.ok, `owner cart read (${conn}) must succeed`).toBe(true);
  const cart = JSON.parse(p.text) as { lines?: { title: string }[] };
  return (cart.lines ?? []).map((l) => l.title);
}

function makeProposal(sessionId: string, cartId: string, productId: string): string {
  const out = execFileSync(
    process.env.CV3_PYTHON || 'python',
    ['scripts/make_test_proposal.py', DEMO, sessionId, cartId, productId],
    { cwd: REPO_ROOT, encoding: 'utf8' },
  );
  const line = out.trim().split('\n').filter((l) => l.trim().startsWith('{'))[0];
  return JSON.parse(line).proposal_id as string;
}

async function newVisitor(browser: Browser, route: string) {
  const context = await browser.newContext();
  const page = await context.newPage();
  const keys = captureKeys(page);
  const errors = watchPageErrors(page);
  await page.goto(route, { waitUntil: 'networkidle' });
  return { context, page, keys, errors };
}

test('two merchants in one browser: keys, sessions and carts stay separate, and each is refused the other', async ({ page }) => {
  test.setTimeout(150000);
  const keys = captureKeys(page);
  const errors = watchPageErrors(page);
  const marker = `tenant-probe-${Date.now()}`;

  // Northfield: a cart with a line, and a stored conversation turn carrying a unique marker.
  await page.goto(NORTHFIELD, { waitUntil: 'networkidle' });
  await addFirstProduct(page);
  await sendChat(page, `${marker}-demo`);
  const demoCart = await storage(page, 'cv3_cart_conn_demo');
  const demoSession = await storage(page, 'cv3_session_conn_demo');

  // Kettle, same browser (same visitor cookie, same origin).
  await page.goto(KETTLE, { waitUntil: 'networkidle' });
  await addFirstProduct(page);
  await sendChat(page, `${marker}-kettle`);
  const ketCart = await storage(page, 'cv3_cart_conn_kettle');
  const ketSession = await storage(page, 'cv3_session_conn_kettle');

  expect(keys[DEMO], 'the storefront sent a Northfield key').toBeTruthy();
  expect(keys[KET], 'the storefront sent a Kettle key').toBeTruthy();
  expect(keys[DEMO], 'each merchant has its own key').not.toBe(keys[KET]);
  expect(demoCart, 'carts are per merchant').not.toBe(ketCart);
  expect(demoSession, 'sessions are per merchant').not.toBe(ketSession);

  // Positive controls: the right key reads the right thing (so refusals below are real).
  const demoLines = await cartLines(page, DEMO, demoCart, keys[DEMO]);
  const ketLines = await cartLines(page, KET, ketCart, keys[KET]);
  expect(demoLines.length).toBeGreaterThan(0);
  expect(ketLines.length).toBeGreaterThan(0);
  const demoTranscript = await probe(page, 'GET', `/api/chat/${DEMO}/${demoSession}`, keys[DEMO]);
  expect(demoTranscript.ok, 'owner transcript read (Northfield) must succeed').toBe(true);
  expect(demoTranscript.text).toContain(`${marker}-demo`);

  const kettleTitles = ['Ethiopia', 'Colombia', 'Brazil', 'Espresso Blend', 'Kettle & Bloom'];
  const northfieldTitles = ['Trailblazer', 'Marathon', 'Starter Road', 'Northfield'];

  // Credentials: one merchant's key against the other merchant's catalogue.
  expectRefused('Northfield key -> Kettle catalogue', await probe(page, 'GET', `/api/shop/${KET}/search?q=&limit=5`, keys[DEMO]), kettleTitles);
  expectRefused('Kettle key -> Northfield catalogue', await probe(page, 'GET', `/api/shop/${DEMO}/search?q=&limit=5`, keys[KET]), northfieldTitles);

  // Carts: the other merchant's cart id, under every credential combination.
  expectRefused('Northfield key -> Kettle cart id on Northfield', await probe(page, 'GET', `/api/shop/${DEMO}/cart/${ketCart}`, keys[DEMO]), ketLines);
  expectRefused('Kettle key -> Northfield cart id on Kettle', await probe(page, 'GET', `/api/shop/${KET}/cart/${demoCart}`, keys[KET]), demoLines);
  expectRefused('Northfield key -> Kettle cart on Kettle', await probe(page, 'GET', `/api/shop/${KET}/cart/${ketCart}`, keys[DEMO]), ketLines);

  // Sessions / transcripts: the other merchant's session id, under every combination.
  expectRefused('Northfield key -> Kettle session on Northfield', await probe(page, 'GET', `/api/chat/${DEMO}/${ketSession}`, keys[DEMO]), [`${marker}-kettle`]);
  expectRefused('Kettle key -> Northfield session on Kettle', await probe(page, 'GET', `/api/chat/${KET}/${demoSession}`, keys[KET]), [`${marker}-demo`]);
  expectRefused('Kettle key -> Northfield session claiming Northfield', await probe(page, 'GET', `/api/chat/${DEMO}/${demoSession}`, keys[KET]), [`${marker}-demo`]);

  // A chat turn aimed at the other merchant's cart/session must not act on it.
  const crossTurn = await probe(page, 'POST', '/api/v2/chat', keys[KET], {
    connection_id: KET, session_id: demoSession, message: 'what is in my cart?', cart_id: demoCart,
  });
  console.log(`  [isolation] Kettle key + Northfield session/cart on /api/v2/chat -> HTTP ${crossTurn.status}`);
  expect(crossTurn.text, 'a cross-merchant chat turn must not echo the other merchant\'s data').not.toContain(`${marker}-demo`);
  for (const t of demoLines) expect(crossTurn.text, 'must not expose Northfield cart lines').not.toContain(t);

  // State outcome: nothing changed on either side.
  expect(await cartLines(page, DEMO, demoCart, keys[DEMO])).toEqual(demoLines);
  expect(await cartLines(page, KET, ketCart, keys[KET])).toEqual(ketLines);
  const after = await probe(page, 'GET', `/api/chat/${DEMO}/${demoSession}`, keys[DEMO]);
  expect(after.ok).toBe(true);
  expect(after.text).not.toContain(`${marker}-kettle`);

  expect(errors, 'no uncaught page errors').toEqual([]);
});

test('a proposal cannot be read, accepted or rejected through another merchant or another visitor, and its owner still can', async ({ page, browser }) => {
  test.setTimeout(180000);
  const keys = captureKeys(page);
  const errors = watchPageErrors(page);

  await page.goto(NORTHFIELD, { waitUntil: 'networkidle' });
  await addFirstProduct(page);
  const cartId = await storage(page, 'cv3_cart_conn_demo');
  await sendChat(page, `proposal-owner-${Date.now()}`);
  const sessionId = await storage(page, 'cv3_session_conn_demo');
  const linesBefore = await cartLines(page, DEMO, cartId, keys[DEMO]);

  const proposalId = makeProposal(sessionId, cartId, 'P3002'); // Performance Socks Three Pack
  await page.reload({ waitUntil: 'networkidle' });
  const suggestions = page.locator('.panel', { hasText: 'Suggestions' });
  await expect(suggestions, 'the owner sees the pending proposal').toBeVisible({ timeout: 10000 });
  await expect(suggestions.getByText(/P3002/)).toBeVisible();

  const listUrl = `/api/chat/proposals?connection_id=${DEMO}&session_id=${sessionId}&cart_id=${cartId}`;
  const ownerList = await probe(page, 'GET', listUrl, keys[DEMO]);
  expect(ownerList.ok, 'owner list must succeed (positive control)').toBe(true);
  expect(ownerList.text).toContain(proposalId);

  const ownBody = { connection_id: DEMO, session_id: sessionId, cart_id: cartId };
  const ketBody = { connection_id: KET, session_id: sessionId, cart_id: cartId };

  // Another MERCHANT's credentials.
  await page.goto(KETTLE, { waitUntil: 'networkidle' });
  expect(keys[KET], 'the storefront sent a Kettle key').toBeTruthy();
  expectRefused('Kettle key claiming Northfield: accept', await probe(page, 'POST', `/api/chat/proposals/${proposalId}/accept`, keys[KET], ownBody), ['P3002', 'Performance Socks']);
  expectRefused('Kettle key claiming Northfield: reject', await probe(page, 'POST', `/api/chat/proposals/${proposalId}/reject`, keys[KET], ownBody), ['P3002']);
  expectRefused('Kettle key on Kettle with Northfield session/cart: accept', await probe(page, 'POST', `/api/chat/proposals/${proposalId}/accept`, keys[KET], ketBody), ['P3002', 'Performance Socks']);
  expectRefused('Kettle key on Kettle with Northfield session/cart: reject', await probe(page, 'POST', `/api/chat/proposals/${proposalId}/reject`, keys[KET], ketBody), ['P3002']);
  expectRefused('Kettle key: list Northfield proposals', await probe(page, 'GET', listUrl, keys[KET]), [proposalId, 'P3002']);

  // Another VISITOR (different cookie) holding the same public Northfield key.
  const other = await newVisitor(browser, NORTHFIELD);
  try {
    expect(other.keys[DEMO], 'the second visitor holds the same public Northfield key').toBe(keys[DEMO]);
    expectRefused('second visitor: accept', await probe(other.page, 'POST', `/api/chat/proposals/${proposalId}/accept`, other.keys[DEMO], ownBody), ['P3002', 'Performance Socks']);
    expectRefused('second visitor: reject', await probe(other.page, 'POST', `/api/chat/proposals/${proposalId}/reject`, other.keys[DEMO], ownBody), ['P3002']);
    const otherList = await probe(other.page, 'GET', listUrl, other.keys[DEMO]);
    console.log(`  [isolation] second visitor: list owner's proposals -> HTTP ${otherList.status}`);
    expect(otherList.text, 'the second visitor must not see the owner\'s proposal').not.toContain(proposalId);
    expect(otherList.text).not.toContain('P3002');
    expect(other.errors, 'no uncaught page errors (second visitor)').toEqual([]);
  } finally {
    await other.context.close();
  }

  // State outcome: the proposal is still pending and the cart is untouched.
  await page.goto(NORTHFIELD, { waitUntil: 'networkidle' });
  const stillThere = await probe(page, 'GET', listUrl, keys[DEMO]);
  expect(stillThere.ok).toBe(true);
  expect(stillThere.text, 'the proposal survived every attack').toContain(proposalId);
  expect(stillThere.text).toMatch(/PENDING/);
  expect(await cartLines(page, DEMO, cartId, keys[DEMO]), 'no attack changed the cart').toEqual(linesBefore);
  const suggestionsAgain = page.locator('.panel', { hasText: 'Suggestions' });
  await expect(suggestionsAgain, 'the owner still sees the pending proposal').toBeVisible({ timeout: 10000 });

  // Positive control: the owner can still act on it.
  await suggestionsAgain.getByRole('button', { name: 'Accept' }).click();
  await expect(suggestionsAgain).toHaveCount(0, { timeout: 15000 });
  await expect(page.locator('.panel .line')).toHaveCount(linesBefore.length + 1, { timeout: 10000 });

  expect(errors, 'no uncaught page errors').toEqual([]);
});
