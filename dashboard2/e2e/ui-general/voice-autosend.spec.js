// 음성 인식 확정 후 자동 보내기(2026-10-08) — 일반 경로. 서버 응답·STT는 가짜다(운영 API·로봇·마이크 없음).
// 흐름: 마이크 → 중간 결과·신뢰도 → 서버 최종 확정 → 같은 계획 요청(/v1/plan) 1회 → 결과 표시 → 사람이 실행 승인.
import { expect, test } from '@playwright/test';
import { mockBackend } from '../mock.js';

test.use({ permissions: ['microphone'], launchOptions: { args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] } });

const FINAL = 'A자재를 컨베이어로 옮겨줘';
const planOf = (n, extra = {}) => ({
  ok: true, session_id: 'qa-session', request_id: `req-${n}`, executable: true, stop_latch: { cleared: true },
  plan: { plan_id: `plan-${n}`, plan_hash: `hash-${n}`, robot_id: 'fr3', profile_id: 'p', profile_version: 'v', created_at: 0, ttl_sec: 600,
    utterance: FINAL, steps: [{ index: 1, skill: 'move', args: { to: 'loc_pallet_1' } }, { index: 2, skill: 'pick', args: { object: 'mat_a' } }] },
  safety: { decision: 'allow', rules: [] }, validation: { decision: 'allow', detail: '' }, ...extra,
});
const failure = (decision, text, code) => ({ ok: false, session_id: 'qa-session', request_id: 'req-x', decision, reason_code: code, detail: text, clarification: text, draft_steps: [], intent_tasks: [] });

// recordings[i]: 녹음 i번째에 STT 서버(가짜)가 할 일.
//   partial: 중간 결과 · onFlush: flush 받으면 보낼 이벤트들 · unsolicited: 연결 뒤 ms 후 스스로 보낼 이벤트들(flush 없이)
//   onAbort: abort 받은 뒤 ms 후 늦게 보낼 이벤트들
async function setup(page, { recordings, plan = (n) => planOf(n), planDelayMs = 0, execute } = {}) {
  await mockBackend(page);
  const calls = { plan: [], decision: [], execute: [], ws: [] };
  let n = 0;
  await page.route('**/v1/sessions**', (route) => route.fulfill({ json: { session_id: 'qa-session', client_id: 'c1' } }));
  await page.route('**/v1/plan', async (route) => {
    calls.plan.push(route.request().postDataJSON());
    const k = calls.plan.length;
    if (planDelayMs) await new Promise((r) => setTimeout(r, planDelayMs));
    const body = plan(k);
    if (body === 'network') return route.abort();
    return route.fulfill({ status: body.ok === false ? 422 : 200, json: body });
  });
  await page.route('**/v1/decision', (route) => { const b = route.request().postDataJSON(); calls.decision.push(b); return route.fulfill({ json: { ok: true, approval_id: `appr-${b.plan_id}` } }); });
  await page.route('**/v1/execute', async (route) => { calls.execute.push(route.request().postDataJSON()); if (execute) await execute(); return route.fulfill({ json: { ok: true, execution_id: 'e1', final: { state: 'completed' } } }); });
  await page.routeWebSocket(/\/v1\/stt/, (ws) => {
    const rec = recordings[Math.min(n, recordings.length - 1)]; n += 1;
    const log = { messages: [] }; calls.ws.push(log);
    let partialSent = false;
    const sendAll = (events) => events.forEach((e) => ws.send(JSON.stringify(e)));
    ws.send(JSON.stringify({ kind: 'session', session_id: 'qa-session', stream_id: 'stt_qa', sample_rate_hz: 16000 }));
    if (rec.unsolicited) setTimeout(() => sendAll(rec.unsolicited.events), rec.unsolicited.ms);
    ws.onMessage((m) => {
      if (typeof m !== 'string') {
        if (rec.partial && !partialSent) { partialSent = true; ws.send(JSON.stringify({ kind: 'partial', ...rec.partial })); }
        return;
      }
      const msg = JSON.parse(m); log.messages.push(msg.type);
      if (msg.type === 'flush' && rec.onFlush) sendAll(rec.onFlush);
      if (msg.type === 'abort' && rec.onAbort) setTimeout(() => sendAll(rec.onAbort.events), rec.onAbort.ms);
    });
  });
  await page.goto('/');
  return calls;
}
const final = (extra = {}) => ({ kind: 'final', text: FINAL, raw_text: FINAL, confidence: 0.91, request_id: 'stt-1', persisted: true, ...extra });
const panel = (page) => page.locator('section.command');
const startMic = (page) => panel(page).getByRole('button', { name: /음성 입력/ }).click();
const stopMic = (page) => panel(page).getByRole('button', { name: /음성 인식 중/ }).click();
const settle = (page, ms = 700) => page.waitForTimeout(ms);

