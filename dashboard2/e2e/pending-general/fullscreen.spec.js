// 기존 전체화면 구현을 실제 Chromium·WebGL로 검증한다. 모든 API·관측은 mockBackend 안에서 처리한다.
import { expect, test } from '@playwright/test';
import { execFileSync, spawnSync } from 'node:child_process';
import { SAMPLES, fixture, mockBackend, startJob } from '../mock.js';

// Chromium의 기본 ESC 종료는 창이 있는 X11 브라우저에서 검증한다(xdotool·가상 디스플레이 권장).
test.use({ headless: false });

// 외부 메시 요청 없이 로봇·작업대·자재를 그리는 최소 3D 테스트 모델이다.
const model = {
  available: true,
  urdf: '<robot name="qa"><link name="base"><visual><origin xyz="0 0 0.3"/><geometry><box size="0.15 0.15 0.6"/></geometry><material name="blue"><color rgba="0.2 0.4 0.8 1"/></material></visual></link></robot>',
  cell: {
    fixed: [{ size_m: [0.8, 0.6, 0.1], center_xyz_m: [0.5, 0, 0.7], color_rgba: [0.5, 0.5, 0.5, 1] }],
    materials: [{ model: 'material_a', size_m: [0.1, 0.1, 0.1], home_xyz_m: [0.5, 0, 0.8], color_rgba: [0.9, 0.3, 0.1, 1] }],
  },
};

async function expectCanvas(page) {
  // DOM 크기와 WebGL drawing buffer가 함께 바뀌어야 한다. 장면은 별도 스크린샷으로 확인한다.
  await expect.poll(() => page.locator('.sim-video canvas').evaluate((canvas) => {
    const gl = canvas.getContext('webgl2');
    if (!gl || gl.isContextLost()) return false;
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    return rect.width > 0 && rect.height > 0
      && Math.abs(canvas.width - rect.width * ratio) <= 1
      && Math.abs(canvas.height - rect.height * ratio) <= 1
      && gl.drawingBufferWidth === canvas.width && gl.drawingBufferHeight === canvas.height;
  })).toBe(true);
  await expect(page.locator('.sim3d-frame')).toBeVisible();
  await expect(page.getByText('3D 화면 연결 중', { exact: true })).toHaveCount(0);
}

test('전체화면 진입·버튼/ESC 종료·3D 리사이즈·모의 즉시 정지', async ({ page }, testInfo) => {
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const calls = await mockBackend(page, {
    plan: SAMPLES.plan(),
    model,
    streamState: fixture('simState', { stale: false }),
    execute: null, progress: [SAMPLES.step],
    stop: () => { setTimeout(() => calls.finishExecution(SAMPLES.stopped), 100); return { requested: true, detail: 'QA: 모의 정지 접수' }; },
  });
  await page.goto('/');
  await startJob(page);
  await expect(page.getByRole('dialog', { name: '가상 동작 확인 중', exact: true })).toBeVisible();
  await expectCanvas(page);
  await page.screenshot({ path: testInfo.outputPath('windowed.png') });
  const fullscreen = page.locator('.sim-fullscreen');
  const isFullscreen = () => page.evaluate(() => document.fullscreenElement?.classList.contains('sim-video') ?? false);
  await fullscreen.click();
  await expect.poll(isFullscreen).toBe(true);
  await expect(fullscreen).toHaveAttribute('aria-pressed', 'true');
  const stop = page.locator('.sim-fs-stop');
  await expect(stop).toBeVisible();
  await stop.click({ trial: true });
  await expectCanvas(page);
  await page.screenshot({ path: testInfo.outputPath('fullscreen.png') });
  await fullscreen.click();
  await expect.poll(isFullscreen).toBe(false);
  await expect(fullscreen).toHaveAttribute('aria-pressed', 'false');
  await expect(stop).toHaveCount(0);
  await expectCanvas(page);
  await fullscreen.click();
  await expect.poll(isFullscreen).toBe(true);
  // CDP의 합성 키는 Chromium 기본 전체화면 단축키를 처리하지 않는다.
  // Linux/X11의 실제 키 입력으로 ESC를 검증한다(xdotool 필요).
  await page.evaluate(() => { document.title = 'QA SimOverlay fullscreen'; });
  let windowId;
  await expect.poll(() => {
    const result = spawnSync('xdotool', ['search', '--onlyvisible', '--name', 'QA SimOverlay fullscreen'], { encoding: 'utf8' });
    windowId = result.stdout.trim().split('\n').at(-1);
    return result.status === 0 && !!windowId;
  }).toBe(true);
  execFileSync('xdotool', ['windowfocus', '--sync', windowId]);
  execFileSync('xdotool', ['mousemove', '--window', windowId, '300', '300', 'click', '1']);
  execFileSync('xdotool', ['key', '--clearmodifiers', 'Escape']);
  await expect.poll(isFullscreen).toBe(false);
  await expect(fullscreen).toHaveAttribute('aria-pressed', 'false');
  await page.setViewportSize({ width: 1000, height: 760 });
  await expectCanvas(page);
  await fullscreen.click();
  await expect.poll(isFullscreen).toBe(true);
  // setViewportSize는 OS 창까지 변경해 Chromium이 전체화면을 해제한다.
  // 전체화면 중에는 화면 크기만 에뮬레이션해 ResizeObserver를 검증한다.
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1200, height: 800, deviceScaleFactor: 1, mobile: false });
  await expect.poll(() => page.locator('.sim-video canvas').evaluate((canvas) => [canvas.width, canvas.height])).toEqual([1200, 800]);
  await expectCanvas(page);
  await expect(stop).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('resized-fullscreen.png') });
  await stop.click();
  await expect.poll(() => calls.filter((call) => call.path === '/v1/stop')).toHaveLength(1);
  await expect(page.locator('.sim-fs-bar')).toContainText('정지 확인됨');
  await expect.poll(isFullscreen).toBe(true);
  await expect(stop).toBeVisible();
  expect(calls.find((call) => call.path.endsWith('/stop'))).toMatchObject({ method: 'POST', body: {} });
  expect(calls.filter((call) => call.method === 'POST').map((call) => call.path)).toEqual([
    '/v1/plan', '/v1/decision', '/v1/execute', '/v1/stop',
  ]);
  await page.screenshot({ path: testInfo.outputPath('stopped-fullscreen.png') });
  await fullscreen.click();
  await expect.poll(isFullscreen).toBe(false);
  await expectCanvas(page);
  expect(errors).toEqual([]);
});

