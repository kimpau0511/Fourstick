// 시뮬레이션 작업 진행 — 가로 스테퍼(2026-10-08). 서버 응답은 가짜다(로봇·LLM·운영 API 없이 결정적으로 돈다).
// 근거: stage_plan(실행기가 시작 전에 남긴 단계 목록), progress(단계가 끝날 때 남긴 줄), 실행 최종 상태(general).
import { expect, test } from '@playwright/test';
import { fixture, mockBackend } from '../mock.js';

const plan = {
  ok: true, session_id: 'qa-session', request_id: 'req-qa', executable: true, stop_latch: { cleared: true },
  plan: { plan_id: 'plan-qa', plan_hash: 'hash-qa', robot_id: 'fr3', profile_id: 'p', profile_version: 'v', created_at: 0, ttl_sec: 600,
    utterance: 'A자재를 컨베이어로 옮겨줘', steps: [{ index: 1, skill: 'move', args: { to: 'loc_pallet_1' } }, { index: 2, skill: 'pick', args: { object: 'mat_a' } }] },
  safety: { decision: 'allow', rules: [] }, validation: { decision: 'allow', detail: '' },
};
const LABELS = ['안전 home', '팔레트 접근', 'pre-grasp', '그리퍼 열기', 'pick 접근', '그리퍼 닫기', 'lift', '컨베이어 접근', 'place 접근', '그리퍼 열기(해제)', 'retreat', '안전 home 복귀'];
const stagePlan = (labels = LABELS) => ({ of: labels.length, stages: labels.map((label, i) => ({ no: i + 1, label })) });
const progress = (n, { failAt = null, labels = LABELS } = {}) => labels.slice(0, n).map((label, i) => ({ no: i + 1, of: labels.length, label, reached: failAt !== i + 1 }));

// ctl: 시험이 바꾸는 가짜 서버 상태. job(진행 조회 응답), execute(실행 응답 — release 전까지 대기), jobDown(진행 조회 실패).
async function setup(page, ctl) {
  await mockBackend(page);
  let release;
  ctl.executed = new Promise((r) => { release = r; });
  ctl.finish = (body) => { ctl.executeBody = body; release(); };
  const json = (route, body, status = 200) => route.fulfill({ status, json: body });
  await page.route('**/v1/sessions**', (route) => json(route, { session_id: 'qa-session', client_id: 'c1' }));
  await page.route('**/v1/plan', (route) => json(route, plan));
  await page.route('**/v1/decision', (route) => json(route, { ok: true, approval_id: 'appr-1' }));
  await page.route('**/v1/execute', async (route) => { await ctl.executed; return json(route, ctl.executeBody); });
  await page.route('**/v1/stop', (route) => json(route, { ok: true, requested: true }));
  await page.route('**/v1/sim-demo/stop', (route) => json(route, { ok: true, requested: true }));
  await page.route('**/v1/executions/active/cancel', (route) => { ctl.pausedAsked = true; return json(route, { ok: true, requested: true, detail: '일시정지를 요청했습니다' }); });
  await page.route((url) => url.pathname === '/v1/sim-demo', (route) => json(route, fixture('simDemo', (base) => ({
    ...base, running_job: ctl.runningJob === undefined ? (ctl.job && ctl.job.status === 'running' ? { job_id: ctl.job.job_id, status: 'running' } : null) : ctl.runningJob,
    materials: ctl.materials ? ctl.materials(base.materials) : base.materials,
    state: { ...base.state, checkpoint: ctl.checkpoint || null },
  }))));
  await page.route((url) => url.pathname === '/v1/sim-demo/confirm', (route) => {
    ctl.confirmed = route.request().postDataJSON();
    return json(route, { decision: 'RUN', job: { job_id: 'simjob-2', status: 'running' } }, 202);
  });
  await page.route((url) => url.pathname.startsWith('/v1/sim-demo/jobs'), (route) => {
    if (route.request().method() === 'POST') {
      ctl.posted = route.request().postDataJSON();                 // 확인 카드만(리뷰 2번) — 승인 뒤 /confirm이 시작한다
      return json(route, { decision: 'CONFIRM', job: null, confirmation: { token: 'tok-1', remaining_sec: 60, summary: '이어서 옮깁니다' } });
    }
    if (ctl.jobDown) return route.abort();
    const id = decodeURIComponent(new URL(route.request().url()).pathname.split('/').pop());
    const body = (ctl.jobs && ctl.jobs[id]) || ctl.job;
    return body ? json(route, body) : json(route, { detail: 'no job' }, 404);
  });
  await page.goto('/');
}

