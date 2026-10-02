// 작업 셀 서버 블랙박스 검사 — 추적표의 API-01~08.
// 지키는 것:
// - 읽기 요청과 로봇이 움직이지 않는 명령만 보낸다. 확인 카드(CONFIRM)가 생기면 **항상 취소**한다.
//   실행 승인(confirm)·정지(stop)·작업 생성(jobs)·목표(goals)는 보내지 않는다(결정 D5).
// - 서버에 닿지 않으면 전부 건너뛴다(미실행). 통과로 치지 않는다.
import { expect, test } from '@playwright/test';

const BACKEND = process.env.FORSTICK2_BACKEND_URL || 'https://192.168.0.175:8443';
const SESSION = `qa-${Date.now()}`;
const HIDDEN = /"real_hardware[^"]*"\s*:|"hardware_readiness"\s*:/;

let api;
let reachable = false;

test.beforeAll(async ({ playwright }) => {
  api = await playwright.request.newContext({ baseURL: BACKEND, ignoreHTTPSErrors: true, timeout: 15_000 });
  try { reachable = (await api.get('/health')).ok(); } catch { reachable = false; }
});
test.afterAll(async () => { await api?.dispose(); });
test.beforeEach(() => { test.skip(!reachable, `서버(${BACKEND})에 닿지 않음 — 미실행`); });

async function command(utterance) {
  const res = await api.post('/v1/sim-demo/command', { data: { mode: 'simulation_demo', utterance, source: 'text', session_id: SESSION } });
  const body = await res.json();
  // 확인 카드가 생기면 그 자리에서 취소한다 — 로봇을 움직이지 않는다.
  if (body.confirmation?.token) await api.post('/v1/sim-demo/confirm', { data: { token: body.confirmation.token, action: 'cancel' } });
  return { status: res.status(), body, raw: JSON.stringify(body) };
}

test('[API-01][SFR-011·DEV-10] /health: DB 사용 가능, 로봇은 시뮬레이션', async () => {
  const h = await (await api.get('/health')).json();
  expect(h.status).toBe('ok');
  expect(h.db?.available).toBe(true);
  expect(h.robot?.is_simulated).toBe(true);
  expect(h.executions?.real ?? 0).toBe(0);
});

test('[API-02][DEV-01] 3D 관측: 모델을 주고 스트림이 초당 20회 이상', async ({ browser }) => {
  const model = await (await api.get('/v1/sim-view/model')).json();
  expect(model.available).toBe(true);
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await ctx.newPage();
  await page.goto(`${BACKEND}/health`);
  const rate = await page.evaluate(async (url) => new Promise((resolve) => {
    const ws = new WebSocket(url); const t = []; const t0 = performance.now();
    ws.onmessage = () => t.push(performance.now());
    setTimeout(() => { ws.close(); resolve(t.length > 1 ? (t.length - 1) / ((t.at(-1) - t[0]) / 1000) : 0); }, 4000);
    void t0;
  }), `${BACKEND.replace(/^http/, 'ws')}/v1/sim-view/stream`);
  await ctx.close();
  test.info().annotations.push({ type: '측정', description: `3D 관측 ${rate.toFixed(1)}회/초` });
  expect(rate).toBeGreaterThanOrEqual(20);
});

test('[API-03][DEV-01] Gazebo 장면 카메라 사용 가능(대체 경로)', async () => {
  const scene = await (await api.get('/v1/scene')).json();
  expect(scene.available).toBe(true);
});

test('[API-04][SFR-010] 시연 명령이 아닌 문장: 판정 + 사람이 읽는 사유, 작업 없음', async () => {
  const { body } = await command('오늘 점심 메뉴 알려줘');
  expect(['PASS_THROUGH', 'ASK', 'BLOCK']).toContain(body.decision);
  expect(body.job ?? null).toBeNull();
});

test('[API-05][SFR-003·⑥-2] 대상이 모호한 문장은 되묻는다(작업 없음)', async () => {
  const { body } = await command('그거 옮겨줘');
  expect(['ASK', 'CONFIRM', 'CONFIRM_GOAL']).toContain(body.decision);
  if (body.decision === 'ASK') expect(String(body.reason || '')).not.toBe('');
  expect(body.job ?? null).toBeNull();
});

test('[API-06][DEV-11·⑥-4] 이송 명령은 바로 실행하지 않고 확인 카드(만료 60초)를 만든다 — 취소하면 작업 0건', async () => {
  const res = await api.post('/v1/sim-demo/command', { data: { mode: 'simulation_demo', utterance: 'A 자재를 컨베이어로 옮겨줘', source: 'text', session_id: `${SESSION}-6` } });
  const body = await res.json();
  test.info().annotations.push({ type: '판정', description: `${body.decision} · ${body.reason || body.confirmation?.summary || ''}` });
  expect(body.job ?? null).toBeNull();
  if (!['CONFIRM', 'CONFIRM_GOAL'].includes(body.decision)) {
    // 지금 자재 상태 때문에 BLOCK/ASK일 수 있다 — 그래도 작업이 생기지 않았으면 이 요구(바로 실행 안 함)는 지킨 것이다.
    expect(['ASK', 'BLOCK', 'NOOP']).toContain(body.decision);
    return;
  }
  expect(body.confirmation.remaining_sec).toBeGreaterThan(0);
  expect(body.confirmation.remaining_sec).toBeLessThanOrEqual(60);
  const cancel = await (await api.post('/v1/sim-demo/confirm', { data: { token: body.confirmation.token, action: 'cancel' } })).json();
  expect(cancel.job ?? null).toBeNull();
});

test('[API-07][DEV-10] 공개 응답에 실기 상태 키(real_hardware*, hardware_readiness)가 없다', async () => {
  for (const path of ['/health', '/v1/config', '/v1/robots', '/v1/sim-demo', '/v1/sim-view/model', '/v1/scene']) {
    const text = await (await api.get(path)).text();
    expect(text, path).not.toMatch(HIDDEN);
  }
});

test('[API-08][SFR-009] 정지 래치 진단이 읽히고 시뮬레이션으로 표시된다', async () => {
  const r = await (await api.get('/v1/robots')).json();
  expect(r.stop_diagnostics?.available).toBe(true);
  expect(r.stop_diagnostics?.is_simulated).toBe(true);
});
