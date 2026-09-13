import { expect, test } from '@playwright/test';

test('admin UI tenant setup and memory explainability smoke', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();

  // Scoped to the nav landmark: unscoped, "Explainability" also matches the
  // Inspector's "Open in Explainability" as soon as a fact is selected (the
  // Explorer auto-selects the first one), which is a strict-mode violation.
  const nav = page.getByRole('navigation');

  await nav.getByRole('button', { name: 'Tenant Setup' }).click();
  await expect(page.getByRole('heading', { name: 'Tenant LLM' })).toBeVisible();
  const key = `smoke-key-${Date.now()}`;
  await page.getByLabel('API key').fill(key);
  await page.getByRole('button', { name: 'Save' }).click();
  await expect(page.getByText('stored')).toBeVisible();
  await expect(page.getByText(key)).toHaveCount(0);

  await nav.getByRole('button', { name: 'Memory Explorer' }).click();
  await expect(page.getByRole('img', { name: 'Memory graph' })).toBeVisible();
  // The graph explorer's Neo4j-style furniture: the information panel with its
  // counted labels, and the zoom controls.
  await expect(page.getByRole('complementary', { name: 'Graph information' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Fit graph to view' })).toBeVisible();
  await page.getByRole('button', { name: 'Table' }).click();
  await expect(page.locator('table.data-table tbody tr').first()).toBeVisible();

  await nav.getByRole('button', { name: 'Explainability' }).click();
  await expect(page.getByRole('heading', { name: 'Evidence episodes' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Source trace' })).toBeVisible();
});
