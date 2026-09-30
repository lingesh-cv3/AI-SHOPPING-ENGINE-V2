import { test, expect, request as pwRequest, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');

/**
 * Browser verification of CV3 Operations: the cross-merchant Overview, what it will
 * and will not claim, and how it behaves when data is slow, missing or stale.
 *
 * Real engine, real data. The figures on screen are compared with the engine's own
 * answers. The only mocked responses are the ones that cannot be produced any other
 * way (slow, failed, refused, empty); they live in this file and nowhere in the app.
 *
 * OPS_URL picks the address under test. It defaults to /operations; before that route
 * is switched to the new console it can point at a local harness.
 */

const OPS_URL = process.env.OPS_URL ?? '/operations';
const ENGINE = 'http://127.0.0.1:8000';

function readKey(name: string): string {
  const text = readFileSync(path.join(REPO_ROOT, '.env.keys'), 'utf8');
  const line = text.split('\n').find((l) => l.startsWith(`${name}=`));
  if (!line) throw new Error(`missing ${name} in .env.keys`);
  return line.split('=')[1].trim();
}

const OPERATOR = readKey('CV3_OPERATOR_KEY');
const SK_DEMO = readKey('CV3_SECRET_CONN_DEMO');

async function signIn(page: Page) {
  await page.goto(OPS_URL, { waitUntil: 'networkidle' });
  await page.getByLabel('Operator key').fill(OPERATOR);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('navigation', { name: 'Operations sections' })).toBeVisible();
}

function row(page: Page, label: string) {
  return page.locator('.ops-row').filter({ hasText: label });
}

async function engineGet(pathname: string) {
  const api = await pwRequest.newContext();
  const r = await api.get(`${ENGINE}${pathname}`, { headers: { Authorization: `Bearer ${OPERATOR}` } });
  const body = await r.json();
  await api.dispose();
  return body;
}

test('it needs the operator key: a merchant key is refused before it is sent', async ({ page }) => {
  await page.goto(OPS_URL, { waitUntil: 'networkidle' });
  await expect(page.getByRole('heading', { name: 'Sign in to CV3 Operations' })).toBeVisible();
  await page.getByLabel('Operator key').fill(SK_DEMO);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByText(/looks like a merchant key/)).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Operations sections' })).toHaveCount(0);
});

test('signed in: CV3 identity, only sections with a real source, and no approval controls', async ({ page }) => {
  await signIn(page);

  await expect(page).toHaveTitle('CV3 Operations');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'cv3-ops');
  await expect(page.getByRole('heading', { name: 'CV3 Operations', level: 1 })).toBeVisible();

  // Only what has a backing source today - nothing greyed out, nothing "coming soon".
  const nav = page.getByRole('navigation', { name: 'Operations sections' });
  await expect(nav.getByRole('button')).toHaveText(['Overview', 'Copilot']);

  // Merchant-owned decisions are not an Operations workflow.
  const body = page.locator('body');
  await expect(body).not.toContainText('Waiting for a decision');
  await expect(body).not.toContainText('Already decided');
  await expect(page.getByRole('button', { name: /approve|reject/i })).toHaveCount(0);
});

test("the Overview's figures are the engine's own", async ({ page }) => {
  const [handovers, stats, connections] = await Promise.all([
    engineGet('/api/ops/handovers?offset=0&limit=50'),
    engineGet('/api/ops/stats'),
    (async () => {
      const api = await pwRequest.newContext();
      const r = await api.get(`${ENGINE}/api/connections`);
      const b = await r.json();
      await api.dispose();
      return b as { connection_id: string; merchant_name: string; mode: string; unsupported: string[] }[];
    })(),
  ]);

  await signIn(page);

  await expect(row(page, 'Shopper follow-ups nobody has picked up').locator('.num')).toHaveText(
    String(handovers.total),
    { timeout: 30000 },
  );
  await expect(row(page, 'Decisions waiting on merchants').locator('.num')).toHaveText(
    String(stats.waiting),
    { timeout: 30000 },
  );

  const rows = page.getByTestId('portfolio-row');
  await expect(rows).toHaveCount(connections.length, { timeout: 30000 });
  for (const c of connections) {
    const r = rows.filter({ hasText: c.merchant_name });
    await expect(r).toHaveCount(1);
    await expect(r.locator('td').nth(2)).toHaveText(c.mode.toLowerCase());
    await expect(r.locator('td').nth(3)).toHaveText(String(c.unsupported.length));
    await expect(r.locator('td').nth(4)).toHaveText(String(stats.by_merchant[c.merchant_name] ?? 0));
  }
});

test('what it does not know, it says - and it invents nothing', async ({ page }) => {
  await signIn(page);

  await expect(row(page, 'Incidents')).toContainText('Not tracked yet');
  await expect(page.getByText(/Operational health .* is not\s+measured yet/)).toBeVisible({ timeout: 30000 });

  const coverage = page.getByRole('region', { name: 'What Operations can see today' });
  await expect(coverage).toContainText('Incidents and alerts');
  await expect(coverage).toContainText('Order and payment failures');
  await expect(coverage).toContainText('maintained by hand');

  const body = page.locator('body');
  await expect(body).not.toContainText(/healthy/i);
  await expect(body).not.toContainText(/at risk/i);
  await expect(body).not.toContainText(/revenue at/i);
});

