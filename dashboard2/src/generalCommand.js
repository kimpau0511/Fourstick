import { useCallback, useEffect, useRef, useState } from 'react';
import { HttpBackend, planFromBundle, validationFromBundle } from '../../html/static/js/backend-http.js';
import { setResourceLabels } from '../../html/static/js/catalog.js';

// 기존 웹의 세션/승인 계약을 재사용한다. 계획 생성은 실행하지 않는다.
// 식별자는 서버 응답으로만 얻고, 승인 버튼에서만 decision → execute를 호출한다.
async function post(path, body) {
  const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const payload = await response.json();
  return { ok: response.ok, payload };
}

// 계획 API 실패 응답의 판정. 의도 단계는 decision(BLOCK·ASK·NOOP)과 clarification을 주고,
// 계획 단계 실패는 blocked를 준다 — 둘 다 서버 값 그대로 쓴다(차단을 되묻기로 바꾸지 않는다).
const FAILURE_LABELS = { BLOCK: '실행 차단', ASK: '추가 확인 필요', NOOP: '할 일 없음' };
export function failureOf(p) {
  const decision = p.decision === 'BLOCK' || p.blocked ? 'BLOCK' : p.decision === 'NOOP' ? 'NOOP' : 'ASK';
  return { decision, label: FAILURE_LABELS[decision], reason: p.clarification || p.detail || p.error || p.reason_code || '' };
}

