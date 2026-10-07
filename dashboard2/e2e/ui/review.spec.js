// Codex 리뷰(2026-10-02) 지적 회귀 테스트 — 확인 안 된 상태를 정상·실행 중·취소됨·정지 확인으로 확정하지 않는다(설계원칙 4).
import { expect, test } from '@playwright/test';
import { SAMPLES, fixture, mockBackend, sendCommand, startJob } from '../mock.js';

const panel = (page) => page.locator('section.command');
const headerStop = (page) => page.locator('header').getByRole('button', { name: /즉시 정지/ });

test.describe('정지 요청 결과', () => {
  test('[UI-REV-01][SFR-009] 미리보기 창이 열려 있어도 정지 버튼은 응답 없이 "정지 확인"을 띄우지 않는다', async ({ page }) => {
    await mockBackend(page, { plan: SAMPLES.plan(60), execute: null, progress: [SAMPLES.step] });
    await page.goto('/');
    await startJob(page);
    await expect(page.locator('.sim-card')).toBeVisible();
    await headerStop(page).click();
    await expect(panel(page)).toContainText('QA: 정지할 작업 없음'); // 서버 응답 그대로
    await expect(page.locator('.sim-card')).not.toContainText('정지 확인됨');
    await expect(page.locator('.sim-card')).not.toContainText('정지 요청됨'); // 접수되지 않았다
  });

  test('[UI-REV-02][SFR-009] 정지 요청이 실패하면 실패로 보인다', async ({ page }) => {
    await mockBackend(page);
    await page.route('**/v1/stop', (route) => route.abort());
    await page.goto('/');
    await headerStop(page).click();
    await expect(panel(page)).toContainText('정지 요청 실패');
  });

  test('[UI-REV-03][SFR-009] 정지가 접수되면 "요청함"이지 "정지됨"이 아니다', async ({ page }) => {
    await mockBackend(page);
    await page.route('**/v1/stop', (route) => route.fulfill({ json: { requested: true, detail: '' } }));
    await page.goto('/');
    await headerStop(page).click();
    await expect(panel(page)).toContainText('정지를 요청했습니다 — 실행 종료 확인을 기다립니다');
  });
});

test('[UI-REV-04][⑥-4] 서버가 취소를 거부하면 "취소했습니다"로 확정하지 않는다', async ({ page }) => {
  await mockBackend(page, { plan: SAMPLES.plan(60) });
  await page.route('**/v1/decision', (route) => route.fulfill({ status: 409, json: { detail: 'QA: 승인 대상 만료' } }));
  await page.goto('/');
  await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
  await panel(page).getByRole('button', { name: '취소' }).click();
  await expect(panel(page)).toContainText('취소가 거부되었습니다');
  await expect(panel(page)).not.toContainText('취소했습니다');
});

