// Codex 리뷰(2026-10-02) 지적 회귀 테스트 — 확인 안 된 상태를 정상·실행 중·취소됨·정지 확인으로 확정하지 않는다(설계원칙 4).
import { expect, test } from '@playwright/test';
import { SAMPLES, mockBackend, sendCommand } from '../mock.js';

const panel = (page) => page.locator('section.command');
const headerStop = (page) => page.locator('header').getByRole('button', { name: /즉시 정지/ });

test.describe('정지 요청 결과', () => {
  test('[UI-REV-01][SFR-009] 미리보기 창이 열려 있어도 정지 버튼은 응답 없이 "정지 확인"을 띄우지 않는다', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await page.locator('.demo-bar').getByRole('button', { name: '안전 검사 중' }).click();
    await headerStop(page).click();
    await expect(panel(page)).toContainText('QA: 정지할 작업 없음'); // 서버 응답 그대로
    await expect(page.locator('.sim-card')).not.toContainText('정지 확인됨');
  });

  test('[UI-REV-02][SFR-009] 정지 요청이 실패하면 실패로 보인다', async ({ page }) => {
    await mockBackend(page);
    await page.route('**/v1/sim-demo/stop', (route) => route.abort());
    await page.goto('/');
    await headerStop(page).click();
    await expect(panel(page)).toContainText('정지 요청을 보내지 못했습니다');
  });

  test('[UI-REV-03][SFR-009] 정지가 접수되면 "요청함"이지 "정지됨"이 아니다', async ({ page }) => {
    await mockBackend(page);
    await page.route('**/v1/sim-demo/stop', (route) => route.fulfill({ json: { requested: true, detail: '' } }));
    await page.goto('/');
    await headerStop(page).click();
    await expect(panel(page)).toContainText('정지를 요청했습니다 — 시뮬레이터가 정지를 확인하면 결과가 표시됩니다');
  });
});

test('[UI-REV-04][⑥-4] 서버가 취소를 거부하면 "취소했습니다"로 확정하지 않는다', async ({ page }) => {
  await mockBackend(page, { command: SAMPLES.confirm(60) });
  await page.route('**/v1/sim-demo/confirm', (route) => route.fulfill({ status: 409, json: { detail: 'QA: 확인 토큰 만료' } }));
  await page.goto('/');
  await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
  await panel(page).getByRole('button', { name: '취소' }).click();
  await expect(panel(page)).toContainText('취소가 거부되었습니다 — QA: 확인 토큰 만료');
  await expect(panel(page)).not.toContainText('취소했습니다');
});

test('[UI-REV-05][§4] 작업 상태 조회가 끊기면 "실행 중"에 머물지 않고 확인 안 됨을 보인다', async ({ page }) => {
  await mockBackend(page, { command: SAMPLES.confirm(60) });
  await page.route('**/v1/sim-demo/jobs/**', (route) => route.fulfill({ status: 500, json: { detail: 'QA: 조회 실패' } }));
  await page.goto('/');
  await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
  await panel(page).getByRole('button', { name: '실행 승인' }).click();
  await expect(panel(page)).toContainText('작업 상태를 확인하지 못했습니다');
  await expect(panel(page).locator('.state-label')).not.toHaveText('실행 중');
});

test('[UI-REV-06][§1] 정지 진단(/v1/robots)을 못 받으면 로봇 상태를 정상으로 보이지 않는다', async ({ page }) => {
  // 3D 관측은 신선하게 둔다 — 그래야 정지 진단 하나만 빠진 상황이 된다.
  await mockBackend(page, { overrides: { simState: { stale: false } } });
  await page.route('**/v1/robots', (route) => route.fulfill({ status: 500, json: {} }));
  await page.goto('/');
  const state = page.locator('.robot').first().locator('[data-level]');
  await expect(state).toHaveAttribute('data-level', 'NO_DATA');
  await expect(page.locator('.robot').first()).toContainText('정지 진단 값을 받지 못했습니다');
});

test('[UI-REV-07][§15] 받던 서버 연결이 끊기면 상태 위젯은 마지막 값으로 정상이라 하지 않는다', async ({ page }) => {
  await mockBackend(page);
  let down = false;
  await page.route('**/health', (route) => (down ? route.fulfill({ status: 500, json: {} }) : route.fallback()));
  await page.goto('/');
  const side = page.locator('aside.sidebar');
  await expect(side).toContainText(/[1-9]\/4 정상/); // 먼저 정상 값을 받는다
  down = true;
  // 서버 정책 environment_max_age_sec(고정 응답 5초)이 지나면 연결이 끊김으로 바뀐다.
  await expect(side).toContainText('0/4 정상', { timeout: 15_000 });
});

test('[UI-REV-08] 실행 진행 막대에 이름이 있다', async ({ page }) => {
  await mockBackend(page, { command: SAMPLES.confirm(60), jobs: [SAMPLES.run.job] });
  await page.goto('/');
  await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
  await panel(page).getByRole('button', { name: '실행 승인' }).click();
  await expect(panel(page).getByRole('progressbar', { name: '작업 진행률' })).toBeVisible();
});

test('[UI-REV-09] 다른 탭에서 위젯 설정을 지우면 이 탭도 기본(모두 보임)으로 돌아간다', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/#/diagnostics');
  const ros = page.getByRole('switch', { name: /ROS 2/ });
  await ros.click();
  await expect(ros).toHaveAttribute('aria-checked', 'false');
  await page.evaluate(() => {
    localStorage.removeItem('forstick.statusWidget.hidden');
    window.dispatchEvent(new StorageEvent('storage', { key: 'forstick.statusWidget.hidden' }));
  });
  await expect(ros).toHaveAttribute('aria-checked', 'true');
});
// 음성과 텍스트가 겹치는 경우(UI-REV-10)는 마이크 권한 설정이 있는 voice.spec.js에 있다.
