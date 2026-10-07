import { useCallback, useEffect, useRef, useState } from 'react';
import { RESULT_LABELS } from './simCommand.js';

// 일반 경로 명령 흐름(/v1/plan → /v1/decision → /v1/execute). 판정·계획·결과는 전부 서버 값이다.
//   발화 → 계획(LLM) + 안전 관문 → 관문 allow면 확인 카드 → 승인(본 계획 해시 대조) → 실행(서버가 실행 직전 재검증)
//   관문 block/ask·계획 실패는 작업이 없다. 정지는 전체 정지(/v1/stop) — 확인 없이 바로.
// useSimCommand와 같은 모양을 돌려준다 — 명령 패널·시뮬레이션 창·기록 화면이 그대로 쓴다.
// 이 경로를 기본으로 쓰는 것은 VITE_COMMAND_MODE=general(이 PC의 .env.local)일 때뿐이다(결정 1: 기본은 시연 명령).

const SESSION_KEY = 'forstick2.dashboard.session_id';
const POLL_MS = 1500; // 실행 중 진행(이송 작업) 조회 주기 — 화면 갱신 속도일 뿐 판단 기준 아님
const LOG_MAX = 50;
// 관문 판정(storage/records.py ValidationDecision) → [명령 패널 판정, 기록용 문구]
const GATE = { allow: ['CONFIRM', '안전 확인됨'], block: ['BLOCK', '실행 차단'], ask: ['ASK', '추가 확인 필요'] };
// 실행 최종 상태(core/execution_state.py) → [톤, 표시]. unknown은 성공으로 바꾸지 않는다.
const FINAL = {
  completed: ['ok', '실행 완료'], stopped: ['danger', '정지 확인됨'], stopping: ['warn', '정지 요청됨 · 확인 전'],
  failed: ['danger', '실행 실패'], unknown: ['warn', '결과 확인 안 됨'],
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

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });
const reasonOf = (res) => res.payload.detail || res.payload.error || res.payload.message || `백엔드 응답 ${res.status}`;
const argText = (args) => Object.values(args || {}).filter((v) => v != null && v !== '').join(', ');

// 관문 사유: 서버 detail, 없으면 막힌 규칙의 메시지(서버 문장 그대로).
function gateReason(bundle) {
  const gate = bundle.validation || {};
  if (gate.detail) return gate.detail;
  const rules = (bundle.safety && bundle.safety.rules) || [];
  const hit = rules.filter((r) => r.status === 'block' || r.status === 'insufficient_data').map((r) => r.message).filter(Boolean);
  return hit.join(' · ') || null;
}

