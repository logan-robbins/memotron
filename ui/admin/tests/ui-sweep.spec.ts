import { test, expect } from '@playwright/test';
import * as fs from 'fs';

/**
 * Sweep every admin nav tab. For each: capture console errors, failed network
 * requests, visible error banners, and a screenshot.
 *
 * Not a pass/fail suite — it is an inventory. Run against whichever server shape
 * you want to characterise:
 *   ADMIN_BASE_URL=http://127.0.0.1:8766 npx playwright test tests/ui-sweep.spec.ts
 */

const TABS = [
  'Overview', 'Memory Explorer', 'Explainability', 'Prompts', 'Project Memory',
  'Dreaming', 'Policy Rollout', 'Tenant Setup', 'Integration',
];

type Finding = {
  tab: string;
  consoleErrors: string[];
  failedRequests: string[];
  errorBanners: string[];
  emptyStateText: string[];
  enabledButtons: number;
};

test('admin UI sweep — every tab', async ({ page }) => {
  const findings: Finding[] = [];
  const consoleErrors: string[] = [];
  const failedRequests: string[] = [];

  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 200));
  });
  page.on('response', async (r) => {
    if (r.status() >= 400) failedRequests.push(`${r.status()} ${new URL(r.url()).pathname}`);
  });

  await page.goto('/');
  await page.waitForLoadState('networkidle');

  for (const tab of TABS) {
    consoleErrors.length = 0;
    failedRequests.length = 0;

    const nav = page.getByRole('navigation');
    const btn = nav.getByRole('button', { name: tab });
    if ((await btn.count()) === 0) {
      findings.push({ tab, consoleErrors: ['TAB NOT FOUND IN NAV'], failedRequests: [],
                      errorBanners: [], emptyStateText: [], enabledButtons: 0 });
      continue;
    }
    await btn.first().click();
    await page.waitForLoadState('networkidle').catch(() => {});
    await page.waitForTimeout(1200);

    // error banners: anything that reads like a failure surfaced to the user
    const bannerTexts: string[] = [];
    for (const re of [/not enabled/i, /error/i, /failed/i, /unavailable/i]) {
      const loc = page.getByText(re);
      const n = Math.min(await loc.count(), 3);
      for (let i = 0; i < n; i++) {
        const t = (await loc.nth(i).innerText().catch(() => ''))?.trim().slice(0, 120);
        if (t && !bannerTexts.includes(t)) bannerTexts.push(t);
      }
    }

    // definitive-looking negatives rendered on the page
    const empties: string[] = [];
    for (const re of [/^no$/i, /not configured/i, /^none$/i, /not yet measured/i]) {
      const loc = page.getByText(re);
      const n = Math.min(await loc.count(), 4);
      for (let i = 0; i < n; i++) {
        const t = (await loc.nth(i).innerText().catch(() => ''))?.trim().slice(0, 60);
        if (t && !empties.includes(t)) empties.push(t);
      }
    }

    const enabledButtons = await page.locator('button:not([disabled])').count();

    await page.screenshot({ path: `/tmp/uisweep/${tab.replace(/\s+/g, '-')}.png`, fullPage: true });

    findings.push({
      tab,
      consoleErrors: [...consoleErrors],
      failedRequests: [...new Set(failedRequests)],
      errorBanners: bannerTexts,
      emptyStateText: empties,
      enabledButtons,
    });
  }

  fs.mkdirSync('/tmp/uisweep', { recursive: true });
  fs.writeFileSync('/tmp/uisweep/findings.json', JSON.stringify(findings, null, 1));

  console.log('\n===== UI SWEEP =====');
  for (const f of findings) {
    const flag = f.failedRequests.length || f.consoleErrors.length ? ' <-- ' : '     ';
    console.log(`${flag}${f.tab.padEnd(18)} reqFail=${String(f.failedRequests.length).padEnd(2)} ` +
                `consoleErr=${String(f.consoleErrors.length).padEnd(2)} btns=${String(f.enabledButtons).padEnd(3)}`);
    if (f.failedRequests.length) console.log(`        failed: ${f.failedRequests.join(', ').slice(0, 150)}`);
    if (f.errorBanners.length) console.log(`        banner: ${f.errorBanners.join(' | ').slice(0, 150)}`);
    if (f.emptyStateText.length) console.log(`        negatives shown: ${f.emptyStateText.join(', ').slice(0, 130)}`);
    if (f.consoleErrors.length) console.log(`        console: ${f.consoleErrors[0].slice(0, 130)}`);
  }
  expect(findings.length).toBe(TABS.length);
});
