// 시뮬레이션 보기 — 관절 상태(왼쪽, 좁으면 아래)와 조건부 오류 안내(2026-10-07).
// 3D 모델·관측 스트림은 가짜다: 모델은 서버 계약 그대로(joint_limits = 현재 Profile 한계, rad), 스트림은 30Hz 관측(rad).
import { expect, test } from '@playwright/test';
import { SAMPLES, fixture, mockBackend, sendCommand } from '../mock.js';

const LIMITS = { j1: [-3.0543, 3.0543], j2: [-4.6251, 1.4835], j3: [-2.7925, 2.7925], j4: [-4.6251, 1.4835], j5: [-3.0543, 3.0543], j6: [-3.0543, 3.0543] };
const MODEL = (limits = { available: true, source: 'capability profile qa 1', joints: Object.fromEntries(Object.entries(LIMITS).map(([k, [lo, hi]]) => [k, { lower: lo, upper: hi, unit: 'rad', kind: 'revolute' }])) }) => ({
  available: true, urdf: '<robot name="qa"><link name="base"/></robot>', cell: { fixed: [], materials: [] },
  arm_joints: ['j1', 'j2', 'j3', 'j4', 'j5', 'j6'], gripper_joint: 'g', missing_meshes: [], stale_after_sec: 0.5, stream_hz: 30,
  is_simulated: true, joint_limits: limits,
});
const BASE = { j1: -4.888e-17, j2: -1.9, j3: 1.2, j4: -1.9, j5: -1.5708, j6: 0.000001, g: 0.01 };
const dialog = (page) => page.getByRole('dialog').first();
const panel = (page) => page.getByRole('complementary', { name: '관절 상태' });
const value = (page, j) => panel(page).getByLabel(`${j} 현재 각도`);
const range = (page, j) => panel(page).getByLabel(`${j} 허용 범위`);
const alertBox = (page) => page.getByRole('alert', { name: '오류 안내' });

/** 가짜 관측 스트림. feed.joints를 바꾸면 다음 메시지에 반영된다. feed.stale: 서버 stale 표시, feed.pause: 보내지 않음. */
async function mockView(page, { model = MODEL() } = {}) {
  const feed = { joints: { ...BASE }, stale: false, pause: false, sockets: 0 };
  await page.route('**/v1/sim-view/model', (route) => route.fulfill({ status: model.available ? 200 : 503, json: model }));
  await page.routeWebSocket(/\/v1\/sim-view\/stream/, (ws) => {
    feed.sockets += 1;
    let seq = 0;
    const timer = setInterval(() => {
      if (feed.pause) return;
      seq += 1;
      const now = Date.now() / 1000;
      ws.send(JSON.stringify({ type: 'state', server_time: now, stale: feed.stale, stale_after_sec: 0.5,
        joints: feed.joints, joint_time: now, joint_seq: seq, materials: {}, pose_time: now, pose_seq: seq, robot_pose: null }));
    }, 33);
    ws.onClose(() => clearInterval(timer));
  });
  return feed;
}

async function openView(page) {
  await page.goto('/');
  await page.getByRole('button', { name: '시뮬레이션 보기', exact: true }).click();
  await expect(dialog(page)).toBeVisible();
}

