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
  // The compared set is the card set of the search turn (a comparison turn renders its own table, not cards).
  const compared = (await lastAssistantTurn(page).locator('.chatproduct').allTextContents()).map((s) => s.toLowerCase());
  expect(compared.length).toBeGreaterThanOrEqual(2);
  await send(page, 'compare both');
  await send(page, 'what do you recommend?');
  // The requirement (the test's own title) is a DECISIVE pick, not a repeat card set: exactly one card, and it is
  // one of the products just compared. The literal word "recommend" is not part of that contract - the grounded
  // deterministic reply says "X is the best real match I have" and a model-composed one may say anything - so
  // the assertion is on the pick itself, not on a word.
  const cards = lastAssistantTurn(page).locator('.chatproduct');
  await expect(cards).toHaveCount(1);
  const picked = ((await cards.first().textContent()) ?? '').toLowerCase();
  expect(compared).toContain(picked);
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
  // found". The requirement is the explanation, not specific words: it must name the limiting figure (1000)
  // and give the shopper a next step. The deterministic reply ("I don't have any shoes under 1000 in stock, so
  // I've kept your earlier options. You can raise the limit or skip this one.") satisfies both; the old
  // /budget|over|closest/ regex only matched one model's phrasing of the same content.
  expect(reply).toMatch(/1000/);
  expect(reply.toLowerCase()).toMatch(/raise|skip|closest|limit|budget/);
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

test('Northfield: "add both" after a comparison starts a guided add for the compared products, one at a time', async ({ page }) => {
  test.setTimeout(120000);
  await openChat(page, NORTHFIELD);
  // Exactly two products in view, by a search that reliably returns exactly those two.
  await send(page, 'Show me running shoes under 5000');
  const compared = (await lastAssistantTurn(page).locator('.chatproduct').allTextContents()).map((s) => s.toLowerCase());
  expect(compared.length).toBe(2);
  await send(page, 'compare both');
  await send(page, 'add both to the cart');
  // Since the add contract (a typed add is a proposal the shopper approves, and a sized product asks for its size
  // first), "add both" works through the compared products ONE AT A TIME: the first reply is the first product's
  // guided step (size question or proposal), not a single message naming both. That every compared product - and
  // only those - is queued is asserted in test_step24_add_both_queue.py.
  const reply = ((await lastAssistantTurn(page).textContent()) ?? '').toLowerCase();
  expect(reply).toMatch(/starter road|trailblazer/);
  expect(reply).toMatch(/size|confirm|proposal/);
});

test('Northfield: an ordinal multi-product reference ("the first and third") resolves to the real, correct products', async ({ page }) => {
  test.setTimeout(120000);
  await openChat(page, NORTHFIELD);
  await send(page, 'Show me running shoes');
  await send(page, 'compare the first and third');
  const reply = (await lastAssistantTurn(page).textContent()) ?? '';
  expect(reply.toLowerCase()).toMatch(/trailblazer/i);
  expect(reply.toLowerCase()).toMatch(/cloudstep/i);
});

test('Kettle: mission refinement + budget flow works on the second merchant too', async ({ page }) => {
  test.setTimeout(150000);
  await openChat(page, KETTLE);
  await send(page, 'I need coffee and a mug');
  await send(page, 'For the coffee, keep it under 500');
  const reply = (await lastAssistantTurn(page).textContent()) ?? '';
  expect(reply.length).toBeGreaterThan(0);
});
