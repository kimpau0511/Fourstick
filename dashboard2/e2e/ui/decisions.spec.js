// 시뮬레이션 창·개발 중 결정·반응형 — 추적표 5장(DEV-01~12), 화면설계서 ⑧
import { readFileSync, readdirSync } from 'node:fs';
import { sep } from 'node:path';
import { expect, test } from '@playwright/test';
import { SAMPLES, mockBackend, sendCommand, startJob } from '../mock.js';

test.beforeEach(async ({ page }) => { await mockBackend(page); });

// 시뮬레이션 창은 명령 흐름의 서버 값으로 열린다: 승인 뒤 작업 실행 중 → 창.
async function openSim(page) {
  await mockBackend(page, { command: SAMPLES.confirm(60), jobs: [SAMPLES.run.job] });
  await page.goto('/');
  await startJob(page);
  await expect(page.locator('.sim-card')).toBeVisible();
}
const card = (page) => page.locator('.sim-card');

test('[UI-SIM-02][⑧·§4] 영상(3D·카메라)을 못 받으면 빈 그림 대신 이유를 적는다', async ({ page }) => {
  await openSim(page);
  await expect(page.locator('.sim-card')).toContainText(/쓸 수 없음|연결하는 중|끊김/);
});

test('[UI-SIM-03][DEV-01] 3D 모델을 못 받으면 Gazebo 영상 경로로 대신한다', async ({ page }) => {
  await openSim(page);
  await expect(page.locator('.sim-head > div small')).toContainText('Gazebo');
});

// UI-SIM-01(⑧ "SIMULATION — 실시간 아님")은 넣지 않는다 — 2026-10-01 "표지 없음" 결정(D3). 지금 창은 실시간 관측이라
// 문구 자체도 사실과 다르다. 기획서 ⑧ 갱신 대상으로 추적표에 남긴다.

test('[UI-DEV-01][DEV-02] 라이트 테마: 페이지 바탕 #e4e9f0, 카드 #e8edf3', async ({ page }) => {
  await page.goto('/');
  expect(await page.evaluate(() => getComputedStyle(document.body).backgroundColor)).toBe('rgb(228, 233, 240)');
  expect(await page.locator('.card').first().evaluate((el) => getComputedStyle(el).backgroundColor)).toBe('rgb(232, 237, 243)');
});

test('[UI-DEV-02][DEV-03] "실제 로봇 아님" 상시 표지가 없다', async ({ page }) => {
  for (const r of ['home', 'robots', 'history', 'diagnostics', 'settings']) {
    await page.goto(`/#/${r}`);
    await expect(page.locator('body'), r).not.toContainText('실제 로봇 아님');
  }
});

test('[UI-DEV-03][DEV-05] 위험 판정 창 영상 칸에 문구 없음 · 설정 정책 문장에 "(안)" 없음', async ({ page }) => {
  await mockBackend(page, { command: SAMPLES.block });
  await page.goto('/');
  await sendCommand(page, '금지 칸으로 옮겨줘');
  await expect(card(page)).toBeVisible();
  await expect(page.locator('.sim-video')).not.toContainText('BLOCKED');
  await card(page).getByRole('button', { name: '명령 수정' }).click();
  await expect(card(page)).toHaveCount(0);
  await page.goto('/#/settings');
  // 설정 3구역 어디에도 초안 표시 "(안)"이 남아 있지 않다.
  for (const zone of [/개인/, /운영/, /안전\s*·\s*권한/]) {
    await page.getByRole('button', { name: zone }).first().click();
    await expect(page.locator('.col-main')).not.toContainText('(안)');
  }
});

