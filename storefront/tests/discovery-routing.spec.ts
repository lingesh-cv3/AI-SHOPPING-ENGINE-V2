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

test('a bare greeting still offers the shop\'s real categories', async ({ page }) => {
  await page.goto('/northfield', { waitUntil: 'networkidle' });
  await page.evaluate(() => { sessionStorage.clear(); });
  await page.reload({ waitUntil: 'networkidle' });

  const chatlaunch = page.locator('.chatlaunch');
  if (await chatlaunch.isVisible()) await chatlaunch.click();

  const input = page.getByPlaceholder('Type a message');
  await input.waitFor({ timeout: 15000 });
  await input.fill('hi');
  await input.press('Enter');

  await expect(page.locator('.chatbody .optionrow', { hasText: 'Footwear' })).toBeVisible({
    timeout: 20000,
  });
});
