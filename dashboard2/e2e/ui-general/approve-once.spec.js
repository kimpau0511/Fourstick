// 승인은 한 번만 — 빠른 두 번 클릭에도 /v1/decision은 1회(중복 실행 방지).
import { expect, test } from '@playwright/test';
import { mockBackend } from '../mock.js';

test('[UI-GEN-ONCE] 실행 승인 두 번 클릭 → 승인 요청 1회', async ({ page }) => {
  await mockBackend(page);
  const calls = [];
  const plan = { ok: true, session_id: 'qa-session', request_id: 'req-qa', executable: true, stop_latch: { cleared: true },
    plan: { plan_id: 'plan-qa', plan_hash: 'hash-qa', robot_id: 'fr3', profile_id: 'p', profile_version: 'v', created_at: 0, ttl_sec: 600,
      utterance: 'A자재를 컨베이어로', steps: [{ index: 1, skill: 'move', args: { target: 'loc_pallet_1' } }] },
    safety: { decision: 'allow', rules: [] }, validation: { decision: 'allow', detail: '' } };
  await page.route('**/v1/sessions**', (route) => route.fulfill({ json: { session_id: 'qa-session', client_id: 'c1' } }));
  await page.route('**/v1/plan', (route) => route.fulfill({ json: plan }));
  await page.route('**/v1/decision', async (route) => { calls.push('decision'); await new Promise((r) => setTimeout(r, 600)); return route.fulfill({ json: { ok: true, approval_id: 'a1' } }); });
  await page.route('**/v1/execute', (route) => { calls.push('execute'); return route.fulfill({ json: { ok: true, execution_id: 'e1', final: { state: 'completed' } } }); });
  await page.goto('/');
  await page.getByLabel('자연어 명령').fill('A자재를 컨베이어로');
  await page.getByRole('button', { name: '보내기' }).click();
  const approve = page.locator('section.command').getByRole('button', { name: '실행 승인' });
  await approve.dblclick();
  await expect.poll(() => calls.filter((c) => c === 'execute').length, { timeout: 8000 }).toBe(1);
  expect(calls.filter((c) => c === 'decision')).toHaveLength(1);
});