test('[UI-DEV-04][DEV-09 D2] 화면 값은 서버에서 온다 — src 어디에서도 목업 데이터 모듈(data.js)을 쓰지 않는다', async () => {
  const src = new URL('../../src/', import.meta.url);
  const files = readdirSync(src, { recursive: true }).filter((f) => /\.(jsx?|mjs)$/.test(f));
  const users = files.filter((f) => /from ['"](\.\.?\/)+data\.js['"]/.test(readFileSync(new URL(f.split(sep).join('/'), src), 'utf-8')));
  expect(users).toEqual([]);
});

test('[UI-DEV-05][DEV-12] 개발 서버는 localhost에만 열린다(인증 없음)', async () => {
  const cfg = readFileSync(new URL('../../vite.config.js', import.meta.url), 'utf-8');
  expect(cfg).toMatch(/host:\s*'localhost'/);
});

for (const width of [1024, 820]) {
  test(`[UI-RESP-01][DEV-07] 폭 ${width}px에서 가로 스크롤 없음, 비상 정지 보임`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await expect(page.locator('header').getByRole('button', { name: /정지/ })).toBeInViewport();
  });
}

test('[UI-RESP-02][DEV-08] 태블릿(1024px): 사이드바는 아이콘 레일(≤80px)', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 900 });
  await page.goto('/');
  const box = await page.locator('aside.sidebar').boundingBox();
  expect(box.width).toBeLessThanOrEqual(80);
});

// 좁은 폭(768~1023, 1A 반응형): 표는 핵심 열만, 나머지는 행 펼침. 화면마다 첫 응답 뒤 나타나는 요소로 로딩을 기다린다.
const READY = {
  home: (page) => page.locator('.robot').first(),
  robots: (page) => page.getByRole('table', { name: '로봇 목록' }),
  history: (page) => page.getByRole('table', { name: '명령 기록' }).locator('tbody tr').first(),
  diagnostics: (page) => page.getByRole('table', { name: '서비스 목록' }),
  settings: (page) => page.getByRole('navigation', { name: '설정 구역' }),
};
for (const width of [768, 1024]) {
  test(`[UI-RESP-03][1A] 폭 ${width}px: 모든 화면에 가로 스크롤이 없다`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    for (const [route, ready] of Object.entries(READY)) {
      await page.goto(`/#/${route}`);
      await expect(ready(page), route).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, route).toBeLessThanOrEqual(0);
    }
  });
}

test('[UI-RESP-04][1A] 좁은 폭(820px): 핵심 열만 보이고 행 펼침 버튼으로 나머지를 본다 — 넓은 폭에서는 버튼이 없다', async ({ page }) => {
  await page.setViewportSize({ width: 820, height: 900 });
  await page.goto('/#/history');
  const table = page.getByRole('table', { name: '명령 기록' });
  await expect(table.getByRole('columnheader', { name: '요청 시각' })).toBeVisible();
  await expect(table.getByRole('columnheader', { name: '요청자' })).toBeHidden(); // 핵심이 아닌 열
  const first = table.getByRole('button', { name: '자세히' }).first();
  await expect(first).toHaveAttribute('aria-expanded', 'false');
  await first.click();
  await expect(first).toHaveAttribute('aria-expanded', 'true');
  await expect(table).toContainText('요청자: 서버 기록');
  await expect(page.getByRole('region', { name: '명령 상세' })).toHaveCount(0); // 펼침이 행 선택으로 번지지 않는다
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(table.getByRole('button', { name: '자세히' })).toHaveCount(0);
  await expect(table.getByRole('columnheader', { name: '요청자' })).toBeVisible();
});

