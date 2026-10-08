// 신뢰도 점수와 상관없이 계획 → 안전 검증 → 승인 → 실행 흐름은 그대로: 점수 99%여도 승인 전에는 실행 요청이 없다.
import { expect, test } from '@playwright/test';
import { mockBackend } from '../mock.js';

test.use({ permissions: ['microphone'], launchOptions: { args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] } });

test('[UI-GEN-CONF] 신뢰도 99%도 승인 없이 실행하지 않는다', async ({ page }) => {
  await mockBackend(page);
  const calls = [];
  const plan = { ok: true, session_id: 'qa-session', request_id: 'req-qa', executable: true, stop_latch: { cleared: true },
    plan: { plan_id: 'plan-qa', plan_hash: 'hash-qa', robot_id: 'fr3', profile_id: 'p', profile_version: 'v', created_at: 0, ttl_sec: 600,
      utterance: 'A자재를 컨베이어로', steps: [{ index: 1, skill: 'move', args: { target: 'loc_pallet_1' } }] },
    safety: { decision: 'allow', rules: [] }, validation: { decision: 'allow', detail: '' } };
  await page.route('**/v1/sessions**', (route) => route.fulfill({ json: { session_id: 'qa-session', client_id: 'c1' } }));
  await page.route('**/v1/plan', (route) => { calls.push('plan'); return route.fulfill({ json: plan }); });
  await page.route('**/v1/decision', (route) => { calls.push('decision'); return route.fulfill({ json: { ok: true, approval_id: 'a1' } }); });
  await page.route('**/v1/execute', (route) => { calls.push('execute'); return route.fulfill({ json: { ok: true, execution_id: 'e1', final: { state: 'completed' } } }); });
  await page.routeWebSocket(/\/v1\/stt/, (ws) => {
    ws.send(JSON.stringify({ kind: 'session', session_id: 'qa-session', stream_id: 'stt_qa', sample_rate_hz: 16000 }));
    ws.onMessage((m) => { if (typeof m === 'string' && JSON.parse(m).type === 'flush') ws.send(JSON.stringify({ kind: 'final', text: 'A자재를 컨베이어로', raw_text: 'A자재를 컨베이어로', confidence: 0.99, request_id: 's1' })); });
  });
  await page.goto('/');
  const panel = page.locator('section.command');
  await panel.getByRole('button', { name: /음성 입력/ }).click();
  await panel.getByRole('button', { name: /음성 인식 중/ }).click();
  // 최종 결과는 자동으로 계획만 요청한다(2026-10-08). 승인·실행은 요청하지 않는다.
  await expect(panel.getByRole('button', { name: '실행 승인' })).toBeVisible();
  await expect(panel.getByTestId('stt-confidence')).toContainText('99%');
  await page.waitForTimeout(500);
  expect(calls).toEqual(['plan']);                      // 승인·실행 요청 없음
  await panel.getByRole('button', { name: '실행 승인' }).click();
  await expect.poll(() => calls.includes('execute'), { timeout: 8000 }).toBe(true);
});
