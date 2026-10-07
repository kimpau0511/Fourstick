// 일반 경로(라이브와 같은 VITE_COMMAND_MODE=general)의 음성 승인: 계획 안내(카탈로그 이름) → "네" → 화면 버튼과 같은
// /v1/decision(본 계획 해시) → /v1/execute. 실행 허가 거부면 시작했다고 말하지 않는다. 서버·STT·TTS는 가짜다.
import { expect, test } from '@playwright/test';
import { mockBackend } from '../mock.js';

test.use({
  permissions: ['microphone'],
  launchOptions: { args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] },
});

const steps = [
  { index: 1, skill: 'move', args: { target: 'loc_pallet_1' } },
  { index: 2, skill: 'pick', args: { location: 'loc_pallet_1', object: 'mat_a' } },
  { index: 3, skill: 'move', args: { target: 'loc_conveyor' } },
  { index: 4, skill: 'place', args: { object: 'mat_a', location: 'loc_conveyor' } },
  { index: 5, skill: 'home', args: {} },
];
const plan = { ok: true, session_id: 'qa-session', request_id: 'req-qa', executable: true, stop_latch: { cleared: true },
  plan: { plan_id: 'plan-qa', plan_hash: 'hash-qa', robot_id: 'fr3', profile_id: 'p', profile_version: 'v', created_at: 0, ttl_sec: 600,
    utterance: 'A자재를 컨베이어로 옮겨줘', steps },
  safety: { decision: 'allow', rules: [] }, validation: { decision: 'allow', detail: '' } };

async function setup(page, execute) {
  await mockBackend(page);
  const calls = [];
  const json = (route, body, status = 200) => route.fulfill({ status, json: body });
  await page.route('**/v1/sessions**', (route) => json(route, { session_id: 'qa-session', client_id: 'c1' }));
  await page.route('**/v1/plan', (route) => { calls.push({ path: '/v1/plan', body: route.request().postDataJSON() }); return json(route, plan); });
  await page.route('**/v1/decision', (route) => { calls.push({ path: '/v1/decision', body: route.request().postDataJSON() }); return json(route, { ok: true, approval_id: 'appr-1' }); });
  await page.route('**/v1/execute', async (route) => { calls.push({ path: '/v1/execute', body: route.request().postDataJSON() }); await new Promise((r) => { setTimeout(r, 300); }); return json(route, execute); });
  await page.addInitScript(() => {
    window.__spoken = [];
    window.SpeechSynthesisUtterance = function Utter(text) { this.text = text; };
    Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: {
      cancel() {}, speak(u) { window.__spoken.push(u.text); setTimeout(() => u.onend && u.onend(), 50); } } });
  });
  const wake = [];
  await page.routeWebSocket(/\/v1\/stt/, (ws) => {
    const isWake = new URL(ws.url()).searchParams.get('mode') === 'wake';
    const entry = { ws, epoch: 0 };
    if (isWake) wake.push(entry);
    ws.onMessage((m) => { if (typeof m === 'string' && JSON.parse(m).type === 'abort') entry.epoch += 1; });
    ws.send(JSON.stringify({ kind: 'session', session_id: 'qa-session', stream_id: 's', sample_rate_hz: 16000 }));
  });
  const say = (text) => { const e = wake[wake.length - 1]; e.ws.send(JSON.stringify({ kind: 'final', text, confidence: 0.9, epoch: e.epoch })); };
  await page.goto('/');
  const voice = page.getByRole('group', { name: '음성 명령' });
  await voice.getByRole('button', { name: '음성 명령 켜기' }).click();
  await expect.poll(() => wake.length).toBe(1);
  say('지니야 A자재를 컨베이어로 옮겨줘');
  await expect(voice.locator('.voice-status')).toContainText('“네·진행해” 또는 “아니·취소해”라고 말씀하세요', { timeout: 8000 });
  return { calls, say, voice, spoken: () => page.evaluate(() => window.__spoken) };
}

test('[UI-GVAPP-01] 일반 경로: 계획 안내는 카탈로그 이름(자재·출발지·목적지), "네"는 화면 버튼과 같은 승인·실행 API', async ({ page }) => {
  const { calls, say, spoken } = await setup(page, { ok: true, execution_id: 'exec-1', final: { state: 'completed' } });
  expect(await spoken()).toContain('다음과 같이 작업을 진행하려고 합니다. A자재를 1번 팔레트에서 컨베이어로 옮깁니다. 이대로 작업을 진행할까요?');
  expect(calls.filter((c) => c.path !== '/v1/plan')).toHaveLength(0);
  say('네 진행해');
  await expect.poll(() => calls.map((c) => c.path)).toEqual(['/v1/plan', '/v1/decision', '/v1/execute']);
  expect(calls[1].body).toMatchObject({ decision: 'approve', plan_id: 'plan-qa', plan_hash: 'hash-qa', request_id: 'req-qa' });
  expect(calls[2].body.approval_id).toBe('appr-1');
  await expect.poll(spoken).toContain('네, 작업을 시작하겠습니다.');
});

test('[UI-GVAPP-02] 일반 경로: 실행 허가가 거부되면 "시작하지 못했습니다"와 사유 — 시작했다고 말하지 않는다', async ({ page }) => {
  const { say, spoken } = await setup(page, { ok: false, reasons: [{ detail: '정지 래치가 풀리지 않았다' }] });
  say('응');
  await expect.poll(async () => (await spoken()).find((t) => t.startsWith('작업을 시작하지 못했습니다.')) || '').toContain('정지 래치가 풀리지 않았다');
  expect(await spoken()).not.toContain('네, 작업을 시작하겠습니다.');
});