async function approve(page) {
  await page.getByLabel('자연어 명령').fill('A자재를 컨베이어로 옮겨줘');
  await page.getByRole('button', { name: '보내기' }).click();
  await page.locator('section.command').getByRole('button', { name: '실행 승인' }).click();
}
// 실행이 끝나면 창은 닫힌다(기존 규칙). 끝난 상태는 '시뮬레이션 보기'로 다시 열어 본다.
async function openView(page) {
  await expect(page.locator('.sim-card')).toHaveCount(0, { timeout: 8000 });
  await page.getByRole('button', { name: '시뮬레이션 보기' }).click();
}
const stepper = (page) => page.locator('.sim-card .stepper').first();
const step = (page, n) => stepper(page).locator('.stepper-step').nth(n - 1);
const running = (n, extra = {}) => ({ job_id: 'simjob-1', action: 'transfer', action_label: '이송', material: 'material_a', status: 'running', stage_plan: stagePlan(), progress: progress(n), ...extra });

test('[UI-STEP-01] 서버 단계 목록·진행 줄대로 전환한다 — 진행 중 단계는 완료가 아니고, 연결 바는 완료 구간만 채운다', async ({ page }) => {
  const ctl = { job: running(0) };
  await setup(page, ctl);
  await approve(page);
  await expect(stepper(page).locator('.stepper-step')).toHaveCount(12, { timeout: 8000 });        // 예시 5단계가 아니라 서버 단계 수
  await expect(step(page, 1)).toHaveClass(/current/);
  await expect(stepper(page)).toContainText('현재: A자재 이송 중 · 1단계 안전 home');
  ctl.job = running(3);
  await expect(step(page, 3)).toHaveClass(/done/, { timeout: 8000 });
  await expect(step(page, 4)).toHaveClass(/current/);
  await expect(step(page, 4).locator('.stepper-dot')).toHaveText('4');
  await expect(stepper(page)).toContainText('3/12 단계 완료');
  await expect(stepper(page)).toContainText('현재: A자재 이송 중 · 4단계 그리퍼 열기');
  await expect(step(page, 3).locator('.stepper-bar')).toHaveClass(/filled/);                     // 2→3 완료 구간
  await expect(step(page, 4).locator('.stepper-bar')).not.toHaveClass(/filled/);                 // 3→4는 진행 중 — 채우지 않는다
  await expect(step(page, 5)).toHaveClass(/wait/);
  // 상세 목록은 접혀 있다가 '상세 보기'로 연다.
  await expect(stepper(page).locator('.stepper-list')).toHaveCount(0);
  await stepper(page).getByRole('button', { name: '상세 보기' }).click();
  await expect(page.locator('.sim-card .stepper-pop .stepper-list li').nth(3)).toContainText('진행 중');   // 칸 위 팝업(2026-10-09)
  // 표시 전용: 단계는 버튼이 아니다.
  await expect(stepper(page).locator('.stepper-track button')).toHaveCount(0);
  ctl.finish({ ok: true, execution_id: 'exec-1', final: { state: 'completed' } });
});

test('[UI-STEP-02] 마지막 단계 줄이 와도 최종 성공 확인 전에는 완료로 보이지 않는다', async ({ page }) => {
  const ctl = { job: running(12) };
  await setup(page, ctl);
  await approve(page);
  await expect(stepper(page)).toContainText('12/12 단계 완료', { timeout: 8000 });
  await expect(stepper(page).locator('.stepper-chip')).toContainText('진행 중');
  await expect(stepper(page).locator('.stepper-chip')).not.toContainText('완료 ·');
  ctl.job = { ...running(12), status: 'finished' };
  ctl.finish({ ok: true, execution_id: 'exec-1', final: { state: 'completed' } });
  await openView(page);
  await expect(stepper(page).locator('.stepper-chip')).toContainText('완료 · 12/12 단계 완료', { timeout: 8000 });
  await expect(stepper(page)).toContainText('현재: A자재 이송 완료');
});

