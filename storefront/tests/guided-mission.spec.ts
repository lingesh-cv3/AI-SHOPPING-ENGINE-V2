import { test, expect, type Page } from '@playwright/test';

/**
 * Step 14 in a real browser: the long-run guided mission and the manual-testing defects it
 * grew out of (compare the pair on screen, add by exact product name, an uncarried category,
 * going back to what was shown).
 *
 * Model-free and payment-free: the engine under test must be started with its provider keys
 * empty (the manual-stack launcher's default) on an ISOLATED database. Nothing here pays, and
 * no provider is called. Gated like the outage specs: CV3_STEP14_BROWSER=1, and DATABASE_URL
 * (passed through by the caller for this check) must not name cv3.db.
 *
 * Every assertion is on what the shopper sees and on the real cart panel - a stage is only
 * "done" when the cart really shows the line.
 */

test.describe.configure({ mode: 'serial' });

test.beforeAll(() => {
  if (process.env.CV3_STEP14_BROWSER !== '1') {
    throw new Error('guided-mission: set CV3_STEP14_BROWSER=1 (and run against an isolated, model-free stack).');
  }
  const url = process.env.DATABASE_URL ?? '';
  if (!url || /cv3\.db(\?|$)/.test(url)) {
    throw new Error('guided-mission: DATABASE_URL must name an isolated database, got "' + url + '".');
  }
});

function watchErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
  return errors;
}

async function openChat(page: Page) {
  const launcher = page.locator('.chatlaunch');
  if (await launcher.isVisible()) await launcher.click();
  await expect(page.locator('.chatwidget')).toBeVisible();
}

/** Types a message and waits for the engine's answer (the next assistant bubble, thinking gone). */
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

/** Lines of the CART panel only - the Suggestions panel reuses the `.line` class for its rows. */
const CART_LINES = '.panel:not(:has-text("Suggestions")) .line';

async function cartLineCount(page: Page): Promise<number> {
  return page.locator(CART_LINES).count();
}

/** Taps a product card inside the newest assistant bubble that has it. */
async function tapCard(page: Page, title: string) {
  const card = page.locator('.chatwidget .chatproduct', { hasText: title }).last();
  await expect(card).toBeVisible({ timeout: 15000 });
  await expect(card).toBeEnabled({ timeout: 15000 });
  await card.click();
}

async function waitForAssistantContaining(page: Page, text: string | RegExp) {
  await expect(page.locator('.chatwidget .bubble.assistant').filter({ hasText: text }).last())
    .toBeVisible({ timeout: 30000 });
  await expect(page.locator('.chatwidget')).not.toContainText('Thinking', { timeout: 30000 });
}

