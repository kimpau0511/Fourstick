import { readFileSync } from 'node:fs';
// 화면 테스트용 가짜 서버 응답. 실제 서버(/v1/sim-demo · /v1/sim-view · /v1/scene)에 닿지 않게 막는다.
// 일반 명령은 계획·승인·실행 API 및 단계 이벤트 계약으로 모의한다.



// 실제 서버에서 저장해 둔 응답(e2e/fixtures). 화면이 서버 값을 그대로 보이는지 보려는 것이라 지어낸 값이 아니다.
const FIXTURE_FILES = {
  health: 'health', config: 'v1_config', robots: 'v1_robots', simDemo: 'v1_sim-demo', simState: 'v1_sim-view_state',
};
const readFixture = (name) => JSON.parse(readFileSync(new URL(`./fixtures/${FIXTURE_FILES[name]}.json`, import.meta.url), 'utf8'));

/** 저장된 응답을 얕게 병합해 돌려준다. patch는 객체(덮어쓸 최상위 키) 또는 (원본) => 새 값 함수. */
export function fixture(name, patch) {
  const base = readFixture(name);
  if (typeof patch === 'function') return patch(base);
  return patch ? { ...base, ...patch } : base;
}

export const SAMPLES = {
  ask: { ok: false, detail: '어느 자재를 옮길지 알 수 없습니다 — 가능한 답: A 자재, B 자재' },
  block: { ok: false, blocked: true, detail: '목적지가 사용 금지 자리라 이 계획을 실행할 수 없습니다' },
  plan: (remaining = 60) => ({
    ok: true,
    payload: {
      request_id: 'qa-request',
      plan: { plan_id: 'qa-plan', plan_hash: 'qa-hash', utterance: 'A 자재를 컨베이어로 옮깁니다', created_at: Date.now() / 1000, ttl_sec: remaining,
        steps: [{ index: 1, skill: 'move', args: { to: 'loc_pallet_1' } }, { index: 2, skill: 'pick', args: { object: 'mat_a', from: 'loc_pallet_1' } }, { index: 3, skill: 'place', args: { object: 'mat_a', to: 'loc_conveyor' } }] },
      validation: { decision: 'allow', detail: 'QA: 안전 검증 통과' },
    },
  }),
  step: { type: 'step', payload: { index: 1, skill: 'move', task_succeeded: true } },
  done: { ok: true, execution_id: 'qa-execution', steps: [
    { index: 1, skill: 'move', task_succeeded: true }, { index: 2, skill: 'pick', task_succeeded: true }, { index: 3, skill: 'place', task_succeeded: true },
  ], final: { task_succeeded: true } },
  stopped: { ok: false, execution_id: 'qa-execution', interrupted: 'exec.stopped', steps: [], final: { task_succeeded: false } },
};

