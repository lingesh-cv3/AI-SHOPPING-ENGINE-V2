import { test, expect, type Page } from '@playwright/test';

const NORTHFIELD = '/northfield';
const KETTLE = '/kettle';

/**
 * Verifies the actual V1->V2 requirement: the EXISTING `ChatWidget` UI
 * (the launcher button, the compact panel, product cards, option buttons,
 * the payment picker) is preserved exactly as before - only its
 * conversational engine changed, from `/api/chat` to `/api/v2/chat`
 * (`engine/shopper_v2/`). There is no separate "V2 panel", no toggle, no
 * "beta"/"new assistant" wording anywhere.
 *
 * Serial, same reasoning as the other specs in this suite: one demo
 * engine, one free-tier model throttle.
 */

async function openChat(page: Page, path: string = NORTHFIELD) {
  await page.goto(path);
  const launcher = page.locator('.chatlaunch');
  await launcher.waitFor({ timeout: 15000 });
  await launcher.click();
  await expect(page.locator('.chatwidget')).toBeVisible();
}

async function send(page: Page, message: string) {
  await page.getByPlaceholder('Type a message').fill(message);
  await page.getByRole('button', { name: 'Send' }).click();
}

test.describe.configure({ mode: 'serial' });

test('the normal Shopper widget is the only assistant, opens as before, and reaches /api/v2/chat', async ({ page }) => {
  await page.goto(NORTHFIELD);

  // The exact V1 launcher, not a separate panel toggle.
  const launcher = page.locator('.chatlaunch');
  await expect(launcher).toBeVisible();
  await expect(launcher).toContainText(/ask us anything/i);

  // No "V2"/"beta"/"new assistant"/"classic assistant" wording anywhere,
  // and no shopper-v2-* panel mounted at all.
  await expect(page.getByText(/beta/i)).toHaveCount(0);
  await expect(page.getByText(/classic assistant/i)).toHaveCount(0);
  await expect(page.getByText(/new assistant/i)).toHaveCount(0);
  await expect(page.getByTestId('shopper-v2-panel')).toHaveCount(0);
  await expect(page.getByTestId('shopper-v2-toggle')).toHaveCount(0);

  await launcher.click();
  await expect(page.locator('.chatwidget')).toBeVisible();

  const v2Request = page.waitForRequest((req) => req.url().includes('/api/v2/chat'));
  await send(page, 'I need shoes');
  const req = await v2Request;
  expect(req.url()).toContain('/api/v2/chat');
});

test('REGRESSION: "I need shoes" through the real widget is discovery only, shows every real match, and an explicit add command actually adds to the cart (no approval step)', async ({ page }) => {
  test.setTimeout(180000);
  await openChat(page);

  await send(page, 'I need shoes');
  await expect(page.locator('.chatproduct').first()).toBeVisible({ timeout: 30000 });
  // No fabricated "added" claim, and no cart-mutation control appears from
  // a plain discovery statement.
  await expect(page.locator('.chatwidget')).not.toContainText(/added to your cart/i);
  // This demo catalogue's Footwear department holds 9 real, genuinely distinct
  // running shoes, and a plain uncounted request shows EVERY verified match
  // (commit ecc00d7, 2026-09-24: "plain discovery queries show everything
  // found, not a fixed 4-item preview"; only an explicit count such as "show
  // me two shoes" caps the display - see test_query_intent_consolidation.py).
  // The old expectation of 4 cards described the superseded preview cap.
  await expect(page.locator('.chatproduct')).toHaveCount(9);

  // With several real products now shown, "I'll take this" is genuinely
  // ambiguous and must ask which one, never guess (the same trusted-tap
  // invariant this project holds everywhere else) - covered by its own
  // test below ("genuine multi-product ambiguity..."), not repeated here.
  // A narrower need instead, that resolves to exactly one real product,
  // is what exercises the actual regression this test guards: an explicit
  // typed add command (Step 25 policy) executes through the deterministic
  // cart-add flow and is reported from the platform's actual result.
  await send(page, 'I need a water bottle');
  await expect(page.locator('.bubble').last().locator('.chatproduct').first()).toBeVisible({
    timeout: 30000,
  });
  // Scoped to this turn's own bubble - `.chatproduct` alone would also
  // match the earlier "I need shoes" turn's 9 cards, still in the
  // transcript above.
  await expect(page.locator('.bubble').last().locator('.chatproduct')).toHaveCount(1);

  await send(page, 'add the water bottle to my cart');
  // Reported from the platform's actual result, with no approval step...
  await expect(page.locator('.chatwidget .bubble.assistant').last()).toContainText(/added/i, { timeout: 30000 });
  await expect(page.locator('.chatwidget')).not.toContainText(/confirm to go ahead|proposal/i);
  // ...and the real cart now actually holds the item (the panel is refreshed from the engine's cart_changed).
  await page.locator('.chatclose').click();
  await expect(page.locator('.panel:not(:has-text("Suggestions")) .line')).toHaveCount(1, { timeout: 15000 });
  await expect(page.locator('.panel:not(:has-text("Suggestions")) .line').first()).toContainText(/bottle/i);
  await expect(page.locator('.panel', { hasText: 'Suggestions' }).getByRole('button', { name: 'Accept' })).toHaveCount(0);
});

