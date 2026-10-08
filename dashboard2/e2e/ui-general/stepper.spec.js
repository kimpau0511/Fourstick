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
  await page.route((url) => url.pathname.startsWith('/v1/sim-demo/jobs'), (route) => {
    if (route.request().method() === 'POST') { ctl.posted = route.request().postDataJSON(); return json(route, { job_id: 'simjob-2', status: 'running' }, 202); }
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
  await expect(stepper(page).locator('.stepper-list li').nth(3)).toContainText('진행 중');
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
