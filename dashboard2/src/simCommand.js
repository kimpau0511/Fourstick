import { useCallback, useEffect, useRef, useState } from 'react';

// 시연 명령 흐름(/v1/sim-demo/*). 판정·요약·자리는 전부 서버 값이다 — 화면이 만들지 않는다.
//   명령 → CONFIRM(확인 카드) → 확인 → 작업 → 진행 조회
//   ASK/BLOCK은 작업이 없고, STOP은 확인 없이 바로 정지 요청이다.

const POLL_MS = 1500;
// 대화 맥락("그거", 되묻기 답)을 이 탭에만 묶는다. 인증이 아니다.
const SESSION_ID = crypto.randomUUID();

// 작업 결과 코드 → [톤, 표시]. html/static/js/sim-demo.js의 RESULT_LABELS와 같은 표다.
export const RESULT_LABELS = {
  simulation_transfer_completed: ['ok', '이송 완료'],
  simulation_transfer_resumed_completed: ['ok', '이어서 이송 완료'],
  returned_to_origin: ['ok', '원래 자리 복귀 완료'],
  simulation_transfer_stopped: ['danger', '정지됨'],
  resume_stopped: ['danger', '이어서 하던 중 정지'],
  return_stopped: ['danger', '복귀 중 정지'],
  resume_blocked: ['warn', '이어서 하기 차단'],
  return_not_started: ['warn', '복귀 시작 안 함'],
  simulation_transfer_not_started: ['warn', '이송 시작 안 함'],
  simulation_transfer_incomplete: ['danger', '이송 실패'],
  resume_failed: ['danger', '이어서 하기 실패'],
  return_failed: ['danger', '복귀 실패'],
};

async function call(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    cache: 'no-store',
  });
  let payload = {};
  try { payload = await response.json(); } catch { /* 프록시 오류 등 JSON이 아닌 응답 */ }
  return { status: response.status, ok: response.ok, payload };
}

function reasonOf(res) {
  const p = res.payload;
  return p.reason || p.detail || p.message || `백엔드 응답 ${res.status}`;
}

export function useSimCommand() {
  const [state, setState] = useState({
    busy: false, sent: '', result: null, pending: null, deadline: null,
    job: null, goal: null, error: null, stopNote: null,
  });
  const alive = useRef(true);
  // StrictMode는 effect를 해제했다 다시 붙인다 — 붙을 때 되살려야 한다.
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const patch = useCallback((next) => { if (alive.current) setState((s) => ({ ...s, ...next })); }, []);

  // 작업·목표는 끝날 때까지 읽기만 한다. 로봇 명령을 보내지 않는다.
  const pollJob = useCallback(async (jobId) => {
    const res = await call('GET', `/v1/sim-demo/jobs/${encodeURIComponent(jobId)}`);
    if (!alive.current) return;
    if (!res.ok) { patch({ error: reasonOf(res) }); return; }
    patch({ job: res.payload });
    if (res.payload.status === 'running') setTimeout(() => pollJob(jobId), POLL_MS);
  }, [patch]);

  const pollGoal = useCallback(async (goalId) => {
    const res = await call('GET', `/v1/sim-demo/goals/${encodeURIComponent(goalId)}`);
    if (!alive.current) return;
    if (!res.ok) { patch({ error: reasonOf(res) }); return; }
    patch({ goal: res.payload });
    if (['running', 'stopping'].includes(res.payload.status)) setTimeout(() => pollGoal(goalId), POLL_MS);
  }, [patch]);

  const send = useCallback(async (text) => {
    const utterance = text.trim();
    if (!utterance) return;
    patch({ busy: true, sent: utterance, result: null, pending: null, deadline: null, job: null, goal: null, error: null, stopNote: null });
    let res;
    try {
      res = await call('POST', '/v1/sim-demo/command', {
        mode: 'simulation_demo', utterance, source: 'text', session_id: SESSION_ID,
      });
    } catch (error) {
      patch({ busy: false, error: `백엔드에 연결하지 못했습니다: ${error.message}` });
      return;
    }
    const result = res.payload;
    if (!result.decision) { patch({ busy: false, error: reasonOf(res) }); return; }
    const pending = ['CONFIRM', 'CONFIRM_GOAL'].includes(result.decision) ? result.confirmation : null;
    patch({
      busy: false, result, pending,
      deadline: pending && pending.remaining_sec != null ? Date.now() + pending.remaining_sec * 1000 : null,
    });
    // 옛 백엔드는 정확한 텍스트 명령을 확인 없이 바로 실행한다(RUN + job).
    if (result.job) pollJob(result.job.job_id);
  }, [patch, pollJob]);

  /** 확인 카드의 버튼. confirm일 때만 작업(또는 고정 목표)이 시작된다. */
  const answer = useCallback(async (action) => {
    const pending = state.pending;
    if (!pending) return;
    patch({ busy: true, error: null });
    let res;
    try {
      res = pending.kind === 'goal'
        ? await call('POST', `/v1/sim-demo/goals/${encodeURIComponent(pending.goal_id)}/confirm`, { action })
        : await call('POST', '/v1/sim-demo/confirm', { token: pending.token, action });
    } catch (error) {
      patch({ busy: false, pending: null, deadline: null, error: `확인 요청 실패: ${error.message}` });
      return;
    }
    patch({ busy: false, pending: null, deadline: null });
    if (action === 'cancel') {
      patch({ result: { decision: 'CANCELLED', reason: '취소했습니다 — 작업을 만들지 않았습니다' } });
      return;
    }
    if (!res.ok) {
      patch({ result: { decision: 'BLOCK', reason: `확인이 거부되었습니다 — ${reasonOf(res)}` } });
      return;
    }
    if (pending.kind === 'goal') {
      patch({ result: { decision: 'RUN', reason: null }, goal: res.payload });
      pollGoal(pending.goal_id);
      return;
    }
    patch({ result: res.payload });
    if (res.payload.job) pollJob(res.payload.job.job_id);
  }, [state.pending, patch, pollJob, pollGoal]);

  /** 작업 정지. 실행 중인 시연 작업(또는 목표)에 정지를 **요청**한다 — 확인 없이 즉시.
   *  프로세스를 죽이지 않는다. 시뮬레이터가 정지를 확인하면 체크포인트가 남는다. */
  const stop = useCallback(async () => {
    let res;
    try {
      res = await call('POST', '/v1/sim-demo/stop', {});
    } catch (error) {
      patch({ stopNote: { tone: 'danger', text: `정지 요청을 보내지 못했습니다: ${error.message}` } });
      return;
    }
    const p = res.payload;
    if (!res.ok) patch({ stopNote: { tone: 'danger', text: `정지 요청 실패 — ${reasonOf(res)}` } });
    else if (p.requested) patch({ stopNote: { tone: 'ok', text: '정지를 요청했습니다 — 시뮬레이터가 정지를 확인하면 결과가 표시됩니다' } });
    else patch({ stopNote: { tone: 'warn', text: p.detail || '정지할 시연 작업이 없습니다' } });
  }, [patch]);

  return { ...state, send, answer, stop };
}
