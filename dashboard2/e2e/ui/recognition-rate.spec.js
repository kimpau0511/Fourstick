// 명령 입력 영역(2026-10-08): 전체·합성 평가 인식률은 보이지 않는다 — 서버 조회 API·평가 데이터는 평가용으로 남아 있다.
import { expect, test } from '@playwright/test';
import { mockBackend } from '../mock.js';

const panel = (page) => page.locator('section.command');

test('[UI-RATE-05] 평가 인식률(합성 89.2% 등)은 명령 입력 영역에 표시하지 않는다', async ({ page }) => {
  await mockBackend(page, { overrides: { recognition: { status: 'measured', source: 'synthetic', rate_percent: 89.2, label: '음성 인식률 89.2%' } } });
  await page.goto('/');
  await expect(page.getByLabel('자연어 명령')).toBeVisible();
  await expect(panel(page)).not.toContainText('음성 인식률');
  await expect(panel(page)).not.toContainText('89.2');
  await expect(panel(page).getByTestId('stt-confidence')).toHaveCount(0);   // 발화 전에는 점수 자리도 비어 있다
  await expect(panel(page).getByRole('button', { name: '실제 발화 내용으로 확정' })).toHaveCount(0);
});