test('[UI-STEP-03] 일시정지는 그 자리에서 펄스를 멈추고, 재개는 앞 실행의 확인된 단계 뒤에 이어서 보인다', async ({ page }) => {
  const ctl = { job: running(6) };
  await setup(page, ctl);
  await approve(page);
  const view = page.getByRole('dialog', { name: '가상 동작 확인 중' });
  await expect(step(page, 7)).toHaveClass(/current/, { timeout: 8000 });
  await view.getByRole('button', { name: /일시정지/ }).click();
  ctl.job = { ...running(6), status: 'finished', report: { status: 'simulation_transfer_stopped' } };
  ctl.checkpoint = { model: 'material_a', checkpoint_id: 'cp-1' };
  ctl.materials = (rows) => rows.map((m) => (m.model !== 'material_a' ? m : { ...m, record: { state: 'stopped_unrestored' }, actions: { ...m.actions, transfer: false, resume: true, restore: true } }));
  ctl.finish({ ok: false, execution_id: 'exec-1', interrupted: 'exec.canceled', final: { state: 'stopped' } });
  const paused = page.getByRole('dialog', { name: '일시정지됨' });
  await expect(paused).toBeVisible({ timeout: 8000 });
  await expect(step(page, 6)).toHaveClass(/done/);
  await expect(step(page, 7)).toHaveClass(/paused/);                                             // 같은 자리, 펄스 없음(.current 아님)
  await expect(stepper(page).locator('.stepper-chip')).toContainText('일시정지됨 · 6/12 단계 완료');
  const anim = await step(page, 7).locator('.stepper-dot').evaluate((el) => getComputedStyle(el).animationName);
  expect(anim).toBe('none');
  // 재개 직후: 재개 실행이 아직 단계 목록을 남기기 전(실행기 사전 확인) — 앞 단계만으로 '완료'처럼 보이지 않는다(실기 시험에서 발견).
  ctl.jobs = { 'simjob-2': { job_id: 'simjob-2', action: 'resume', action_label: '재개', material: 'material_a', status: 'running', stage_plan: null, progress: [] } };
  await paused.getByRole('button', { name: /재개/ }).click();
  await page.getByRole('group', { name: '재개 승인' }).getByRole('button', { name: '승인' }).click();
  await expect(stepper(page).locator('.stepper-chip')).toContainText('진행 중 · 6단계 완료 · 남은 단계 확인 중', { timeout: 8000 });
  await expect(step(page, 7)).toHaveClass(/current/);
  await expect(step(page, 7)).toContainText('남은 단계 확인 중');
  await expect(stepper(page)).toContainText(/현재: A자재 .*재개 중 · 재개 단계 목록을 기다리는 중/);
  // 재개 실행이 단계 목록(복구 접근 + 남은 단계)을 남기면 그 목록으로 바뀐다.
  const resumeLabels = ['복구 접근(현재 → 정지 단계 목표)', ...LABELS.slice(7)];
  ctl.jobs = { 'simjob-2': { job_id: 'simjob-2', action: 'resume', action_label: '재개', material: 'material_a', status: 'running', stage_plan: stagePlan(resumeLabels), progress: progress(1, { labels: resumeLabels }) } };
  await expect(stepper(page).locator('.stepper-step')).toHaveCount(6 + resumeLabels.length, { timeout: 8000 });
  await expect(step(page, 6)).toHaveClass(/done/);                                               // 앞 실행에서 확인된 6단계는 그대로
  await expect(step(page, 7)).toHaveClass(/done/);                                               // 재개 실행의 1단계(복구 접근) 완료
  await expect(step(page, 8)).toHaveClass(/current/);
  await expect(step(page, 7)).toContainText('복구 접근');
});

for (const [name, interrupted, final, chip, failAt] of [
  ['실패', null, { state: 'failed', reason_code: 'exec.goal_not_reached' }, '실패', 5],
  ['취소', 'exec.canceled', { state: 'stopped' }, '취소됨', null],
]) {
  test(`[UI-STEP-04-${name}] ${name}은 마지막 확인 지점에서 멈추고 상태를 보인다 — 완료로 보이지 않는다`, async ({ page }) => {
    const ctl = { job: running(4) };
    await setup(page, ctl);
    await approve(page);
    await expect(step(page, 5)).toHaveClass(/current/, { timeout: 8000 });
    ctl.job = { ...running(failAt || 4, failAt ? {} : {}), progress: progress(failAt || 4, { failAt }), status: 'finished' };
    ctl.finish({ ok: false, execution_id: 'exec-1', interrupted, final });
    await openView(page);
    await expect(stepper(page).locator('.stepper-chip')).toContainText(`${chip} · 4/12 단계 완료`, { timeout: 8000 });
    await expect(step(page, 4)).toHaveClass(/done/);
    await expect(step(page, 5)).toHaveClass(failAt ? /failed/ : /halted/);
    await expect(step(page, 6)).toHaveClass(/wait/);
    if (name === '실패') {
      await expect(page.locator('.sim-card')).toContainText('실행 실패');                                     // 기존 오류 안내 유지
      await expect(page.locator('.sim-card')).toContainText('exec.goal_not_reached');
      await expect(stepper(page)).toContainText('현재: A자재 이송 실패 · 5단계 pick 접근');
    }                 // 기존 오류 원인·조치 안내 유지
  });
}

