import { test, expect } from '@playwright/test';

/**
 * Browser verification of a real bug found by manual testing: a shopper
 * plainly naming a product ("I need running shoes") sometimes reached the
 * category-choice offer instead of actual discovery, because the model's
 * own search-query extraction is not perfectly reliable and RECOMMEND_PRODUCTS
 * had no deterministic backstop for that case. Fixed in
 * engine/execution/service.py with a rescue search against the shopper's raw
 * message before falling back to categories - see the comment there.
 */

test('a plain product request reaches real discovery, not the category offer', async ({ page }) => {
  await page.goto('/northfield', { waitUntil: 'networkidle' });
  await page.evaluate(() => { sessionStorage.clear(); });
  await page.reload({ waitUntil: 'networkidle' });

  const chatlaunch = page.locator('.chatlaunch');
  if (await chatlaunch.isVisible()) await chatlaunch.click();

  const input = page.getByPlaceholder('Type a message');
  await input.waitFor({ timeout: 15000 });
  await input.fill('I need running shoes.');
  await input.press('Enter');

  // Real products, tappable, from the actual catalog.
  await page.locator('.chatproduct').first().waitFor({ timeout: 20000 });
  // Never the department picker (a chat "optionrow" of category buttons)
  // in place of an actual result - scoped to the chat body since the
  // storefront's own nav legitimately has a "Footwear" link elsewhere.
  await expect(page.locator('.chatbody .optionrow', { hasText: 'Footwear' })).toHaveCount(0);
});

test('a bare greeting gets a natural conversational reply, not a category offer or a product search', async ({ page }) => {
  // Superseded expectation (was: "a bare greeting still offers real
  // categories" via V1's RECOMMEND_PRODUCTS-with-empty-query browse
  // fallback). The real widget now always talks to Shopper V2
  // (`/api/v2/chat`), which classifies a bare greeting as
  // `Capability.CONVERSATIONAL` and answers naturally - it deliberately
  // never triggers retrieval, a category picker, or product cards for a
  // turn with no shopping intent at all. See engine/shopper_v2/
  // conversational.py and capability.py::route's own docstring for the
  // architecture this asserts against.
  await page.goto('/northfield', { waitUntil: 'networkidle' });
  await page.evaluate(() => { sessionStorage.clear(); });
  await page.reload({ waitUntil: 'networkidle' });

  const chatlaunch = page.locator('.chatlaunch');
  if (await chatlaunch.isVisible()) await chatlaunch.click();

  const v2Request = page.waitForRequest((req) => req.url().includes('/api/v2/chat'));
  const input = page.getByPlaceholder('Type a message');
  await input.waitFor({ timeout: 15000 });
  await input.fill('hi');
  await input.press('Enter');
  const req = await v2Request;
  expect(req.url()).toContain('/api/v2/chat');

  // A real, natural reply appears - never empty, never an error.
  await expect(page.locator('.bubble').last()).toBeVisible({ timeout: 20000 });
  await expect(page.locator('.chatwidget')).not.toContainText(/something went wrong/i);

  // Never the category picker (that was V1's browse-fallback UI) and
  // never a product search from a message with no shopping intent at all.
  await expect(page.locator('.chatbody .optionrow', { hasText: 'Footwear' })).toHaveCount(0);
  await expect(page.locator('.bubble').last().locator('.chatproduct')).toHaveCount(0);
});