test('panels load independently: slow says loading, failed says could not load - never zero', async ({ page }) => {
  // Slow: hold the stats call.
  let release: () => void = () => {};
  const gate = new Promise<void>((r) => (release = r));
  await page.route('**/api/ops/stats', async (route) => {
    await gate;
    await route.continue();
  });
  await signIn(page);
  await expect(row(page, 'Decisions waiting on merchants')).toContainText('Loading…');
  await expect(row(page, 'Decisions waiting on merchants').locator('.num')).toHaveCount(0);
  // ...while a panel that has answered is already showing.
  await expect(row(page, 'Shopper follow-ups nobody has picked up').locator('.num')).toBeVisible({ timeout: 30000 });
  release();
  await expect(row(page, 'Decisions waiting on merchants').locator('.num')).toBeVisible({ timeout: 30000 });
  await page.unroute('**/api/ops/stats');

  // Failed: a fresh page where the follow-ups call cannot complete.
  await page.route('**/api/ops/handovers**', (route) => route.abort());
  await page.reload({ waitUntil: 'networkidle' });
  await expect(row(page, 'Shopper follow-ups nobody has picked up')).toContainText('Could not load', { timeout: 30000 });
  await expect(row(page, 'Shopper follow-ups nobody has picked up').locator('.num')).toHaveCount(0);
  // The other panels are unaffected, and the follow-ups column says it is unknown.
  await expect(row(page, 'Decisions waiting on merchants').locator('.num')).toBeVisible();
  const firstRow = page.getByTestId('portfolio-row').first();
  await expect(firstRow.locator('td').nth(5)).toHaveText('—');
});

test('zero data is stated, not drawn as a problem or hidden', async ({ page }) => {
  await page.route('**/api/connections', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/ops/handovers**', (route) => route.fulfill({ json: { handovers: [], total: 0 } }));
  await page.route('**/api/ops/stats', (route) =>
    route.fulfill({ json: { waiting: 0, oldest_wait_minutes: 0, by_merchant: {}, today: 0 } }),
  );
  await signIn(page);
  await expect(page.getByText('No merchants are connected.')).toBeVisible({ timeout: 30000 });
  await expect(row(page, 'Shopper follow-ups nobody has picked up').locator('.num')).toHaveText('0');
  await expect(row(page, 'Decisions waiting on merchants').locator('.num')).toHaveText('0');
});

test('a failed refresh keeps the last data and says it is stale', async ({ page }) => {
  await signIn(page);
  const figure = row(page, 'Decisions waiting on merchants').locator('.num');
  await expect(figure).toBeVisible({ timeout: 30000 });
  const before = await figure.textContent();

  await page.route('**/api/ops/stats', (route) => route.abort());
  await page.getByRole('button', { name: 'Refresh' }).click();
  await expect(page.getByText(/Could not refresh/).first()).toBeVisible({ timeout: 30000 });
  await expect(figure).toHaveText(before ?? '');
});

test('the copilot is labelled with the narrow scope it really has', async ({ page }) => {
  await signIn(page);
  await page.getByRole('navigation', { name: 'Operations sections' }).getByRole('button', { name: 'Copilot' }).click();

  // Not presented as the finished Operations Intelligence Copilot.
  const note = page.getByRole('note');
  await expect(note).toContainText('Limited scope');
  await expect(note).toContainText('not the Operations Intelligence Copilot');
  await expect(note).toContainText('no incident, health, integration, order, payment or revenue data');
  await expect(page.getByText('Workload copilot', { exact: true })).toBeVisible();
  await expect(page.getByText('Read-only')).toBeVisible();

  // Its suggestions are only things that data can answer - nothing about incidents or health.
  const chips = page.locator('.copilot-chip');
  await expect(chips).toHaveCount(3);
  for (const text of await chips.allInnerTexts()) {
    expect(text).not.toMatch(/incident|health|outage|latency|revenue|approve|decided/i);
  }
  await expect(page.getByRole('button', { name: /follow-ups nobody has picked up/ })).toBeVisible();
});

test('signing out forgets the key, and a refused key returns to sign-in', async ({ page }) => {
  await signIn(page);
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in to CV3 Operations' })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem('cv3_operator_key'))).toBeNull();
  await page.reload({ waitUntil: 'networkidle' });
  await expect(page.getByRole('heading', { name: 'Sign in to CV3 Operations' })).toBeVisible();

  // A key the engine refuses is treated the same way.
  await signIn(page);
  await page.route('**/api/ops/stats', (route) => route.fulfill({ status: 401, json: { detail: 'refused' } }));
  await page.getByRole('button', { name: 'Refresh' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in to CV3 Operations' })).toBeVisible({ timeout: 30000 });
});