test('[UI-STEP-05] 즉시 정지는 확인된 지점에서 멈춘 것으로 보이고 진행 중으로 보이지 않는다', async ({ page }) => {
  const ctl = { job: running(3) };
  await setup(page, ctl);
  await approve(page);
  await expect(step(page, 4)).toHaveClass(/current/, { timeout: 8000 });
  await page.getByRole('button', { name: '즉시 정지' }).first().click();
  const stopping = page.getByRole('dialog', { name: /정지 요청됨/ });
  await expect(stopping).toBeVisible({ timeout: 8000 });
  await expect(step(page, 4)).toHaveClass(/halted/);
  await expect(stepper(page).locator('.stepper-chip')).toContainText('즉시 정지 요청됨');
  ctl.job = { ...running(3), status: 'finished' };
  ctl.finish({ ok: false, execution_id: 'exec-1', interrupted: 'exec.stopped', final: { state: 'stopped' } });
});

test('[UI-STEP-06] 진행 조회가 연속 실패하면 마지막 확인 상태를 두고 통신 확인 안 됨을 보인다 — 완료로 바꾸지 않는다', async ({ page }) => {
  const ctl = { job: running(5) };
  await setup(page, ctl);
  await approve(page);
  await expect(step(page, 6)).toHaveClass(/current/, { timeout: 8000 });
  ctl.jobDown = true;
  await expect(stepper(page).locator('.stepper-chip')).toContainText('통신 확인 안 됨 · 5/12 단계 완료', { timeout: 12000 });
  await expect(stepper(page)).toContainText('진행 조회가 연속으로 실패했습니다');
  await expect(step(page, 6)).toHaveClass(/current/);                                            // 그대로, 완료로 넘기지 않음
  await expect(step(page, 6)).not.toHaveClass(/done/);
  const anim = await step(page, 6).locator('.stepper-dot').evaluate((el) => getComputedStyle(el).animationName);
  expect(anim).toBe('none');                                                                     // 살아 있는 것처럼 펄스하지 않는다
  ctl.finish({ ok: true, execution_id: 'exec-1', final: { state: 'completed' } });
});

test('[UI-STEP-06B] 조회는 정상인데 단계가 오래 안 바뀌면 끊김이 아니라 단계 경과로만 보인다', async ({ page }) => {
  const ctl = { job: running(5) };
  await setup(page, ctl);
  await approve(page);
  await expect(step(page, 6)).toHaveClass(/current/, { timeout: 8000 });
  await expect(stepper(page)).toContainText(/이 단계 \d+초째 진행 중 · 서버 조회 정상/, { timeout: 12000 });
  await expect(stepper(page).locator('.stepper-chip')).toContainText('진행 중 · 5/12 단계 완료');
  await expect(stepper(page)).not.toContainText('통신 확인 안 됨');
  await expect(stepper(page)).not.toContainText('갱신 지연');
  const anim = await step(page, 6).locator('.stepper-dot').evaluate((el) => getComputedStyle(el).animationName);
  expect(anim).toBe('stepper-pulse');                                                            // 정상 진행 — 펄스 유지
  ctl.finish({ ok: true, execution_id: 'exec-1', final: { state: 'completed' } });
});

test('[UI-STEP-07] 새 실행은 실행 ID 기준으로 이전 진행을 지운다', async ({ page }) => {
  const ctl = { job: running(12) };
  await setup(page, ctl);
  await approve(page);
  await expect(step(page, 12)).toHaveClass(/done/, { timeout: 8000 });
  ctl.job = { ...running(12), status: 'finished' };
  ctl.finish({ ok: true, execution_id: 'exec-1', final: { state: 'completed' } });
  await openView(page);
  await expect(stepper(page).locator('.stepper-chip')).toContainText('완료', { timeout: 8000 });
  await page.locator('.sim-card').getByRole('button', { name: '창 닫기' }).click();
  // 두 번째 명령 — 새 실행이 시작되면 첫 단계부터.
  let release2;
  ctl.executed = new Promise((r) => { release2 = r; });
  ctl.finish = (body) => { ctl.executeBody = body; release2(); };
  ctl.job = { ...running(0), job_id: 'simjob-9' };
  await page.locator('section.command').getByRole('button', { name: '새 명령 입력' }).click();
  await approve(page);
  await expect(step(page, 1)).toHaveClass(/current/, { timeout: 8000 });
  await expect(stepper(page).locator('.stepper-step.done')).toHaveCount(0);
  ctl.finish({ ok: true, execution_id: 'exec-2', final: { state: 'completed' } });
});