test('[UI-SIM-04][⑧] 서버가 BLOCK으로 판정하면 위험 창 — 서버 사유, 명령 수정/다시 보내기(서버가 다시 판정)', async ({ page }) => {
  const calls = await mockBackend(page, { command: SAMPLES.block });
  await page.goto('/');
  await sendCommand(page, '금지 칸으로 옮겨줘');
  await expect(page.getByRole('dialog', { name: '실행 불가 — 서버가 차단했습니다' })).toBeVisible();
  await expect(card(page)).toContainText(SAMPLES.block.reason);
  await expect(card(page)).toContainText('실행 차단');
  await expect(card(page)).not.toContainText('안전 규칙 확인'); // BLOCK만으로 안전 검사를 했다고 단정하지 않는다
  await card(page).getByRole('button', { name: '다시 보내기' }).click();
  await expect.poll(() => calls.filter((c) => c.path.endsWith('/command')).length).toBe(2);
  expect(calls.filter((c) => c.path.endsWith('/command'))[1].body.utterance).toBe('금지 칸으로 옮겨줘');
  await card(page).getByRole('button', { name: '명령 수정' }).click();
  await expect(card(page)).toHaveCount(0);
  await expect(page.getByLabel('자연어 명령')).toBeVisible();
});

test('[UI-SIM-05][⑧] 확인 거부로 생긴 BLOCK은 위험 창을 열지 않는다', async ({ page }) => {
  await mockBackend(page, { command: SAMPLES.confirm(60) });
  await page.route('**/v1/sim-demo/confirm', (route) => route.fulfill({ status: 409, json: { detail: 'QA: 확인 토큰 만료' } }));
  await page.goto('/');
  await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
  await page.locator('section.command').getByRole('button', { name: '취소' }).click();
  await expect(page.locator('section.command')).toContainText('취소가 거부되었습니다');
  await expect(card(page)).toHaveCount(0);
});

test('[UI-SIM-06][⑧] 실행 중 창 — 서버 진행 단계(라벨)·"N/M 단계 완료 · 마지막 갱신", 닫으면 같은 상태 동안 다시 열지 않는다', async ({ page }) => {
  await openSim(page);
  await expect(page.getByRole('dialog', { name: '가상 동작 확인 중' })).toBeVisible();
  for (const label of ['집기 접근', '집기', '놓기']) await expect(card(page)).toContainText(label); // 서버 progress[].label 그대로
  await expect(card(page)).toContainText('1/3 단계 완료');
  await expect(card(page)).toContainText(/마지막 갱신 \d\d:\d\d:\d\d/);
  await card(page).getByRole('button', { name: '창 닫기' }).click();
  await expect(card(page)).toHaveCount(0);
  await page.waitForTimeout(2500); // 작업 조회가 한 번 더 와도 같은 상태라 다시 열지 않는다
  await expect(card(page)).toHaveCount(0);
});

test('[UI-SIM-07][⑧] 정지가 접수되면 "정지 요청됨", 작업이 정지 결과로 끝나야 "정지 확인됨"', async ({ page }) => {
  await mockBackend(page, { command: SAMPLES.confirm(60) });
  let stopped = false;
  await page.route('**/v1/sim-demo/stop', (route) => { stopped = true; return route.fulfill({ json: { requested: true, detail: '' } }); });
  await page.route('**/v1/sim-demo/jobs/**', (route) => route.fulfill({
    json: stopped ? { ...SAMPLES.done, status: 'stopped', report: { status: 'simulation_transfer_stopped' } } : SAMPLES.run.job,
  }));
  await page.goto('/');
  await startJob(page);
  await expect(card(page)).toContainText('가상 동작 확인 중');
  await page.locator('header').getByRole('button', { name: /즉시 정지/ }).click(); // 창이 열려 있어도 눌린다
  await expect(page.getByRole('dialog', { name: /정지 요청됨|정지 확인됨/ })).toBeVisible();
  await expect(page.getByRole('dialog', { name: '정지 확인됨' })).toBeVisible({ timeout: 8000 });
  await expect(card(page)).toContainText('정지됨');
});

