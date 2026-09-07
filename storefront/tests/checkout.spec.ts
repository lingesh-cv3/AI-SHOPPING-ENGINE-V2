import { test, expect, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * Browser test suite for the checkout flow and for transcript integrity.
 *
 * The checkout legs (add to cart, gate, sign up, pay) are model-free - REST
 * routes only - so they run at full speed regardless of the Groq throttle.
 *
 * The transcript tests need one thing the model decides: which action it
 * picks for a dead search. Getting size *choices* onto screen means tapping a
 * product that turns out to need one, and only the model knows which of the
 * recommended products that will be - so the tests try each recommended
 * product in turn rather than assuming the first one is right.
 *
 * Serial only (see playwright.config.ts): every test drives the same demo
 * engine and database, and more than one worker at a time both races cart
 * bootstrap and burns the per-minute model budget across tests that never
 * needed to overlap.
 */

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const NORTHFIELD = '/northfield';

async function addFirstProduct(page: Page) {
  const addButton = page.locator('button', { hasText: 'Add to cart' }).first();
  await addButton.waitFor({ timeout: 15000 });
  // Give the cart bootstrap a moment before the first click - clicking before
  // the cart exists is a guaranteed no-op.
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

async function signUp(page: Page, username: string, email: string) {
  await page.getByRole('link', { name: 'Create an account' }).click();
  await page.getByLabel('Username').fill(username);
  await page.getByLabel('Password').fill('password123');
  await page.getByLabel('Email').fill(email);
  await page.getByRole('button', { name: 'Create account' }).click();
}

function mintLegacyAccount(): { username: string; password: string } {
  const out = execFileSync('python', ['scripts/legacy_account.py'], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
  });
  const line = out.trim().split('\n').filter((l) => l.trim().startsWith('{'))[0];
  return JSON.parse(line);
}

/** Trigger a dead search, which opens the chat with recommended products - the
 *  one model call these transcript tests need. Costs one Groq turn. */
async function triggerDeadSearchProducts(page: Page) {
  const stamp = Date.now();
  await page.getByPlaceholder(/Shoes, tights, gels|Search/).fill(`nonexistent-item-${stamp}`);
  await page.getByRole('button', { name: 'SEARCH' }).click();

  // The model call this triggers can take a while under the Groq throttle -
  // longer than the usual UI timeout is deliberate here, not a fudge.
  const chatWidget = page.locator('.chatwidget');
  await chatWidget.waitFor({ timeout: 45000 });
  await page.locator('.chatproduct').first().waitFor({ timeout: 45000 });
}

/** Tap recommended products one at a time until one produces size choices.
 *  Most of Northfield's catalog has more than one buyable size, so this
 *  usually succeeds on the first try - the loop exists because which
 *  products get recommended is the model's call, not ours. */
async function tapUntilChoicesAppear(page: Page): Promise<boolean> {
  const products = page.locator('.chatproduct');
  const count = Math.min(await products.count(), 4);

  for (let i = 0; i < count; i++) {
    await products.nth(i).click();
    await page.waitForTimeout(2500);
    if (await page.locator('.optionbtn').count() > 0) return true;
  }
  return false;
}

test.describe('Checkout flow', () => {
  test('Leg A: guest adds item, hits gate, creates account with email, pays', async ({ page }) => {
    await page.goto(NORTHFIELD, { waitUntil: 'networkidle' });
    await addFirstProduct(page);

    // The gate: no card picker, no pay button, and the sign-in prompt.
    await expect(page.locator('.gate-label', { hasText: 'Sign in so we can send your order confirmation' })).toBeVisible();
    await expect(page.locator('select.card-picker')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Pay now' })).toHaveCount(0);

    // What the guest basket actually holds, so the claim below is that THIS
    // basket survived sign-up, not merely that a basket happens to exist.
    const linesBefore = await page.locator('.panel .line').allTextContents();
    expect(linesBefore.length).toBeGreaterThan(0);

    const stamp = Date.now();
    await signUp(page, `walka_${stamp}`, `walka_${stamp}@example.com`);

    // Back at the shop, the card buttons now exist - and the basket is the
    // same one the guest built, not an empty one the account started fresh.
    await expect(page.locator('select.card-picker')).toBeVisible({ timeout: 15000 });
    const linesAfter = await page.locator('.panel .line').allTextContents();
    expect(linesAfter).toEqual(linesBefore);

    await page.getByRole('button', { name: 'Pay now' }).click();

    const orderTitle = page.locator('.order-ok .detail-title');
    await expect(orderTitle).toBeVisible({ timeout: 15000 });
    expect((await orderTitle.textContent())?.trim()).toBeTruthy();
  });

  test('Leg B: a legacy account (no email) is prompted for one, then sees the card picker', async ({ page }) => {
    const legacy = mintLegacyAccount();

    await page.goto(NORTHFIELD, { waitUntil: 'networkidle' });
    await page.locator('.panel', { hasText: 'Nothing here yet' }).waitFor({ timeout: 15000 });
    await addFirstProduct(page);

    await page.goto(`${NORTHFIELD}/signin`, { waitUntil: 'networkidle' });
    await page.getByLabel('Username').fill(legacy.username);
    await page.getByLabel('Password').fill(legacy.password);
    await page.getByRole('button', { name: 'Sign in' }).click();

    const emailGate = page.locator('.gate-label', { hasText: 'Add your email so we can send the order confirmation' });
    await expect(emailGate).toBeVisible({ timeout: 15000 });
    await expect(page.locator('select.card-picker')).toHaveCount(0);

    await page.getByLabel('Email').fill(`legacyfix_${Date.now()}@example.com`);
    await page.getByRole('button', { name: 'Save' }).click();
    await expect(page.locator('select.card-picker')).toBeVisible({ timeout: 10000 });
  });
});

test.describe('Transcript integrity', () => {
  // Issue #1: restored turns used to carry text only, so a reload mid-choice
  // left a question on screen with nothing left to tap.
  test('Issue #1: option buttons survive a reload', async ({ page }) => {
    await page.goto(NORTHFIELD, { waitUntil: 'networkidle' });
    await triggerDeadSearchProducts(page);

    const gotChoices = await tapUntilChoicesAppear(page);
    expect(gotChoices).toBe(true);

    const labelsBefore = await page.locator('.optionbtn').allTextContents();
    expect(labelsBefore.length).toBeGreaterThan(0);

    await page.reload({ waitUntil: 'networkidle' });

    // The chat panel itself collapses on reload - `open` is React state, not
    // persisted - so it has to be reopened before its restored turns show.
    // That collapse is expected; what this test guards is that once reopened,
    // the choice is still there to tap rather than restored as text only.
    await page.locator('.chatlaunch').click();

    const optionBtn = page.locator('.optionbtn').first();
    await expect(optionBtn).toBeVisible({ timeout: 15000 });
    await expect(optionBtn).toBeEnabled();
  });

  // Issue #6: tapping a size used to record the shopper as having said the
  // bare label ("8") rather than a sentence naming what it was an answer to.
  test('Issue #6: a tapped size is recorded as a sentence, not a bare label', async ({ page }) => {
    await page.goto(NORTHFIELD, { waitUntil: 'networkidle' });
    await triggerDeadSearchProducts(page);

    const gotChoices = await tapUntilChoicesAppear(page);
    expect(gotChoices).toBe(true);

    const optionBtn = page.locator('.optionbtn').first();
    const bareLabel = (await optionBtn.textContent())?.trim() ?? '';

    await optionBtn.click();
    await page.waitForTimeout(2000);

    const shopperTurns = await page.locator('.bubble.shopper').allTextContents();
    const lastShopperTurn = shopperTurns[shopperTurns.length - 1]?.trim() ?? '';

    expect(lastShopperTurn).not.toBe(bareLabel);
    expect(lastShopperTurn.length).toBeGreaterThan(bareLabel.length);
  });

  // Issue #5: a declined payment used to write its own internal, invented
  // message ("My card was declined.") into the transcript as if the shopper
  // had typed it.
  test('Issue #5: a declined payment does not fabricate a shopper turn', async ({ page }) => {
    await page.goto(NORTHFIELD, { waitUntil: 'networkidle' });
    await page.locator('.panel', { hasText: 'Nothing here yet' }).waitFor({ timeout: 15000 });
    await addFirstProduct(page);

    const stamp = Date.now();
    await signUp(page, `walkd_${stamp}`, `walkd_${stamp}@example.com`);
    await expect(page.locator('select.card-picker')).toBeVisible({ timeout: 15000 });

    // 0002 always declines.
    await page.locator('select.card-picker').selectOption('0002');
    await page.getByRole('button', { name: 'Pay now' }).click();

    await expect(page.locator('.gate-label', { hasText: 'Payment did not go through' })).toBeVisible({ timeout: 15000 });

    // The decline opens the chat with the engine's own explanation. None of
    // the shopper's own turns should be the engine's invented sentence.
    const chatlaunch = page.locator('.chatlaunch');
    if (await chatlaunch.isVisible()) await chatlaunch.click();

    const shopperTurns = await page.locator('.bubble.shopper').allTextContents();
    for (const turn of shopperTurns) {
      expect(turn.trim()).not.toBe('My card was declined.');
    }
  });
});