test('[UI-STEP-08] 반복 작업은 회차 표시를 유지하고, 스테퍼는 지금 회차의 개별 작업을 따른다', async ({ page }) => {
  const ctl = { runningJob: { job_id: 'simjob-r1', status: 'running' },
    jobs: { 'simjob-r1': { ...running(5), job_id: 'simjob-r1' },
      'simjob-r2': { job_id: 'simjob-r2', action: 'return', action_label: '복귀', material: 'material_a', status: 'running', stage_plan: stagePlan(), progress: progress(1) } } };
  await setup(page, ctl);
  await page.route((url) => url.pathname.startsWith('/v1/sim-demo/repeat'), (route) => route.fulfill({ json: { run: { run_id: 'rep_1', state: 'running', active: true, count: 3,
    label: '1/3회 · A자재 이송 중', finish_after_round: false, materials: [{ id: 'mat_a', name: 'A자재' }], reason: null } } }));
  await page.reload();
  await page.getByRole('button', { name: '시뮬레이션 보기' }).click();
  await expect(page.locator('.sim-card')).toContainText('반복 작업: 1/3회 · A자재 이송 중', { timeout: 8000 });
  await expect(step(page, 6)).toHaveClass(/current/, { timeout: 8000 });
  await expect(stepper(page)).toContainText('현재: A자재 이송 중');
  ctl.runningJob = { job_id: 'simjob-r2', status: 'running' };
  await expect(stepper(page)).toContainText('현재: A자재 복귀 중', { timeout: 12000 });
  await expect(step(page, 2)).toHaveClass(/current/);
  await expect(stepper(page).locator('.stepper-step.done')).toHaveCount(1);                       // 앞 회차 진행이 남지 않는다
});

test('[UI-STEP-09] 전체화면에서도 같은 스테퍼를 3D 아래 자기 줄에 보인다', async ({ page }) => {
  const ctl = { job: running(3) };
  await setup(page, ctl);
  await approve(page);
  await expect(step(page, 4)).toHaveClass(/current/, { timeout: 8000 });
  await page.locator('.sim-card').getByRole('button', { name: '전체화면' }).click();
  const fs = page.locator('.sim-stage .stepper');
  await expect(fs).toBeVisible();
  await expect(fs.locator('.stepper-step').nth(3)).toHaveClass(/current/);
  const video = await page.locator('.sim-stage .sim-video').boundingBox();
  const bar = await fs.boundingBox();
  expect(bar.y).toBeGreaterThanOrEqual(video.y + video.height - 1);                            // 3D를 덮지 않는다
  ctl.finish({ ok: true, execution_id: 'exec-1', final: { state: 'completed' } });
});

test('[UI-STEP-10] 실행기가 시작 전에 거절하면 "이송 시작 안 함"과 실행기의 원인·다음 조치를 보인다(결과 확인 안 됨이 아님)', async ({ page }) => {
  // 2026-10-09 격리 셀 재현: 그리퍼 닫기 중 정지 → 복구 뒤 팔이 집기 자세에 남아, 다음 이송의 첫 경로가 자재와 닿아 시작하지 않았다.
  const ctl = {};
  await setup(page, ctl);
  ctl.job = running(0);
  await approve(page);
  await page.waitForTimeout(1500);
  ctl.job = { ...running(0), status: 'finished', report: { status: 'simulation_transfer_not_started', not_started_reason: 'geometry.collision',
    detail: '안전 home 경로 5/158 단계에서 선언되지 않은 접촉:finger↔material_b — 로봇이 안전 home 자세가 아니라 출발 경로가 자재와 닿습니다',
    next_action: "로봇이 안전 home 자세가 아니라 출발 경로가 자재와 닿습니다 — '로봇을 홈 위치로 이동해'로 먼저 홈에 보낸 뒤 다시 요청해 주세요" } };
  ctl.finish({ ok: false, execution_id: 'exec-1', final: { state: 'unknown', reason_code: 'exec.unverifiable',
    evidence: { detail: '이송 실행기 결과 simulation_transfer_not_started (종료 코드 1)' } } });
  const card = page.locator('section.command .cmd-card');
  await expect(card).toContainText('이송 시작 안 함 · 로봇 이동 없음', { timeout: 8000 });
  await expect(card).not.toContainText('결과 확인 안 됨');
  const guide = card.getByRole('note', { name: '오류 원인과 조치' });
  await expect(guide).toContainText('원인안전 home 경로 5/158 단계');
  await expect(guide).toContainText("조치로봇이 안전 home 자세가 아니라 출발 경로가 자재와 닿습니다 — '로봇을 홈 위치로 이동해'");
});

