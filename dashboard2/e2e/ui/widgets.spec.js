// 상태 위젯 표시 설정(진단 화면) · 로딩 스피너
import { expect, test } from '@playwright/test';
import { fixture, mockBackend } from '../mock.js';

const side = (page) => page.locator('aside.sidebar');
const plc = (page) => page.getByRole('group', { name: '상태 위젯 표시' }).getByRole('switch', { name: 'SAFETY PLC' });

test.describe('상태 위젯 표시', () => {
  test.beforeEach(async ({ page }) => { await mockBackend(page); });

  test('[UI-WIDGET-01] SAFETY PLC를 끄면 사이드바에서 사라지고 N/3, 다시 켜면 돌아온다', async ({ page }) => {
    await page.goto('/#/diagnostics');
    await expect(plc(page)).toHaveAttribute('aria-checked', 'true');
    await expect(side(page)).toContainText('SAFETY PLC');
    await plc(page).click();
    await expect(plc(page)).toHaveAttribute('aria-checked', 'false');
    await expect(side(page)).not.toContainText('SAFETY PLC');
    await expect(side(page)).toContainText(/\d\/3 정상/);
    await expect(side(page)).toContainText('숨김 1');
    await expect(page.getByRole('table', { name: '서비스 목록' })).toContainText('SAFETY PLC'); // 진단 목록은 그대로
    await page.getByRole('button', { name: '모두 보이기' }).click();
    await expect(side(page)).toContainText('SAFETY PLC');
    await expect(side(page)).toContainText(/\d\/4 정상/);
  });

  test('[UI-WIDGET-02] 설정은 새로고침 후에도 유지된다', async ({ page }) => {
    await page.goto('/#/diagnostics');
    await plc(page).click();
    await page.reload();
    await expect(plc(page)).toHaveAttribute('aria-checked', 'false');
    await expect(side(page)).not.toContainText('SAFETY PLC');
  });
});

test('[UI-SPIN-01] 첫 로딩 중에는 스피너가 보이고 응답 후 사라진다', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/health', async (route) => {
    if (new URL(route.request().url()).pathname !== '/health') return route.fallback();
    await new Promise((r) => setTimeout(r, 1500));
    return route.fulfill({ json: fixture('health') });
  });
  await page.goto('/#/home');
  await expect(page.locator('.spinner').first()).toBeVisible();
  await expect(page.locator('.spinner')).toHaveCount(0, { timeout: 10000 });
});
