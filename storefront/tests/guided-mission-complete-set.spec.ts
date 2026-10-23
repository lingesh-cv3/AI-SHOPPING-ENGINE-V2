import { test, expect, type Page } from '@playwright/test';

/**
 * Cross-stage compatibility + complete-the-set, in a real browser, through the COMPLETE guided-mission journey.
 *
 * Model-free and payment-free: the engine under test must be started with its provider keys empty on an ISOLATED database. Gated like the
 * other guided-mission specs: CV3_STEP14_BROWSER=1, and DATABASE_URL must not name cv3.db. This spec NEVER types "checkout", never taps
 * anything named checkout or pay, and records every request whose URL looks like a payment/checkout call, asserting there are none (the
 * closing summary itself says "say checkout whenever you're ready"; that is an invitation, not a step this test takes).
 *
 * Every assertion is on what the shopper sees and on the real cart panel: a stage is only "done" when the cart really shows the line. The
 * add-ons asserted here come from the merchants' own declared `compatible_with` data (Northfield: Trailblazer -> Performance Socks and
 * Recovery Slide; Kettle: Burr Hand Grinder -> House Filter Blend, Pour Over Dripper -> Digital Brew Scale).
 */

test.describe.configure({ mode: 'serial' });

test.beforeAll(() => {
  if (process.env.CV3_STEP14_BROWSER !== '1') {
    throw new Error('guided-mission-complete-set: set CV3_STEP14_BROWSER=1 (and run against an isolated, model-free stack).');
  }
  const url = process.env.DATABASE_URL ?? '';
  if (!url || /cv3\.db(\?|$)/.test(url)) {
    throw new Error('guided-mission-complete-set: DATABASE_URL must name an isolated database, got "' + url + '".');
  }
});

function watchErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
  return errors;
}

/**
 * Records any API request that looks like a payment or checkout call (a path under /api/ naming pay, checkout or payment). Static source
 * files the dev server hands out (for example /src/PaymentsPanel.tsx, a UI module) are not calls and are ignored. The assertion at the end
 * of each test is that this stays empty.
 */
function watchPayments(page: Page): string[] {
  const hits: string[] = [];
  page.on('request', (req) => {
    const path = new URL(req.url()).pathname;
    if (path.startsWith('/api/') && /(^|\/)pay(\/|$)|checkout|payment/i.test(path)) hits.push(req.method() + ' ' + path);
  });
  return hits;
}

async function openChat(page: Page) {
  const launcher = page.locator('.chatlaunch');
  if (await launcher.isVisible()) await launcher.click();
  await expect(page.locator('.chatwidget')).toBeVisible();
}

async function say(page: Page, message: string) {
  await openChat(page);
  const before = await page.locator('.chatwidget .bubble.assistant').count();
  await page.getByPlaceholder('Type a message').fill(message);
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.locator('.chatwidget .bubble.assistant')).toHaveCount(before + 1, { timeout: 30000 });
  await expect(page.locator('.chatwidget')).not.toContainText('Thinking', { timeout: 30000 });
  return lastBubble(page);
}

async function lastBubble(page: Page) {
  const bubbles = page.locator('.chatwidget .bubble.assistant');
  const n = await bubbles.count();
  return bubbles.nth(n - 1);
}

const CART_LINES = '.panel:not(:has-text("Suggestions")) .line';

async function cartLineCount(page: Page): Promise<number> {
  return page.locator(CART_LINES).count();
}

async function tapCard(page: Page, title: string) {
  const card = page.locator('.chatwidget .chatproduct', { hasText: title }).last();
  await expect(card).toBeVisible({ timeout: 15000 });
  await expect(card).toBeEnabled({ timeout: 15000 });
  await card.click();
}

async function waitForAssistantContaining(page: Page, text: string | RegExp) {
  await expect(page.locator('.chatwidget .bubble.assistant').filter({ hasText: text }).last()).toBeVisible({ timeout: 30000 });
  await expect(page.locator('.chatwidget')).not.toContainText('Thinking', { timeout: 30000 });
}

/** Taps the Trailblazer (sized), picks the first size, and returns the bubble that follows the confirmed add. */
async function addTrailblazer(page: Page) {
  await tapCard(page, 'Trailblazer Running Shoe');
  await waitForAssistantContaining(page, /size|option/i);
  expect(await cartLineCount(page), 'no add before the size is chosen').toBe(0);
  await (await lastBubble(page)).locator('.optionbtn').first().click();
  await waitForAssistantContaining(page, /Confirmed in your cart/);
  await expect(page.locator(CART_LINES)).toHaveCount(1, { timeout: 15000 });
  return lastBubble(page);
}

