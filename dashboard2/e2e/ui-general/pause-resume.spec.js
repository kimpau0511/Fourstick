// 시뮬레이션 보기의 '일시정지 / 재개'(2026-10-07): 이 작업만 멈추고(전체 정지 아님) 정지 지점에서 이어서 옮긴다.
// 재개 버튼은 서버가 그 자재를 재개 가능(actions.resume)하다고 할 때만 열린다. 서버 응답은 가짜다.
import { expect, test } from '@playwright/test';
import { fixture, mockBackend } from '../mock.js';

const plan = {
  ok: true, session_id: 'qa-session', request_id: 'req-qa', executable: true, stop_latch: { cleared: true },
  plan: { plan_id: 'plan-qa', plan_hash: 'hash-qa', robot_id: 'fr3', profile_id: 'p', profile_version: 'v', created_at: 0, ttl_sec: 600,
    utterance: 'A자재를 컨베이어로 옮겨줘', steps: [{ index: 1, skill: 'move', args: { to: 'loc_pallet_1' } }, { index: 2, skill: 'pick', args: { object: 'mat_a' } }] },
  safety: { decision: 'allow', rules: [] }, validation: { decision: 'allow', detail: '' },
};
const STAGES = ['안전 home', '팔레트 접근', 'pre-grasp', '그리퍼 열기', 'pick 접근', '그리퍼 닫기', 'lift'];
const progress = (n) => STAGES.slice(0, n).map((label, i) => ({ no: i + 1, of: 12, label, reached: true }));

async function setup(page, { resumable = true } = {}) {
  await mockBackend(page);
  const calls = [];
  let paused = false;
  let release;
  const executed = new Promise((r) => { release = r; });
  let resumeChecks = 0;
  let restored = false;
  const json = (route, body, status = 200) => route.fulfill({ status, json: body });
  const simDemo = () => fixture('simDemo', (base) => ({
    ...base,
    running_job: paused ? null : { job_id: 'simjob-1', action: 'transfer', material: 'material_a', status: 'running' },
    materials: base.materials.map((m) => (m.model !== 'material_a' || !paused || restored ? m
      : { ...m, record: { state: 'stopped_unrestored' }, actions: { ...m.actions, transfer: false, resume: resumable, restore: true } })),
    state: { ...base.state, checkpoint: paused ? { model: 'material_a', checkpoint_id: 'cp-1' } : null },
  }));
  await page.route('**/v1/sessions**', (route) => json(route, { session_id: 'qa-session', client_id: 'c1' }));
  await page.route('**/v1/plan', (route) => json(route, plan));
  await page.route('**/v1/decision', (route) => json(route, { ok: true, approval_id: 'appr-1' }));
  await page.route('**/v1/execute', async (route) => { calls.push('/v1/execute'); await executed; return json(route, { ok: false, execution_id: 'exec-1', interrupted: 'exec.canceled', final: { state: 'stopped' } }); });
  await page.route('**/v1/stop', (route) => { calls.push('/v1/stop'); return json(route, { ok: true, requested: true }); });
  await page.route('**/v1/executions/active/cancel', (route) => {
    calls.push({ path: 'pause', body: route.request().postDataJSON() });
    paused = true; release();
    return json(route, { ok: true, requested: true, execution_ids: ['exec-1'], detail: '일시정지를 요청했습니다 — 실행기가 정지 지점(체크포인트)을 남기고 멈춥니다' });
  });
  await page.route((url) => url.pathname === '/v1/sim-demo', (route) => json(route, simDemo()));
  await page.route((url) => url.pathname.startsWith('/v1/sim-demo/jobs'), (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      calls.push({ path: body.action, body });
      if (body.action === 'restore') restored = true;
      return json(route, { job_id: 'simjob-2', status: 'running' }, 202);
    }
    if (path.endsWith('simjob-1')) return json(route, paused
      ? { job_id: 'simjob-1', material: 'material_a', status: 'finished', progress: progress(7), report: { status: 'simulation_transfer_stopped' } }
      : { job_id: 'simjob-1', material: 'material_a', status: 'running', progress: progress(5) });
    resumeChecks += 1;
    return json(route, resumeChecks < 2
      ? { job_id: 'simjob-2', material: 'material_a', status: 'running', progress: progress(7) }
      : { job_id: 'simjob-2', material: 'material_a', status: 'finished', exit_code: 0, report: { status: restored ? 'restored' : 'simulation_transfer_resumed_completed' } });
  });
  await page.goto('/');
  await page.getByLabel('자연어 명령').fill('A자재를 컨베이어로 옮겨줘');
  await page.getByRole('button', { name: '보내기' }).click();
  await page.locator('section.command').getByRole('button', { name: '실행 승인' }).click();
  return calls;
}

test('[UI-PAUSE-01] 일시정지는 이 작업만 멈추고(전체 정지 아님) 재개는 정지 지점에서 이어서 옮긴다', async ({ page }) => {
  const calls = await setup(page);
  const view = page.getByRole('dialog', { name: '가상 동작 확인 중' });
  await expect(view.getByRole('button', { name: /일시정지/ })).toBeVisible({ timeout: 8000 });
  await view.getByRole('button', { name: /일시정지/ }).click();
  const paused = page.getByRole('dialog', { name: '일시정지됨' });
  await expect(paused).toBeVisible({ timeout: 8000 });
  await expect(paused).toContainText('정지 지점에서 멈췄습니다');
  expect(calls.find((c) => c.path === 'pause').body).toEqual({ session_id: 'qa-session' });
  expect(calls).not.toContain('/v1/stop');                                   // 전체 정지가 아니다
  await expect(paused.getByRole('button', { name: /복구/ })).toHaveCount(0);       // 재개 가능하면 복구는 보이지 않는다
  await paused.getByRole('button', { name: /재개/ }).click();
  await expect.poll(() => calls.find((c) => c.path === 'resume')?.body).toEqual({ action: 'resume', material: 'material_a', checkpoint_id: 'cp-1' });
  await expect(page.locator('section.command')).toContainText('재개 · 이어서 이송 완료', { timeout: 10000 });
});

test('[UI-PAUSE-02] 서버가 재개 가능하지 않다고 하면 재개를 잠그고 이유와 복구(원래 자리로)를 보인다', async ({ page }) => {
  const calls = await setup(page, { resumable: false });
  const view = page.getByRole('dialog', { name: '가상 동작 확인 중' });
  await view.getByRole('button', { name: /일시정지/ }).click({ timeout: 8000 });
  const paused = page.getByRole('dialog', { name: '일시정지됨' });
  await expect(paused.getByRole('button', { name: /재개/ })).toBeDisabled({ timeout: 8000 });
  await expect(paused).toContainText('이 지점에서는 이어서 할 수 없습니다');
  expect(calls.some((c) => c.path === 'resume')).toBe(false);
  // 재개할 수 없으면 복구(원래 자리로)를 제공한다 — 결과는 서버 자재 기록으로 판정.
  await paused.getByRole('button', { name: /복구/ }).click();
  await expect.poll(() => calls.find((c) => c.path === 'restore')?.body).toEqual({ action: 'restore', material: 'material_a' });
  await expect(page.locator('section.command')).toContainText('복구 완료 · 원래 자리', { timeout: 10000 });
});

test('[UI-PAUSE-03] 명령 패널에는 즉시 정지가 없고, 헤더의 즉시 정지는 그대로 전체 정지다', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  await expect(page.locator('section.command').getByRole('button', { name: '즉시 정지' })).toHaveCount(0);
  await expect(page.locator('header').getByRole('button', { name: /즉시 정지/ })).toBeEnabled();
});
