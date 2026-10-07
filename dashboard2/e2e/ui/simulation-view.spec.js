// 로그인한 관측 화면은 명령 전송 없이 열고 닫을 수 있어야 한다.
import { expect, test } from '@playwright/test';
import { mockBackend, SAMPLES, startJob } from '../mock.js';

for (const mode of ['sim', 'general']) {
  test.describe(mode, () => {
    test.use({ baseURL: `http://localhost:${Number(process.env.QA_PORT || 5181) + (mode === 'general' ? 1 : 0)}` });

    test('로그인 후 수동 열기·전체화면·닫기·재열기는 명령을 보내지 않는다', async ({ page }, testInfo) => {
      const writes = [];
      page.on('request', (request) => {
        if (request.method() !== 'GET') writes.push(request.url());
      });
      // 모든 API를 차단한 뒤 필요한 응답만 mockBackend로 제공한다.
      await page.route('**/v1/**', (route) => route.fulfill({ status: 503, json: {} }));
      await mockBackend(page);
      await page.goto('/');
      const open = page.getByRole('button', { name: '시뮬레이션 보기', exact: true });
      await open.click();
      const dialog = page.getByRole('dialog', { name: '시뮬레이션 보기', exact: true });
      await expect(dialog).toBeVisible();
      await page.screenshot({ path: testInfo.outputPath('manual-view.png') });
      await dialog.getByRole('button', { name: '전체화면', exact: true }).click();
      await expect.poll(() => page.evaluate(() => !!document.fullscreenElement)).toBe(true);
      await expect(dialog.getByRole('button', { name: '■ 즉시 정지', exact: true })).toBeVisible();
      await dialog.getByRole('button', { name: '전체화면 종료 (Esc)', exact: true }).click();
      await expect.poll(() => page.evaluate(() => !!document.fullscreenElement)).toBe(false);
      await dialog.getByRole('button', { name: /창 닫기/ }).click();
      await expect(dialog).toHaveCount(0);
      await open.click();
      await expect(dialog).toBeVisible();
      expect(writes).toEqual([]);
    });
  });
}

test('실행 중 창을 닫고 다시 열어도 서버 작업 상태를 유지한다', async ({ page }) => {
  await page.route('**/v1/**', (route) => route.fulfill({ status: 503, json: {} }));
  const calls = await mockBackend(page, { command: SAMPLES.confirm(), jobs: [SAMPLES.run.job] });
  await page.goto('/');
  await startJob(page);
  const dialog = page.getByRole('dialog', { name: '가상 동작 확인 중', exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: /창 닫기/ }).click();
  await expect(dialog).toHaveCount(0);
  const writesBefore = calls.filter((call) => call.method !== 'GET').length;
  await page.getByRole('button', { name: '시뮬레이션 보기', exact: true }).click();
  await expect(dialog).toBeVisible();
  expect(calls.filter((call) => call.method !== 'GET')).toHaveLength(writesBefore);
});

test('[2026-10-07 요청] 머리 오른쪽은 아이콘만(전체화면·창 닫기), 마우스를 올리면 이름이 보이고, 창 바깥을 눌러도 닫힌다', async ({ page }) => {
  await page.route('**/v1/**', (route) => route.fulfill({ status: 503, json: {} }));
  await mockBackend(page);
  await page.goto('/');
  const open = page.getByRole('button', { name: '시뮬레이션 보기', exact: true });
  await open.click();
  const dialog = page.getByRole('dialog', { name: '시뮬레이션 보기', exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog).not.toContainText('창만 닫음');
  await expect(dialog.locator('hr')).toHaveCount(0);
  for (const name of ['전체화면', '창 닫기']) {
    const btn = dialog.getByRole('button', { name, exact: true });
    await expect(btn).toHaveText(''); // 아이콘만
    await btn.hover();
    await expect.poll(() => btn.evaluate((el) => [getComputedStyle(el, '::after').content, getComputedStyle(el, '::after').opacity])).toEqual([JSON.stringify(name), '1']);
  }
  const order = await dialog.locator('.sim-head-actions button').evaluateAll((els) => els.map((e) => e.getAttribute('aria-label')));
  expect(order).toEqual(['전체화면', '창 닫기']); // 전체화면이 창 닫기 왼쪽
  await page.mouse.click(1430, 890); // 창 바깥(덮개) — 화면 폭 1440×900의 오른쪽 아래 구석
  await expect(dialog).toHaveCount(0);
  await open.click();
  await dialog.click({ position: { x: 40, y: 40 } }); // 창 안을 누르면 닫히지 않는다
  await expect(dialog).toBeVisible();
});
