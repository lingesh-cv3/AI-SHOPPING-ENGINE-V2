import { test, expect, type Page } from '@playwright/test';

/**
 * The storefront restores the shopper's cart from sessionStorage on load.
 *
 * Found while investigating an intermittent tenant-isolation failure: a TRANSIENT failure
 * of that one read (a 5xx, a dropped connection) used to silently replace the shopper's real
 * basket with a new empty one - and with it their pending suggestions. Only a definite
 * "that cart does not exist" answer may do that now. Model-free, payment-free; runs against
 * any isolated engine.
 */

test.describe.configure({ mode: 'serial' });

const CART_GET = /\/api\/shop\/conn_demo\/cart\/[^/]+$/;

async function startWithOneLine(page: Page): Promise<string> {
  await page.goto('/northfield', { waitUntil: 'networkidle' });
  await page.waitForSelector('article.card', { timeout: 20000 });
  await page.locator('button', { hasText: 'Add to cart' }).first().click();
  await expect(page.locator('.panel:not(:has-text("Suggestions")) .line')).toHaveCount(1, { timeout: 15000 });
  const id = await page.evaluate(() => sessionStorage.getItem('cv3_cart_conn_demo'));
  expect(id, 'a cart id is stored').toBeTruthy();
  return id as string;
}

test('a transient failure reading the cart on load keeps the shopper\'s cart', async ({ page }) => {
  test.setTimeout(90000);
  const before = await startWithOneLine(page);

  let failed = 0;
  await page.route(CART_GET, (route) => {
    if (route.request().method() === 'GET' && failed < 2) {
      failed++;
      return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' });
    }
    return route.continue();
  });
  await page.reload({ waitUntil: 'networkidle' });
  await expect(page.locator('.panel:not(:has-text("Suggestions")) .line')).toHaveCount(1, { timeout: 15000 });

  const after = await page.evaluate(() => sessionStorage.getItem('cv3_cart_conn_demo'));
  expect(failed, 'the failures were really injected').toBe(2);
  expect(after, 'the stored cart id is unchanged').toBe(before);
});

test('a cart the engine says does not exist is replaced by a fresh one', async ({ page }) => {
  test.setTimeout(90000);
  const before = await startWithOneLine(page);

  await page.route(CART_GET, (route) => {
    if (route.request().method() === 'GET') {
      return route.fulfill({
        status: 404, contentType: 'application/json',
        body: JSON.stringify({ detail: { code: 'CART_NOT_FOUND', message: 'no such cart', retryable: false } }),
      });
    }
    return route.continue();
  });
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  const after = await page.evaluate(() => sessionStorage.getItem('cv3_cart_conn_demo'));
  expect(after, 'a definitely-gone cart is replaced').toBeTruthy();
  expect(after).not.toBe(before);
});
