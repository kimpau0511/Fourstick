// 음성 인식률 표시(2026-10-08): 입력칸 아래 왼쪽, 보내기 버튼 왼쪽. 숫자는 모의 응답으로만 검증한다(운영 성적 저장 없음).
import { expect, test } from '@playwright/test';
import { mockBackend } from '../mock.js';

const NOTE = '글자 기준 평가 결과이며 현재 발화의 정확도를 뜻하지 않습니다.';
const MEASURED = { status: 'measured', rate_percent: 92.3, label: '음성 인식률 92.3%', note: NOTE };
const panel = (page) => page.locator('section.command');

async function layout(page) {
  const rate = await panel(page).getByTestId('recognition-rate').boundingBox();
  const send = await panel(page).getByRole('button', { name: '보내기' }).boundingBox();
  const input = await page.getByLabel('자연어 명령').boundingBox();
  return { rate, send, input };
}

for (const [name, width, height] of [['wide', 1440, 900], ['narrow', 390, 844]]) {
  test(`[UI-RATE-01] 인식률 위치 — 입력칸 아래·보내기 왼쪽 (${name} ${width}px)`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await mockBackend(page, { overrides: { recognition: MEASURED } });
    await page.goto('/');
    // 좁은 화면은 명령 패널이 접혀 있다 — 상단 '명령 패널' 버튼으로 연다(기존 동작).
    if (width < 1024) await page.getByRole('button', { name: '명령 패널' }).click();
    const label = panel(page).getByTestId('recognition-rate');
    await expect(label).toHaveText(/^음성 인식률 92\.3%/);
    await expect(label).toHaveAttribute('title', NOTE);
    const { rate, send, input } = await layout(page);
    expect(rate.y).toBeGreaterThanOrEqual(input.y + input.height - 1);        // 입력칸 아래
    expect(rate.x + rate.width).toBeLessThanOrEqual(send.x + 1);              // 보내기 왼쪽
    expect(Math.abs((rate.y + rate.height / 2) - (send.y + send.height / 2))).toBeLessThan(send.height);  // 같은 줄
    expect(rate.x).toBeGreaterThanOrEqual(input.x - 1);                       // 입력칸 왼쪽 끝에 맞춤
    await panel(page).screenshot({ path: `e2e-results/screens/rate-measured-${name}.png` });
  });
}

test('[UI-RATE-02] 평가 결과가 없으면 미측정 — 숫자를 지어내지 않는다', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  const label = panel(page).getByTestId('recognition-rate');
  await expect(label).toHaveText(/^음성 인식률 미측정/);
  await expect(label).not.toContainText('%');
  await expect(label).toHaveAttribute('title', new RegExp(NOTE));
  await panel(page).screenshot({ path: 'e2e-results/screens/rate-unmeasured-wide.png' });
});

test('[UI-RATE-03] 조회 실패·형식 이상이면 미측정', async ({ page }) => {
  await mockBackend(page, { overrides: { recognition: { status: 'measured', label: '음성 인식률 99%', rate_percent: 'x' } } });
  await page.goto('/');
  await expect(panel(page).getByTestId('recognition-rate')).toHaveText(/^음성 인식률 미측정/);
});

test('[UI-RATE-04] 합성 음성 평가 결과도 라벨은 그대로, 설명(title)에 합성 근거', async ({ page }) => {
  const note = '합성 음성(windows-sapi:Microsoft Heami Desktop) 28문장, 글자 기준 평가 결과입니다. 실제 사용자 음성·현재 발화의 정확도가 아닙니다.';
  await mockBackend(page, { overrides: { recognition: { status: 'measured', source: 'synthetic', rate_percent: 89.2, label: '음성 인식률 89.2%', note } } });
  await page.goto('/');
  const label = panel(page).getByTestId('recognition-rate');
  await expect(label).toHaveText(/^음성 인식률 89\.2%/);
  await expect(label).toHaveAttribute('title', note);
});
