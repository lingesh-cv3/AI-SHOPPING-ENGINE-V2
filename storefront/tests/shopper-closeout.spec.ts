import { test, expect, type Page } from '@playwright/test';

/**
 * Browser journeys for the four shopper-facing defects reported from the 2026-10-21 manual test:
 *
 *   A  a signed-in shopper's cart survives sign-out / sign-in
 *   B  "what do you have?" answers with the shop's real categories as tappable choices, and a tap leads to products
 *   C  "add it to the cart" typed explicitly adds it (no approval step), reported from the platform's actual result
 *   D  several products named in one request are all shown, in the order named
 *
 * Model-free and payment-free: run against an isolated engine with no provider keys (replies are deterministic, so the
 * assertions are on behaviour - which products, which chips, what is in the cart - not on exact wording).
 */

test.describe.configure({ mode: 'serial' });

const CART_LINES = '.panel:not(:has-text("Suggestions")) .line';
const ASSISTANT = '.bubble.assistant:not(.thinking)';

async function openChat(page: Page, path: string) {
  await page.goto(path, { waitUntil: 'networkidle' });
  const launcher = page.locator('.chatlaunch');
  await launcher.waitFor({ timeout: 20000 });
  await launcher.click();
  await expect(page.locator('.chatwidget')).toBeVisible();
}

async function send(page: Page, message: string) {
  const before = await page.locator(ASSISTANT).count();
  await page.getByPlaceholder('Type a message').fill(message);
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.locator(ASSISTANT)).toHaveCount(before + 1, { timeout: 60000 });
  return page.locator(ASSISTANT).last();
}

async function addFirstProductToCart(page: Page) {
  await page.waitForSelector('article.card', { timeout: 20000 });
  await page.waitForTimeout(1500); // the cart bootstrap must finish before the first click
  await page.locator('button', { hasText: 'Add to cart' }).first().click();
  await expect(page.locator(CART_LINES)).toHaveCount(1, { timeout: 15000 });
}

test('A: the cart survives sign-out and sign-in', async ({ page }) => {
  test.setTimeout(150000);
  const username = `cp_${Date.now()}`;

  await page.goto('/northfield', { waitUntil: 'networkidle' });
  await addFirstProductToCart(page);
  const itemBefore = (await page.locator(CART_LINES).first().textContent()) ?? '';

  // Sign up with that cart in the tab: it becomes the account's cart.
  await page.goto('/northfield/signin', { waitUntil: 'networkidle' });
  await page.getByRole('link', { name: 'Create an account' }).click();
  await page.getByLabel('Username').fill(username);
  await page.getByLabel('Password').fill('correct-horse-9');
  await page.getByLabel('Email').fill(`${username}@example.test`);
  await page.getByRole('button', { name: 'Create account' }).click();
  await page.waitForURL(/\/northfield\/?$/, { timeout: 20000 });
  await expect(page.locator(CART_LINES)).toHaveCount(1, { timeout: 15000 });

  // Sign out: the page reloads with a fresh, empty guest cart.
  await page.locator('.acctmenu-name').click();
  await page.getByRole('menuitem', { name: 'Sign out' }).click();
  await page.waitForURL(/\/northfield\/?$/, { timeout: 20000 });
  await page.waitForSelector('article.card', { timeout: 20000 });
  await expect(page.locator(CART_LINES)).toHaveCount(0, { timeout: 15000 });

  // Sign back in: the account's original cart, product intact (this was the reported bug: an empty cart).
  await page.goto('/northfield/signin', { waitUntil: 'networkidle' });
  await page.getByLabel('Username').fill(username);
  await page.getByLabel('Password').fill('correct-horse-9');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await page.waitForURL(/\/northfield\/?$/, { timeout: 20000 });
  await expect(page.locator(CART_LINES)).toHaveCount(1, { timeout: 15000 });
  expect((await page.locator(CART_LINES).first().textContent()) ?? '').toBe(itemBefore);
  await page.reload({ waitUntil: 'networkidle' });
  await expect(page.locator(CART_LINES)).toHaveCount(1, { timeout: 15000 });
});