test('[UI-AUTO-01] 중간 결과는 요청 0건, 서버가 확정한 최종 결과는 계획 요청 1건 — 승인 전 실행 요청 0건', async ({ page }) => {
  const calls = await setup(page, { recordings: [{ partial: { text: 'A자재를', confidence: 0.7 }, onFlush: [final()] }] });
  await startMic(page);
  await expect(panel(page).locator('.cmd-partial')).toHaveText('A자재를');
  await settle(page);
  expect(calls.plan).toHaveLength(0);
  await stopMic(page);
  await expect(panel(page).getByRole('button', { name: '실행 승인' })).toBeVisible();
  await settle(page);
  expect(calls.plan).toHaveLength(1);
  expect(calls.plan[0]).toMatchObject({ utterance: FINAL, stt: { stt_request_id: 'stt-1', stt_text: FINAL, stt_confidence: 0.91, edited: false } });
  await expect(panel(page).getByTestId('voice-sent')).toHaveText(`음성으로 보낸 문장: “${FINAL}”`);
  await expect(panel(page).getByTestId('stt-confidence')).toHaveText('음성 인식 신뢰도 91%');
  await panel(page).screenshot({ path: 'e2e-results/screens/voice-autosend-confirm.png' });
  expect(calls.decision).toHaveLength(0);
  expect(calls.execute).toHaveLength(0);
});

test('[UI-AUTO-02] 같은 최종 결과가 두 번 와도, 자동 전송 중 보내기·Enter를 연타해도 계획 요청은 1건', async ({ page }) => {
  const calls = await setup(page, { planDelayMs: 1500, recordings: [{ onFlush: [final(), final(), final()] }] });
  await startMic(page);
  await stopMic(page);
  await expect.poll(() => calls.plan.length).toBe(1);
  // 자동 전송 응답을 기다리는 동안 사용자가 문장을 쓰고 보내기·Enter를 연타한다.
  await page.getByLabel('자연어 명령').fill('B자재를 컨베이어로 옮겨줘');
  await panel(page).getByRole('button', { name: /보내/ }).click({ force: true }).catch(() => {});
  await page.getByLabel('자연어 명령').press('Enter');
  await page.getByLabel('자연어 명령').press('Enter');
  await settle(page, 2500);
  expect(calls.plan).toHaveLength(1);
});

test('[UI-AUTO-02B] 직접 입력은 보내기를 동시에 여러 번 눌러도 1건', async ({ page }) => {
  const calls = await setup(page, { planDelayMs: 800, recordings: [{ onFlush: [final()] }] });
  await page.getByLabel('자연어 명령').fill(FINAL);
  await expect(panel(page).getByRole('button', { name: '보내기' })).toBeEnabled();   // 서버 상태를 받아 잠금이 풀린 뒤(부하 때 늦다)
  await page.evaluate(() => { const b = [...document.querySelectorAll('section.command button[type=submit]')][0]; b.click(); b.click(); b.click(); });
  await expect.poll(() => calls.plan.length, { timeout: 10000 }).toBe(1);        // 첫 요청이 도착할 때까지(부하 때 느릴 수 있다)
  await settle(page, 1500);
  expect(calls.plan).toHaveLength(1);                                             // 그 뒤로도 더 오지 않는다
  expect(calls.plan[0].stt).toBeUndefined();                                    // 직접 입력은 음성 기록을 붙이지 않는다
});

