import { expect, test } from '@playwright/test';
import { SAMPLES, mockBackend, sendCommand } from '../mock.js';

const bundle = {
  request_id: 'general-request',
  plan: { plan_id: 'general-plan', plan_hash: 'general-hash', created_at: Date.now() / 1000, ttl_sec: 300, utterance: 'A자재 컨베이어로', steps: [
    { index: 1, skill: 'pick', args: { object: 'mat_a', from: 'loc_pallet_1' } },
    { index: 2, skill: 'place', args: { object: 'mat_a', to: 'loc_conveyor' } },
  ] },
  validation: { decision: 'allow', detail: '검증 통과' },
};

async function setup(page, validation = bundle.validation) {
  const calls = await mockBackend(page, {
    plan: () => ({ ok: true, payload: { ...bundle, plan: { ...bundle.plan, created_at: Date.now() / 1000 }, validation } }),
    decision: { ok: true, approval_id: 'general-approval' },
    execute: { ok: true, execution_id: 'general-execution', steps: [], final: { task_succeeded: true } },
  });
  await page.route('**/v1/sessions**', (route) => route.fulfill({ json: { session_id: 'general-session', client_id: 'general-client' } }));
  await page.goto('/');
  await expect(page.getByLabel('명령 모드', { exact: true })).toHaveCount(0);
  return calls;
}

test('일반 계획은 승인 전에 실행하지 않고 취소는 reject만 보낸다', async ({ page }, testInfo) => {
  const calls = await setup(page);
  await sendCommand(page, 'A자재 컨베이어로');
  await expect(page.getByRole('list', { name: '작업 계획' })).toBeVisible();
  await expect(page.getByRole('list', { name: '작업 계획' }).locator('li')).toHaveCount(2);
  await expect(page.locator('section.command')).toContainText('안전 검증: 검증 통과');
  await page.screenshot({ path: testInfo.outputPath('general-plan-approval.png') });
  expect(calls.map((c) => c.path)).toEqual(['/v1/plan']);
  await page.locator('section.command').getByRole('button', { name: '취소', exact: true }).click();
  await expect(page.locator('section.command')).toContainText('계획을 취소했습니다');
  expect(calls.at(-1).body).toMatchObject({ decision: 'reject', plan_id: 'general-plan', plan_hash: 'general-hash' });
  expect(calls.some((c) => c.path === '/v1/execute')).toBe(false);
});

for (const [label, validation] of [
  ['차단', { decision: 'block', detail: 'QA: 안전 검증 차단' }],
  ['판정 없음', null],
]) {
  test(`일반 계획의 ${label}에서는 승인을 제공하지 않고 실행하지 않는다`, async ({ page }) => {
    const calls = await setup(page, validation);
    await sendCommand(page, 'A자재 컨베이어로');
    await expect(page.locator('section.command')).toContainText(validation?.detail || '관문 판정이 응답에 없다');
    await expect(page.getByRole('button', { name: '실행 승인', exact: true })).toHaveCount(0);
    expect(calls.map((c) => c.path)).toEqual(['/v1/plan']);
  });
}

test('서버가 승인을 거부하면 execute를 보내지 않는다', async ({ page }) => {
  const calls = await mockBackend(page, { decision: { ok: false, detail: 'QA: 승인 거부' } });
  await page.goto('/');
  await sendCommand(page, 'A자재 컨베이어로');
  await page.locator('section.command').getByRole('button', { name: '실행 승인', exact: true }).click();
  await expect(page.locator('section.command')).toContainText('QA: 승인 거부');
  expect(calls.map((c) => c.path)).toEqual(['/v1/plan', '/v1/decision']);
});

test('헤더와 패널의 즉시 정지는 세션 연결·계획·승인 없이 일반 전체 정지를 요청한다', async ({ page }, testInfo) => {
  const calls = await mockBackend(page);
  await page.goto('/');
  await page.locator('header').getByRole('button', { name: '즉시 정지', exact: true }).click();
  await expect(page.locator('section.command')).toContainText('QA: 정지할 작업 없음');
  await page.locator('section.command').getByRole('button', { name: '즉시 정지', exact: true }).click();
  await expect.poll(() => calls.length).toBe(2);
  expect(calls.map((c) => [c.method, c.path, c.body])).toEqual([
    ['POST', '/v1/stop', {}], ['POST', '/v1/stop', {}],
  ]);
  await page.screenshot({ path: testInfo.outputPath('general-default-panel.png') });
});

test('실행 단계 이벤트는 계획의 전체 단계 수로 진행률을 갱신한다', async ({ page }) => {
  const calls = await mockBackend(page, { execute: null, progress: [SAMPLES.step] });
  await page.goto('/');
  await sendCommand(page, 'A자재 컨베이어로');
  await expect(page.getByRole('list', { name: '작업 계획' })).toBeVisible();
  expect(calls.map((c) => c.path)).toEqual(['/v1/plan']);
  await page.locator('section.command').getByRole('button', { name: '실행 승인', exact: true }).click();
  const bar = page.locator('section.command').getByRole('progressbar', { name: '작업 진행률' });
  await expect(bar).toHaveAttribute('aria-valuenow', '33');
  calls.emit({ type: 'step', payload: { index: 2, skill: 'pick', task_succeeded: true } });
  await expect(bar).toHaveAttribute('aria-valuenow', '67');
  calls.finishExecution(SAMPLES.done);
  await expect(page.locator('section.command')).toContainText('작업 완료');
});

test('승인 클릭에서만 승인 ID를 받은 뒤 execute를 호출한다', async ({ page }) => {
  const calls = await setup(page);
  await sendCommand(page, 'A자재 컨베이어로');
  await page.locator('section.command').getByRole('button', { name: '실행 승인', exact: true }).click();
  await expect(page.locator('section.command')).toContainText('작업 완료');
  expect(calls.map((c) => c.path)).toEqual(['/v1/plan', '/v1/decision', '/v1/execute']);
  expect(calls.at(-1).body).toMatchObject({ session_id: 'general-session', request_id: 'general-request', plan_id: 'general-plan', approval_id: 'general-approval' });
});

test('서버 ASK에는 승인 버튼이 없다', async ({ page }) => {
  const calls = await setup(page, { decision: 'ask', detail: '출발 위치 확인 필요' });
  await sendCommand(page, 'A자재 컨베이어로');
  await expect(page.locator('section.command')).toContainText('출발 위치 확인 필요');
  await expect(page.getByRole('button', { name: '실행 승인', exact: true })).toHaveCount(0);
  expect(calls.map((c) => c.path)).toEqual(['/v1/plan']);
});
