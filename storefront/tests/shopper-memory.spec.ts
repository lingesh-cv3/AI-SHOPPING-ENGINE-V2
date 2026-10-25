import { test, expect, type Page } from '@playwright/test';

/**
 * "What I remember about you": a signed-in shopper can list and forget what the assistant stored. The account and memory endpoints are mocked here
 * (the backend behaviour - scoping, isolation, deletion - is covered by tests/shopper_v2/test_shopper_memory_control.py); this proves the panel only
 * ever shows what the engine returned, shows an honest empty state and an honest failure, and forgets only through the engine.
 */

async function signedInShop(page: Page, facts: { key: string; value: string; provenance: string }[], opts: { failList?: boolean; failDelete?: boolean } = {}) {
  const store = [...facts];
  const deletes: string[] = [];
  await page.route('**/api/account/me*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ signed_in: true, username: 'ann', display_name: 'Ann', email: 'ann@example.com' }) }),
  );
  await page.route('**/api/account/memory*', async (route) => {
    const req = route.request();
    if (req.method() === 'DELETE') {
      deletes.push(new URL(req.url()).searchParams.get('fact_key') ?? '*');
      if (opts.failDelete) return route.fulfill({ status: 500, body: '{}' });
      const key = new URL(req.url()).searchParams.get('fact_key');
      for (let i = store.length - 1; i >= 0; i--) if (!key || store[i].key === key) store.splice(i, 1);
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ forgotten: 1 }) });
    }
    if (opts.failList) return route.fulfill({ status: 500, body: '{}' });
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ signed_in: true, facts: store }) });
  });
  await page.goto('/northfield');
  await page.locator('.acctmenu-name').click();
  await page.getByTestId('memory-open').click();
  return { deletes };
}

test.describe.configure({ mode: 'serial' });

test('lists what the engine returned and forgets one fact through the engine', async ({ page }) => {
  const { deletes } = await signedInShop(page, [
    { key: 'prefers_light', value: 'prefers lightweight gear', provenance: 'shopper_stated' },
    { key: 'size', value: 'wears size 9', provenance: 'shopper_stated' },
  ]);
  const panel = page.getByTestId('memory-panel');
  await expect(panel).toContainText('prefers lightweight gear');
  await expect(panel).toContainText('wears size 9');
  await panel.getByTestId('memory-forget').first().click();
  await expect(panel).not.toContainText('prefers lightweight gear');
  await expect(panel).toContainText('wears size 9');
  expect(deletes).toEqual(['prefers_light']);
});

test('forget everything clears the list and says so honestly', async ({ page }) => {
  const { deletes } = await signedInShop(page, [{ key: 'a', value: 'likes decaf', provenance: 'shopper_stated' }]);
  await page.getByTestId('memory-forget-all').click();
  await expect(page.getByTestId('memory-panel')).toContainText('Nothing yet');
  expect(deletes).toEqual(['*']);
});

test('an empty memory is stated, not drawn as an error', async ({ page }) => {
  await signedInShop(page, []);
  await expect(page.getByTestId('memory-panel')).toContainText('Nothing yet');
  await expect(page.getByTestId('memory-forget-all')).toHaveCount(0);
});

test('a failed load says so and invents no list', async ({ page }) => {
  await signedInShop(page, [{ key: 'a', value: 'secret-looking value', provenance: 'shopper_stated' }], { failList: true });
  await expect(page.getByTestId('memory-panel')).toContainText("couldn't load");
  await expect(page.getByTestId('memory-panel')).not.toContainText('secret-looking value');
});

test('a failed delete leaves the fact on screen (the engine decides, not the page)', async ({ page }) => {
  await signedInShop(page, [{ key: 'a', value: 'likes decaf', provenance: 'shopper_stated' }], { failDelete: true });
  await page.getByTestId('memory-forget').click();
  await expect(page.getByTestId('memory-panel')).toContainText('likes decaf');
});
