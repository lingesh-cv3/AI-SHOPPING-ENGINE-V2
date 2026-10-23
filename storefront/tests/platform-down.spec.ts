import { test, expect, type Page } from '@playwright/test';
import { execFileSync, spawn } from 'node:child_process';
import { openSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Browser-level behaviour when a service the shopper depends on goes away, and comes back.
 *
 * Model-free and payment-free; nothing here calls an external service. The "outage" is a
 * real one: the test stops the Kettle merchant process, or the engine process, and starts it
 * again afterwards. That is what makes this different from a mocked 500: it exercises the
 * engine's real adapter-failure path and the storefront's real fetch-failure path.
 *
 * Asserted for each outage:
 *   - the shopper is told something is wrong (an honest, plain notice) - never a blank
 *     failure and never a raw code, stack or proxy message;
 *   - no fabricated success: a failed add leaves the cart unchanged, a failed chat turn
 *     shows no product cards and no "added to your cart" claim;
 *   - a failed catalogue load is never presented as an EMPTY shop ("Nothing in this
 *     category yet.") - an outage is not the same fact as an empty category;
 *   - one merchant being down does not take the other merchant down;
 *   - when the service returns the storefront recovers without the shopper losing their cart.
 *
 * Because it stops and starts processes, it only runs when CV3_OUTAGE_TESTS=1, and it refuses
 * to run if DATABASE_URL is missing or names cv3.db. It stops a process only after checking
 * that the process on the port really is the expected service.
 */

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');
const NORTHFIELD = '/northfield';
const KETTLE = '/kettle';
const ENGINE_PORT = 8000;
const KETTLE_PORT = 8002;

const NOTICE = /(went wrong|try (that )?again|unavailable|can.?t reach|couldn.?t|could not|not reachable|temporar|slow)/i;
/** A shop-level announcement (role=alert) that the catalogue could not be loaded. */
const SHOP_DOWN = /(can.?t reach|unavailable|not responding)/i;
const EMPTY_SHOP_CLAIM = /Nothing in this category yet/i;
const FABRICATED_SUCCESS = /(I'?ve added|has been added|added .{0,60} to your cart|your cart (now|is)|order (placed|confirmed))/i;
const RAW_LEAK = /(ECONNREFUSED|proxy error|Traceback|undefined|\[object|EngineError|platform_unavailable|status code)/i;

test.describe.configure({ mode: 'serial' });

test.skip(process.env.CV3_OUTAGE_TESTS !== '1', 'set CV3_OUTAGE_TESTS=1 (it stops and restarts local services)');

test.beforeAll(() => {
  const url = process.env.DATABASE_URL ?? '';
  if (!url || /cv3\.db(\?|$)/.test(url)) {
    throw new Error('platform-down: DATABASE_URL must name an isolated database (it is "' + url + '").');
  }
});

/* ---------------- process control (checked, local, restorable) ---------------- */

/**
 * Runs a PowerShell command and returns its output. `; exit 0` because PowerShell reports exit
 * code 1 whenever the LAST statement "failed" - including Stop-Process on a process that already
 * went away (a venv launcher exits by itself when its child is stopped) - even under
 * -ErrorAction SilentlyContinue. Success/failure here is judged from the output, not the code.
 */
function ps(command: string): string {
  return execFileSync('powershell', ['-NoProfile', '-NonInteractive', '-Command', `${command}; exit 0`], { encoding: 'utf8' }).trim();
}

interface Listener { pid: number; parent: number; cmd: string }

function listener(port: number): Listener | null {
  const out = ps(
    `$c=Get-NetTCPConnection -LocalPort ${port} -State Listen -ErrorAction SilentlyContinue | Select -First 1; ` +
      `if($c){ $p=Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)"; "$($p.ProcessId)|$($p.ParentProcessId)|$($p.CommandLine)" }`,
  );
  if (!out) return null;
  const [pid, parent, ...rest] = out.split('|');
  return { pid: Number(pid), parent: Number(parent), cmd: rest.join('|') };
}

async function waitUntil(label: string, check: () => boolean | Promise<boolean>, ms = 60000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (await check()) return;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`timed out waiting for: ${label}`);
}

async function httpOk(url: string): Promise<boolean> {
  try {
    const r = await fetch(url, { signal: AbortSignal.timeout(2000) });
    return r.ok;
  } catch {
    return false;
  }
}

/** Stops the service on `port` - but only if its command line really is `expected`. */
async function stopService(port: number, expected: string) {
  const l = listener(port);
  if (!l) return;
  if (!l.cmd.includes(expected)) {
    throw new Error(`refusing to stop port ${port}: its process is not "${expected}" (${l.cmd.slice(0, 80)})`);
  }
  const parent = ps(`(Get-CimInstance Win32_Process -Filter "ProcessId=${l.parent}").CommandLine`);
  ps(`Stop-Process -Id ${l.pid} -ErrorAction SilentlyContinue`);
  if (parent.includes(expected)) ps(`Stop-Process -Id ${l.parent} -ErrorAction SilentlyContinue`);
  await waitUntil(`port ${port} to close`, () => listener(port) === null, 20000);
}

function startDetached(args: string[], logName: string) {
  const dir = process.env.CV3_OUTAGE_LOGDIR || os.tmpdir();
  const out = openSync(path.join(dir, `${logName}.out`), 'a');
  const err = openSync(path.join(dir, `${logName}.err`), 'a');
  const child = spawn(process.env.CV3_PYTHON || 'python', args, {
    cwd: REPO_ROOT, detached: true, stdio: ['ignore', out, err], windowsHide: true, env: process.env,
  });
  child.unref();
}

async function startKettle() {
  if (listener(KETTLE_PORT)) return;
  startDetached(['-m', 'uvicorn', 'sample_merchant_two.api.main:app', '--port', String(KETTLE_PORT)], 'outage-kettle');
  await waitUntil('Kettle merchant to answer', () => httpOk(`http://127.0.0.1:${KETTLE_PORT}/openapi.json`));
}

async function startEngine() {
  if (listener(ENGINE_PORT)) return;
  // Same shape the suite always uses: provider keys forced empty inside the process.
  startDetached(
    ['-c', "import os; os.environ['OPENAI_API_KEY']=''; os.environ['GROQ_API_KEY']=''; import uvicorn; uvicorn.run('engine.api.main:app', port=8000)"],
    'outage-engine',
  );
  await waitUntil('the engine to answer', () => httpOk(`http://127.0.0.1:${ENGINE_PORT}/api/connections`), 90000);
}

test.afterAll(async () => {
  // Whatever happened above, leave both services running for whoever shuts the stack down.
  await startKettle();
  await startEngine();
});

/* ---------------- storefront helpers ---------------- */

function watchPageErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
  return errors;
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

const cartNotice = (page: Page) => page.locator('.panel p.note').filter({ hasText: NOTICE });

async function expectHonestNotice(page: Page, where: string) {
  await expect(cartNotice(page).first(), `${where}: the shopper must be told something went wrong`).toBeVisible({ timeout: 8000 });
  const text = await page.locator('body').innerText();
  expect(text, `${where}: no raw error leaks into the page`).not.toMatch(RAW_LEAK);
}

/** Sends a chat message and waits for the REAL reply (the "Thinking…" bubble gone), up to 60s. */
async function chatTurn(page: Page, message: string): Promise<{ text: string; seconds: number }> {
  const launcher = page.locator('.chatlaunch');
  if (await launcher.isVisible()) await launcher.click();
  await expect(page.locator('.chatwidget')).toBeVisible();
  await page.getByPlaceholder('Type a message').fill(message);
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.locator('.chatbody')).toContainText(message, { timeout: 10000 });
  const started = Date.now();
  await expect(
    page.locator('.bubble.assistant.thinking'),
    'the shopper must get an answer, not an endless "Thinking…"',
  ).toHaveCount(0, { timeout: 60000 });
  const seconds = (Date.now() - started) / 1000;
  await expect(page.locator('.bubble.assistant:not(.thinking)').last(), 'an actual assistant reply is shown').toBeVisible();
  const text = await page.locator('.chatbody').innerText();
  console.log(`  [outage] chat "${message}" answered after ${seconds.toFixed(1)}s`);
  return { text, seconds };
}

/* ---------------- tests ---------------- */

test('Kettle merchant down: the Kettle shop is honest about it, Northfield is unaffected, and Kettle recovers', async ({ page, context }) => {
  test.setTimeout(300000);
  const errors = watchPageErrors(page);

  await page.goto(KETTLE, { waitUntil: 'networkidle' });
  await addFirstProduct(page);
  const linesBefore = await page.locator('.panel .line').count();
  expect(await page.locator('article.card').count()).toBeGreaterThan(0);

  await stopService(KETTLE_PORT, 'sample_merchant_two.api.main');
  try {
    // (a) A cart change while the merchant is down: refused honestly, cart unchanged.
    const add = page.locator('article.card button', { hasText: 'Add to cart' }).first();
    await add.click();
    await page.waitForTimeout(3000);
    await expectHonestNotice(page, 'add to cart while the merchant is down');
    expect(await page.locator('.panel .line').count(), 'a failed add must not create a cart line').toBe(linesBefore);

    // (b) A chat turn while the merchant is down: no products, no success claim, an actual reply.
    const { text: chat } = await chatTurn(page, 'show me coffee');
    expect(chat, 'the failed turn must not claim the cart changed').not.toMatch(FABRICATED_SUCCESS);
    expect(chat, 'no raw error leaks into the chat').not.toMatch(RAW_LEAK);
    expect(await page.locator('.chatproduct').count(), 'no fabricated product cards').toBe(0);
    const reply = chat.replace('show me coffee', '').trim();
    expect(reply.length, 'the shopper gets an actual reply, not silence').toBeGreaterThan(10);
    await page.locator('.chatclose').click();

    // (c) A fresh visit while the merchant is down: a failed load is not an empty shop.
    const fresh = await context.newPage();
    const freshErrors = watchPageErrors(fresh);
    await fresh.goto(KETTLE, { waitUntil: 'networkidle' });
    await fresh.waitForTimeout(2500);
    const freshText = await fresh.locator('body').innerText();
    expect(freshText, 'an outage must not be presented as an empty category').not.toMatch(EMPTY_SHOP_CLAIM);
    await expect(fresh.getByRole('alert').filter({ hasText: SHOP_DOWN }), 'a failed catalogue load is announced to the shopper').toBeVisible();
    await expectHonestNotice(fresh, 'fresh Kettle visit while the merchant is down');
    expect(await fresh.locator('article.card').count(), 'no fabricated catalogue').toBe(0);
    expect(freshErrors, 'no uncaught page errors on the fresh visit').toEqual([]);
    await fresh.close();

    // (d) The OTHER merchant is unaffected, end to end.
    const north = await context.newPage();
    const northErrors = watchPageErrors(north);
    await north.goto(NORTHFIELD, { waitUntil: 'networkidle' });
    expect(await north.locator('article.card').count(), 'Northfield still shows its catalogue').toBeGreaterThan(0);
    await addFirstProduct(north);
    expect(await north.locator('.panel .line').count(), 'Northfield can still take an item').toBeGreaterThan(0);
    expect(northErrors, 'no uncaught page errors on Northfield').toEqual([]);
    await north.close();
  } finally {
    await startKettle();
  }

  // (e) Recovery: the service is back, the shopper can shop again.
  const back = await context.newPage();
  const backErrors = watchPageErrors(back);
  await back.goto(KETTLE, { waitUntil: 'networkidle' });
  expect(await back.locator('article.card').count(), 'the Kettle catalogue is back').toBeGreaterThan(0);
  await addFirstProduct(back);
  expect(await back.locator('.panel .line').count(), 'Kettle takes an item again').toBeGreaterThan(0);
  expect(backErrors, 'no uncaught page errors after recovery').toEqual([]);
  await back.close();

  expect(errors, 'no uncaught page errors during the outage').toEqual([]);
});

test('engine down: the storefront is honest about it, never fabricates a result, and recovers with the cart intact', async ({ page, context }) => {
  test.setTimeout(300000);
  const errors = watchPageErrors(page);

  await page.goto(NORTHFIELD, { waitUntil: 'networkidle' });
  await addFirstProduct(page);
  const linesBefore = await page.locator('.panel .line').count();
  expect(linesBefore).toBeGreaterThan(0);
  const cardsBefore = await page.locator('article.card').count();
  expect(cardsBefore).toBeGreaterThan(1);

  await stopService(ENGINE_PORT, 'engine.api.main');
  try {
    // (a) A cart change with the engine down: refused honestly, cart unchanged.
    const add = page.locator('article.card button', { hasText: 'Add to cart' }).nth(1);
    await add.click();
    await page.waitForTimeout(3000);
    await expectHonestNotice(page, 'add to cart while the engine is down');
    expect(await page.locator('.panel .line').count(), 'a failed add must not create a cart line').toBe(linesBefore);

    // (b) A chat turn with the engine down.
    const { text: chat } = await chatTurn(page, 'show me socks');
    expect(chat, 'the failed turn must not claim the cart changed').not.toMatch(FABRICATED_SUCCESS);
    expect(chat, 'no raw error leaks into the chat').not.toMatch(RAW_LEAK);
    expect(await page.locator('.chatproduct').count(), 'no fabricated product cards').toBe(0);
    expect(chat.replace('show me socks', '').trim().length, 'the shopper gets an actual reply').toBeGreaterThan(10);
    await page.locator('.chatclose').click();

    // (c) A fresh visit with the engine down: a failed load is not an empty shop.
    const fresh = await context.newPage();
    const freshErrors = watchPageErrors(fresh);
    await fresh.goto(NORTHFIELD, { waitUntil: 'networkidle' });
    await fresh.waitForTimeout(2500);
    const freshText = await fresh.locator('body').innerText();
    expect(freshText, 'an outage must not be presented as an empty category').not.toMatch(EMPTY_SHOP_CLAIM);
    await expect(fresh.getByRole('alert').filter({ hasText: SHOP_DOWN }), 'a failed catalogue load is announced to the shopper').toBeVisible();
    await expectHonestNotice(fresh, 'fresh visit while the engine is down');
    expect(await fresh.locator('article.card').count(), 'no fabricated catalogue').toBe(0);
    expect(freshErrors, 'no uncaught page errors on the fresh visit').toEqual([]);
    await fresh.close();
  } finally {
    await startEngine();
  }

  // (d) Recovery: back up, the same shopper still has their cart, and can shop and chat again.
  await page.reload({ waitUntil: 'networkidle' });
  expect(await page.locator('article.card').count(), 'the catalogue is back').toBeGreaterThan(0);
  expect(await page.locator('.panel .line').count(), 'the cart survived the outage').toBe(linesBefore);
  const { text: reply } = await chatTurn(page, 'show me socks again');
  expect(reply, 'the engine answers again').not.toMatch(RAW_LEAK);
  expect(reply.replace('show me socks again', '').trim().length).toBeGreaterThan(10);

  expect(errors, 'no uncaught page errors during the outage').toEqual([]);
});