export function useGeneralCommand({ onSettled } = {}) {
  const backend = useRef(null);
  // 작업이 끝나거나 취소되면 서버 상태를 다시 조회한다(버튼 갱신). 최신 콜백만 부른다.
  const settled = useRef(onSettled);
  settled.current = onSettled;
  const resync = useCallback(() => { try { settled.current?.(); } catch { /* 조회 실패는 server.js가 표시한다 */ } }, []);
  const connecting = useRef(null);
  const busy = useRef(false);
  const next = useRef(0);
  const alive = useRef(true);
  const [state, setState] = useState({ busy: false, sent: '', pending: null, result: null, job: null, error: null, deadline: null, seq: null, updatedAt: null, stopNote: null });
  const [log, setLog] = useState([]);
  const patch = useCallback((value) => { if (alive.current) setState((s) => ({ ...s, ...value })); }, []);
  const addEvent = useCallback((kind, label, detail = '') => {
    const id = next.current;
    if (alive.current) setLog((entries) => entries.map((e) => e.id !== id ? e : ({ ...e, events: [...e.events, { at: Date.now(), kind, label, detail }] })));
  }, []);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      const socket = backend.current?.socket;
      if (socket) { socket.onclose = null; socket.close(); }
    };
  }, []);
  const getBackend = useCallback(async () => {
    if (!backend.current) {
      backend.current = new HttpBackend();
      backend.current.onEvent((event) => {
        if (!alive.current) return;
        if (event.kind === 'step-result') {
          setState((s) => !s.job ? s : ({ ...s, updatedAt: Date.now(), job: { ...s.job, progress: (s.job.progress || []).map((p) => p.no !== event.result.no ? p : { ...p, reached: event.result.taskSuccess === true }) } }));
        }
        if (event.kind === 'step-progress') {
          // 실행기가 지난 계획 스텝을 차례로 표시한다(진행 표시). 최종 성공·실패는 step-result가 덮어쓴다.
          const done = new Set(event.doneSteps || []);
          setState((s) => !s.job || s.job.status !== 'running' ? s : ({ ...s, updatedAt: Date.now(), job: { ...s.job,
            stage: event.stage ? `${event.stage.no}/${event.stage.of} ${event.stage.label}` : s.job.stage,
            progress: (s.job.progress || []).map((p) => (done.has(p.no) && !p.reached ? { ...p, reached: true } : p)) } }));
        }
        if (event.kind === 'connection' && event.state !== 'ok') {
          setState((s) => s.job?.status !== 'running' ? s : ({ ...s, statusUnknown: true, error: '실행 상태 연결이 끊겼습니다 — 정지는 계속 요청할 수 있습니다' }));
        }
      });
    }
    if (!connecting.current) {
      connecting.current = backend.current.connect().catch((error) => { connecting.current = null; throw error; });
    }
    await connecting.current;
    const catalogs = backend.current.config?.catalogs || {};
    setResourceLabels([...(catalogs.locations || []), ...(catalogs.objects || [])]);
    return backend.current;
  }, []);
  const send = useCallback(async (text, robot = null, stt = null) => {
    if (busy.current || !text.trim()) return;
    busy.current = true;
    const id = ++next.current;
    patch({ busy: true, sent: text, pending: null, result: null, job: null, error: null, deadline: null, seq: id, stopNote: null, statusUnknown: false });
    setLog((entries) => [...entries, { id, sentAt: Date.now(), text, robot, via: stt ? '음성' : '텍스트', events: [] }].slice(-50));
    try {
      const b = await getBackend();
      b.lastPlanBundle = null;
      const response = await post('/v1/plan', { session_id: b.sessionId, utterance: text });
      const p = response.payload;
      if (p.stopped) {
        const stop = p.stop || p;
        addEvent(stop.requested === true ? 'stop' : 'stop_none', stop.requested === true ? '정지 요청' : '정지 요청 (정지할 작업 없음)', stop.detail || '');
        patch({ result: { decision: 'STOP', stop, reason: p.detail } }); return;
      }
      if (!response.ok || !p.ok) {
        const failure = failureOf(p);
        addEvent('decision', failure.label, failure.reason);
        patch({ result: { decision: failure.decision, reason: failure.reason, tasks: p.intent_tasks || [] } });
        return;
      }
      const bundle = p.payload || p;
      const plan = planFromBundle(bundle);
      const validation = validationFromBundle(bundle);
      if (validation.verdict !== 'PASS') {
        addEvent('decision', validation.verdict === 'BLOCK' ? '실행 차단' : '추가 확인 필요', validation.detail);
        patch({ result: { decision: validation.verdict, reason: validation.detail } });
        return;
      }
      b.lastPlanBundle = bundle;
      addEvent('decision', '안전 확인됨', validation.detail);
      patch({ result: { decision: 'CONFIRM' }, pending: { kind: 'general', summary: plan.utterance, plan, validation, created_at: plan.createdAt }, deadline: (plan.createdAt + plan.ttlSec) * 1000 });
    } catch (error) { patch({ error: `계획 요청 실패: ${error.message}` }); }
    finally { busy.current = false; patch({ busy: false }); }
  }, [getBackend, patch, addEvent]);
  const answer = useCallback(async (action) => {
    if (busy.current || !state.pending) return;
    busy.current = true;
    patch({ busy: true, error: null });
    try {
      const b = await getBackend();
      const bundle = b.lastPlanBundle;
      if (!bundle || state.pending.plan.planId !== bundle.plan.plan_id) throw new Error('현재 승인 대상 계획이 일치하지 않습니다');
      if (action === 'cancel') {
        const response = await post('/v1/decision', { session_id: b.sessionId, request_id: bundle.request_id, plan_id: bundle.plan.plan_id, plan_hash: bundle.plan.plan_hash, decision: 'reject', note: '대시보드에서 취소' });
        if (!response.ok || !response.payload.ok) throw new Error(`취소가 거부되었습니다${response.payload.detail ? ` — ${response.payload.detail}` : ''}`);
        b.lastPlanBundle = null;
        addEvent('cancel', '취소');
        patch({ pending: null, deadline: null, result: { decision: 'CANCELLED', reason: '계획을 취소했습니다 — 실행하지 않았습니다' } });
        resync();
        return;
      }
      if (action !== 'confirm') return;
      addEvent('confirm', '승인');
      patch({ pending: null, deadline: null, result: { decision: 'RUN', summary: state.sent }, job: { status: 'running', action_label: state.sent, progress: state.pending.plan.steps.map((step) => ({ no: step.no, of: state.pending.plan.steps.length, label: step.description, reached: false })) }, totalSteps: state.pending.plan.steps.length, updatedAt: Date.now() });
      const result = await b.startExecution();
      if (!result.ok) {
        patch({ job: null, result: { decision: 'BLOCK', rejected: true, reason: result.detail || result.reasonCode } });
        resync();
        return;
      }
      addEvent('result', result.interrupted ? '작업 중단' : result.final?.task_succeeded === true ? '작업 완료' : '작업 실패', result.interrupted ? 'general_interrupted' : result.final?.task_succeeded === true ? 'general_completed' : 'general_failed');
      patch({ job: { status: 'completed', execution_id: result.executionId, progress: result.steps.map((s) => ({ no: s.index, of: state.pending.plan.steps.length, reached: s.task_succeeded, label: s.skill })), report: { status: result.interrupted ? 'general_interrupted' : result.final?.task_succeeded === true ? 'general_completed' : 'general_failed', reason_code: result.interrupted } }, updatedAt: Date.now(), statusUnknown: false, error: null });
      resync();
    } catch (error) {
      setState((s) => ({ ...s, pending: null, statusUnknown: s.job?.status === 'running', error: `확인 요청 실패: ${error.message}` }));
      resync();
    }
    finally { busy.current = false; patch({ busy: false }); }
  }, [getBackend, patch, addEvent, resync, state.pending, state.sent]);
  const stop = useCallback(async () => {
    try {
      const response = await post('/v1/stop', {});
      const p = response.payload;
      if (response.ok) addEvent(p.requested === true ? 'stop' : 'stop_none', p.requested === true ? '정지 요청' : '정지 요청 (정지할 작업 없음)', p.detail || '');
      else addEvent('error', '정지 요청 실패', p.detail || p.error || '');
      patch({ stopNote: { tone: response.ok ? p.requested ? 'ok' : 'warn' : 'danger', text: p.detail || (!response.ok ? p.error || '정지 요청이 거부되었습니다' : p.confirmed ? '서버가 정지를 확인했습니다' : p.requested ? '정지를 요청했습니다 — 실행 종료 확인을 기다립니다' : '정지할 작업이 없습니다') } });
    } catch (error) { patch({ stopNote: { tone: 'danger', text: `정지 요청 실패: ${error.message}` } }); addEvent('error', '정지 요청 실패', error.message); }
  }, [patch, addEvent]);
  const reset = useCallback(() => {
    if (busy.current || state.job?.status === 'running') return;
    if (backend.current) backend.current.lastPlanBundle = null;
    patch({ sent: '', pending: null, result: null, job: null, error: null, deadline: null, stopNote: null });
  }, [patch, state.job]);
  return { ...state, log, send, answer, stop, reset };
}