test('the long-run request becomes a guided mission: plan, shoes (size), socks, energy, summary - confirmed by the real cart', async ({ page }) => {
  test.setTimeout(240000);
  const errors = watchErrors(page);
  await page.goto('/northfield', { waitUntil: 'networkidle' });

  const plan = await say(
    page,
    'I am going for a long run. I want shoes, socks and something for energy. Suggest me which are available now',
  );
  await expect(plan).toContainText("Here's what I'd cover");
  for (const word of ['shoes', 'socks', 'energy']) await expect(plan).toContainText(new RegExp(word, 'i'));
  await expect(plan).toContainText('INR');
  await expect(plan).toContainText(/start with/i);
  await expect(plan.locator('.chatproduct', { hasText: /Shoe/ }).first()).toBeVisible();
  await expect(plan.locator('.chatproduct', { hasText: 'Performance Socks' })).toBeVisible();
  await expect(plan.locator('.chatproduct', { hasText: 'Energy Gel' })).toBeVisible();
  expect(await cartLineCount(page), 'planning adds nothing').toBe(0);

  // stage 1: tap a shoe -> it asks which size; nothing is in the cart yet
  await tapCard(page, 'Trailblazer Running Shoe');
  await waitForAssistantContaining(page, /size|option/i);
  expect(await cartLineCount(page), 'no add before the size is chosen').toBe(0);
  const sizeButtons = page.locator('.chatwidget .optionbtn');
  await expect(sizeButtons.first()).toBeVisible();
  await sizeButtons.first().click();

  // the cart confirms it, and the mission opens the socks stage by itself
  await waitForAssistantContaining(page, /Confirmed in your cart/);
  const afterShoe = await lastBubble(page);
  await expect(afterShoe).toContainText(/socks/i);
  await expect(afterShoe).toContainText('Trailblazer Running Shoe');
  await expect(page.locator(CART_LINES)).toHaveCount(1, { timeout: 15000 });
  await expect(afterShoe.locator('.chatproduct', { hasText: 'Performance Socks' })).toBeVisible();

  // stage 2: socks, by tapping the card
  await tapCard(page, 'Performance Socks Three Pack');
  await waitForAssistantContaining(page, /Confirmed in your cart[\s\S]*energy/i);
  await expect(page.locator(CART_LINES)).toHaveCount(2, { timeout: 15000 });

  // stage 3: energy -> the closing summary
  await tapCard(page, 'Energy Gel Box of 12');
  await waitForAssistantContaining(page, /completes your list/i);
  const summary = await lastBubble(page);
  await expect(summary).toContainText('Trailblazer Running Shoe');
  await expect(summary).toContainText('Performance Socks Three Pack');
  await expect(summary).toContainText('Energy Gel Box of 12');
  await expect(summary).toContainText(/Cart total/);
  await expect(summary).toContainText(/Nothing left to choose/);
  await expect(page.locator(CART_LINES)).toHaveCount(3, { timeout: 15000 });

  expect(errors, 'no uncaught page errors').toEqual([]);
});

test('typed path: add by name, skip a stage, change the budget - state survives and the cart stays honest', async ({ page }) => {
  test.setTimeout(240000);
  const errors = watchErrors(page);
  await page.goto('/northfield', { waitUntil: 'networkidle' });

  await say(page, 'I want running shoes, socks and something for energy');
  const budget = await say(page, 'keep the shoes under 3000');
  await expect(budget).toContainText(/under 3000/);
  await expect(budget.locator('.chatproduct')).not.toHaveCount(0);

  // A typed "add" is an explicit command and EXECUTES (no approval step): a product with sizes asks which one first,
  // and the shopper's own tap adds that size. Nothing reaches the cart before that choice.
  const named = await say(page, 'add Starter Road Running Shoe');
  await expect(named).toContainText(/size/i);
  expect(await cartLineCount(page), 'asking for the size changes nothing').toBe(0);
  await expect(
    page.locator('.panel', { hasText: 'Suggestions' }).getByRole('button', { name: 'Accept' }),
    'no proposal is created for an explicit command',
  ).toHaveCount(0);
  await named.locator('.optionbtn').first().click();
  await waitForAssistantContaining(page, /Confirmed in your cart/);
  await expect(page.locator(CART_LINES)).toHaveCount(1, { timeout: 15000 });
  // asking again must not add a second unit
  const again = await say(page, 'add Starter Road Running Shoe');
  await expect(again).toContainText(/already/i);
  expect(await cartLineCount(page)).toBe(1);

  const skipped = await say(page, 'skip the socks');
  await expect(skipped).toContainText(/Skipped socks/i);
  await expect(skipped).toContainText(/energy/i);
  expect(await cartLineCount(page), 'skipping never touches the cart').toBe(1);

  const aside = await say(page, "what's next?");
  await expect(aside).toContainText(/energy/i);
  expect(errors, 'no uncaught page errors').toEqual([]);
});

