// 속도 카드 삭제: 창·전체화면 DOM과 불필요한 API 요청을 확인한다. 실서버에 요청하지 않는다.
import { expect, test } from '@playwright/test';
import { mockBackend, SAMPLES, startJob } from '../mock.js';

async function expectRemoved(page) {
  await expect(page.locator('.motion-speed, .motion-values, .motion-actions')).toHaveCount(0);
  await expect(page.getByRole('region', { name: '이동 속도', exact: true })).toHaveCount(0);
  await expect(page.locator('.sim-video input[type="range"]')).toHaveCount(0);
  for (const text of ['이동 속도', '선택:', '서버 저장:', '다음 작업부터 적용됩니다', '0%는 다음 실행 차단', '100% 및 가변 이동 시간은 Gazebo 검증 전', '경로가 없다']) {
    await expect(page.locator('.sim-video').getByText(text, { exact: false })).toHaveCount(0);
  }
  for (const name of ['적용', '다시 조회']) {
    await expect(page.locator('.sim-video').getByRole('button', { name, exact: true })).toHaveCount(0);
  }
}

test('카드를 제거한 전체화면에서도 실행 중 즉시 정지 요청을 보낸다', async ({ page }, info) => {
  const calls = await mockBackend(page, {
    plan: SAMPLES.plan(), execute: null, progress: [SAMPLES.step],
    stop: () => {
      setTimeout(() => calls.finishExecution(SAMPLES.stopped), 100);
      return { requested: true, detail: 'QA: 모의 정지 접수' };
    },
  });
  await page.goto('/');
  await startJob(page);
  await expect(page.getByRole('dialog', { name: '가상 동작 확인 중', exact: true })).toBeVisible();
  await page.locator('.sim-fullscreen').click();
  await expect(page.locator('.sim-fs-stop')).toBeVisible();
  await expectRemoved(page);
  await page.locator('.sim-fs-stop').click();
  await expect.poll(() => calls.filter((call) => call.path === '/v1/stop')).toHaveLength(1);
  expect(calls.find((call) => call.path === '/v1/stop')).toMatchObject({ method: 'POST', body: {} });
  await expect(page.locator('.sim-fs-bar')).toContainText('정지 확인됨');
  await page.screenshot({ path: info.outputPath('stop-preserved-fullscreen.png') });
});

for (const response of ['saved', 'missing-route']) {
  test(`속도 카드와 조회를 삭제하고 즉시 정지를 유지한다 (${response})`, async ({ page }, info) => {
    const requests = [];
    page.on('request', (request) => {
      if (new URL(request.url()).pathname === '/v1/sim-demo/motion') requests.push(request.method());
    });
    await mockBackend(page, { motion: (route) => route.fulfill(response === 'saved'
      ? { json: { speed_percent: 100 } }
      : { status: 404, json: { detail: '경로가 없다' } }) });
    await page.goto('/');
    await page.getByRole('button', { name: '시뮬레이션 보기', exact: true }).click();
    await expect(page.getByRole('dialog', { name: '시뮬레이션 보기', exact: true })).toBeVisible();
    await expectRemoved(page);
    await expect(page.getByRole('button', { name: /즉시 정지/ }).first()).toBeVisible();
    await page.screenshot({ path: info.outputPath('speed-card-removed-windowed.png') });
    await page.locator('.sim-fullscreen').click();
    await expect.poll(() => page.evaluate(() => document.fullscreenElement?.classList.contains('sim-video') ?? false)).toBe(true);
    await expectRemoved(page);
    await expect(page.locator('.sim-fs-stop')).toBeVisible();
    await page.locator('.sim-fs-stop').click({ trial: true });
    await page.screenshot({ path: info.outputPath('speed-card-removed-fullscreen.png') });
    await page.locator('.sim-fullscreen').click();
    await expect.poll(() => page.evaluate(() => document.fullscreenElement === null)).toBe(true);
    await page.getByRole('button', { name: /창 닫기/ }).click();
    await page.getByRole('button', { name: '시뮬레이션 보기', exact: true }).click();
    await expectRemoved(page);
    expect(requests).toEqual([]);
  });
}