test('[UI-STEP-11] 이송이 아닌 계획(홈 이동)의 결과 카드 제목에 undefined가 보이지 않는다', async ({ page }) => {
  const ctl = { runningJob: null };
  await setup(page, ctl);
  await approve(page);
  ctl.finish({ ok: true, execution_id: 'exec-2', final: { state: 'completed', reason_code: null, evidence: {} } });
  const card = page.locator('section.command .cmd-card');
  await expect(card).toContainText('실행 완료', { timeout: 8000 });
  await expect(card).not.toContainText('undefined');
  await expect(card).toContainText('계획 실행');
});

// ── 레이아웃(2026-10-09): 안내·단계 영역 자리 고정, 단계 이름은 의미 단위로만 줄바꿈 ─────────────────────
const RETURN = ['안전 home', '컨베이어 접근', 'pre-grasp(컨베이어)', '그리퍼 열기', '컨베이어 pick 접근', '그리퍼 닫기', 'lift',
  '원래 팔레트 접근', '원래 슬롯 접근', '그리퍼 열기(해제)', 'retreat', '안전 home 복귀'];
const SHOTS = process.env.STEPPER_SHOTS || null;
const waiting = () => ({ job_id: 'simjob-1', action: 'return', action_label: '복귀', material: 'material_a', status: 'running', stage_plan: null, progress: [] });
const returning = (n, extra = {}) => ({ ...waiting(), stage_plan: stagePlan(RETURN), progress: progress(n, { labels: RETURN }), ...extra });

// 스테퍼 박스 높이와 바로 아래 요소의 위치.
async function box(page, root) {
  const got = await page.locator(root).first().evaluate((el) => {
    const r = el.getBoundingClientRect();
    const next = el.nextElementSibling;
    const parts = [...el.children].map((c) => `${c.className.split(' ')[0]}:${Math.round(c.getBoundingClientRect().height)}`).join(' ');
    return { h: Math.round(r.height), below: next ? Math.round(next.getBoundingClientRect().top - r.top) : null, parts };
  });
  if (process.env.STEPPER_DEBUG) console.log(root, JSON.stringify(got));
  return { h: got.h, below: got.below };
}
// 단계 이름: 덩어리(낱말·괄호 묶음) 안에서 줄이 바뀌지 않고, 괄호는 한 덩어리 안에서 열고 닫히며, 글자를 자르지 않는다.
async function labelsIntact(page, root, labels) {
  const rows = await page.locator(`${root} .stepper-label`).evaluateAll((els) => els.map((el) => ({
    text: el.textContent,
    segs: [...el.querySelectorAll('.stepper-seg')].map((s) => ({ t: s.textContent, lines: s.getClientRects().length })),
    clipped: el.scrollWidth > el.clientWidth + 1,
  })));
  expect(rows.map((r) => r.text.replace(/\s+/g, ' '))).toEqual(labels);
  for (const r of rows) {
    expect(r.clipped).toBe(false);
    for (const s of r.segs) {
      expect(s.lines, `'${s.t}'가 두 줄로 나뉘었다`).toBe(1);
      expect(s.t.includes('(')).toBe(s.t.includes(')'));
    }
  }
}