test('the manual-testing defects, in the browser: compare the pair, add by name, not carried, go back', async ({ page }) => {
  test.setTimeout(240000);
  const errors = watchErrors(page);
  await page.goto('/northfield', { waitUntil: 'networkidle' });

  // compare both -> a real two-product comparison, not a request for a second product
  await say(page, 'show me running shoes under 5000');
  const cmp = await say(page, 'compare both');
  await expect(cmp.locator('.comparetable')).toBeVisible();
  await expect(cmp.locator('.comparetable thead th')).toHaveCount(3); // label column + two products
  await expect(cmp).not.toContainText(/second|one side/i);
  const cmp2 = await say(page, 'compare both the shoes showed above');
  await expect(cmp2.locator('.comparetable')).toBeVisible();

  // add by exact name with nothing searched first: an explicit command executes, and a sized product asks the size
  // first - nothing reaches the cart before the shopper's own size choice, and no proposal is created
  const add = await say(page, 'add Trailblazer Running Shoe');
  await expect(add).toContainText('Trailblazer Running Shoe');
  await expect(add).toContainText(/size/i);
  await expect(page.locator('.panel', { hasText: 'Suggestions' }).getByRole('button', { name: 'Accept' })).toHaveCount(0);
  expect(await cartLineCount(page), 'asking for the size changes nothing').toBe(0);
  const skippedAdd = await say(page, 'skip');
  await expect(skippedAdd).toContainText(/skipped/i);
  expect(await cartLineCount(page), 'skipping the size question adds nothing').toBe(0);

  // a category this store does not sell
  for (const ask of ['I want jockey underwear', 'I want coffee']) {
    const nope = await say(page, ask);
    await expect(nope).toContainText(/doesn't seem to carry/);
    await expect(nope.locator('.chatproduct')).toHaveCount(0);
  }

  // a pointer that does not say WHICH product is answered with a question, never a guess
  await say(page, 'show me running shoes');
  const which = await say(page, "I'll take this");
  await expect(which).toContainText(/which one/i);
  await expect(which).not.toContainText(/couldn.t process/i);
  expect(await cartLineCount(page)).toBe(0);

  // going back restores what was shown, not a fresh catalogue-wide list
  const first = await say(page, 'show me running shoes');
  const firstCount = await first.locator('.chatproduct').count();
  const firstTitles = await first.locator('.chatproduct .cptitle span:first-child').allTextContents();
  await say(page, 'show me socks');
  const back = await say(page, 'go back to the running shoes I saw earlier');
  await expect(back).not.toContainText(/don't have two/i);
  expect(await back.locator('.chatproduct').count()).toBe(firstCount);
  expect(await back.locator('.chatproduct .cptitle span:first-child').allTextContents()).toEqual(firstTitles);

  expect(errors, 'no uncaught page errors').toEqual([]);
});

test('typed approval: a proposal made by a non-explicit intent, "yes" approves exactly that one, a replay adds nothing, a bare "yes" is a no-op', async ({ page }) => {
  test.setTimeout(150000);
  const errors = watchErrors(page);
  await page.goto('/northfield', { waitUntil: 'networkidle' });

  const bare = await say(page, 'yes');
  await expect(bare).toContainText(/nothing waiting/i);
  expect(await cartLineCount(page), 'a bare yes changes nothing').toBe(0);

  // Step 25: an explicit "add X" executes; a proposal comes from something that merely sounds like wanting it.
  const proposed = await say(page, "I'll take the Performance Socks Three Pack");
  await expect(proposed).toContainText(/approval|proposal/i);
  const suggestions = page.locator('.panel', { hasText: 'Suggestions' });
  await expect(suggestions).toBeVisible({ timeout: 15000 });
  expect(await cartLineCount(page), 'the proposal alone changes nothing').toBe(0);

  const approved = await say(page, 'yes');
  await expect(approved).not.toContainText(/couldn.t process/i);
  await expect(page.locator(CART_LINES)).toHaveCount(1, { timeout: 15000 });
  await expect(suggestions).toHaveCount(0, { timeout: 15000 });

  const replay = await say(page, 'yes');
  await expect(replay).toContainText(/nothing waiting/i);
  await expect(page.locator(CART_LINES)).toHaveCount(1);
  expect(errors, 'no uncaught page errors').toEqual([]);
});
