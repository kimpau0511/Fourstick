// 실시간 STT + 음성 인식 신뢰도(2026-10-08): 말하는 중 중간 결과와 그 점수(없으면 '인식 중…'), 끝나면 최종 점수.
// 점수는 서버가 준 값만 쓴다(없는 점수를 만들지 않음). 계획 요청은 '보내기'로만, 실행은 승인 뒤에만(기존 그대로).
import { expect, test } from '@playwright/test';
import { mockBackend } from '../mock.js';

test.use({ permissions: ['microphone'], launchOptions: { args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] } });
const TEXT = 'A 자재를 컨베이어로 옮겨줘';
const panel = (page) => page.locator('section.command');
const score = (page) => panel(page).getByTestId('stt-confidence');

// recordings: 녹음마다 { partial?: {text, confidence?}, end: {kind, confidence, ...} }
async function setup(page, recordings) {
  const calls = await mockBackend(page);
  await page.route('**/v1/sessions**', (route) => route.fulfill({ json: { session_id: 'qa-session', client_id: 'qa-client' } }));
  let n = 0;
  await page.routeWebSocket(/\/v1\/stt/, (ws) => {
    const rec = recordings[Math.min(n, recordings.length - 1)]; n += 1;
    const id = n;
    let partialSent = false;
    ws.send(JSON.stringify({ kind: 'session', session_id: 'qa-session', stream_id: 'stt_qa', sample_rate_hz: 16000 }));
    ws.onMessage((m) => {
      if (typeof m !== 'string') {
        if (rec.partial && !partialSent) { partialSent = true; ws.send(JSON.stringify({ kind: 'partial', ...rec.partial })); }
        return;
      }
      if (JSON.parse(m).type === 'flush') ws.send(JSON.stringify({ text: TEXT, raw_text: TEXT, request_id: `stt-req-${id}`, persisted: true, ...rec.end }));
    });
  });
  return { calls };
}
const startMic = (page) => panel(page).getByRole('button', { name: /음성 입력/ }).click();
const stopMic = (page) => panel(page).getByRole('button', { name: /음성 인식 중/ }).click();

test('[UI-CONF-01] 말하는 중 중간 결과·중간 점수 → 끝나면 최종 점수, 안내 문구 없음, 자동 전송 없음', async ({ page }) => {
  const { calls } = await setup(page, [{ partial: { text: 'A 자재를', confidence: 0.721 }, end: { kind: 'final', confidence: 0.934 } }]);
  await page.goto('/');
  await expect(score(page)).toHaveCount(0);
  await startMic(page);
  await expect(panel(page).locator('.cmd-partial')).toHaveText('A 자재를');
  await expect(score(page)).toHaveText('음성 인식 신뢰도 72%');
  await expect(score(page)).toHaveAttribute('data-kind', 'partial');
  await panel(page).screenshot({ path: 'e2e-results/screens/stt-live-partial.png' });
  await stopMic(page);
  await expect(score(page)).toHaveText('음성 인식 신뢰도 93%');
  await expect(score(page)).toHaveAttribute('data-kind', 'final');
  await expect(page.getByLabel('자연어 명령')).toHaveValue(TEXT);
  await expect(panel(page)).not.toContainText('실제 인식 정확도');
  await page.waitForTimeout(400);
  expect(calls.filter((c) => c.path.endsWith('/command'))).toHaveLength(0);
  await panel(page).screenshot({ path: 'e2e-results/screens/stt-final.png' });
});

test('[UI-CONF-06] 중간 결과에 점수가 없으면 "인식 중…"', async ({ page }) => {
  await setup(page, [{ partial: { text: 'A 자재를' }, end: { kind: 'final', confidence: 0.88 } }]);
  await page.goto('/');
  await startMic(page);
  await expect(panel(page).locator('.cmd-partial')).toHaveText('A 자재를');
  await expect(score(page)).toHaveText('인식 중…');
  await stopMic(page);
  await expect(score(page)).toHaveText('음성 인식 신뢰도 88%');
});

for (const [name, value] of [['없음', null], ['범위 밖(1.5)', 1.5], ['음수', -0.1], ['문자', 'x']]) {
  test(`[UI-CONF-02] 최종 점수 ${name} → 확인 불가`, async ({ page }) => {
    await setup(page, [{ end: { kind: 'final', confidence: value } }]);
    await page.goto('/');
    await startMic(page);
    await stopMic(page);
    await expect(score(page)).toHaveText('음성 인식 신뢰도 확인 불가');
  });
}

test('[UI-CONF-03] 새 녹음 시작 → 이전 음성 결과 문장·점수 지움', async ({ page }) => {
  await setup(page, [{ end: { kind: 'final', confidence: 0.81 } }, { end: { kind: 'final', confidence: 0.66, text: 'B 자재를 컨베이어로', raw_text: 'B 자재를 컨베이어로' } }]);
  await page.goto('/');
  await startMic(page);
  await stopMic(page);
  await expect(score(page)).toHaveText('음성 인식 신뢰도 81%');
  await expect(page.getByLabel('자연어 명령')).toHaveValue(TEXT);
  await startMic(page);
  await expect(page.getByLabel('자연어 명령')).toHaveValue('');
  await expect(score(page)).toHaveText('인식 중…');
  await stopMic(page);
  await expect(page.getByLabel('자연어 명령')).toHaveValue('B 자재를 컨베이어로');
  await expect(score(page)).toHaveText('음성 인식 신뢰도 66%');
});

test('[UI-CONF-07] 직접 친 문장은 녹음을 시작해도 지우지 않는다', async ({ page }) => {
  await setup(page, [{ end: { kind: 'final', confidence: 0.8 } }]);
  await page.goto('/');
  await page.getByLabel('자연어 명령').fill('직접 친 명령');
  await startMic(page);
  await expect(page.getByLabel('자연어 명령')).toHaveValue('직접 친 명령');
});

test('[UI-CONF-04] 되묻기(기준 미만)에도 그 점수를 보이고 입력칸에는 넣지 않는다', async ({ page }) => {
  await setup(page, [{ end: { kind: 'clarify', confidence: 0.45, detail: '신뢰도가 기준 미만이다 — 다시 말해 달라고 요청한다' } }]);
  await page.goto('/');
  await startMic(page);
  await stopMic(page);
  await expect(score(page)).toHaveText('음성 인식 신뢰도 45%');
  await expect(page.getByLabel('자연어 명령')).toHaveValue('');
});

test('[UI-CONF-05] 보내기는 기존 그대로(수정 가능), 보낸 뒤 점수 사라짐 · 확정 버튼 없음', async ({ page }) => {
  const { calls } = await setup(page, [{ end: { kind: 'final', confidence: 0.99 } }]);
  await page.goto('/');
  await startMic(page);
  await stopMic(page);
  await expect(score(page)).toHaveText('음성 인식 신뢰도 99%');
  await expect(panel(page).getByRole('button', { name: '실제 발화 내용으로 확정' })).toHaveCount(0);
  await page.getByLabel('자연어 명령').fill('B 자재를 컨베이어로 옮겨줘');
  await panel(page).getByRole('button', { name: '보내기' }).click();
  await expect.poll(() => calls.filter((c) => c.path.endsWith('/command')).length).toBe(1);
  expect(calls.find((c) => c.path.endsWith('/command')).body).toMatchObject({ source: 'text', utterance: 'B 자재를 컨베이어로 옮겨줘' });
  await expect(score(page)).toHaveCount(0);
});