/** 모든 서버 경로를 막고, 명령 응답만 주어진 값으로 돌려준다. 호출 기록을 돌려준다. */
export async function mockBackend(page, { plan, decision, execute = SAMPLES.done, progress = [], stop, model, streamState, motion, overrides = {} } = {}) {
  const calls = [];
  let events;
  let finish;
  const completion = new Promise((resolve) => { finish = resolve; });
  // 비열거 메서드라 호출 목록은 순수 배열로 비교할 수 있다. 실행 응답/관측 이벤트를 테스트가 제어한다.
  Object.defineProperties(calls, {
    finishExecution: { value: finish },
    emit: { value: (event) => events.send(JSON.stringify(event)) },
    disconnect: { value: () => events.close() },
  });
  page.on('close', () => finish(null));
  // 정의하지 않은 API도 실제 서버로 보내지 않는다. 뒤에 등록하는 개별 모의 응답이 우선한다.
  await page.route((url) => url.pathname.startsWith('/v1/'), (route) => route.fulfill({ status: 503, json: { detail: 'QA: 정의하지 않은 모의 응답' } }));
  // page.route는 나중에 등록한 것이 먼저 처리된다 — 겹치는 경로는 pathname으로 직접 가른다.
  const pathOf = (url) => new URL(url).pathname;
  await page.route('**/health', (route) => (pathOf(route.request().url()) === '/health'
    ? route.fulfill({ json: fixture('health', overrides.health) }) : route.fallback()));
  await page.route('**/v1/config', (route) => route.fulfill({ json: fixture('config', overrides.config) }));
  await page.route('**/v1/robots', (route) => route.fulfill({ json: fixture('robots', overrides.robots) }));
  await page.route((url) => url.pathname.startsWith('/v1/sim-view/'), (route) => (
    pathOf(route.request().url()) === '/v1/sim-view/state'
      ? route.fulfill({ json: fixture('simState', overrides.simState) })
      : pathOf(route.request().url()) === '/v1/sim-view/model' && model
        ? route.fulfill({ json: model })
      : route.fulfill({ status: 503, json: { available: false, detail: 'QA: 서버 없음' } })));
  await page.route('**/v1/scene**', (route) => route.fulfill({ status: 503, json: { available: false, reason_code: 'config.missing', detail: 'QA: 서버 없음' } }));
  // 영상 스트림(웹소켓)도 실제 서버로 나가지 않게 바로 닫는다.
  await page.routeWebSocket(/\/v1\//, (ws) => {
    if (pathOf(ws.url()) === '/v1/events') { events = ws; return; }
    if (!streamState || pathOf(ws.url()) !== '/v1/sim-view/stream') { ws.close(); return; }
    // 주기적 모의 관측으로 실제 WebGL 렌더러와 리사이즈를 검증한다.
    let seq = 0;
    const send = () => {
      const now = Date.now() / 1000;
      ws.send(JSON.stringify({ ...streamState, type: 'state', server_time: now, joint_time: now, pose_time: now, joint_seq: ++seq, pose_seq: seq }));
    };
    send();
    const timer = setInterval(send, 100);
    ws.onClose(() => clearInterval(timer));
  });
  await page.route('**/v1/sessions**', (route) => route.fulfill({ json: { session_id: 'qa-session', client_id: 'qa-client' } }));
  for (const path of ['/v1/plan', '/v1/decision', '/v1/execute', '/v1/stop']) {
    await page.route((url) => url.pathname === path, async (route) => {
      const request = route.request();
      const body = request.postDataJSON();
      calls.push({ method: request.method(), path, body });
      let response;
      if (path === '/v1/plan') response = typeof plan === 'function' ? await plan(body) : plan ?? SAMPLES.plan();
      if (path === '/v1/decision') response = decision ?? { ok: true, approval_id: 'qa-approval' };
      if (path === '/v1/stop') response = (typeof stop === 'function' ? await stop() : stop) ?? { requested: false, detail: 'QA: 정지할 작업 없음' };
      if (path === '/v1/execute') {
        for (const event of progress) events.send(JSON.stringify(event));
        response = execute === null ? await completion : typeof execute === 'function' ? await execute() : execute;
        if (response === null) return;
      }
      await route.fulfill({ json: response });
    });
  }
  await page.route((url) => url.pathname.startsWith('/v1/sim-demo'), async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    // 상태 조회(GET /v1/sim-demo)는 fixture — 명령 호출 기록(calls)에 넣지 않는다.
    if (path === '/v1/sim-demo' && req.method() === 'GET') return route.fulfill({ json: fixture('simDemo', overrides.simDemo) });
    calls.push({ method: req.method(), path, body: req.postDataJSON?.() ?? null });
    // 화면의 명령 경로에서 시연 제어 API를 호출하면 기록하고 실패 응답을 준다.
    return route.fulfill({ status: 404, json: {} });
  });
  let savedSpeed = 50;
  await page.route('**/v1/sim-demo/motion', async (route) => {
    const req = route.request();
    const body = req.method() === 'POST' ? req.postDataJSON() : null;
    // 속도 API 조회는 제어 명령 기록과 분리한다. 실제 서버에는 닿지 않는다.
    if (req.method() === 'POST') calls.push({ method: 'POST', path: '/v1/sim-demo/motion', body });
    if (motion) return motion(route);
    if (body) savedSpeed = body.speed_percent;
    return route.fulfill({ json: { speed_percent: savedSpeed, min: 0, max: 100, step: 10, applies_to: 'next_job' } });
  });
  return calls;
}

export async function sendCommand(page, text) {
  await page.getByLabel('자연어 명령').fill(text);
  await page.getByRole('button', { name: '보내기' }).click();
}

/** 일반 계획 → 사용자 승인까지 눌러 모의 실행을 시작한다. execute:null이면 실행 응답을 대기시킬 수 있다. */
export async function startJob(page, text = 'A 자재를 컨베이어로 옮겨줘') {
  await sendCommand(page, text);
  await page.locator('section.command').getByRole('button', { name: '실행 승인' }).click();
}
