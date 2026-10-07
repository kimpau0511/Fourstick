import { expect, test } from '@playwright/test';
import { fixture, mockBackend } from '../mock.js';

// 통합 작업 명령 패널의 자재 버튼(복귀 포함) — 일반 계획 API·승인·취소·상태 재조회 (2026-10-06).
// 서버 상태(/v1/sim-demo)는 저장된 실제 응답(e2e/fixtures)을 바꿔 가며 쓴다: B자재가 컨베이어 1번 칸에 있다.
// 첫 조회 뒤 시계를 멈춰 2초 주기 조회를 끈다 — 작업 뒤 상태 조회가 '주기'가 아니라 '작업 종료' 때문에 일어났는지 본다.

const RETURN_PLAN = (utterance) => ({
  ok: true,
  payload: {
    request_id: 'ret-request',
    plan: { plan_id: 'ret-plan', plan_hash: 'ret-hash', utterance, created_at: Date.now() / 1000, ttl_sec: 300, steps: [
      { index: 1, skill: 'move', args: { to: 'loc_conveyor' } },
      { index: 2, skill: 'pick', args: { object: 'mat_b', from: 'loc_conveyor' } },
      { index: 3, skill: 'move', args: { to: 'loc_pallet_2' } },
      { index: 4, skill: 'place', args: { object: 'mat_b', to: 'loc_pallet_2' } },
      { index: 5, skill: 'home', args: {} },
    ] },
    validation: { decision: 'allow', detail: 'QA: 안전 검증 통과' },
  },
});

/** B자재가 원래 자리로 돌아간 상태(기록 없음 → 이송 가능). */
const returned = (base) => ({ ...base, materials: base.materials.map((m) => (m.model !== 'material_b' ? m
  : { ...m, record: null, slot: null, slot_label: null, actions: { ...m.actions, transfer: true, return: false, restore: false } })) });
const withRecord = (state) => (base) => ({ ...base, materials: base.materials.map((m) => (m.model !== 'material_b' ? m
  : { ...m, record: { ...m.record, state }, actions: Object.fromEntries(Object.keys(m.actions).map((k) => [k, false])) })) });

async function setup(page, { plan, execute, initial } = {}) {
  await page.clock.install();
  const calls = await mockBackend(page, { plan, execute });
  const sim = { state: initial ? initial(fixture('simDemo')) : fixture('simDemo'), gets: 0, fail: false };
  // mockBackend보다 나중에 등록 — 상태 조회만 여기서 가로챈다.
  await page.route((url) => url.pathname === '/v1/sim-demo', (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    sim.gets += 1;
    return sim.fail ? route.fulfill({ status: 500, json: { detail: 'QA: 상태 조회 실패' } }) : route.fulfill({ json: sim.state });
  });
  await page.goto('/');
  const panel = page.locator('section.command');
  await expect(panel.getByRole('button', { name: 'A자재 → 컨베이어' })).toBeVisible();
  // 첫 조회가 끝난 뒤 시계를 멈춘다 — 이후의 상태 조회는 주기 타이머가 아니라 화면 동작이 부른 것이다.
  await page.clock.pauseAt(Date.now() + 1000);
  return { calls, sim, panel };
}

test('[RET-1] 복귀 버튼은 일반 계획 API로 현재 위치→원래 위치 계획을 받고, 승인 후에만 실행하고, 끝나면 상태를 다시 조회한다', async ({ page }) => {
  let sim;
  const ctx = await setup(page, {
    plan: (body) => RETURN_PLAN(body.utterance),
    execute: () => { sim.state = returned(sim.state); return { ok: true, execution_id: 'ret-exec', steps: [], final: { task_succeeded: true } }; },
  });
  sim = ctx.sim;
  const { calls, panel } = ctx;
  const back = panel.getByRole('button', { name: 'B자재 원래 자리로' });
  await expect(back).toBeEnabled();
  await expect(back).toHaveAttribute('title', /현재: 컨베이어.*→ 원래: 2번 팔레트/);
  await expect(panel.getByRole('button', { name: 'B자재 → 컨베이어' })).toHaveCount(0);   // 서버: 이송 불가
  await back.click();
  await expect(panel.getByRole('list', { name: '작업 계획' }).locator('li')).toHaveCount(5);
  expect(calls).toEqual([{ method: 'POST', path: '/v1/plan', body: expect.objectContaining({ utterance: 'B자재를 원래 자리로 돌려놔' }) }]);
  expect(calls.some((c) => c.path.startsWith('/v1/sim-demo'))).toBe(false);        // 옛 시연 API를 쓰지 않는다
  const before = sim.gets;
  await panel.getByRole('button', { name: '실행 승인' }).click();
  await expect(panel).toContainText('작업 종료');
  expect(calls.map((c) => c.path)).toEqual(['/v1/plan', '/v1/decision', '/v1/execute']);
  await expect.poll(() => sim.gets).toBeGreaterThan(before);                       // 주기 조회가 아니라 종료 직후 재조회
  await panel.getByRole('button', { name: '새 명령 입력' }).click();
  await expect(panel.getByRole('button', { name: 'B자재 → 컨베이어' })).toBeEnabled();
  await expect(panel.getByRole('button', { name: 'B자재 원래 자리로' })).toHaveCount(0);
});