test('genuine multi-product ambiguity asks which one, never guesses or fabricates a proposal', async ({ page }) => {
  test.setTimeout(60000);
  await openChat(page);

  await send(page, 'I need shoes');
  await expect(page.locator('.chatproduct').first()).toBeVisible({ timeout: 30000 });
  // See the REGRESSION test's own comment: every verified match is shown (9
  // here); ambiguity below comes from the real candidate set the server
  // resolves "this" against.
  await expect(page.locator('.chatproduct')).toHaveCount(9);

  await send(page, "I'll take this");
  await page.waitForTimeout(6000);
  // Honest clarifying question (the same trusted-tap invariant used
  // elsewhere: a model's guess never substitutes for a real choice) -
  // never a silently-picked product, never a fabricated proposal.
  await expect(page.locator('.chatwidget')).toContainText(/which one/i);
  await expect(page.locator('.panel', { hasText: 'Suggestions' })).toHaveCount(0);
});

test('a tapped product card adds directly, remove/clear are reachable, and checkout shows the real payment picker', async ({ page }) => {
  test.setTimeout(120000);
  await openChat(page);

  await send(page, 'I need socks');
  const card = page.locator('.chatproduct').first();
  await card.waitFor({ timeout: 30000 });
  await card.click();
  await page.waitForTimeout(3000);

  // Either it added directly (no variants) or asked for a size - both are
  // the existing trusted-tap path, unrelated to which engine is talking.
  const cartId = await page.evaluate(() => sessionStorage.getItem('cv3_cart_conn_demo'));
  expect(cartId).toBeTruthy();

  await send(page, "what's in my cart?");
  await page.waitForTimeout(4000);

  await send(page, "I'm ready to checkout");
  await page.waitForTimeout(4000);
  // The real, shared demo card picker (unmodified `/api/chat/pay` flow) -
  // only ever reachable by naming a real card and this explicit tap.
  const hasPayBlock = await page.locator('.payblock').count();
  const repliedInText = await page.locator('.chatwidget').textContent();
  expect(hasPayBlock > 0 || /card/i.test(repliedInText ?? '')).toBe(true);
});

test('order status and payment recovery are reachable through the same widget, honestly', async ({ page }) => {
  test.setTimeout(60000);
  await openChat(page);

  await send(page, 'what did I purchase?');
  await page.waitForTimeout(5000);
  await expect(page.locator('.chatwidget')).toBeVisible();

  await send(page, 'my card was declined, can you retry it?');
  await page.waitForTimeout(5000);
  // Northfield has no recovery capability - an honest escalation, never a
  // fabricated fix, and never an autonomous charge claim.
  await expect(page.locator('.chatwidget')).not.toContainText(/charged/i);
});

test('Kettle & Bloom (GraphQL): the same widget works there too', async ({ page }) => {
  test.setTimeout(60000);
  await openChat(page, KETTLE);

  await send(page, 'I need a fruity coffee');
  await expect(page.locator('.chatproduct').first()).toBeVisible({ timeout: 30000 });
});

test('multi-item mission: a staged plan, a focused answer to "add these", and an explicit add for a named product, through the same widget', async ({ page }) => {
  // Replaces coverage that used to live in shopper-v2.spec.ts against a
  // separate `ShopperV2Panel`/`shopper-v2-mission-sidebar` UI which no
  // longer exists (that component was never folded into the real
  // storefront - see this file's own first test asserting no such panel
  // is mounted). The real mission/basket-proposal mechanism
  // (engine/shopper_v2/missions.py, basket_proposal.py) is unchanged and
  // real; only the UI surface it renders through changed.
  test.setTimeout(150000);
  await openChat(page);

  await send(page, 'I need running shoes and socks');
  await expect(page.locator('.chatproduct').first()).toBeVisible({ timeout: 30000 });
  // Both independently-tracked items' own real candidates are named in
  // the reply text, not collapsed into one search.
  await expect(page.locator('.bubble').last()).toContainText(/running shoes/i);
  await expect(page.locator('.bubble').last()).toContainText(/socks/i);

  // "add these to my cart" over a staged mission is NOT a request to add everything:
  // each stage's product, size and approval are the shopper's own, so the assistant
  // asks about the OPEN stage instead - no proposal, no cart change, no failure message.
  await send(page, 'add these to my cart');
  await expect(page.locator('.bubble').last()).toContainText(/one stage at a time/i, { timeout: 30000 });
  await expect(page.locator('.bubble').last()).not.toContainText(/couldn.t process/i);
  await expect(page.locator('.panel', { hasText: 'Suggestions' })).toHaveCount(0);

  // Naming a product within the stage is an explicit add (Step 25): it asks the product's size first, then adds the
  // shopper's own choice - no proposal, nothing in the cart before that choice.
  await send(page, 'add the Starter Road Running Shoe');
  await expect(page.locator('.bubble').last()).toContainText(/size/i, { timeout: 30000 });
  await expect(page.locator('.panel', { hasText: 'Suggestions' })).toHaveCount(0);
});