for (const [name, rec, reason] of [
  ['인식 실패(오류)', { onFlush: [{ kind: 'error', reason_code: 'stt.no_speech', detail: 'no speech' }] }, '말소리를 감지하지 못했습니다'],
  ['다시 말하기 요청(기준 미만)', { onFlush: [{ kind: 'clarify', confidence: 0.42, detail: '잘 알아듣지 못했습니다. 다시 말해 주세요' }] }, '다시 말해 주세요'],
  ['빈 문장', { onFlush: [final({ text: '  ', raw_text: '' })] }, '인식된 문장이 비어 있어 보내지 않았습니다'],
]) {
  test(`[UI-AUTO-03] ${name}은 요청 0건이고 이유를 보인다`, async ({ page }) => {
    const calls = await setup(page, { recordings: [rec] });
    await startMic(page);
    await stopMic(page);
    await expect(panel(page)).toContainText(reason);
    await settle(page);
    expect(calls.plan).toHaveLength(0);
  });
}

test('[UI-AUTO-04] 녹음 중 화면을 벗어나면 취소하고, 그 뒤 늦게 온 최종 결과는 보내지 않는다', async ({ page }) => {
  const calls = await setup(page, { recordings: [{ partial: { text: 'A자재' }, onAbort: { ms: 300, events: [final({ request_id: 'late' })] } }] });
  await startMic(page);
  await expect(panel(page).locator('.cmd-partial')).toHaveText('A자재');
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.evaluate(() => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' }); });
  await expect(panel(page)).toContainText('화면을 벗어나 음성 입력을 취소했습니다');
  await settle(page, 1200);
  expect(calls.ws[0].messages).toContain('abort');
  expect(calls.plan).toHaveLength(0);
});

test('[UI-AUTO-04B] 마이크를 다시 눌러 정상적으로 마치면 그대로 확정·자동 전송된다', async ({ page }) => {
  const calls = await setup(page, { recordings: [{ partial: { text: 'A자재' }, onFlush: [final()] }] });
  await startMic(page);
  await expect(panel(page).locator('.cmd-partial')).toHaveText('A자재');
  await stopMic(page);
  await expect.poll(() => calls.plan.length).toBe(1);
  expect(calls.ws[0].messages).toContain('flush');
  expect(calls.ws[0].messages).not.toContain('abort');
});

test('[UI-AUTO-05] 계획 요청 중에 확정된 새 음성은 보내지 않고 쌓지도 않는다', async ({ page }) => {
  const calls = await setup(page, { planDelayMs: 2500, recordings: [{ onFlush: [final({ request_id: 'stt-1' })] }, { onFlush: [final({ request_id: 'stt-2', text: 'B자재를 컨베이어로', raw_text: 'B자재를 컨베이어로' })] }] });
  await startMic(page);
  await stopMic(page);
  await expect.poll(() => calls.plan.length).toBe(1);
  await startMic(page);                                                           // 첫 요청 응답 전
  await stopMic(page);
  await expect(panel(page)).toContainText('계획 요청이 진행 중입니다 — 음성 결과를 보내지 않았습니다');
  await settle(page, 3500);
  expect(calls.plan).toHaveLength(1);                                             // 응답 뒤에도 늦게 보내지 않는다(대기열 없음)
});

// 승인 대기·실행 중에는 입력칸(마이크)이 사라진다. 그 직전에 시작한 녹음의 최종 결과가 뒤늦게 오는 경우를 본다:
// 녹음을 걸어 둔 채 자재 버튼으로 계획을 요청 → 승인 대기(·실행 중) → 녹음의 최종 결과 도착.
test('[UI-AUTO-06] 승인 대기 중에 확정된 음성은 자동 전송하지 않는다(쌓지도 않는다)', async ({ page }) => {
  const calls = await setup(page, { recordings: [{ unsolicited: { ms: 1500, events: [final({ request_id: 'stt-late' })] } }] });
  await startMic(page);
  await panel(page).getByRole('button', { name: 'A자재 → 컨베이어' }).click();
  await expect(panel(page).getByRole('button', { name: '실행 승인' })).toBeVisible();
  await expect(panel(page)).toContainText('실행 승인을 기다리는 계획이 있습니다 — 음성 결과를 보내지 않았습니다', { timeout: 5000 });
  await settle(page, 1000);
  expect(calls.plan).toHaveLength(1);                                             // 자재 버튼 요청 1건뿐
  expect(calls.plan[0].stt).toBeUndefined();
  await panel(page).screenshot({ path: 'e2e-results/screens/voice-autosend-blocked-confirm.png' });
});

test('[UI-AUTO-06B] 실행 중에 확정된 음성은 자동 전송하지 않는다', async ({ page }) => {
  let releaseExec;
  const held = new Promise((r) => { releaseExec = r; });
  const calls = await setup(page, { execute: () => held, recordings: [{ unsolicited: { ms: 2500, events: [final({ request_id: 'stt-late' })] } }] });
  await startMic(page);
  await panel(page).getByRole('button', { name: 'A자재 → 컨베이어' }).click();
  await panel(page).getByRole('button', { name: '실행 승인' }).click();
  await expect.poll(() => calls.execute.length).toBe(1);
  await expect(panel(page)).toContainText('작업이 실행 중입니다 — 음성 결과를 보내지 않았습니다', { timeout: 5000 });
  await settle(page, 800);
  expect(calls.plan).toHaveLength(1);
  releaseExec();
});

test('[UI-AUTO-07] 녹음 중 입력칸을 고치면 음성 결과로 덮어쓰거나 보내지 않고 수동 보내기로 둔다', async ({ page }) => {
  const calls = await setup(page, { recordings: [{ partial: { text: 'A자재' }, onFlush: [final()] }] });
  await startMic(page);
  await expect(panel(page).locator('.cmd-partial')).toHaveText('A자재');
  await page.getByLabel('자연어 명령').fill('C자재를 컨베이어로 옮겨줘');
  await stopMic(page);
  await expect(panel(page)).toContainText('입력칸에 직접 쓴 문장이 있어 음성 결과를 넣지도 보내지도 않았습니다');
  await expect(page.getByLabel('자연어 명령')).toHaveValue('C자재를 컨베이어로 옮겨줘');
  await settle(page);
  expect(calls.plan).toHaveLength(0);
  await panel(page).screenshot({ path: 'e2e-results/screens/voice-autosend-edited.png' });
  await panel(page).getByRole('button', { name: '보내기' }).click();
  await expect.poll(() => calls.plan.length).toBe(1);
  expect(calls.plan[0]).toMatchObject({ utterance: 'C자재를 컨베이어로 옮겨줘' });
  expect(calls.plan[0].stt).toBeUndefined();
});

test('[UI-AUTO-08] 계획 요청이 실패하면 자동 재시도 없이, 문장을 확인해 수동으로 다시 보낼 수 있다', async ({ page }) => {
  const calls = await setup(page, { plan: (n) => (n === 1 ? 'network' : planOf(n)), recordings: [{ onFlush: [final()] }] });
  await startMic(page);
  await stopMic(page);
  await expect(panel(page)).toContainText('백엔드에 연결하지 못했습니다');
  await settle(page, 1500);
  expect(calls.plan).toHaveLength(1);
  await panel(page).getByRole('button', { name: '문장 수정해 다시 보내기' }).click();
  await expect(page.getByLabel('자연어 명령')).toHaveValue(FINAL);
  await panel(page).getByRole('button', { name: '보내기' }).click();
  await expect(panel(page).getByRole('button', { name: '실행 승인' })).toBeVisible();
  expect(calls.plan).toHaveLength(2);
  expect(calls.plan[1]).toMatchObject({ utterance: FINAL, stt: { stt_request_id: 'stt-1', edited: false } });
});

test('[UI-AUTO-09] 잘못 인식했으면 승인 대기 계획을 취소하고 고친 문장으로 새 계획 — 이전 승인이 이어지지 않는다', async ({ page }) => {
  const calls = await setup(page, { recordings: [{ onFlush: [final({ text: 'B자재를 컨베이어로 옮겨줘', raw_text: '비자재를 컨베이어로 옮겨줘' })] }] });
  await startMic(page);
  await stopMic(page);
  await expect(panel(page).getByRole('button', { name: '실행 승인' })).toBeVisible();
  await expect(panel(page).getByTestId('voice-sent')).toContainText('B자재를 컨베이어로 옮겨줘');
  await panel(page).getByRole('button', { name: '계획 취소 후 문장 수정' }).click();
  await expect.poll(() => calls.decision.length).toBe(1);
  expect(calls.decision[0]).toMatchObject({ decision: 'reject', plan_id: 'plan-1', plan_hash: 'hash-1' });
  await expect(page.getByLabel('자연어 명령')).toHaveValue('B자재를 컨베이어로 옮겨줘');
  await page.getByLabel('자연어 명령').fill('A자재를 컨베이어로 옮겨줘');
  await panel(page).getByRole('button', { name: '보내기' }).click();
  await expect(panel(page).getByRole('button', { name: '실행 승인' })).toBeVisible();
  expect(calls.plan[1]).toMatchObject({ utterance: 'A자재를 컨베이어로 옮겨줘', stt: { edited: true } });
  await panel(page).getByRole('button', { name: '실행 승인' }).click();
  await expect.poll(() => calls.decision.length).toBe(2);
  expect(calls.decision[1]).toMatchObject({ decision: 'approve', plan_id: 'plan-2', plan_hash: 'hash-2', request_id: 'req-2' });
  await expect.poll(() => calls.execute.length).toBe(1);
  expect(calls.execute[0]).toMatchObject({ plan_id: 'plan-2', approval_id: 'appr-plan-2' });
});

for (const [decision, text, code, label] of [
  ['ASK', 'A자재을(를) 어디로 옮길까요?', 'plan.clarification_required', '되묻기 · 실행 안 함'],
  ['BLOCK', "'보라색 물체'은(는) 등록된 자재가 아닙니다 — 실행하지 않습니다", 'plan.unknown_resource', '차단 · 실행 안 함'],
  ['NOOP', 'C자재은(는) 이미 원래 자리(3번 팔레트)에 있습니다 — 옮길 필요가 없어 실행하지 않습니다', 'plan.clarification_required', '할 일 없음'],
]) {
  test(`[UI-AUTO-10-${decision}] 자동 전송 결과의 서버 판정 ${decision} 표시는 기존 그대로`, async ({ page }) => {
    const calls = await setup(page, { plan: () => failure(decision, text, code), recordings: [{ onFlush: [final()] }] });
    await startMic(page);
    await stopMic(page);
    await expect(panel(page)).toContainText(text);
    await expect(panel(page)).toContainText(label);
    await expect(panel(page).getByTestId('voice-sent')).toContainText(FINAL);
    await expect(page.getByRole('button', { name: '실행 승인' })).toHaveCount(0);
    await settle(page);
    expect(calls.plan).toHaveLength(1);
    expect(calls.decision).toHaveLength(0);
    expect(calls.execute).toHaveLength(0);
    if (decision === 'BLOCK') await panel(page).screenshot({ path: 'e2e-results/screens/voice-autosend-block.png' });
  });
}