const FORBIDDEN = /incompatible|not compatible|won't work|doesn't fit/i;

test('Northfield, budgeted: the whole journey - declared pairs named, an optional extra with its trade-off, announced once, summary reminder', async ({ page }) => {
  test.setTimeout(300000);
  const errors = watchErrors(page);
  const payments = watchPayments(page);
  await page.goto('/northfield', { waitUntil: 'networkidle' });

  const plan = await say(page, "I'm going for a long run. I want shoes, socks and something for energy, total budget 9000");
  await expect(plan).toContainText("Here's what I'd cover");
  await expect(plan).toContainText('INR');
  await expect(plan).toContainText('all of your 9000 INR budget is available');
  await expect(plan, 'a declared pair across two open stages is noted before anything is chosen')
    .toContainText('Trailblazer Running Shoe is also listed by the merchant as going with Performance Socks Three Pack');
  await expect(plan).not.toContainText(FORBIDDEN);
  expect(await cartLineCount(page), 'planning adds nothing').toBe(0);

  // stage 1: the Trailblazer (sized); the cart confirms; the socks stage names the pair and announces the extra
  const afterShoe = await addTrailblazer(page);
  await expect(afterShoe).toContainText('The merchant lists Performance Socks Three Pack as going with your Trailblazer Running Shoe');
  await expect(afterShoe).toContainText('Optional extra, not on your list - the merchant lists Recovery Slide (1499 INR)');
  await expect(afterShoe, 'the extra states what it does to the budget').toContainText(/It adds 1499 INR; your remaining budget would go from 4701 INR to 3202 INR/);
  await expect(afterShoe).not.toContainText(FORBIDDEN);
  const titles = await afterShoe.locator('.chatproduct').allTextContents();
  const iSocks = titles.findIndex((t) => t.includes('Performance Socks'));
  const iSlide = titles.findIndex((t) => t.includes('Recovery Slide'));
  expect(iSocks, 'the stage option is a card').toBeGreaterThanOrEqual(0);
  expect(iSlide, 'the extra is a card (one tap away)').toBeGreaterThanOrEqual(0);
  expect(iSocks, 'the extra never sits above the shopper\'s own stage option').toBeLessThan(iSlide);

  // stage 2: socks - the extra is NOT announced again, and the undeclared energy gel is not labelled
  await tapCard(page, 'Performance Socks Three Pack');
  await waitForAssistantContaining(page, /Confirmed in your cart[\s\S]*energy/i);
  await expect(page.locator(CART_LINES)).toHaveCount(2, { timeout: 15000 });
  const afterSocks = await lastBubble(page);
  await expect(afterSocks).not.toContainText('Optional extra');
  await expect(afterSocks).not.toContainText('Energy Gel Box of 12 as going with');
  await expect(afterSocks).not.toContainText(FORBIDDEN);

  // stage 3: energy - the closing summary names the confirmed pair and reminds once about the extra
  await tapCard(page, 'Energy Gel Box of 12');
  await waitForAssistantContaining(page, /completes your list/i);
  const summary = await lastBubble(page);
  await expect(summary).toContainText('The merchant lists Performance Socks Three Pack as going with your Trailblazer Running Shoe');
  await expect(summary).toContainText('Optional extra, not on your list - the merchant lists Recovery Slide');
  await expect(summary.locator('.chatproduct', { hasText: 'Recovery Slide' }), 'the last reminder is a card too').toBeVisible();
  await expect(summary).toContainText(/Against your 9000 INR budget/);
  await expect(summary).not.toContainText(FORBIDDEN);
  expect((await summary.innerText()).split('Optional extra').length - 1, 'announced once in the summary').toBe(1);
  await expect(page.locator(CART_LINES)).toHaveCount(3, { timeout: 15000 });

  await expect(page.locator('.panel', { hasText: 'Suggestions' }).getByRole('button', { name: 'Accept' })).toHaveCount(0);
  expect(payments, 'no payment or checkout request was made').toEqual([]);
  expect(errors, 'no uncaught page errors').toEqual([]);
});

