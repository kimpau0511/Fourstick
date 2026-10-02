// 로봇을 실제로 움직이는 검사 — TER-003(E2E), NFR-009(실행 성공률), NFR-002(정지 반응 시간).
//
// 지키는 것(결정 D5, 2026-10-02):
// - **기본은 꺼져 있다.** 팀원(작업 셀 PC 담당)의 허락을 받고, 그 PC를 쓰지 않는 시간에
//   `QA_ROBOT=1 npm run qa:api` 로만 돈다. 꺼져 있으면 "미실행"으로 남는다(통과 아님).
// - 실행 승인은 이 파일만 보낸다. 끝나면 자재를 원래 자리로 되돌린다(성공·실패와 무관하게).
// - 한 번에 하나씩만 돈다(같은 셀을 같이 쓰면 결과가 섞인다).
// - 횟수는 QA_ROBOT_RUNS(기본 1). 기획서 기준(성공률 90%, 정지 30회)을 채우려면 늘려서 돌린다.
import { expect, test } from '@playwright/test';

const BACKEND = process.env.FORSTICK2_BACKEND_URL || 'https://192.168.0.175:8443';
const ENABLED = process.env.QA_ROBOT === '1';
const RUNS = Number(process.env.QA_ROBOT_RUNS || 1);
const MATERIAL = 'material_a';
const POLL_MS = 1500;
const JOB_TIMEOUT_MS = 6 * 60_000; // 이송 한 번(접근·집기·이동·놓기·복귀)에 넉넉히

test.describe.configure({ mode: 'serial' });
test.setTimeout(RUNS * 2 * JOB_TIMEOUT_MS + 60_000);

let api;
test.beforeAll(async ({ playwright }) => {
  api = await playwright.request.newContext({ baseURL: BACKEND, ignoreHTTPSErrors: true, timeout: 30_000 });
});
test.afterAll(async () => { await api?.dispose(); });
test.beforeEach(async () => {
  test.skip(!ENABLED, 'QA_ROBOT=1이 아님 — 로봇을 움직이는 검사는 팀원 허락 뒤에만 돈다(미실행)');
  test.skip(!(await api.get('/health').then((r) => r.ok()).catch(() => false)), `서버(${BACKEND})에 닿지 않음 — 미실행`);
});

const sleep = (ms) => new Promise((r) => { setTimeout(r, ms); });

/** 명령 → 확인 카드 → 승인. 작업 id를 돌려준다. 확인 카드가 아니면 그 판정을 돌려준다. */
async function commandAndConfirm(utterance, session) {
  const res = await (await api.post('/v1/sim-demo/command', { data: { mode: 'simulation_demo', utterance, source: 'text', session_id: session } })).json();
  if (res.job?.job_id) return { jobId: res.job.job_id, decision: res.decision };
  if (!res.confirmation?.token) return { decision: res.decision, reason: res.reason };
  const ok = await (await api.post('/v1/sim-demo/confirm', { data: { token: res.confirmation.token, action: 'confirm' } })).json();
  return { jobId: ok.job?.job_id, decision: ok.decision, reason: ok.reason };
}

async function waitJob(jobId) {
  const t0 = Date.now();
  for (;;) {
    const job = await (await api.get(`/v1/sim-demo/jobs/${encodeURIComponent(jobId)}`)).json();
    if (job.status !== 'running') return job;
    if (Date.now() - t0 > JOB_TIMEOUT_MS) return { ...job, status: 'timeout' };
    await sleep(POLL_MS);
  }
}

/** 성공·실패와 무관하게 자재를 원래 자리로 맞춘다 — 고정 장치 복구(restore)는 로봇을 움직이지 않는다. */
async function restoreMaterial() {
  const job = await (await api.post('/v1/sim-demo/jobs', { data: { action: 'restore', material: MATERIAL } })).json();
  if (job.job_id) await waitJob(job.job_id);
}

test('[API-ROBOT-01][TER-003·NFR-009] 이송 → 원래 자리 복귀 E2E, 실행 성공률', async () => {
  let ok = 0;
  const log = [];
  try {
    for (let i = 0; i < RUNS; i += 1) {
      const session = `qa-robot-${Date.now()}-${i}`;
      const go = await commandAndConfirm('A 자재를 컨베이어로 옮겨줘', session);
      const goJob = go.jobId ? await waitJob(go.jobId) : { status: go.decision, report: { status: go.reason } };
      const back = await commandAndConfirm('A 자재를 원래 자리로 돌려놔', session);
      const backJob = back.jobId ? await waitJob(back.jobId) : { status: back.decision, report: { status: back.reason } };
      const pass = goJob.report?.status === 'simulation_transfer_completed' && backJob.report?.status === 'returned_to_origin';
      if (pass) ok += 1;
      log.push(`${i + 1}: 이송 ${goJob.report?.status || goJob.status} / 복귀 ${backJob.report?.status || backJob.status}`);
    }
  } finally {
    await restoreMaterial();
  }
  const rate = ok / RUNS;
  test.info().annotations.push({ type: '측정', description: `성공 ${ok}/${RUNS} (${(rate * 100).toFixed(0)}%) · ${log.join(' · ')}` });
  expect(rate).toBeGreaterThanOrEqual(0.9);
});

test('[API-ROBOT-02][NFR-002·TER-004] 실행 중 즉시 정지: 요청→응답 시간, 정지 확인', async () => {
  const times = [];
  try {
    for (let i = 0; i < RUNS; i += 1) {
      const go = await commandAndConfirm('A 자재를 컨베이어로 옮겨줘', `qa-stop-${Date.now()}-${i}`);
      expect(go.jobId, `작업이 시작되지 않음: ${go.decision} ${go.reason || ''}`).toBeTruthy();
      await sleep(4000); // 팔이 움직이기 시작한 뒤
      const t0 = performance.now();
      const stop = await (await api.post('/v1/sim-demo/stop', { data: {} })).json();
      times.push(performance.now() - t0);
      expect(stop.requested).toBe(true);
      const job = await waitJob(go.jobId);
      // 작업은 끝나면 status=finished, 결과는 report.status(RESULT_LABELS) — 정지면 *_stopped
      expect(job.status).toBe('finished');
      expect(String(job.report?.status || '')).toMatch(/stopped/);
      await restoreMaterial();
    }
  } finally {
    await restoreMaterial();
  }
  const max = Math.max(...times);
  test.info().annotations.push({ type: '측정', description: `정지 요청→응답 ${times.map((t) => t.toFixed(0)).join(', ')} ms (기준: 버튼 200 ms)` });
  expect(max).toBeLessThanOrEqual(200);
});