for (const view of [{ name: '일반', width: 1440, height: 900, full: false }, { name: '전체화면', width: 1440, height: 900, full: true },
  { name: '좁은 화면', width: 390, height: 844, full: false }]) {
  test(`[UI-STEP-12-${view.name}] 대기 → 진행 → 완료에서 스테퍼 높이·아래 UI 위치가 그대로이고 단계 이름이 의미 단위로만 줄바꿈된다`, async ({ page }) => {
    test.setTimeout(60_000);
    await page.setViewportSize({ width: view.width, height: view.height });
    const ctl = { job: waiting() };
    await setup(page, ctl);
    if (view.width < 800) await page.locator('header').getByRole('button', { name: '명령 패널' }).click();   // 좁은 화면: 명령 패널은 서랍
    await approve(page);
    if (view.width < 800) await page.locator('header').getByRole('button', { name: '명령 패널' }).click();   // 서랍을 닫고 시뮬레이션 보기를 본다
    const root = view.full ? '.sim-stage .stepper' : '.sim-card .stepper';
    if (view.full) await page.locator('.sim-card').getByRole('button', { name: '전체화면' }).click();
    await expect(page.locator(`${root} .stepper-waiting`)).toBeVisible({ timeout: 8000 });
    const shot = async (n) => {
      if (!SHOTS) return;
      await page.waitForTimeout(400);                                            // 서랍 애니메이션이 끝난 뒤
      await page.locator(root).first().scrollIntoViewIfNeeded({ timeout: 1000 }).catch(() => {});
      await page.screenshot({ path: `${SHOTS}/${view.name}-${n}.png` });
    };
    const wait = await box(page, root);
    await shot('1-대기');
    ctl.job = returning(2);
    await expect(page.locator(`${root} .stepper-step`)).toHaveCount(12, { timeout: 8000 });
    const run = await box(page, root);
    await shot('2-진행');
    await labelsIntact(page, root, RETURN);
    // 같은 단계가 6초 넘게 이어지면 안내 줄('이 단계 N초째…')이 생긴다 — 그래도 움직이지 않는다.
    await expect(page.locator(`${root} .stepper-note`)).toContainText('초째', { timeout: 12000 });
    const note = await box(page, root);
    await shot('3-진행-안내');
    expect(run).toEqual(wait);
    expect(note).toEqual(wait);
    // 완료: 마지막 단계까지 도달 확인 → 실행 완료.
    ctl.job = returning(12);
    await expect(page.locator(root)).toContainText('12/12 단계 완료', { timeout: 8000 });
    await shot('4-모든-단계-완료');
    expect(await box(page, root)).toEqual(wait);
    ctl.job = returning(12, { status: 'finished' });
    ctl.finish({ ok: true, execution_id: 'exec-1', final: { state: 'completed' } });
    if (!view.full) {
      const drawer = page.locator('header').getByRole('button', { name: '명령 패널' });
      if (view.width < 800) await drawer.click();
      await openView(page);
      if (view.width < 800) await drawer.click();
      await expect(page.locator(root)).toContainText('완료 · 12/12 단계 완료', { timeout: 8000 });
      await shot('5-실행-완료');
      expect((await box(page, root)).h).toBe(wait.h);
      await labelsIntact(page, root, RETURN);
    } else {
      await page.waitForTimeout(3000);
      await shot('5-실행-완료');
    }
  });
}

// ── 동작 세부 진행 칸 고정(2026-10-09): 상태가 바뀌어도 시뮬레이션 화면의 위치·크기와 진행 칸 높이가 그대로 ───────────
// 3D 화면 위치는 창 안 내용 기준(창 스크롤과 무관)으로 잰다.
async function geom(page, full) {
  return page.evaluate((isFull) => {
    const card = document.querySelector('.sim-card');
    const v = document.querySelector('.sim-video').getBoundingClientRect();
    const c = card.getBoundingClientRect();
    const prog = document.querySelector(isFull ? '.sim-fs-progress' : '.sim-progress-wrap').getBoundingClientRect();
    const oy = isFull ? 0 : card.scrollTop - c.top;
    return { video: [Math.round(v.left), Math.round(v.top + oy), Math.round(v.width), Math.round(v.height)], progress: Math.round(prog.height),
      card: isFull ? null : Math.round(card.scrollHeight) };
  }, full);
}