test('기본 일반 흐름에서 명령 없이 시뮬레이션을 열고 닫는다', async ({ page }, testInfo) => {
  const calls = await mockBackend(page, { model, streamState: fixture('simState', { stale: false }) });
  const generalStops = [];
  await page.route('**/v1/stop', (route) => {
    generalStops.push({ method: route.request().method(), body: route.request().postDataJSON() });
    return route.fulfill({ json: { requested: false, detail: 'QA: 실행 중인 일반 작업 없음' } });
  });
  await page.goto('/');
  await expect(page.getByLabel('명령 모드', { exact: true })).toHaveCount(0);
  for (const name of ['first', 'reopened']) {
    await page.locator('section.command').getByRole('button', { name: '시뮬레이션 보기', exact: true }).click();
    await expect(page.getByRole('dialog', { name: '시뮬레이션 보기', exact: true })).toBeVisible();
    await expectCanvas(page);
    await page.screenshot({ path: testInfo.outputPath(`view-${name}.png`) });
    await page.getByRole('button', { name: /창 닫기/ }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
  }
  expect(calls).toEqual([]);
  expect(generalStops).toEqual([]);
  await page.locator('section.command').getByRole('button', { name: '시뮬레이션 보기', exact: true }).click();
  await page.locator('.sim-fullscreen').click();
  await expect(page.locator('.sim-fs-stop')).toBeVisible();
  await page.locator('.sim-fs-stop').click();
  await expect.poll(() => generalStops).toEqual([{ method: 'POST', body: {} }]);
  await page.locator('.sim-fullscreen').click();
  await expect(page.locator('section.command')).toContainText('QA: 실행 중인 일반 작업 없음');
  expect(calls).toEqual([]);
  await page.getByRole('button', { name: /창 닫기/ }).click();
});


test('전체화면에서 이동 속도를 적용해도 즉시 정지 연결은 유지된다', async ({ page }, info) => {
  const calls = await mockBackend(page, { model, streamState: fixture('simState', { stale: false }) });
  await page.goto('/');
  await page.getByRole('button', { name: '시뮬레이션 보기', exact: true }).click();
  await page.locator('.sim-fullscreen').click();
  await expect(page.locator('.sim-fullscreen')).toHaveAttribute('aria-pressed', 'true');
  const card = page.getByRole('region', { name: '이동 속도', exact: true });
  await expect(card.getByRole('slider')).toBeVisible();
  await card.getByRole('slider').fill('70');
  await card.getByRole('button', { name: '적용', exact: true }).click();
  await expect(card.getByText('서버 저장: 70%', { exact: true })).toBeVisible();
  await expectCanvas(page);
  await expect(page.locator('.sim-fs-stop')).toBeVisible();
  await page.screenshot({ path: info.outputPath('speed-fullscreen.png') });
  await page.locator('.sim-fs-stop').click();
  await expect.poll(() => calls.map((c) => c.path)).toEqual(['/v1/sim-demo/motion', '/v1/stop']);
  await page.locator('.sim-fullscreen').click();
  await expect(card.getByText('서버 저장: 70%', { exact: true })).toBeVisible();
});
