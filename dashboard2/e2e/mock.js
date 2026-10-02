// 화면 테스트용 가짜 서버 응답. 실제 서버(/v1/sim-demo · /v1/sim-view · /v1/scene)에 닿지 않게 막는다.
// 응답 모양은 dashboard2/src/simCommand.js가 읽는 필드에 맞춘다(서버 값을 화면이 그대로 보이는지 보려는 것).

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
export async function mockBackend(page, { command, confirm, jobs = [] } = {}) {
  const calls = [];
  await page.route('**/v1/sim-view/**', (route) => route.fulfill({ status: 503, json: { available: false, detail: 'QA: 서버 없음' } }));
  await page.route('**/v1/scene**', (route) => route.fulfill({ status: 503, json: { available: false, reason_code: 'config.missing', detail: 'QA: 서버 없음' } }));
  // 영상 스트림(웹소켓)도 실제 서버로 나가지 않게 바로 닫는다.
  await page.routeWebSocket(/\/v1\/(scene|sim-view)\/stream/, (ws) => ws.close());
  let jobIndex = 0;
  await page.route('**/v1/sim-demo/**', async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
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
