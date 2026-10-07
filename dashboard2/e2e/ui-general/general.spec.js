// 일반 경로(VITE_COMMAND_MODE=general) 화면 검사: 계획 → 안전 관문 → 승인(본 계획 해시) → 실행 → 결과, 정지는 /v1/stop.
// 서버 응답은 가짜다(로봇·LLM 없이 결정적으로 돈다).
import { expect, test } from '@playwright/test';
import { mockBackend } from '../mock.js';

const plan = (decision, extra = {}) => ({
  ok: true, session_id: 'qa-session', request_id: 'req-qa', executable: decision === 'allow', stop_latch: { cleared: true },
  plan: { plan_id: 'plan-qa', plan_hash: 'hash-qa', robot_id: 'fr3', profile_id: 'p', profile_version: 'v', created_at: 0, ttl_sec: 600,
    utterance: 'A자재를 컨베이어에 놓아줘', steps: [{ index: 1, skill: 'move', args: { target: 'loc_pallet_1' } }, { index: 2, skill: 'pick', args: { object: 'mat_a' } }] },
  safety: { decision, rules: decision === 'block' ? [{ code: 'R1', status: 'block', message: '사용 금지 구역입니다' }] : [] },
  validation: { decision, detail: '' }, ...extra,
});

async function general(page, { planResponse, execute, stop } = {}) {
  await mockBackend(page);
  const calls = [];
  const json = (route, body, status = 200) => route.fulfill({ status, json: body });
  await page.route('**/v1/sessions**', (route) => json(route, { session_id: 'qa-session', client_id: 'c1' }));
  await page.route('**/v1/plan', (route) => { calls.push({ path: '/v1/plan', body: route.request().postDataJSON() }); return json(route, planResponse); });
  await page.route('**/v1/decision', (route) => { calls.push({ path: '/v1/decision', body: route.request().postDataJSON() }); return json(route, { ok: true, approval_id: 'appr-1' }); });
  await page.route('**/v1/execute', async (route) => { calls.push({ path: '/v1/execute', body: route.request().postDataJSON() }); await new Promise((r) => setTimeout(r, 800)); return json(route, execute); });
  await page.route('**/v1/stop', (route) => { calls.push({ path: '/v1/stop' }); return json(route, stop || { ok: true, requested: true, confirmed: true }); });
  return calls;
}

async function send(page, text) {
  await page.goto('/');
  await page.getByLabel('자연어 명령').fill(text);
  await page.getByRole('button', { name: '보내기' }).click();
}
const panel = (page) => page.locator('section.command');

test('[UI-GEN-01][SFR] 관문 allow → 계획 단계·실행 승인 → 승인은 본 계획 해시로, 실행 결과는 서버 최종 상태', async ({ page }) => {
  const calls = await general(page, { planResponse: plan('allow'), execute: { ok: true, execution_id: 'exec-1', final: { state: 'completed' } } });
  await send(page, 'A자재를 컨베이어에 놓아줘');
  await expect(panel(page)).toContainText('일반 경로');
  await expect(panel(page).getByRole('list', { name: '계획 단계' })).toContainText('2. pick (mat_a)');
  await panel(page).getByRole('button', { name: '실행 승인' }).click();
  await expect(panel(page)).toContainText('실행 완료', { timeout: 8000 });
  const decision = calls.find((c) => c.path === '/v1/decision');
  expect(decision.body).toMatchObject({ decision: 'approve', plan_hash: 'hash-qa', plan_id: 'plan-qa', request_id: 'req-qa' });
  expect(calls.find((c) => c.path === '/v1/execute').body.approval_id).toBe('appr-1');
});

test('[UI-GEN-02][SFR] 관문 block이면 승인 버튼 없이 서버 사유, 실행 요청 없음', async ({ page }) => {
  const calls = await general(page, { planResponse: plan('block') });
  await send(page, '금지 구역에 놓아줘');
  await expect(page.getByRole('dialog', { name: '실행 불가 — 서버가 차단했습니다' })).toContainText('사용 금지 구역입니다');
  await expect(page.getByRole('button', { name: '실행 승인' })).toHaveCount(0);
  expect(calls.some((c) => c.path === '/v1/decision' || c.path === '/v1/execute')).toBe(false);
});

test('[UI-GEN-03][SFR] 관문 ask·계획 되묻기는 실행하지 않고 질문을 보인다', async ({ page }) => {
  await general(page, { planResponse: { ok: false, clarification: '어느 자재를 옮길까요?' } });
  await send(page, '그거 옮겨');
  await expect(panel(page)).toContainText('어느 자재를 옮길까요?');
  await expect(panel(page).getByRole('button', { name: '답변 보내기' })).toBeVisible();
});

test('[UI-GEN-04][SFR] 실행 허가 거부(실행 id 없음)는 실행 안 됨으로, 성공으로 보이지 않는다', async ({ page }) => {
  await general(page, { planResponse: plan('allow'), execute: { ok: false, granted: false, reasons: [{ reason: 'exec.stale', detail: '관측이 오래되었습니다' }] } });
  await send(page, 'A자재를 컨베이어에 놓아줘');
  await panel(page).getByRole('button', { name: '실행 승인' }).click();
  await expect(panel(page)).toContainText('실행하지 않았습니다 — 관측이 오래되었습니다', { timeout: 8000 });
  await expect(panel(page)).not.toContainText('실행 완료');
});

test('[UI-GEN-05][SFR] 결과 unknown은 성공으로 바꾸지 않는다, 헤더 즉시 정지는 /v1/stop', async ({ page }) => {
  const calls = await general(page, { planResponse: plan('allow'), execute: { ok: false, execution_id: 'exec-2', final: { state: 'unknown' } } });
  await send(page, 'A자재를 컨베이어에 놓아줘');
  await panel(page).getByRole('button', { name: '실행 승인' }).click();
  await expect(panel(page)).toContainText('결과 확인 안 됨', { timeout: 8000 });
  await page.locator('header').getByRole('button', { name: /즉시 정지/ }).click();
  await expect.poll(() => calls.filter((c) => c.path === '/v1/stop').length).toBe(1);
  await expect(panel(page)).toContainText('정지를 확인했습니다');
});