test('Northfield: taking the extra goes through the existing add path (size first), a repeat add is refused, the budget includes it', async ({ page }) => {
  test.setTimeout(300000);
  const errors = watchErrors(page);
  const payments = watchPayments(page);
  await page.goto('/northfield', { waitUntil: 'networkidle' });

  await say(page, "I'm going for a long run. I want shoes, socks and something for energy, total budget 9000");
  await addTrailblazer(page);

  await tapCard(page, 'Recovery Slide');
  await waitForAssistantContaining(page, /size|option/i);
  expect(await cartLineCount(page), 'the add-on asks its size first; nothing is added yet').toBe(1);
  await (await lastBubble(page)).locator('.optionbtn').first().click();
  await expect(page.locator(CART_LINES)).toHaveCount(2, { timeout: 15000 });

  const again = await say(page, 'add Recovery Slide');
  await expect(again).toContainText(/already/i);
  expect(await cartLineCount(page), 'a repeat add never adds a second unit').toBe(2);
  await expect(page.locator('.panel', { hasText: 'Suggestions' }).getByRole('button', { name: 'Accept' }), 'no proposal for an explicit command').toHaveCount(0);

  const next = await say(page, "what's next?");
  await expect(next, 'the remaining budget now includes the add-on: 9000 - (4299 + 1499)').toContainText('about 3202 INR of your 9000 INR budget is left');
  await expect(next).not.toContainText('Optional extra');
  expect(payments, 'no payment or checkout request was made').toEqual([]);
  expect(errors, 'no uncaught page errors').toEqual([]);
});

test('Northfield: an extra that would leave too little for the rest says so, and the verdict about the list is unchanged', async ({ page }) => {
  test.setTimeout(300000);
  const errors = watchErrors(page);
  const payments = watchPayments(page);
  await page.goto('/northfield', { waitUntil: 'networkidle' });

  await say(page, 'I want shoes, socks and something for energy, total budget 6000');
  const afterShoe = await addTrailblazer(page);
  await expect(afterShoe).toContainText('leave too little for the rest of your list');
  await expect(afterShoe, 'the verdict stays about the stated list, not about the extra').toContainText(/won't fit your 6000 INR budget/);
  await expect(afterShoe).toContainText('Optional extra, not on your list');
  expect(payments, 'no payment or checkout request was made').toEqual([]);
  expect(errors, 'no uncaught page errors').toEqual([]);
});

test('Northfield: with nothing declared for what is in the cart, nothing is suggested and the summary says so', async ({ page }) => {
  test.setTimeout(300000);
  const errors = watchErrors(page);
  const payments = watchPayments(page);
  await page.goto('/northfield', { waitUntil: 'networkidle' });

  await say(page, 'I want socks and something for energy');
  await tapCard(page, 'Performance Socks Three Pack');
  await waitForAssistantContaining(page, /Confirmed in your cart/);
  const afterSocks = await lastBubble(page);
  await expect(afterSocks).not.toContainText('Optional extra');
  await expect(afterSocks).not.toContainText('The merchant lists');
  await expect(afterSocks).not.toContainText(FORBIDDEN);
  expect(await cartLineCount(page)).toBe(1);

  const skipped = await say(page, 'skip the energy');
  await expect(skipped).toContainText("I found no merchant-confirmed pairings for what's in your cart, so I'm not suggesting extras.");
  await expect(skipped.locator('.chatproduct'), 'no extra cards').toHaveCount(0);
  await expect(skipped).not.toContainText(FORBIDDEN);
  expect(await cartLineCount(page), 'skipping never touches the cart').toBe(1);
  expect(payments, 'no payment or checkout request was made').toEqual([]);
  expect(errors, 'no uncaught page errors').toEqual([]);
});

test('Kettle & Bloom (GraphQL): the same journey shape, its own declared add-on, nothing from Northfield', async ({ page }) => {
  test.setTimeout(300000);
  const errors = watchErrors(page);
  const payments = watchPayments(page);
  await page.goto('/kettle', { waitUntil: 'networkidle' });

  const plan = await say(page, 'I want a grinder, a dripper and a scale, total budget 12000');
  await expect(plan).toContainText('Burr Hand Grinder');
  await expect(plan).toContainText('all of your 12000 INR budget is available');

  await tapCard(page, 'Burr Hand Grinder');
  await waitForAssistantContaining(page, /Confirmed in your cart/);
  await expect(page.locator(CART_LINES)).toHaveCount(1, { timeout: 15000 });
  const after = await lastBubble(page);
  await expect(after).toContainText('Optional extra, not on your list - the merchant lists House Filter Blend (1050 INR) as going with your Burr Hand Grinder');
  await expect(after).toContainText('Pour Over Dripper is also listed by the merchant as going with Digital Brew Scale');
  await expect(after.locator('.chatproduct', { hasText: 'House Filter Blend' })).toBeVisible();
  await expect(after).not.toContainText(/Trailblazer|Recovery Slide|Performance Socks/);
  await expect(after).not.toContainText(FORBIDDEN);
  expect(payments, 'no payment or checkout request was made').toEqual([]);
  expect(errors, 'no uncaught page errors').toEqual([]);
});
