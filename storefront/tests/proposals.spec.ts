import { test, expect, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..', '..');
const NORTHFIELD = '/northfield';

/**
 * Browser test for the Suggestions (cart-proposal) panel.
 *
 * Model-free by design: `make_test_proposal.py` writes a real CartProposal
 * row directly through the same `engine.db.cart_proposals.create` the real
 * PROPOSE_CART_CHANGE execution path calls, rather than relying on the
 * model to pick that action for a given phrasing (which it does not do
 * reliably - see engine/reasoning/prompts.py). The panel, the accept/reject
 * routes, and the Decision-Engine/Risk-Gate execution underneath are all
 * real; only "getting a proposal to exist" is short-circuited, the same
 * shape `legacy_account.py` already uses to reach an account state the API
 * has no direct route to create.
 */

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
      // Cart not ready or no-op; try again.
    }
  }
  throw new Error('could not get an item into the cart');
}

async function readSessionAndCart(page: Page): Promise<{ sessionId: string; cartId: string }> {
  const sessionId = await page.evaluate(() => sessionStorage.getItem('cv3_session_conn_demo'));
  const cartId = await page.evaluate(() => sessionStorage.getItem('cv3_cart_conn_demo'));
  if (!sessionId || !cartId) {
    throw new Error(`missing session/cart in sessionStorage: ${sessionId} / ${cartId}`);
  }
  return { sessionId, cartId };
}

function makeProposal(sessionId: string, cartId: string, productId: string): string {
  const out = execFileSync(
    'python',
    ['scripts/make_test_proposal.py', 'conn_demo', sessionId, cartId, productId],
    { cwd: REPO_ROOT, encoding: 'utf8' },
  );
  const line = out.trim().split('\n').filter((l) => l.trim().startsWith('{'))[0];
  return JSON.parse(line).proposal_id as string;
}

test.describe('Cart proposals', () => {
  test('a pending proposal can be rejected without touching the cart', async ({ page }) => {
    await page.goto(NORTHFIELD, { waitUntil: 'networkidle' });
    await addFirstProduct(page);

    const itemCountBefore = await page.locator('.panel .num').first().textContent();
    const { sessionId, cartId } = await readSessionAndCart(page);

    makeProposal(sessionId, cartId, 'P3002'); // Performance Socks Three Pack
    // The panel refetches on mount and on every new chat turn - it does not
    // poll. A proposal created out of band (as this test does, to avoid a
    // model call) needs one of those triggers; a reload is a real one a
    // shopper revisiting the page would also produce.
    await page.reload({ waitUntil: 'networkidle' });

    const suggestions = page.locator('.panel', { hasText: 'Suggestions' });
    await expect(suggestions).toBeVisible({ timeout: 10000 });
    await expect(suggestions.getByText(/P3002/)).toBeVisible();

    await suggestions.getByRole('button', { name: 'No thanks' }).click();
    await expect(suggestions).toHaveCount(0, { timeout: 10000 });

    // The cart must be exactly what it was - reject mutates nothing.
    const itemCountAfter = await page.locator('.panel .num').first().textContent();
    expect(itemCountAfter).toBe(itemCountBefore);
  });

  test('a pending proposal can be accepted and actually changes the cart', async ({ page }) => {
    await page.goto(NORTHFIELD, { waitUntil: 'networkidle' });
    await addFirstProduct(page);

    const linesBefore = await page.locator('.panel .line').count();
    const { sessionId, cartId } = await readSessionAndCart(page);

    makeProposal(sessionId, cartId, 'P3002');
    await page.reload({ waitUntil: 'networkidle' });

    const suggestions = page.locator('.panel', { hasText: 'Suggestions' });
    await expect(suggestions).toBeVisible({ timeout: 10000 });

    await suggestions.getByRole('button', { name: 'Accept' }).click();

    // The suggestion resolves and disappears...
    await expect(suggestions).toHaveCount(0, { timeout: 15000 });

    // ...and the real Cart panel - not a locally-guessed state - now shows
    // one more line than before.
    await expect(page.locator('.panel .line')).toHaveCount(linesBefore + 1, { timeout: 10000 });
  });

  // Unlike the two tests above, this one does NOT use make_test_proposal.py -
  // it drives the real chat turn a shopper would type, to prove the actual
  // gap found by an earlier value-auditor pass (the model did not reliably
  // choose CART_INTELLIGENCE for phrasing that plainly asked about the
  // cart) is now closed by the deterministic bridge in
  // engine/reasoning/cart_intent.py - not just closed in a backend script,
  // but reachable by a real shopper typing a real sentence in this UI.
  // Costs one real Groq model call (the turn's own reasoning pass runs
  // regardless of what the deterministic bridge adds).
  test('a real chat question about the cart produces a real, visible suggestion', async ({ page }) => {
    await page.goto(NORTHFIELD, { waitUntil: 'networkidle' });
    await addFirstProduct(page);

    const chatlaunch = page.locator('.chatlaunch');
    if (await chatlaunch.isVisible()) await chatlaunch.click();

    const composer = page.getByPlaceholder(/Type a message|message/i);
    await composer.fill('Is there anything wrong with my cart, or anything I should know?');
    await composer.press('Enter');

    // The assistant's own reply, then the Suggestions panel appearing from
    // the same turn - both driven by the real request/response cycle, no
    // reload and no injected row.
    await expect(page.locator('.bubble.assistant').last()).toBeVisible({ timeout: 45000 });

    const suggestions = page.locator('.panel', { hasText: 'Suggestions' });
    await expect(suggestions).toBeVisible({ timeout: 15000 });
  });
});
