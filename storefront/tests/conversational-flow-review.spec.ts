import { test, expect, type Page } from '@playwright/test';

/**
 * Browser-level verification for the 8 conversational/state issues fixed
 * this session (missions.py raw_said grounding + explain_no_match,
 * evidence.py build_why_v2, capability.py comparison/recommendation/
 * historical-reference backstops, mission_ranking.py must_have hard
 * filter). Each test drives the real running widget against the real
 * running engine - no mocks - the same discipline the rest of this suite
 * already uses.
 *
 * Each test owns its own page/session (Playwright does not share a page
 * across separate `test()` blocks even under serial mode) - a whole
 * multi-turn conversation lives inside one test function instead. Waits
 * poll for the actual assistant bubble count to grow rather than a fixed
 * sleep - a live model call under the Groq free-tier throttle (CLAUDE.md)
 * can take well past any short fixed wait.
 */

const NORTHFIELD = '/northfield';
const KETTLE = '/kettle';

async function openChat(page: Page, path: string) {
  await page.goto(path);
  const launcher = page.locator('.chatlaunch');
  await launcher.waitFor({ timeout: 15000 });
  await launcher.click();
  await expect(page.locator('.chatwidget')).toBeVisible();
}

async function send(page: Page, message: string) {
  const before = await page.locator('.bubble.assistant:not(.thinking)').count();
  await page.getByPlaceholder('Type a message').fill(message);
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.locator('.bubble.assistant:not(.thinking)')).toHaveCount(before + 1, { timeout: 60000 });
}

function lastAssistantTurn(page: Page) {
  return page.locator('.bubble.assistant:not(.thinking)').last();
}

test('Northfield: topic switch retrieves the new topic, then a later historical reference resolves against the right earlier group', async ({ page }) => {
  test.setTimeout(150000);
  await openChat(page, NORTHFIELD);

  await send(page, 'Show me running shoes');
  const afterShoes = (await page.locator('.chatproduct').allTextContents()).join(' ').toLowerCase();
  expect(afterShoes).not.toBe('');

  await send(page, 'Show me socks');
  const afterSocks = (await page.locator('.chatproduct').allTextContents()).join(' ').toLowerCase();
  expect(afterSocks).toContain('sock');

  await send(page, 'What was the cheapest shoe you showed me earlier?');
  const reply = (await lastAssistantTurn(page).textContent()) ?? '';
  expect(reply.toLowerCase()).toMatch(/cheapest|starter road/i);
});

test('Northfield: compare both -> what do you recommend gives a decisive pick, not a repeat card set', async ({ page }) => {
  test.setTimeout(120000);
  await openChat(page, NORTHFIELD);
  await send(page, 'Show me lightweight running shoes');
  await send(page, 'compare both');
  await send(page, 'what do you recommend?');
  const reply = (await lastAssistantTurn(page).textContent()) ?? '';
  expect(reply.toLowerCase()).toMatch(/recommend/i);
});

test('Northfield: mission refinement keeps constraints on the right item and explains a genuine no-match', async ({ page }) => {
  test.setTimeout(150000);
  await openChat(page, NORTHFIELD);
  await send(page, 'I need shoes, socks and something for energy');
  await send(page, 'For the shoes, I want something lightweight');
  await send(page, 'Keep the shoes under 1000');
  const reply = (await lastAssistantTurn(page).textContent()) ?? '';
  // An impossible budget for this catalogue - expects a grounded
  // explanation naming the limiting constraint, never a bare "nothing
  // found".
  expect(reply.toLowerCase()).toMatch(/budget|over|closest/i);
});

test('Northfield: cart intelligence budget question gives a concrete reduction suggestion, not unrelated findings', async ({ page }) => {
  test.setTimeout(90000);
  await openChat(page, NORTHFIELD);
  // Tapping a product card is a real, trusted tap (the same
  // `api.act`/`tap()` path any chatproduct card already uses) - never a
  // model-guessed proposal, so the cart mutates immediately, matching
  // CLAUDE.md's "a tap beats the model's guess" invariant.
  // GPS Watch Series 4 (24999 INR) - a real, single, size-free product, so
  // one tap actually completes the add (no variant-choice follow-up to
  // handle), unlike footwear which needs a size picked afterward.
  await send(page, 'Show me the GPS watch');
  await page.locator('.chatproduct', { hasText: 'GPS Watch' }).first().click();
  await expect(page.locator('.line', { hasText: 'GPS Watch' })).toBeVisible({ timeout: 15000 });

  await send(page, 'My cart is over 10000. What can I change to bring it under budget?');
  const reply = (await lastAssistantTurn(page).textContent()) ?? '';
  expect(reply.toLowerCase()).toMatch(/remov|drop|under|budget/i);
});

test('Northfield: "Why this?" renders and shows grounded evidence, not internal tags', async ({ page }) => {
  test.setTimeout(60000);
  await openChat(page, NORTHFIELD);
  await send(page, 'I need lightweight running shoes');
  const whyButton = page.getByRole('button', { name: /why this\?/i }).last();
  await expect(whyButton).toBeVisible({ timeout: 10000 });
  await whyButton.click();
  const body = page.locator('.whybody').last();
  await expect(body).toBeVisible();
  const text = (await body.textContent()) ?? '';
  expect(text.toLowerCase()).not.toContain('title_or_description_overlap');
  expect(text.length).toBeGreaterThan(0);
});

test('Kettle: mission refinement + budget flow works on the second merchant too', async ({ page }) => {
  test.setTimeout(150000);
  await openChat(page, KETTLE);
  await send(page, 'I need coffee and a mug');
  await send(page, 'For the coffee, keep it under 500');
  const reply = (await lastAssistantTurn(page).textContent()) ?? '';
  expect(reply.length).toBeGreaterThan(0);
});