test('[UI-REV-05][§4] 일반 실행 이벤트 연결이 끊기면 실행 상태 확인 안 됨을 보인다', async ({ page }) => {
  const calls = await mockBackend(page, { execute: null });
  await page.goto('/');
  await startJob(page);
  await expect(page.locator('.sim-card')).toBeVisible();
  calls.disconnect();
  await expect(panel(page)).toContainText('실행 상태 연결이 끊겼습니다');
  await expect(panel(page).locator('.state-label')).not.toHaveText('실행 중');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(headerStop(page)).toBeEnabled();
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
  await mockBackend(page, { plan: SAMPLES.plan(60), execute: null, progress: [SAMPLES.step] });
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

// 서버 recent_jobs 한 줄(고정 응답과 같은 모양). 이 탭에서 보낸 명령이 만든 작업으로 쓴다.
const QA_JOB = { job_id: 'qa-job', action: 'transfer', action_label: '컨베이어로 이송', material: 'material_a', slot: null, slot_label: null, status: 'finished', exit_code: 0, started_at: 1790891200, result_status: 'simulation_transfer_completed' };
const goHistory = (page) => page.locator('nav.nav a[href="#/history"]').click(); // 같은 탭 안에서 이동 — 이 탭의 명령 기록(log)이 남는다

test('[UI-REV-11][§1] 정지 진단을 사용할 수 없으면(available:false) 로봇 상태는 정상이 아니다', async ({ page }) => {
  const stop = fixture('robots').stop_diagnostics;
  await mockBackend(page, { overrides: { simState: { stale: false }, robots: { stop_diagnostics: { ...stop, available: false } } } });
  await page.goto('/');
  const robot = page.locator('.robot').first();
  await expect(robot.locator('[data-level]')).toHaveAttribute('data-level', 'NO_DATA');
  await expect(robot).toContainText('정지 진단을 사용할 수 없습니다');
  await page.locator('nav.nav a[href="#/robots"]').click();
  await expect(page.locator('.col-main')).toContainText('운용 불가');
  await expect(page.locator('.col-main')).toContainText('정지 진단 확인 안 됨');
});

test('[UI-REV-12][§4] 작업 상태(/v1/sim-demo) 조회가 실패하면 경고 알림과 "마지막 값" 안내를 보이고 로봇 상태는 정상이 아니다', async ({ page }) => {
  await mockBackend(page, { overrides: { simState: { stale: false } } });
  await page.route((url) => url.pathname === '/v1/sim-demo', (route) => route.fulfill({ status: 500, json: {} }));
  await page.goto('/');
  await expect(page.getByText('작업 상태 조회가 실패하고 있습니다(마지막 값 표시 중)')).toBeVisible();
  await expect(page.getByText('작업 상태 조회 실패 — 마지막으로 받은 값입니다')).toBeVisible();
  const robot = page.locator('.robot').first();
  await expect(robot.locator('[data-level]')).toHaveAttribute('data-level', 'NO_DATA');
  await expect(robot).toContainText('작업 상태 값을 받지 못했습니다');
  await goHistory(page);
  await expect(page.getByText('작업 상태 조회 실패 — 마지막으로 받은 값입니다')).toBeVisible();
});

test('[UI-REV-14][⑯-2] 서버가 정지를 접수하지 않은 정지 명령은 붉은 정지 노드가 아니라 "정지할 작업 없음"으로 기록된다', async ({ page }) => {
  await mockBackend(page, { plan: { stopped: true, stop: { requested: false, detail: 'QA: 정지할 작업 없음' } } });
  await page.goto('/');
  await sendCommand(page, '멈춰');
  await goHistory(page);
  await page.getByRole('row', { name: /멈춰/ }).click();
  const timeline = page.locator('.timeline');
  await expect(timeline).toContainText('정지 요청 — 정지할 작업 없음');
  await expect(timeline.locator('li[data-state="stop"]')).toHaveCount(0);
  await expect(timeline.locator('li[data-state="none"]').filter({ hasText: '정지 요청 — 정지할 작업 없음' })).toHaveCount(1);
});

test('[UI-REV-15][⑯-2] 서버가 정지를 접수한 정지 명령만 붉은 정지 노드로 기록된다', async ({ page }) => {
  await mockBackend(page, { plan: { stopped: true, stop: { requested: true, detail: '' } } });
  await page.goto('/');
  await sendCommand(page, '멈춰');
  await goHistory(page);
  await page.getByRole('row', { name: /멈춰/ }).click();
  await expect(page.locator('.timeline li[data-state="stop"]')).toHaveCount(1);
});

test('[UI-REV-16][⑯-1] 일반 실행 기록과 별도 시연 작업 기록은 서로 합치지 않는다', async ({ page }) => {
  await mockBackend(page, {
    plan: SAMPLES.plan(60), execute: SAMPLES.done,
    overrides: { simDemo: (base) => ({ ...base, recent_jobs: [QA_JOB, ...base.recent_jobs] }) }, // 고정 응답 4건 + 이 작업 = 서버 5건
  });
  await page.goto('/');
  await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
  await panel(page).getByRole('button', { name: '실행 승인' }).click();
  await expect(panel(page)).toContainText('작업 완료');
  await goHistory(page);
  const rows = page.getByRole('table', { name: '명령 기록' }).locator('tbody tr');
  await expect(rows).toHaveCount(6); // 일반 명령 1 + 별도 시연 작업 5. 서로 다른 실행 기록을 합치지 않는다
  await page.getByRole('row', { name: /A 자재를 컨베이어로 옮겨줘/ }).click();
  await expect(page.locator('.timeline')).toContainText('작업 완료');
  await expect(page.locator('.timeline')).not.toContainText('작업 ID qa-job');
});

test.describe('명령 잠금(결정3)', () => {
  const stop = fixture('robots').stop_diagnostics;
  const send = (page) => panel(page).getByRole('button', { name: '보내기' });

  test('[UI-REV-17][§1] 긴급(정지 래치)이면 보내기·스킬 버튼이 잠기고 즉시 정지는 잠기지 않는다', async ({ page }) => {
    await mockBackend(page, { overrides: { simState: { stale: false }, robots: { stop_diagnostics: { ...stop, stop_latch_active: true } } } });
    await page.goto('/');
    await panel(page).getByLabel('자연어 명령').fill('A 자재를 컨베이어로 옮겨줘');
    await expect(panel(page)).toContainText('긴급 상태(정지 래치)라 새 명령을 보낼 수 없습니다');
    await expect(send(page)).toBeDisabled();
    await expect(panel(page).getByRole('button', { name: 'A자재 → 컨베이어' })).toBeDisabled();
    await expect(panel(page).getByRole('button', { name: '즉시 정지' })).toBeEnabled();
    await expect(headerStop(page)).toBeEnabled();
  });

  test('[UI-REV-18][§1] 로봇 상태를 확인할 수 없으면(NO_DATA) 사유와 함께 잠긴다', async ({ page }) => {
    await mockBackend(page, { overrides: { simState: { stale: true } } });
    await page.goto('/');
    await panel(page).getByLabel('자연어 명령').fill('A 자재를 컨베이어로 옮겨줘');
    await expect(panel(page)).toContainText('로봇 상태를 확인할 수 없어 명령을 보낼 수 없습니다 — 3D 관측이 오래되었습니다');
    await expect(send(page)).toBeDisabled();
    await expect(panel(page).getByRole('button', { name: '즉시 정지' })).toBeEnabled();
  });

  test('[UI-REV-19][§1] 확인 카드의 실행 승인도 같은 잠금을 따른다', async ({ page }) => {
    await mockBackend(page, { plan: SAMPLES.plan(60) });
    await page.goto('/');
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘'); // 정상일 때 확인 카드까지
    const approve = panel(page).getByRole('button', { name: '실행 승인' });
    await expect(approve).toBeEnabled();
    const latch = { stop_diagnostics: { ...stop, stop_latch_active: true } };
    await page.route('**/v1/robots', (route) => route.fulfill({ json: fixture('robots', latch) }));
    await expect(approve).toBeDisabled({ timeout: 15_000 }); // /v1/robots는 10초 주기
    await expect(headerStop(page)).toBeEnabled();
  });
});