export function useGeneralCommand() {
  const [state, setState] = useState({
    busy: false, sent: '', result: null, pending: null, deadline: null,
    job: null, goal: null, error: null, stopNote: null, statusUnknown: false, seq: null, updatedAt: null,
  });
  const [log, setLog] = useState([]);
  const alive = useRef(true);
  const currentId = useRef(null);
  const nextId = useRef(1);
  const session = useRef(null);
  const bundleRef = useRef(null); // 확인 카드에 보인 계획 묶음 — 승인 때 이 plan_hash로 대조한다
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const patch = useCallback((next) => { if (alive.current) setState((s) => ({ ...s, ...next })); }, []);
  const addEvent = useCallback((kind, label, detail = '', id = currentId.current) => {
    if (id == null || !alive.current) return;
    const event = { at: Date.now(), kind, label, detail };
    setLog((entries) => entries.map((e) => (e.id === id ? { ...e, events: [...e.events, event] } : e)));
  }, []);

  // 서버 세션(대화·계획을 이 탭에 묶는 id, 인증 아님). 새로고침은 이어받고, 거부되면 새로 받는다.
  const ensureSession = useCallback(async () => {
    if (session.current) return session.current;
    let stored = null;
    try { stored = sessionStorage.getItem(SESSION_KEY); } catch { /* 저장소를 못 쓰면 새 세션 */ }
    if (stored) {
      const claimed = await call('POST', `/v1/sessions/${encodeURIComponent(stored)}/clients`, {});
      if (claimed.ok) { session.current = stored; return stored; }
    }
    const created = await call('POST', '/v1/sessions', { origin: 'dashboard2' });
    if (!created.ok || !created.payload.session_id) throw new Error(`세션을 만들 수 없습니다 — ${reasonOf(created)}`);
    session.current = created.payload.session_id;
    try { sessionStorage.setItem(SESSION_KEY, session.current); } catch { /* 이번 탭에서만 유지 */ }
    return session.current;
  }, []);

  const send = useCallback(async (text, robot = null, stt = null) => {
    const utterance = text.trim();
    if (!utterance) return;
    const id = nextId.current++;
    currentId.current = id;
    bundleRef.current = null;
    setLog((entries) => [...entries, { id, sentAt: Date.now(), text: utterance, robot, via: stt ? '음성' : '텍스트', events: [] }].slice(-LOG_MAX));
    patch({ busy: true, sent: utterance, result: null, pending: null, deadline: null, job: null, goal: null, error: null, stopNote: null, statusUnknown: false, seq: id, updatedAt: null });
    let res;
    try {
      const sessionId = await ensureSession();
      // 음성 확정본도 같은 계획 입구로 간다(서버가 STT 기록과 잇는 id는 이 화면 음성 경로가 주지 않는다).
      res = await call('POST', '/v1/plan', { session_id: sessionId, utterance });
    } catch (error) {
      patch({ busy: false, error: `백엔드에 연결하지 못했습니다: ${error.message}` });
      addEvent('error', '연결 실패', error.message, id);
      return;
    }
    const p = res.payload;
    if (!p.ok) {
      // 계획을 못 만들었다: 되묻기(clarification)면 ASK, 그 밖(모델 없음·슬롯 부족 등)은 실행 없음.
      const decision = p.clarification ? 'ASK' : 'BLOCK';
      const reason = p.clarification || p.detail || reasonOf(res);
      patch({ busy: false, result: { decision, reason } });
      addEvent('decision', decision === 'ASK' ? '추가 확인 필요' : '계획 실패', reason, id);
      return;
    }
    const [decision, label] = GATE[(p.validation || {}).decision] || ['BLOCK', '확인 안 됨'];
    const reason = decision === 'CONFIRM' ? null : gateReason(p) || '관문 판정을 받지 못했습니다';
    addEvent('decision', label, reason || '', id);
    const latch = p.stop_latch || {};
    if (decision !== 'CONFIRM' || !p.executable) {
      patch({ busy: false, result: { decision: decision === 'CONFIRM' ? 'BLOCK' : decision, reason: reason || '실행할 수 없는 계획입니다' } });
      return;
    }
    bundleRef.current = p;
    const plan = p.plan || {};
    const steps = (plan.steps || []).map((s) => `${s.index}. ${s.skill}${argText(s.args) ? ` (${argText(s.args)})` : ''}`);
    // 남은 시간 = 계획 유효 시간(서버 ttl_sec). 서버가 만료를 다시 판정한다.
    const ttlMs = typeof plan.ttl_sec === 'number' ? plan.ttl_sec * 1000 : null;
    patch({
      busy: false, result: { decision: 'CONFIRM', reason: null },
      pending: { kind: 'plan', summary: `계획 ${steps.length}단계 — 안전 확인됨`, steps,
        created_at: plan.created_at, latch: latch.cleared === false ? latch.detail || '정지 래치가 풀리지 않았습니다' : null },
      deadline: ttlMs ? Date.now() + ttlMs : null,
    });
  }, [patch, addEvent, ensureSession]);

  // 실행은 서버가 끝날 때까지 응답하지 않는다. 그동안 이송 작업(/v1/sim-demo running_job)의 진행만 읽는다.
  const watchTransfer = useCallback(async (done, logId) => {
    let seen = null;
    while (!done.current && alive.current) {
      const st = await call('GET', '/v1/sim-demo').catch(() => null);
      const running = st && st.ok ? st.payload.running_job : null;
      if (running && !done.current) {
        if (seen !== running.job_id) { seen = running.job_id; addEvent('job', '이송 작업 시작', running.job_id, logId); }
        const jr = await call('GET', `/v1/sim-demo/jobs/${encodeURIComponent(running.job_id)}`).catch(() => null);
        if (jr && jr.ok && !done.current) patch({ job: { ...jr.payload, status: 'running' }, updatedAt: Date.now() });
      }
      await sleep(POLL_MS);
    }
    return seen;
  }, [patch, addEvent]);

  const answer = useCallback(async (action) => {
    const bundle = bundleRef.current;
    if (!state.pending || !bundle) return;
    const id = currentId.current;
    const plan = bundle.plan || {};
    patch({ busy: true, error: null });
    let res;
    try {
      res = await call('POST', '/v1/decision', {
        session_id: session.current, request_id: bundle.request_id, plan_id: plan.plan_id, plan_hash: plan.plan_hash,
        decision: action === 'confirm' ? 'approve' : 'reject', note: action === 'confirm' ? '대시보드에서 실행 승인' : '대시보드에서 취소',
        robot_id: plan.robot_id, profile_id: plan.profile_id, profile_version: plan.profile_version,
      });
    } catch (error) {
      patch({ busy: false, pending: null, deadline: null, error: `확인 요청 실패: ${error.message}` });
      addEvent('error', '확인 요청 실패', error.message, id);
      return;
    }
    patch({ pending: null, deadline: null });
    if (!res.ok || !res.payload.ok) {
      const what = action === 'cancel' ? '취소' : '승인';
      addEvent('error', `${what} 거부됨`, reasonOf(res), id);
      patch({ busy: false, result: { decision: 'BLOCK', rejected: true, reason: `${what}이 거부되었습니다 — ${reasonOf(res)}` } });
      return;
    }
    if (action === 'cancel') {
      addEvent('cancel', '취소', '', id);
      patch({ busy: false, result: { decision: 'CANCELLED', reason: '취소했습니다 — 실행하지 않았습니다' } });
      return;
    }
    addEvent('confirm', '승인', '', id);
    // 서버가 실행 직전에 승인 조건·관문·실행 허가를 다시 본다. 응답이 올 때까지는 '실행 중'이다.
    patch({ busy: false, result: { decision: 'RUN', reason: null }, job: { job_id: null, status: 'running', progress: [] }, updatedAt: Date.now() });
    const done = { current: false };
    const watching = watchTransfer(done, id);
    const exec = await call('POST', '/v1/execute', {
      session_id: session.current, request_id: bundle.request_id, plan_id: plan.plan_id, approval_id: res.payload.approval_id,
    }).catch((e) => ({ ok: false, status: 0, payload: { detail: e.message } }));
    done.current = true;
    const transferId = await watching;
    if (!alive.current) return;
    const e = exec.payload;
    if (!e.execution_id) {
      // 실행 허가 거부·관문 변경·정지 래치 등 — 실행이 만들어지지 않았다.
      const why = (e.reasons || []).map((r) => r.detail || r.reason).filter(Boolean).join(' · ') || reasonOf(exec);
      addEvent('result', '실행 안 됨', why, id);
      patch({ job: null, result: { decision: 'BLOCK', rejected: true, reason: `실행하지 않았습니다 — ${why}` } });
      return;
    }
    const final = e.final || {};
    const [tone, label] = e.interrupted ? ['danger', `실행 중단 (${e.interrupted})`] : FINAL[final.state] || ['warn', '결과 확인 안 됨'];
    addEvent('result', label, final.state || '', id);
    // 이송 작업 결과(시연 실행기 코드)가 있으면 그것도 보인다 — 실행 결과의 근거다.
    let report = null;
    if (transferId) {
      const jr = await call('GET', `/v1/sim-demo/jobs/${encodeURIComponent(transferId)}`).catch(() => null);
      if (jr && jr.ok) report = jr.payload.report || null;
    }
    patch({
      job: { job_id: transferId, execution_id: e.execution_id, status: 'finished', report,
        general: { state: final.state, tone, label: report && RESULT_LABELS[report.status] ? `${label} · ${RESULT_LABELS[report.status][1]}` : label } },
      updatedAt: Date.now(),
    });
  }, [state.pending, patch, addEvent, watchTransfer]);

  /** 전체 정지(/v1/stop). 확인 없이 즉시. 서버가 접수·확인한 만큼만 말한다. */
  const stop = useCallback(async () => {
    let res;
    try {
      const sessionId = await ensureSession();
      res = await call('POST', '/v1/stop', { session_id: sessionId });
    } catch (error) {
      patch({ stopNote: { tone: 'danger', text: `정지 요청을 보내지 못했습니다: ${error.message}` } });
      addEvent('error', '정지 요청 실패', error.message);
      return;
    }
    const p = res.payload;
    if (!res.ok || !p.requested) {
      patch({ stopNote: { tone: 'danger', text: `정지 요청 실패 — ${reasonOf(res)}` } });
      addEvent('error', '정지 요청 실패', reasonOf(res));
      return;
    }
    addEvent('stop', p.confirmed ? '정지 확인됨' : '정지 요청', p.detail || '');
    patch({ stopNote: { tone: 'ok', text: p.confirmed ? '정지를 확인했습니다 — 새 계획을 요청하면 정지 래치가 풀립니다' : '정지를 요청했습니다 — 정지 확인 전입니다' } });
  }, [patch, addEvent, ensureSession]);

  const reset = useCallback(() => {
    bundleRef.current = null;
    patch({ sent: '', result: null, pending: null, deadline: null, job: null, goal: null, error: null, stopNote: null, statusUnknown: false, updatedAt: null });
  }, [patch]);

  return { ...state, log, send, answer, stop, reset, mode: 'general' };
}