test('[UI-SIM-08][⑧] 정지를 요청했어도 작업이 정지 결과가 아니면 "정지 확인됨"으로 보이지 않는다', async ({ page }) => {
  await mockBackend(page, { command: SAMPLES.confirm(60) });
  let stopped = false;
  await page.route('**/v1/sim-demo/stop', (route) => { stopped = true; return route.fulfill({ json: { requested: true, detail: '' } }); });
  await page.route('**/v1/sim-demo/jobs/**', (route) => route.fulfill({ json: stopped ? SAMPLES.done : SAMPLES.run.job })); // 정지 요청 뒤 '이송 완료'로 끝남
  await page.goto('/');
  await startJob(page);
  await expect(card(page)).toContainText('가상 동작 확인 중');
  await page.locator('header').getByRole('button', { name: /즉시 정지/ }).click();
  await expect(page.getByRole('dialog', { name: '정지 요청됨 · 시뮬레이터 확인 대기' })).toBeVisible(); // 접수 직후엔 요청 창
  await expect(page.locator('section.command')).toContainText('이송 완료', { timeout: 8000 });
  await expect(page.getByRole('dialog')).toHaveCount(0); // 정지 결과가 아니면 창이 닫힌다
  await expect(page.getByRole('dialog', { name: '정지 확인됨' })).toHaveCount(0);
});

test('[UI-SIM-09][⑧] 목표(여러 단계) 실행 중 정지가 접수되고 목표가 stopped로 끝나면 "정지 확인됨"', async ({ page }) => {
  const goalCmd = { decision: 'CONFIRM_GOAL', confirmation: { kind: 'goal', goal_id: 'qa-goal', summary: '두 자재를 옮깁니다', remaining_sec: 60,
    plan: [{ step: 1, material_label: 'A자재', from_label: '1번 팔레트', to_label: '컨베이어' }] } };
  await mockBackend(page, { command: goalCmd });
  let stopped = false;
  const goal = (status, stepStatus) => ({ goal_id: 'qa-goal', status, plan: [{ step: 1, material_label: 'A자재', from_label: '1번 팔레트', to_label: '컨베이어', status: stepStatus, job_id: 'qa-goal-job' }] });
  await page.route('**/v1/sim-demo/goals/**', (route) => route.fulfill({ json: route.request().method() === 'POST' ? goal('running', 'running') : stopped ? goal('stopped', 'stopped') : goal('running', 'running') }));
  await page.route('**/v1/sim-demo/stop', (route) => { stopped = true; return route.fulfill({ json: { requested: true, detail: '' } }); });
  await page.goto('/');
  await sendCommand(page, '두 자재 옮겨줘');
  await page.locator('section.command').getByRole('button', { name: '전체 실행 승인' }).click();
  await expect(card(page)).toContainText('가상 동작 확인 중');
  await page.locator('header').getByRole('button', { name: /즉시 정지/ }).click();
  await expect(page.getByRole('dialog', { name: '정지 확인됨' })).toBeVisible({ timeout: 8000 });
});

test('[UI-SIM-10][⑧] 위험 창의 "다시 보내기"도 명령 잠금을 따른다(로봇 상태 확인 불가면 잠김)', async ({ page }) => {
  await mockBackend(page, { command: SAMPLES.block });
  await page.goto('/');
  await sendCommand(page, '금지 칸으로 옮겨줘');
  await expect(card(page).getByRole('button', { name: '다시 보내기' })).toBeEnabled();
  await page.route('**/v1/sim-view/state', (route) => route.fulfill({ status: 503, json: {} })); // 3D 관측 끊김 → 데이터 없음
  await expect(card(page).getByRole('button', { name: '다시 보내기' })).toBeDisabled({ timeout: 8000 });
});

test('[UI-REV-20][§1] 시연 명령 서비스가 꺼져 있으면(enabled:false) 정상으로 보지 않고 보내기를 잠근다', async ({ page }) => {
  await mockBackend(page, { overrides: { simDemo: { enabled: false } } });
  await page.goto('/');
  await expect(page.locator('section.command')).toContainText('시연 명령 서비스가 꺼져 있습니다');
  await page.getByLabel('자연어 명령').fill('A 자재를 컨베이어로 옮겨줘');
  await expect(page.getByRole('button', { name: '보내기' })).toBeDisabled();
});