test('[RET-2] 복귀 계획을 취소하면 reject만 보내고 실행하지 않으며 상태를 다시 조회한다', async ({ page }) => {
  const { calls, sim, panel } = await setup(page, { plan: (body) => RETURN_PLAN(body.utterance) });
  await panel.getByRole('button', { name: 'B자재 원래 자리로' }).click();
  await expect(panel.getByRole('button', { name: '실행 승인' })).toBeVisible();
  const before = sim.gets;
  await panel.getByRole('button', { name: '취소', exact: true }).click();
  await expect(panel).toContainText('계획을 취소했습니다');
  expect(calls.map((c) => c.path)).toEqual(['/v1/plan', '/v1/decision']);
  expect(calls.at(-1).body).toMatchObject({ decision: 'reject', plan_id: 'ret-plan' });
  await expect.poll(() => sim.gets).toBeGreaterThan(before);
});

test('[RET-3] 이미 원래 자리면 실행 대신 안내한다(NOOP) — 승인 버튼이 없다', async ({ page }) => {
  const detail = 'B자재은(는) 이미 원래 자리(2번 팔레트)에 있습니다 — 옮길 필요가 없어 실행하지 않습니다';
  const { calls, panel } = await setup(page, { plan: () => ({ ok: false, decision: 'NOOP', detail, clarification: detail, blocked: null }) });
  await panel.getByRole('button', { name: 'B자재 원래 자리로' }).click();
  await expect(panel).toContainText('할 일 없음');
  await expect(panel).toContainText(detail);
  await expect(panel.getByRole('button', { name: '실행 승인' })).toHaveCount(0);
  expect(calls.map((c) => c.path)).toEqual(['/v1/plan']);
});

test('[RET-4] 의도 단계 차단(decision=BLOCK, blocked 없음)을 되묻기로 바꾸지 않고 이유를 보인다', async ({ page }) => {
  const detail = '원래 자리(2번 팔레트)에 A자재이(가) 있습니다(기록) — 먼저 비워야 합니다. 실행하지 않습니다';
  const { panel } = await setup(page, { plan: () => ({ ok: false, decision: 'BLOCK', detail, clarification: null, blocked: null, reason_code: 'plan.resource_mismatch' }) });
  await panel.getByRole('button', { name: 'B자재 원래 자리로' }).click();
  await expect(panel).toContainText('차단 · 실행 안 함');
  await expect(panel).toContainText(detail);
  await expect(panel).not.toContainText('되묻기');
  await expect(panel.getByRole('button', { name: '명령 수정' })).toBeVisible();
});

test('[RET-5] 되묻기는 서버 질문(clarification)을 그대로 보이고 답을 받는다', async ({ page }) => {
  const question = 'B자재을(를) 어디로 옮길까요? (지금 컨베이어, 원래 자리는 2번 팔레트)';
  const { panel } = await setup(page, { plan: () => ({ ok: false, decision: 'ASK', detail: 'x', clarification: question, blocked: null }) });
  await panel.getByRole('button', { name: 'B자재 원래 자리로' }).click();
  await expect(panel).toContainText(question);
  await expect(panel.getByRole('button', { name: '답변 보내기' })).toBeVisible();
});

test('[RET-6] 위치가 확정되지 않은 기록이면 복귀 버튼을 잠그고 이유를 보인다', async ({ page }) => {
  const { calls, panel } = await setup(page, { initial: withRecord('stopped_unrestored') });
  const back = panel.getByRole('button', { name: 'B자재 원래 자리로' });
  await expect(back).toBeDisabled();
  await expect(back).toHaveAttribute('title', /현재: 위치 확인 필요/);
  await expect(panel).toContainText('B자재의 위치를 확인할 수 없어(stopped_unrestored) 복귀 버튼을 잠갔습니다');
  expect(calls).toEqual([]);
});

test('[RET-7] 상태 조회가 실패하면 자재 버튼을 잠그고 이유를 보인다', async ({ page }) => {
  const { sim, panel } = await setup(page);
  await expect(panel.getByRole('button', { name: 'B자재 원래 자리로' })).toBeEnabled();
  sim.fail = true;
  await page.clock.runFor(2500);                                                   // 다음 주기 조회 → 실패
  await expect(panel.getByRole('button', { name: 'B자재 원래 자리로' })).toBeDisabled();
  await expect(panel.getByRole('button', { name: 'A자재 → 컨베이어' })).toBeDisabled();
  await expect(panel).toContainText('작업 상태 조회가 실패해 자재 버튼을 잠갔습니다');
});

test('[RET-8] 다른 작업이 실행 중이면 자재 버튼을 잠근다', async ({ page }) => {
  const { panel } = await setup(page, { initial: (base) => ({ ...base, running_job: { job_id: 'j1', action: 'transfer', status: 'running' } }) });
  await expect(panel.getByRole('button', { name: 'B자재 원래 자리로' })).toBeDisabled();
  await expect(panel).toContainText('다른 작업이 실행 중이라 자재 버튼을 잠갔습니다');
});