test('B: "what do you have?" offers the shop\'s real categories and a tap leads to products', async ({ page }) => {
  test.setTimeout(120000);
  await openChat(page, '/northfield');
  const reply = await send(page, 'what do you have?');
  // The chips belong to the reply's turn; the widget's empty-state buttons are gone once a turn exists.
  const chips = page.locator('.chatbody .optionbtn');
  await expect(chips).toHaveCount(6);
  expect((await chips.allTextContents()).map((s) => s.trim()).sort()).toEqual(
    ['Accessories', 'Apparel', 'Footwear', 'Nutrition', 'Recovery', 'Tech'],
  );
  expect((await reply.textContent()) ?? '').not.toMatch(/couldn't find anything/i);

  await chips.filter({ hasText: /^Tech$/ }).click();
  await expect(page.locator(ASSISTANT)).toHaveCount(2, { timeout: 60000 });
  const products = page.locator(ASSISTANT).last().locator('.chatproduct');
  await expect(products.first()).toBeVisible({ timeout: 30000 });
  expect(await products.count()).toBeGreaterThanOrEqual(1);
});

test('B: the other merchant offers its OWN categories (no leakage)', async ({ page }) => {
  test.setTimeout(120000);
  await openChat(page, '/kettle');
  await send(page, 'what do you have?');
  const chips = page.locator('.chatbody .optionbtn');
  await expect(chips).toHaveCount(5);
  expect((await chips.allTextContents()).map((s) => s.trim()).sort()).toEqual(
    ['Blends', 'Equipment', 'Gifts', 'Single origin', 'Subscriptions'],
  );
});

test('C: a typed explicit add executes - no approval step - and a repeat does not double it', async ({ page }) => {
  test.setTimeout(150000);
  await openChat(page, '/northfield');
  await page.waitForTimeout(1500); // cart bootstrap
  await send(page, 'show me socks');
  const reply = await send(page, 'add it to the cart');
  const text = ((await reply.textContent()) ?? '').toLowerCase();
  expect(text).toMatch(/added/);
  expect(text).not.toMatch(/confirm to go ahead|proposal/);
  await expect(page.locator(CART_LINES)).toHaveCount(1, { timeout: 15000 });
  expect(((await page.locator(CART_LINES).first().textContent()) ?? '').toLowerCase()).toContain('sock');
  // Nothing is waiting in the Suggestions panel for approval.
  await expect(page.locator('.panel:has-text("Suggestions") .proposal, .panel:has-text("Suggestions") [data-proposal]')).toHaveCount(0);

  const again = await send(page, 'add it to the cart');
  expect(((await again.textContent()) ?? '').toLowerCase()).toMatch(/already/);
  await expect(page.locator(CART_LINES)).toHaveCount(1);
});

test('C: a typed add for a sized product asks the size, and the shopper\'s tap adds that size', async ({ page }) => {
  test.setTimeout(150000);
  await openChat(page, '/northfield');
  await page.waitForTimeout(1500);
  const reply = await send(page, 'add Trailblazer Running Shoe');
  expect(((await reply.textContent()) ?? '').toLowerCase()).toMatch(/size/);
  await expect(page.locator(CART_LINES)).toHaveCount(0);
  const sizes = page.locator('.chatbody .optionbtn');
  await expect(sizes.first()).toBeVisible({ timeout: 15000 });
  await sizes.first().click();
  await expect(page.locator(CART_LINES)).toHaveCount(1, { timeout: 30000 });
  expect(((await page.locator(CART_LINES).first().textContent()) ?? '').toLowerCase()).toContain('trailblazer');
});

test('D: two named products are both shown, in the order named', async ({ page }) => {
  test.setTimeout(120000);
  await openChat(page, '/northfield');
  const reply = await send(page, 'Show me the Trailblazer and the Starter Road Shoe');
  const cards = reply.locator('.chatproduct');
  await expect(cards).toHaveCount(2, { timeout: 30000 });
  const texts = (await cards.allTextContents()).map((s) => s.toLowerCase());
  expect(texts[0]).toContain('trailblazer');
  expect(texts[1]).toContain('starter road');
});
