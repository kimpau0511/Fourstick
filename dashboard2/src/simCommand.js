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

// 판정 코드 → 기록용 사람 말(내부 코드를 화면에 쓰지 않는다).
const DECISION_LOG = {
  CONFIRM: '안전 확인됨', CONFIRM_GOAL: '안전 확인됨', RUN: '안전 확인됨',
  BLOCK: '실행 차단', ASK: '추가 확인 필요', STOP: '정지 요청',
};
const LOG_MAX = 50;

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

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

function reasonOf(res) {
  const p = res.payload;
  return p.reason || p.detail || p.message || `백엔드 응답 ${res.status}`;
}

export function useSimCommand() {
  const [state, setState] = useState({
    busy: false, sent: '', result: null, pending: null, deadline: null,
    job: null, goal: null, error: null, stopNote: null, statusUnknown: false,
    seq: null, updatedAt: null, // seq = 지금 명령의 기록 id(오버레이가 '새 명령인가'를 가린다), updatedAt = 작업·목표 응답을 마지막으로 받은 시각
  });
  // 이 탭에서 보낸 명령 기록(최근 LOG_MAX개, 메모리). 서버 기록(recent_jobs)과 별개다.
  const [log, setLog] = useState([]);
  const alive = useRef(true);
  const currentId = useRef(null); // 지금 이어지고 있는 명령의 기록 id
  const nextId = useRef(1);
  // StrictMode는 effect를 해제했다 다시 붙인다 — 붙을 때 되살려야 한다.
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const patch = useCallback((next) => { if (alive.current) setState((s) => ({ ...s, ...next })); }, []);

  // 기록 항목에 사건 하나를 덧붙인다. id가 없으면 현재 명령에 붙인다(명령 없이 정지만 누른 경우는 버린다).
  const addEvent = useCallback((kind, label, detail = '', id = currentId.current) => {
    if (id == null || !alive.current) return;
    const event = { at: Date.now(), kind, label, detail };
    setLog((entries) => entries.map((e) => (e.id === id ? { ...e, events: [...e.events, event] } : e)));
  }, []);

  // 작업 상태 조회가 실패하면 마지막 '실행 중'을 그대로 두지 않는다 — 서버에서 아직 돌고 있을 수도,
  // 끝났을 수도 있으니 '상태 확인 안 됨'으로 보인다(설계원칙 4). 정지는 헤더에서 계속 요청할 수 있다.
  const lost = useCallback((res, logId) => {
    const why = `작업 상태를 확인하지 못했습니다(마지막 관측: 실행 중) — ${reasonOf(res)}`;
    patch({ error: why, statusUnknown: true });
    addEvent('error', '상태 확인 안 됨', reasonOf(res), logId);
  }, [patch, addEvent]);

  // 작업·목표는 끝날 때까지 읽기만 한다. 로봇 명령을 보내지 않는다.
  const pollJob = useCallback(async (jobId, logId) => {
    addEvent('job', '작업 시작', jobId, logId);
    for (;;) {
      const res = await call('GET', `/v1/sim-demo/jobs/${encodeURIComponent(jobId)}`).catch((e) => ({ ok: false, payload: { detail: e.message } }));
      if (!alive.current) return;
      if (!res.ok) { lost(res, logId); return; }
      patch({ job: res.payload, updatedAt: Date.now() });
      if (res.payload.status !== 'running') {
        const report = res.payload.report;
        addEvent('result', (report && RESULT_LABELS[report.status]?.[1]) || '작업 종료', report ? report.status : '', logId);
        return;
      }
      await sleep(POLL_MS);
    }
  }, [patch, addEvent, lost]);

  const pollGoal = useCallback(async (goalId, logId) => {
    addEvent('job', '목표 시작', goalId, logId);
    const seen = new Set();
    for (;;) {
      const res = await call('GET', `/v1/sim-demo/goals/${encodeURIComponent(goalId)}`).catch((e) => ({ ok: false, payload: { detail: e.message } }));
      if (!alive.current) return;
      if (!res.ok) { lost(res, logId); return; }
      patch({ goal: res.payload, updatedAt: Date.now() });
      // 목표가 단계마다 만든 작업 id — 기록 화면이 서버 작업 줄과 이 명령을 한 줄로 묶는 근거.
      (res.payload.plan || []).forEach((p) => { if (p.job_id && !seen.has(p.job_id)) { seen.add(p.job_id); addEvent('goal_job', '단계 작업', p.job_id, logId); } });
      if (!['running', 'stopping'].includes(res.payload.status)) {
        addEvent('result', '목표 종료', res.payload.status, logId);
        return;
      }
      await sleep(POLL_MS);
    }
  }, [patch, addEvent, lost]);

  // robot: 대상 로봇 id(모르면 null). 서버는 이 값을 받지 않고 기록용으로만 쓴다.
  // stt: 음성 final이면 { rawText, confidence } — source를 stt_final로 보낸다(옛 화면 simDemoCommand와 같다).
  const send = useCallback(async (text, robot = null, stt = null) => {
    const utterance = text.trim();
    if (!utterance) return;
    const id = nextId.current++;
    currentId.current = id;
    setLog((entries) => [...entries, { id, sentAt: Date.now(), text: utterance, robot, via: stt ? '음성' : '텍스트', events: [] }].slice(-LOG_MAX));
    patch({ busy: true, sent: utterance, result: null, pending: null, deadline: null, job: null, goal: null, error: null, stopNote: null, statusUnknown: false, seq: id, updatedAt: null });
    const requestedAt = Date.now(); // 남은 시간은 요청을 보낸 시각부터 센다 — 왕복 시간만큼 짧게(안전한 쪽) 보인다
    let res;
    try {
      res = await call('POST', '/v1/sim-demo/command', {
        mode: 'simulation_demo', utterance, source: stt ? 'stt_final' : 'text', session_id: SESSION_ID,
        ...(stt ? { raw_transcript: stt.rawText, stt_confidence: stt.confidence } : {}),
      });
    } catch (error) {
      patch({ busy: false, error: `백엔드에 연결하지 못했습니다: ${error.message}` });
      addEvent('error', '연결 실패', error.message, id);
      return;
    }
    const result = res.payload;
    if (!result.decision) { patch({ busy: false, error: reasonOf(res) }); addEvent('error', '오류', reasonOf(res), id); return; }
    addEvent('decision', DECISION_LOG[result.decision] || '시연 명령 아님', result.reason || (result.confirmation && result.confirmation.summary) || '', id);
    // 'stop'은 서버가 정지를 접수했을 때만 남긴다. 접수 안 됨(작업 없음 등)은 'stop_none'.
    if (result.decision === 'STOP') {
      if (result.stop?.requested === true) addEvent('stop', '정지 요청', '', id);
      else addEvent('stop_none', '정지 요청 (정지할 작업 없음)', result.stop?.detail || '', id);
    }
    const pending = ['CONFIRM', 'CONFIRM_GOAL'].includes(result.decision) ? result.confirmation : null;
    patch({
      busy: false, result, pending,
      deadline: pending && pending.remaining_sec != null ? requestedAt + pending.remaining_sec * 1000 : null,
    });
    // 옛 백엔드는 정확한 텍스트 명령을 확인 없이 바로 실행한다(RUN + job).
    if (result.job) pollJob(result.job.job_id, id);
  }, [patch, pollJob, addEvent]);

  /** 확인 카드의 버튼. confirm일 때만 작업(또는 고정 목표)이 시작된다. */
  const answer = useCallback(async (action) => {
    const pending = state.pending;
    if (!pending) return;
    const id = currentId.current;
    patch({ busy: true, error: null });
    let res;
    try {
      res = pending.kind === 'goal'
        ? await call('POST', `/v1/sim-demo/goals/${encodeURIComponent(pending.goal_id)}/confirm`, { action })
        : await call('POST', '/v1/sim-demo/confirm', { token: pending.token, action });
    } catch (error) {
      patch({ busy: false, pending: null, deadline: null, error: `확인 요청 실패: ${error.message}` });
      addEvent('error', '확인 요청 실패', error.message, id);
      return;
    }
    patch({ busy: false, pending: null, deadline: null });
    // 취소도 서버가 받아들였는지 먼저 본다 — 거부를 "취소했습니다"로 확정하지 않는다.
    if (!res.ok) {
      const what = action === 'cancel' ? '취소' : '확인';
      addEvent('error', `${what} 거부됨`, reasonOf(res), id);
      // rejected: 서버가 명령을 차단한 판정이 아니라 확인 요청이 거부된 것 — 위험 판정 창을 열지 않는다.
      patch({ result: { decision: 'BLOCK', rejected: true, reason: `${action === 'cancel' ? '취소가' : '확인이'} 거부되었습니다 — ${reasonOf(res)}` } });
      return;
    }
    if (action === 'cancel') {
      addEvent('cancel', '취소', '', id);
      patch({ result: { decision: 'CANCELLED', reason: '취소했습니다 — 작업을 만들지 않았습니다' } });
      return;
    }
    addEvent('confirm', '승인', '', id);
    if (pending.kind === 'goal') {
      patch({ result: { decision: 'RUN', reason: null }, goal: res.payload });
      pollGoal(pending.goal_id, id);
      return;
    }
    patch({ result: res.payload });
    if (res.payload.job) pollJob(res.payload.job.job_id, id);
  }, [state.pending, patch, pollJob, pollGoal, addEvent]);

  /** 작업 정지. 실행 중인 시연 작업(또는 목표)에 정지를 **요청**한다 — 확인 없이 즉시.
   *  프로세스를 죽이지 않는다. 시뮬레이터가 정지를 확인하면 체크포인트가 남는다. */
  const stop = useCallback(async () => {
    let res;
    try {
      res = await call('POST', '/v1/sim-demo/stop', {});
    } catch (error) {
      patch({ stopNote: { tone: 'danger', text: `정지 요청을 보내지 못했습니다: ${error.message}` } });
      addEvent('error', '정지 요청 실패', error.message);
      return;
    }
    const p = res.payload;
    if (!res.ok) addEvent('error', '정지 요청 실패', reasonOf(res));
    else if (p.requested === true) addEvent('stop', '정지 요청', p.detail || '');
    else addEvent('stop_none', '정지 요청 (정지할 작업 없음)', p.detail || '');
    if (!res.ok) patch({ stopNote: { tone: 'danger', text: `정지 요청 실패 — ${reasonOf(res)}` } });
    else if (p.requested) patch({ stopNote: { tone: 'ok', text: '정지를 요청했습니다 — 시뮬레이터가 정지를 확인하면 결과가 표시됩니다' } });
    else patch({ stopNote: { tone: 'warn', text: p.detail || '정지할 시연 작업이 없습니다' } });
  }, [patch, addEvent]);

  /** 결과 카드를 닫고 입력으로 돌아간다(피그마 '새 명령 입력'). 서버에는 아무것도 보내지 않는다. */
  const reset = useCallback(() => {
    patch({ sent: '', result: null, pending: null, deadline: null, job: null, goal: null, error: null, stopNote: null, statusUnknown: false, updatedAt: null });
  }, [patch]);

  return { ...state, log, send, answer, stop, reset };
}
