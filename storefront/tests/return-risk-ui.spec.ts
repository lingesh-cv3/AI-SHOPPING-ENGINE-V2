import { test, expect } from '@playwright/test';

/**
 * Browser verification that Return-Risk Prevention actually reaches the
 * shopper: engine/execution/return_risk.py computes findings, the API
 * sends them (confirmed directly against /api/chat), but ChatWidget.tsx
 * previously never rendered the `return_risk` field at all - a real gap
 * found by repository audit. Northfield's "Merino Base Layer Tee" (P2001)
 * has a deterministic seeded rating of 3.6/32 reviews - below the
 * known_fact threshold (3.7, min 15 reviews) - so asking for it by name
 * reliably triggers a real, evidence-grounded finding, not a fabricated one.
 */

test('a known low-rating product shows an advisory return-risk notice', async ({ page }) => {
  await page.goto('/northfield', { waitUntil: 'networkidle' });
  await page.evaluate(() => { sessionStorage.clear(); });
  await page.reload({ waitUntil: 'networkidle' });

  const chatlaunch = page.locator('.chatlaunch');
  if (await chatlaunch.isVisible()) await chatlaunch.click();

  const input = page.getByPlaceholder('Type a message');
  await input.waitFor({ timeout: 15000 });
  await input.fill('Show me merino base layer tees');
  await input.press('Enter');

  await page.locator('.chatproduct').first().waitFor({ timeout: 20000 });
  const notice = page.locator('.cpreturnrisk-fact').first();
  await expect(notice).toBeVisible({ timeout: 10000 });
  await expect(notice).toContainText('Known:');
  await expect(notice).toContainText('rating');

  // Advisory only - the product remains tappable/purchasable, never blocked.
  await expect(page.locator('.chatproduct').first()).toBeEnabled();
});