test.describe('시뮬레이션 보기 — 관절 상태', () => {
  test('[UI-JNT-01] J1~J6 세로·°·소수 한 자리, 허용 범위는 서버 Profile 한계, 관측이 바뀌면 갱신', async ({ page }) => {
    await mockBackend(page);
    const feed = await mockView(page);
    await openView(page);
    await expect(panel(page).locator('li')).toHaveCount(6);
    await expect(panel(page).locator('.joint-name')).toHaveText(['J1', 'J2', 'J3', 'J4', 'J5', 'J6']);
    await expect(value(page, 'J2')).toHaveText('-108.9°');                 // -1.9 rad
    await expect(value(page, 'J5')).toHaveText('-90.0°');                  // -1.5708 rad
    await expect(value(page, 'J6')).toHaveText('0.0°');
    await expect(value(page, 'J1')).toHaveText('0.0°');                    // -4.888e-17 rad(격리 셀 실측) → '-0.0°' 아님
    await expect(range(page, 'J1')).toHaveText('-175.0° ~ 175.0°');       // ±3.0543 rad
    await expect(range(page, 'J2')).toHaveText('-265.0° ~ 85.0°');
    feed.joints = { ...BASE, j1: 0.5 };
    await expect(value(page, 'J1')).toHaveText('28.6°');
    // 세로 배치: J2가 J1 아래
    const [a, b] = await Promise.all(['J1', 'J2'].map((j) => value(page, j).boundingBox()));
    expect(b.y).toBeGreaterThan(a.y);
  });

  test('[UI-JNT-02] 누락·숫자가 아닌 값은 —, 서버 stale·관측 끊김이면 전부 —(0이나 정상으로 바꾸지 않는다)', async ({ page }) => {
    await mockBackend(page);
    const feed = await mockView(page);
    await openView(page);
    await expect(value(page, 'J1')).toHaveText('0.0°');
    feed.joints = { ...BASE, j3: null, j4: 'abc' };
    delete feed.joints.j5;
    await expect(value(page, 'J3')).toHaveText('—');
    await expect(value(page, 'J4')).toHaveText('—');
    await expect(value(page, 'J5')).toHaveText('—');
    await expect(value(page, 'J1')).toHaveText('0.0°');
    feed.joints = { ...BASE };
    feed.stale = true;                                                      // 서버가 낡았다고 알림
    await expect(value(page, 'J1')).toHaveText('—');
    await expect(value(page, 'J6')).toHaveText('—');
    feed.stale = false;
    await expect(value(page, 'J1')).toHaveText('0.0°');
    feed.pause = true;                                                      // 관측이 끊김(0.5초 넘게 새 값 없음)
    await expect(value(page, 'J1')).toHaveText('—', { timeout: 3000 });
    await expect(range(page, 'J1')).toHaveText('-175.0° ~ 175.0°');        // 범위는 설정 값이라 그대로
  });

  test('[UI-JNT-03] 허용 범위 밖은 반올림 전 원본으로 판정해 강조(경계와 같으면 강조 안 함)', async ({ page }) => {
    await mockBackend(page);
    const feed = await mockView(page);
    await openView(page);
    feed.joints = { ...BASE, j1: 3.0543 };                                  // 정확히 상한
    await expect(value(page, 'J1')).toHaveText('175.0°');
    await expect(panel(page).locator('li[data-joint="J1"]')).not.toHaveClass(/out/);
    feed.joints = { ...BASE, j1: 3.0543 + 1e-6 };                           // 상한을 아주 조금 넘음 — 표시는 같은 175.0°
    await expect(panel(page).locator('li[data-joint="J1"]')).toHaveClass(/out/);
    await expect(value(page, 'J1')).toHaveText('175.0° 허용 범위 밖');
    feed.joints = { ...BASE, j2: -4.7 };
    await expect(panel(page).locator('li[data-joint="J2"]')).toHaveClass(/out/);
    await expect(panel(page).locator('li.out')).toHaveCount(1);
  });

  test('[UI-JNT-04] 서버가 Profile 한계를 주지 못하면 범위는 —(다른 사양을 지어내지 않는다)', async ({ page }) => {
    await mockBackend(page);
    await mockView(page, { model: MODEL({ available: false, detail: '로봇 Profile이 없다', joints: {} }) });
    await openView(page);
    await expect(value(page, 'J1')).toHaveText('0.0°');
    await expect(range(page, 'J1')).toHaveText('—');
    await expect(panel(page).locator('li.out')).toHaveCount(0);
  });
});

