import { test, expect } from '@playwright/test';

/**
 * Browser verification of the product-discovery relevance and mission-
 * preservation fix (engine/execution/relevance.py, and the ShoppingState
 * goal/budget backfill in engine/execution/service.py). A real bug found
 * by manual testing: "I need lightweight running shoes under 2000"
 * returned a Recovery Slide and a Canvas Sneaker (unrelated, merely
 * cheap), and a follow-up ("Comfort is more important to me") lost the
 * running-shoe intent entirely and fell back to the category offer.
 */

async function openChat(page: import('@playwright/test').Page, path: string) {
  await page.goto(path, { waitUntil: 'networkidle' });
  await page.evaluate(() => { sessionStorage.clear(); });
  await page.reload({ waitUntil: 'networkidle' });
  const chatlaunch = page.locator('.chatlaunch');
  if (await chatlaunch.isVisible()) await chatlaunch.click();
  const input = page.getByPlaceholder('Type a message');
  await input.waitFor({ timeout: 15000 });
  return input;
}

test('a real product query never surfaces a same-word-only unrelated product', async ({ page }) => {
  const input = await openChat(page, '/northfield');
  await input.fill('I need running shoes.');
  await input.press('Enter');
  await page.locator('.chatproduct').first().waitFor({ timeout: 20000 });

  const titles = await page.locator('.chatproduct .cptitle span').first().allTextContents();
  const allTitles = await page.locator('.chatproduct').allTextContents();
  // Running Cap shares only the word "running"; Recovery Slide and Canvas
  // Sneaker share none - none of the three is an actual running shoe.
  for (const bad of ['Running Cap', 'Recovery Slide', 'Everyday Canvas Sneaker']) {
    expect(allTitles.join(' ')).not.toContain(bad);
  }
  expect(titles.length).toBeGreaterThan(0);
});

test('product intent and budget survive a follow-up that only restates priority', async ({ page }) => {
  const input = await openChat(page, '/northfield');

  await input.fill('I need running shoes.');
  await input.press('Enter');
  await page.locator('.chatproduct').first().waitFor({ timeout: 20000 });

  await input.fill('I need lightweight running shoes under 2000');
  await input.press('Enter');
  await page.waitForTimeout(6000);

  await input.fill('Comfort is more important to me.');
  await input.press('Enter');
  await page.waitForTimeout(6000);

  // The reply must still be about running shoes and the stated budget -
  // never a silent fall-through to the department picker, and never an
  // unrelated cheap product presented as if it were a running shoe.
  const lastBubble = page.locator('.bubble.assistant').last();
  const text = (await lastBubble.textContent()) ?? '';
  expect(text.toLowerCase()).not.toContain('accessories, apparel, footwear');
  const allTitles = await page.locator('.chatproduct').allTextContents();
  for (const bad of ['Recovery Slide', 'Everyday Canvas Sneaker']) {
    expect(allTitles.join(' ')).not.toContain(bad);
  }
});

test('category browsing still works for a genuine bare request', async ({ page }) => {
  const input = await openChat(page, '/northfield');
  await input.fill('What categories do you have?');
  await input.press('Enter');
  await expect(
    page.locator('.chatbody .optionrow', { hasText: 'Footwear' }),
  ).toBeVisible({ timeout: 20000 });
});
