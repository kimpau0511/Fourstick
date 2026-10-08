// 일반 경로: 음성 결과를 입력칸에서 확인·수정한 뒤 보내면 /v1/plan에 고친 문장 + STT 원래 결과(stt)가 함께 간다.
// 서버는 이것으로 사용자 수정 발생(user_edited)과 수정 없이 보낸 결과를 나눠 기록한다(판정에는 쓰지 않는다).
import { expect, test } from '@playwright/test';
import { mockBackend } from '../mock.js';

test.use({ permissions: ['microphone'], launchOptions: { args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] } });
const FINAL = '에이 자재를 컨베이어로 옮겨줘';

async function setup(page) {
  await mockBackend(page);
  const calls = [];
  await page.route('**/v1/sessions**', (route) => route.fulfill({ json: { session_id: 'qa-session', client_id: 'c1' } }));
  await page.route('**/v1/plan', (route) => { calls.push(route.request().postDataJSON()); return route.fulfill({ status: 422, json: { ok: false, reason_code: 'plan.clarification_required', clarification: 'QA' } }); });
  await page.routeWebSocket(/\/v1\/stt/, (ws) => {
    ws.send(JSON.stringify({ kind: 'session', session_id: 'qa-session', stream_id: 'stt_qa', sample_rate_hz: 16000 }));
    ws.onMessage((m) => { if (typeof m === 'string' && JSON.parse(m).type === 'flush') ws.send(JSON.stringify({ kind: 'final', text: FINAL, raw_text: FINAL, confidence: 0.81, request_id: 'stt-req-9' })); });
  });
  return calls;
}

for (const [edited, typed] of [[false, null], [true, 'A 자재를 컨베이어로 옮겨줘']]) {
  test(`[UI-GEN-VOICE-${edited ? 2 : 1}] 음성 결과 ${edited ? '수정 후' : '그대로'} 보내기 → /v1/plan stt.edited=${edited}`, async ({ page }) => {
    const calls = await setup(page);
    await page.goto('/');
    const panel = page.locator('section.command');
    await panel.getByRole('button', { name: /음성 입력/ }).click();
    await panel.getByRole('button', { name: /음성 인식 중/ }).click();
    await expect(page.getByLabel('자연어 명령')).toHaveValue(FINAL);
    expect(calls).toHaveLength(0);                                   // 자동으로 계획을 요청하지 않는다
    if (typed) await page.getByLabel('자연어 명령').fill(typed);
    await panel.getByRole('button', { name: '보내기' }).click();
    await expect.poll(() => calls.length).toBe(1);
    expect(calls[0]).toMatchObject({ utterance: typed || FINAL,
      stt: { stt_request_id: 'stt-req-9', stt_text: FINAL, stt_confidence: 0.81, edited } });
  });
}
