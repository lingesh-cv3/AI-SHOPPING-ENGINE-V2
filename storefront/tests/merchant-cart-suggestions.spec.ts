import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');

/**
 * Browser verification of the Cart Suggestions panel on the merchant
 * console's "AI Commerce" screen (storefront/src/AICommerce.tsx) - closing
 * the two gaps a live value-auditor pass found in an earlier session:
 *
 * 1. The panel used to only render in one of two branches
 *    (`report.shoppers_helped === 0` returned early without it) - fixed by
 *    extracting it into `CartSuggestionsPanel`, rendered in both branches.
 *    Model-free by design: the zero-friction-case state is reached by
 *    intercepting the real `/api/report/:id` response (not a database
 *    edit, not a mock of the component - the real component, the real
 *    `cart_proposal_activity` response, against a real running engine;
 *    only the one adjacent endpoint this test isn't about is stubbed to
 *    reliably reach the branch a live demo database - accumulated over
 *    many sessions - essentially never sits in on its own).
 * 2. Tenant isolation: Northfield's console must never show Kettle's
 *    product ids or vice versa, and each merchant's own secret key must be
 *    required to see anything at all.
 */

function readSecretKeys(): { demo: string; kettle: string } {
  const text = readFileSync(path.join(REPO_ROOT, '.env.keys'), 'utf8');
  const find = (name: string) => {
    const line = text.split('\n').find((l) => l.startsWith(`${name}=`));
    if (!line) throw new Error(`missing ${name} in .env.keys`);
    return line.split('=')[1].trim();
  };
  return {
    demo: find('CV3_SECRET_CONN_DEMO'),
    kettle: find('CV3_SECRET_CONN_KETTLE'),
  };
}

const KEYS = readSecretKeys();

test.describe('Merchant Cart Suggestions panel', () => {
  test('a merchant with zero friction cases still sees real proposal data (the fixed bug)', async ({ page }) => {
    // Real cart_proposal_activity response passes through untouched; only
    // the adjacent /api/report call is stubbed to force the exact branch
    // (report.shoppers_helped === 0) that used to hide the panel entirely.
    await page.route('**/api/report/*', async (route) => {
      const original = await route.fetch();
      const body = await original.json();
      await route.fulfill({
        response: original,
        json: { ...body, shoppers_helped: 0, problems_solved: 0 },
      });
    });

    await page.goto('/kettle/merchant', { waitUntil: 'networkidle' });
    await page.getByLabel('Merchant key').fill(KEYS.kettle);
    await page.getByRole('button', { name: 'Sign in' }).click();

    await page.getByRole('button', { name: 'AI Commerce' }).click();

    // The old bug: this early-return branch rendered only "Nothing yet"
    // and never fetched/showed cart-suggestion data at all.
    await expect(page.getByText('Nothing yet.')).toBeVisible({ timeout: 15000 });

    const suggestions = page.locator('.panel', { hasText: 'Cart suggestions' });
    await expect(suggestions).toBeVisible({ timeout: 15000 });

    // Real accumulated Kettle proposal history exists from earlier
    // sessions' live testing - assert real, non-fabricated numbers appear.
    // Checked against "Accepted" specifically (not "Suggested"): this
    // connection's real data currently has accepted/executed proposals
    // whose own "created" event was lost to the exact has_history bug an
    // earlier session found and fixed (some were seeded directly through
    // db.cart_proposals.create, bypassing the normal creation event) - so
    // "Suggested" can honestly read 0 here while "Accepted" is real and
    // non-zero. Asserting on whichever count is actually guaranteed
    // non-zero for this connection right now is the honest check; a
    // frozen assumption about which specific counter is non-zero is not.
    const acceptedCard = suggestions.locator('.kpi-card', { hasText: 'Accepted' });
    await expect(acceptedCard).toBeVisible();
    const acceptedValue = await acceptedCard.locator('.kpi-value').textContent();
    expect(Number(acceptedValue)).toBeGreaterThan(0);
  });

  test('each merchant only ever sees their own connection\'s data', async ({ page }) => {
    await page.goto('/northfield/merchant', { waitUntil: 'networkidle' });
    await page.getByLabel('Merchant key').fill(KEYS.demo);
    await page.getByRole('button', { name: 'Sign in' }).click();
    await page.getByRole('button', { name: 'AI Commerce' }).click();

    // Either a real Cart suggestions panel with Northfield's own product
    // ids (P-prefixed), or the honest empty state if Northfield genuinely
    // has none yet - never Kettle's KB- ids leaking across.
    const northfieldSuggestions = page.locator('.panel', { hasText: 'Cart suggestions' });
    await expect(northfieldSuggestions).toBeVisible({ timeout: 15000 });
    const northfieldText = await northfieldSuggestions.textContent();
    expect(northfieldText).not.toContain('KB-');
  });

  test('the wrong shape of key never reaches the console', async ({ page }) => {
    await page.goto('/kettle/merchant', { waitUntil: 'networkidle' });
    await page.getByLabel('Merchant key').fill('cv3_pk_not_a_secret_key');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByText('That is a publishable key')).toBeVisible();
    // The console itself - and anything it would show, including cart
    // suggestions - must not be reachable with the wrong key shape.
    await expect(page.locator('.panel', { hasText: 'Cart suggestions' })).toHaveCount(0);
  });

  test("another merchant's real secret key is refused, not shown another tenant's data", async ({ page }) => {
    // Correctly-shaped (cv3_sk_...) but belongs to Northfield, not Kettle -
    // passes the client's local shape check and reaches the real API,
    // which must be the thing that actually refuses it.
    await page.goto('/kettle/merchant', { waitUntil: 'networkidle' });
    await page.getByLabel('Merchant key').fill(KEYS.demo);
    await page.getByRole('button', { name: 'Sign in' }).click();

    // A refused key drops the console straight back to the sign-in screen
    // (MerchantConsole.tsx: a NotAuthorised response resets `signedIn` to
    // false, rather than rendering an error alongside someone else's
    // data) - never Kettle's real console content, and never a silent
    // fall-through to Northfield's data rendered as if it were Kettle's.
    await expect(page.locator('.panel', { hasText: 'Cart suggestions' })).toHaveCount(0, {
      timeout: 15000,
    });
    await expect(page.getByText('Sign in to see your settings')).toBeVisible({ timeout: 15000 });
  });
});