for (const view of [{ name: '일반', width: 1440, height: 900, full: false }, { name: '전체화면', width: 1440, height: 900, full: true },
  { name: '좁은 화면', width: 390, height: 844, full: false }]) {
  const drawer = (page) => page.locator('header').getByRole('button', { name: '명령 패널' });
  const narrow = view.width < 800;
  const root = view.full ? '.sim-fs-progress' : '.sim-progress';                 // 보이는 스테퍼
  async function start(page, ctl) {
    await page.setViewportSize({ width: view.width, height: view.height });
    await setup(page, ctl);
    if (narrow) await drawer(page).click();
    await approve(page);
    if (narrow) await drawer(page).click();
    if (view.full) await page.locator('.sim-card').getByRole('button', { name: '전체화면' }).click();
    await expect(page.locator(`${root} .stepper`)).toBeVisible({ timeout: 8000 });
    await page.waitForTimeout(400);
  }
  // 실행이 끝나면 창이 닫힌다(기존 규칙) — 다시 열고(전체화면이면 다시 전체화면) 같은 자리를 잰다.
  async function reopen(page) {
    // 정지로 끝나면 창이 정지 확인 화면으로 남는다 — 그때는 다시 열지 않는다.
    const closed = await expect(page.locator('.sim-card')).toHaveCount(0, { timeout: 4000 }).then(() => true, () => false);
    if (!closed) { await page.waitForTimeout(400); return; }
    if (narrow) await drawer(page).click();
    await page.getByRole('button', { name: '시뮬레이션 보기' }).click();
    if (narrow) await drawer(page).click();
    if (view.full) await page.locator('.sim-card').getByRole('button', { name: '전체화면' }).click();
    await page.waitForTimeout(400);
  }
  const shotDir = process.env.STEPPER_SHOTS;
  const shot = (page, n) => shotDir && page.screenshot({ path: `${shotDir}/고정-${view.name}-${n}.png` });

  test(`[UI-STEP-13-${view.name}] 대기→실행→상세 보기→정지 요청→정지: 시뮬레이션 화면 위치·크기와 진행 칸 높이가 그대로`, async ({ page }) => {
    test.setTimeout(60_000);
    const ctl = { job: waiting() };
    await start(page, ctl);
    await expect(page.locator(`${root} .stepper-waiting`)).toBeVisible();
    const base = await geom(page, view.full);
    await shot(page, '1-대기');
    ctl.job = returning(3);
    await expect(page.locator(`${root} .stepper-step`)).toHaveCount(12, { timeout: 8000 });
    expect(await geom(page, view.full)).toEqual(base);
    await shot(page, '2-실행');
    if (!view.full) {
      await page.locator('.sim-card .stepper').getByRole('button', { name: '상세 보기' }).click();
      await expect(page.locator('.sim-card .stepper-pop')).toBeVisible();
      expect(await geom(page, view.full)).toEqual(base);
      await shot(page, '3-상세-팝업');
      await page.locator('.sim-card .stepper-pop').getByRole('button', { name: '상세 닫기' }).click();
    }
    // 전체화면에서는 헤더가 가려진다 — 화면 안 막대의 즉시 정지를 쓴다.
    await (view.full ? page.locator('.sim-fs-stop') : page.locator('header').getByRole('button', { name: '즉시 정지' }).first()).click();
    await expect(page.locator(`${root} .stepper-chip`)).toContainText('즉시 정지 요청됨', { timeout: 8000 });
    expect(await geom(page, view.full)).toEqual(base);
    await shot(page, '4-정지-요청');
    ctl.job = returning(3, { status: 'finished' });
    ctl.finish({ ok: false, execution_id: 'exec-1', interrupted: 'exec.stopped', final: { state: 'stopped' } });
    await reopen(page);
    await expect(page.locator(`${root} .stepper-step`).nth(3)).toHaveClass(/halted/, { timeout: 8000 });
    expect(await geom(page, view.full)).toEqual(base);
    await shot(page, '5-정지');
  });

  for (const [what, finalBody, job, text] of [
    ['실패', { ok: false, execution_id: 'exec-1', final: { state: 'failed', reason_code: 'exec.goal_not_reached' } },
      () => returning(5, { status: 'finished', progress: progress(5, { failAt: 5, labels: RETURN }) }), '실패'],
    ['완료', { ok: true, execution_id: 'exec-1', final: { state: 'completed' } }, () => returning(12, { status: 'finished' }), '12/12 단계 완료'],
  ]) {
    test(`[UI-STEP-13-${view.name}-${what}] 실행 → ${what}에서도 시뮬레이션 화면 위치·크기와 진행 칸 높이가 그대로`, async ({ page }) => {
      test.setTimeout(60_000);
      const ctl = { job: returning(4) };
      await start(page, ctl);
      await expect(page.locator(`${root} .stepper-step`)).toHaveCount(12, { timeout: 8000 });
      const base = await geom(page, view.full);
      ctl.job = job();
      ctl.finish(finalBody);
      await reopen(page);
      await expect(page.locator(`${root} .stepper-chip`)).toContainText(text, { timeout: 8000 });
      expect(await geom(page, view.full)).toEqual(base);
      await shot(page, `6-${what}`);
    });
  }
}
