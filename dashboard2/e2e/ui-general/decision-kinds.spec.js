// 명령 해석 개선(2026-10-08): 의도 단계 판정(ASK·BLOCK·NOOP)을 화면이 서로 다르게 보이고, 어느 것도 승인·실행을 요청하지 않는다.
// 서버 응답은 가짜다(로봇·LLM 없이 결정적으로 돈다). 실행 실패 표시는 general.spec.js [UI-GEN-04·05]가 본다.
import { expect, test } from '@playwright/test';
import { mockBackend } from '../mock.js';

async function general(page, planResponse) {
  await mockBackend(page);
  const calls = [];
  await page.route('**/v1/sessions**', (route) => route.fulfill({ json: { session_id: 'qa-session', client_id: 'c1' } }));
  await page.route('**/v1/plan', (route) => { calls.push('/v1/plan'); return route.fulfill({ json: planResponse }); });
  await page.route('**/v1/decision', (route) => { calls.push('/v1/decision'); return route.fulfill({ json: { ok: true } }); });
  await page.route('**/v1/execute', (route) => { calls.push('/v1/execute'); return route.fulfill({ json: { ok: true } }); });
  return calls;
}

const failure = (decision, text, code) => ({ ok: false, session_id: 'qa-session', request_id: 'req-qa', decision, reason_code: code,
  detail: text, clarification: text, draft_steps: [], intent_tasks: [] });

const cases = [
  ['ASK', 'A 옮겨줘', 'A자재을(를) 어디로 옮길까요? (지금 원래 자리인 1번 팔레트에 있습니다)', 'plan.clarification_required', '되묻기 · 실행 안 함'],
  ['BLOCK', '보라색 물체 컨베이어로', "'보라색 물체'은(는) 등록된 자재가 아닙니다 — 실행하지 않습니다", 'plan.unknown_resource', '차단 · 실행 안 함'],
  ['NOOP', '초록 원래 자리로', 'C자재은(는) 이미 원래 자리(3번 팔레트)에 있습니다 — 옮길 필요가 없어 실행하지 않습니다', 'plan.clarification_required', '할 일 없음'],
];

for (const [decision, utterance, text, code, label] of cases) {
  test(`[UI-GEN-KIND-${decision}] 의도 단계 ${decision}는 '${label}'로 보이고 승인·실행을 요청하지 않는다`, async ({ page }) => {
    const calls = await general(page, failure(decision, text, code));
    await page.goto('/');
    await page.getByLabel('자연어 명령').fill(utterance);
    await page.getByRole('button', { name: '보내기' }).click();
    const panel = page.locator('section.command');
    await expect(panel).toContainText(text);
    await expect(panel).toContainText(label);
    for (const other of ['되묻기 · 실행 안 함', '차단 · 실행 안 함', '할 일 없음'].filter((l) => l !== label)) await expect(panel).not.toContainText(other);
    await expect(page.getByRole('button', { name: '실행 승인' })).toHaveCount(0);
    // 질문에 답하는 버튼은 되묻기에만 있다(할 일 없음·차단에 '답변 보내기'를 보이지 않는다).
    await expect(panel.getByRole('button', { name: '답변 보내기' })).toHaveCount(decision === 'ASK' ? 1 : 0);
    expect(calls.filter((c) => c !== '/v1/plan')).toEqual([]);
  });
}
