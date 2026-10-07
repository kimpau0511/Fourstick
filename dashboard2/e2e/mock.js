import { readFileSync } from 'node:fs';
// 화면 테스트용 가짜 서버 응답. 실제 서버(/v1/sim-demo · /v1/sim-view · /v1/scene)에 닿지 않게 막는다.
// 응답 모양은 dashboard2/src/simCommand.js가 읽는 필드에 맞춘다(서버 값을 화면이 그대로 보이는지 보려는 것).



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
  ask: { decision: 'ASK', reason: '어느 자재를 옮길지 알 수 없습니다 — 가능한 답: A 자재, B 자재' },
  block: { decision: 'BLOCK', reason: '목적지가 사용 금지 자리라 이 계획을 실행할 수 없습니다' },
  passThrough: { decision: 'PASS_THROUGH', reason: '' },
  confirm: (remaining = 60) => ({
    decision: 'CONFIRM',
    confirmation: { token: 'qa-token', kind: 'job', summary: 'A 자재를 컨베이어로 옮깁니다', evidence: { slot_label: '컨베이어 1번 칸' }, remaining_sec: remaining },
  }),
  run: { decision: 'RUN', reason: null, job: { job_id: 'qa-job', status: 'running', action: 'transfer', progress: [
    { no: 1, of: 3, label: '집기 접근', reached: true },
    { no: 2, of: 3, label: '집기', reached: false },
    { no: 3, of: 3, label: '놓기', reached: false },
  ] } },
  done: { job_id: 'qa-job', status: 'completed', action: 'transfer', report: { status: 'simulation_transfer_completed' }, progress: [
    { no: 1, of: 3, label: '집기 접근', reached: true },
    { no: 2, of: 3, label: '집기', reached: true },
    { no: 3, of: 3, label: '놓기', reached: true },
  ] },
};

/** 모든 서버 경로를 막고, 명령 응답만 주어진 값으로 돌려준다. 호출 기록을 돌려준다. */
export const QA_USER = { email: 'operator@example.com', name: '김민우', picture: null };

export async function mockBackend(page, { command, confirm, jobs = [], overrides = {} } = {}) {
  const calls = [];
  // page.route는 나중에 등록한 것이 먼저 처리된다 — 겹치는 경로는 pathname으로 직접 가른다.
  const pathOf = (url) => new URL(url).pathname;
  // 로그인 — 기본은 로그인된 상태(대시보드 검사용). overrides.auth: false면 로그인 안 됨(401), 객체면 그 사용자.
  const user = overrides.auth === undefined ? QA_USER : overrides.auth;
  await page.route('**/v1/auth/me', (route) => (user ? route.fulfill({ json: { user } }) : route.fulfill({ status: 401, json: { reason_code: 'session.unauthenticated' } })));
  await page.route('**/v1/auth/logout', (route) => route.fulfill({ json: { ok: true } }));
  await page.route('**/health', (route) => (pathOf(route.request().url()) === '/health'
    ? route.fulfill({ json: fixture('health', overrides.health) }) : route.fallback()));
  await page.route('**/v1/config', (route) => route.fulfill({ json: fixture('config', overrides.config) }));
  await page.route('**/v1/robots', (route) => route.fulfill({ json: fixture('robots', overrides.robots) }));
  // 서버 이력(/v1/history) — 기본은 빈 목록. overrides.history로 바꾼다.
  await page.route('**/v1/history**', (route) => route.fulfill({ json: { is_simulated: true, commands: [], sim_jobs: [], ...(overrides.history || {}) } }));
  await page.route((url) => url.pathname.startsWith('/v1/sim-view/'), (route) => (
    pathOf(route.request().url()) === '/v1/sim-view/state'
      ? route.fulfill({ json: fixture('simState', overrides.simState) }) // 3D 모델(/model)은 계속 503 — Gazebo 대체 검사용
      : route.fulfill({ status: 503, json: { available: false, detail: 'QA: 서버 없음' } })));
  await page.route('**/v1/scene**', (route) => route.fulfill({ status: 503, json: { available: false, reason_code: 'config.missing', detail: 'QA: 서버 없음' } }));
  // 영상 스트림(웹소켓)도 실제 서버로 나가지 않게 바로 닫는다.
  await page.routeWebSocket(/\/v1\/(scene|sim-view)\/stream/, (ws) => ws.close());
  // 로봇 이름·호출어(/v1/settings/robot-name) — 저장하면 이 페이지(새로고침 포함) 동안 유지된다. overrides.robotName으로 시작 값.
  const nameStore = { name: overrides.robotName || '지니' };
  await page.route('**/v1/settings/robot-name**', (route) => {
    const req = route.request();
    if (req.method() === 'POST') {
      const name = String((req.postDataJSON() || {}).name || '').trim();
      calls.push({ method: 'POST', path: '/v1/settings/robot-name', body: { name } });
      if (!/^[가-힣]{2,4}$/.test(name)) return route.fulfill({ status: 400, json: { detail: '로봇 이름은 한글 2~4글자로 입력하세요(공백·영문·숫자 제외)' } });
      nameStore.name = name;
    }
    return route.fulfill({ json: { robot_id: 'fr3', name: nameStore.name, wake_word: `${nameStore.name}야` } });
  });
  let jobIndex = 0;
  await page.route((url) => url.pathname.startsWith('/v1/sim-demo'), async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    // 상태 조회(GET /v1/sim-demo)는 fixture — 명령 호출 기록(calls)에 넣지 않는다.
    if (path === '/v1/sim-demo' && req.method() === 'GET') return route.fulfill({ json: fixture('simDemo', overrides.simDemo) });
    calls.push({ method: req.method(), path, body: req.postDataJSON?.() ?? null });
    if (path.endsWith('/command')) return route.fulfill({ json: command ?? SAMPLES.passThrough });
    if (path.endsWith('/confirm')) return route.fulfill({ json: confirm ?? SAMPLES.run });
    if (path.includes('/jobs/')) return route.fulfill({ json: jobs[Math.min(jobIndex++, jobs.length - 1)] ?? SAMPLES.done });
    if (path.endsWith('/stop')) return route.fulfill({ json: { requested: false, detail: 'QA: 정지할 작업 없음' } });
    return route.fulfill({ status: 404, json: {} });
  });
  return calls;
}

export async function sendCommand(page, text) {
  await page.getByLabel('자연어 명령').fill(text);
  await page.getByRole('button', { name: '보내기' }).click();
}

/** 명령 → 실행 승인까지 눌러 작업이 시작되게 한다(시뮬레이션 창이 열리는 흐름). mockBackend에 command: SAMPLES.confirm()이 있어야 한다. */
export async function startJob(page, text = 'A 자재를 컨베이어로 옮겨줘') {
  await sendCommand(page, text);
  await page.locator('section.command').getByRole('button', { name: '실행 승인' }).click();
}