test.describe('시뮬레이션 보기 — 오류 안내', () => {
  test('[UI-SAL-01] 정상(보기만)일 때는 오류 안내가 없다', async ({ page }) => {
    await mockBackend(page);
    await mockView(page);
    await openView(page);
    await expect(value(page, 'J1')).toHaveText('0.0°');
    await expect(alertBox(page)).toHaveCount(0);
  });

  test('[UI-SAL-02] 서버 차단: 사유 그대로·조치는 코드가 없으면 원인 확인 필요·상세 보기, 닫기는 화면만(요청 없음)', async ({ page }) => {
    const calls = await mockBackend(page, { command: SAMPLES.block });
    await mockView(page);
    await page.goto('/');
    await sendCommand(page, '금지 구역에 놓아줘');
    const box = alertBox(page);
    await expect(box).toBeVisible();
    await expect(page.getByRole('dialog').first()).toContainText('실행 불가 — 서버가 차단했습니다');
    await expect(box).toContainText('원인목적지가 사용 금지 자리라 이 계획을 실행할 수 없습니다');
    await expect(box).toContainText('조치원인 확인 필요');
    // 사유는 한 곳(안내)에만 — 카드 머리·발에 중복하지 않는다
    await expect(page.locator('.sim-head')).not.toContainText('목적지가 사용 금지 자리라');
    await expect(page.locator('.sim-card > .sim-foot')).not.toContainText('목적지가 사용 금지 자리라');
    await box.getByText('상세 보기').click();
    await expect(box).toContainText('코드없음');
    const before = calls.length;
    await box.getByRole('button', { name: '안내 닫기' }).click();
    await expect(alertBox(page)).toHaveCount(0);
    await page.waitForTimeout(300);
    expect(calls.slice(before).filter((c) => c.method === 'POST')).toHaveLength(0);   // 정지·복구·잠금 해제를 보내지 않는다
  });

  test('[UI-SAL-03] 취소·사용자 정지는 오류가 아니다', async ({ page }) => {
    await mockBackend(page, { command: SAMPLES.confirm(60), confirm: { decision: 'CANCELLED' } });
    await mockView(page);
    await page.goto('/');
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
    await page.locator('section.command').getByRole('button', { name: '취소' }).click();
    await page.getByRole('button', { name: '시뮬레이션 보기', exact: true }).click();
    await expect(value(page, 'J1')).toHaveText('0.0°');
    await expect(alertBox(page)).toHaveCount(0);
  });

  test('[UI-SAL-04] 서버 상태의 미해결 오류(복구 필요)는 조회가 성공해도 남고, 닫으면 같은 오류 동안 숨는다', async ({ page }) => {
    const recovery = (base) => ({ ...base, recovery_required: { required: true, reason: '활성 작업 기록을 읽을 수 없다: OSError', job_id: 'simjob_x' } });
    const calls = await mockBackend(page, { overrides: { simDemo: recovery } });
    await mockView(page);
    await openView(page);
    const box = alertBox(page);
    await expect(box).toContainText('작업 셀 복구 필요');
    await expect(box).toContainText('원인활성 작업 기록을 읽을 수 없다: OSError');
    await page.waitForTimeout(2500);                                        // 주기 조회가 여러 번 성공해도
    await expect(box).toBeVisible();
    await box.getByRole('button', { name: '안내 닫기' }).click();
    await page.waitForTimeout(2500);
    await expect(alertBox(page)).toHaveCount(0);
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(0);
  });

  test('[UI-SAL-05] 화면 캡처 — 일반·전체화면·좁은 화면', async ({ page }, testInfo) => {
    await mockBackend(page, { command: SAMPLES.block });
    const feed = await mockView(page);
    feed.joints = { ...BASE, j2: -4.7 };                                    // 범위 밖 강조도 보이게
    await page.goto('/');
    await sendCommand(page, '금지 구역에 놓아줘');
    await expect(value(page, 'J2')).toHaveText('-269.3° 허용 범위 밖');
    await page.screenshot({ path: testInfo.outputPath('sim-normal.png') });
    await page.locator('.sim-fullscreen').first().click();
    await page.waitForFunction(() => !!document.fullscreenElement);
    await expect(page.locator('.sim-stage:fullscreen .joint-panel')).toBeVisible();
    await expect(page.locator('.sim-stage:fullscreen .sim-alert')).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath('sim-fullscreen.png') });
    await page.keyboard.press('Escape');
    await page.evaluate(() => document.fullscreenElement && document.exitFullscreen());
    await page.setViewportSize({ width: 720, height: 1100 });
    const [videoBox, panelBox] = await Promise.all([page.locator('.sim-video').boundingBox(), panel(page).boundingBox()]);
    expect(panelBox.y).toBeGreaterThan(videoBox.y + videoBox.height - 1);    // 좁으면 관절 상태가 아래
    await page.screenshot({ path: testInfo.outputPath('sim-narrow.png'), fullPage: true });
  });
});

test('[UI-JNT-05] simDemo fixture는 그대로(복구 필요 없음) — 기본 상태에 오류 안내가 생기지 않는다', async () => {
  expect(fixture('simDemo').recovery_required ?? null).toBeNull();
});
