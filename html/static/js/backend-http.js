/** 서버 백엔드 — 실제 `/v1/*` 계약에 붙는다.
 *
 * 화면은 이 파일과 `backend-sim.js`를 **같은 인터페이스로** 쓴다. 그래서 화면
 * 코드에는 엔드포인트가 없고, 서버가 없을 때도 흐름을 확인할 수 있다.
 *
 * 세션 경계는 기존 계약을 그대로 지킨다(`md/웹UI_구조.md`):
 *  - `session_id`는 서버가 만든다. sessionStorage에 둔다(탭 복제 시 복사된다).
 *  - `client_id`는 **메모리에만** 둔다. 복제되지 않는다.
 *  - 새로고침은 같은 세션을 이어받고, 복제 탭은 거부되면 새 세션을 받는다.
 *  - 탭 유예 시간(`client_grace_sec`)은 `/v1/config`에서 받아 쓴다 —
 *    **화면 코드에 숫자를 두지 않는다.**
 *
 * 화면에 승인 UI가 없는 대신, 사용자가 "실행 시작"을 누른 그 한 번의 동작이
 * 서버의 결정 기록(`POST /v1/decision`)과 실행(`POST /v1/execute`)을 차례로
 * 만든다. **누르지 않으면 아무 것도 실행되지 않는다.**
 */

import { describeStep } from './catalog.js';
import { verdictFromDecision } from './state.js';

const SESSION_KEY = 'forstick2.session_id';

async function request(method, path, body) {
  const options = { method, headers: { 'content-type': 'application/json' } };
  if (body !== undefined) options.body = JSON.stringify(body);
  const response = await fetch(path, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  return { status: response.status, ok: response.ok, payload: payload || {} };
}

function nowTime() {
  return new Date().toLocaleTimeString('ko-KR', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

/** 서버 계획 묶음을 화면 모양으로. 서버 값을 새로 만들지 않고 옮기기만 한다. */
export function planFromBundle(bundle) {
  const plan = bundle.plan || {};
  const consistency = bundle.consistency || {};
  const requested = consistency.request_resources || [];
  const planResources = consistency.plan_resources || [];
  const onlyInPlan = new Set(consistency.only_in_plan || []);
  const rows = requested.map((entry) => ({
    utterance: `${entry.surface} → ${entry.resource_id}`,
    plan: planResources.includes(entry.resource_id) ? entry.resource_id : '—',
    match: planResources.includes(entry.resource_id),
  }));
  planResources
    .filter((id) => onlyInPlan.has(id))
    .forEach((id) => rows.push({ utterance: '(요청에 없음)', plan: id, match: false }));
  return {
    requestId: bundle.request_id,
    planId: plan.plan_id,
    planHash: plan.plan_hash,
    createdAt: plan.created_at,
    ttlSec: plan.ttl_sec,
    utterance: plan.utterance,
    steps: (plan.steps || []).map((step) => ({
      no: step.index,
      skill: step.skill,
      args: step.args || {},
      description: describeStep(step.skill, step.args || {}),
    })),
    resources: rows,
  };
}

/** 관문 판정을 화면 모양으로. PASS/BLOCK/ASK 외의 값을 만들지 않는다. */
export function validationFromBundle(bundle) {
  const gate = bundle.validation;
  const rules = (bundle.safety && bundle.safety.rules) || [];
  if (!gate) {
    return {
      verdict: 'ASK',
      reasonCodes: ['config.missing'],
      detail: '관문 판정이 응답에 없다.',
      rules,
      blocked: [],
      missing: ['서버 판정'],
      fixes: ['계획을 다시 생성한다.'],
    };
  }
  const verdict = verdictFromDecision(gate.decision);
  const blocked = rules
    .filter((rule) => rule.status === 'block')
    .map((rule) => `${rule.code}: ${rule.message}`);
  const insufficient = rules
    .filter((rule) => rule.status === 'insufficient_data')
    .map((rule) => `${rule.code}: ${rule.message}`);
  const codes = [];
  if (gate.reason_code) codes.push(gate.reason_code);
  rules.forEach((rule) => {
    if (rule.reason_code && !codes.includes(rule.reason_code)) codes.push(rule.reason_code);
  });
  const geometry = gate.geometry || {};
  return {
    verdict,
    reasonCodes: codes,
    detail: gate.detail || '',
    rules,
    blocked,
    missing: insufficient,
    fixes:
      verdict === 'ASK'
        ? [
            '부족한 항목을 채운 뒤 계획을 다시 생성한다.',
            geometry.decision === 'ask'
              ? '환경 검사 입력(snapshot·좌표계)이 없으면 실행할 수 없다.'
              : '요청 문장에 위치·자재를 카탈로그 표현으로 넣는다.',
          ]
        : [],
    clarification: bundle.clarification || null,
    geometry,
  };
}

export class HttpBackend {
  constructor() {
    this.kind = 'server';
    this.label = '서버 연결';
    this.sessionId = null;
    /** 메모리에만 둔다. 탭 복제로 복사되지 않는다. */
    this.clientId = null;
    this.graceSec = null;
    this.config = null;
    this.socket = null;
    this.listeners = new Set();
    this.lastPlanBundle = null;
    this.approvalId = null;
    this.reconnectAttempts = 0;
    this.maxReconnect = 6;
    this.stt = { socket: null, context: null, node: null, stream: null };
  }

  onEvent(handler) {
    this.listeners.add(handler);
    return () => this.listeners.delete(handler);
  }

  emit(event) {
    this.listeners.forEach((fn) => fn(event));
  }

  // ── 세션 ────────────────────────────────────────────────────────────
  async connect() {
    const config = await request('GET', '/v1/config');
    if (!config.ok) throw new Error('서버 설정을 읽을 수 없다');
    this.config = config.payload;
    // 유예 시간은 서버가 유일한 출처다. 화면에 숫자를 두지 않는다.
    this.graceSec = (this.config.session || {}).client_grace_sec;

    const stored = sessionStorage.getItem(SESSION_KEY);
    if (stored) {
      const claimed = await request('POST', `/v1/sessions/${stored}/clients`, {});
      if (claimed.ok) {
        this.sessionId = stored;
        this.clientId = claimed.payload.client_id;
      } else {
        // 복제 탭이거나 만료된 세션이다. 새 세션을 받는다.
        this.emit({
          kind: 'log',
          level: 'warning',
          message: `이전 세션을 이어받지 못했다 (${claimed.payload.reason_code || claimed.status}). 새 세션을 만든다.`,
        });
        sessionStorage.removeItem(SESSION_KEY);
      }
    }
    if (!this.sessionId) {
      const created = await request('POST', '/v1/sessions', { origin: 'web-console' });
      if (!created.ok) throw new Error('세션을 만들 수 없다');
      this.sessionId = created.payload.session_id;
      this.clientId = created.payload.client_id;
      sessionStorage.setItem(SESSION_KEY, this.sessionId);
    }

    this.openEvents();
    const features = this.config.features || {};
    return {
      session: { session_id: this.sessionId, client_id: this.clientId },
      config: this.config,
      sttAvailable: Boolean(features.stt && features.stt.available),
    };
  }

  async restore() {
    const response = await request(
      'GET',
      `/v1/state?session_id=${encodeURIComponent(this.sessionId)}`,
    );
    if (!response.ok) return null;
    return response.payload;
  }

  // ── 이벤트 WebSocket ────────────────────────────────────────────────
  openEvents() {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const query = new URLSearchParams({ session_id: this.sessionId || '' });
    if (this.clientId) query.set('client_id', this.clientId);
    const socket = new WebSocket(`${scheme}//${location.host}/v1/events?${query}`);
    this.socket = socket;

    socket.onopen = () => {
      this.reconnectAttempts = 0;
      this.emit({ kind: 'connection', state: 'ok', detail: '서버 연결됨' });
    };
    socket.onmessage = (message) => {
      let event = null;
      try {
        event = JSON.parse(message.data);
      } catch {
        return;
      }
      this.handleServerEvent(event);
    };
    socket.onclose = () => {
      if (this.reconnectAttempts >= this.maxReconnect) {
        this.emit({
          kind: 'connection',
          state: 'lost',
          detail: '재연결 한도를 넘었다. 새로고침이 필요하다.',
        });
        return;
      }
      const delay = Math.min(15000, 500 * 2 ** this.reconnectAttempts);
      this.reconnectAttempts += 1;
      this.emit({ kind: 'connection', state: 'connecting', detail: '재연결 중' });
      setTimeout(() => this.openEvents(), delay);
    };
  }

  handleServerEvent(event) {
    const payload = event.payload || {};
    switch (event.type) {
      case 'step':
        this.emit({
          kind: 'step-result',
          result: {
            no: payload.index,
            skill: payload.skill,
            status: payload.task_succeeded ? 'done' : 'failed',
            requestAccepted: payload.request_accepted,
            motionDone: payload.motion_completed,
            goalReached: payload.target_reached,
            taskSuccess: payload.task_succeeded,
            reasonCode: payload.reason_code || '',
            retry: 0,
            // 어댑터 근거를 그대로 전달한다. 화면이 관측값을 만들지 않는다.
            evidence: payload.evidence || null,
            args: payload.args || null,
          },
        });
        this.emit({ kind: 'step-started', step: (payload.index || 0) + 1 });
        break;

      case 'execution_final':
        if (payload.interrupted) {
          this.emit({
            kind: 'log',
            level: 'warning',
            message: `실행이 중단됐다: ${payload.interrupted}`,
          });
        } else if (payload.ok) {
          this.emit({ kind: 'completed' });
        } else {
          this.emit({
            kind: 'failed',
            reasonCode: (payload.final && payload.final.reason_code) || '',
          });
        }
        break;

      case 'stop':
        // 전체 정지는 모든 세션에 간다. 다른 세션의 식별자는 들어 있지 않다.
        this.emit({
          kind: 'stop-result',
          record: {
            scope: 'global',
            requestTime: nowTime(),
            confirmedTime: payload.confirmed ? nowTime() : null,
            confirmed: Boolean(payload.confirmed),
            reasonCode: payload.reason_code || 'exec.stopped',
            result: payload.confirmed ? 'confirmed' : 'unconfirmed',
            affectedExecutionCount: payload.affected_execution_count || 0,
            yourExecutionIds: payload.your_execution_ids || [],
            detail: payload.detail || '',
          },
        });
        break;

      case 'cancel':
        this.emit({
          kind: 'cancel-result',
          record: {
            scope: 'execution',
            executionId: payload.execution_id,
            requestTime: nowTime(),
            confirmedTime: payload.cancel_result === 'confirmed' ? nowTime() : null,
            stoppedAtStep: payload.stopped_at_step || 0,
            reasonCode: 'exec.canceled',
            result: payload.cancel_result === 'confirmed' ? 'confirmed' : 'unconfirmed',
            wasRunning: Boolean(payload.was_running),
            detail: payload.detail || '',
          },
        });
        break;

      case 'plan_failed':
        this.emit({
          kind: 'log',
          level: 'error',
          message: `계획 생성 실패: ${payload.reason_code || ''} ${payload.detail || ''}`,
        });
        break;

      case 'state':
        this.emit({
          kind: 'log',
          level: 'info',
          message: `상태 전이: ${payload.from_state || '—'} → ${payload.to_state || '—'}`,
        });
        break;

      case 'error':
        this.emit({
          kind: 'log',
          level: 'error',
          message: `${event.reason_code || ''} ${event.detail || ''}`.trim(),
        });
        break;

      default:
        break;
    }
  }

  // ── 흐름 ────────────────────────────────────────────────────────────
  async createPlan(utterance, sttInferenceId = null) {
    const body = { session_id: this.sessionId, utterance };
    if (sttInferenceId) body.stt_inference_id = sttInferenceId;
    const response = await request('POST', '/v1/plan', body);
    const payload = response.payload;
    if (!payload.ok) {
      return {
        ok: false,
        reasonCode: payload.reason_code || null,
        detail: payload.detail || payload.error || '계획을 만들지 못했다',
        clarification: payload.clarification || null,
        // 차단된 요청도 **무엇을 하려 했는지**와 **무엇이 부족한지**를 보여준다.
        // 화면이 detail 문자열을 파싱하지 않게 서버가 구조로 준다.
        slots: payload.slots || null,
        draftSteps: payload.draft_steps || [],
        blocked: payload.blocked || null,
        // pick/place 계획 **사전 검증** 결과(8-10). 통과한 항목과 걸린
        // 항목을 그대로 보여준다. 이것은 실행 허가가 아니다.
        planValidation: payload.plan_validation || null,
      };
    }
    const bundle = payload.payload || payload;
    this.lastPlanBundle = bundle;
    this.approvalId = null;
    const latch = payload.stop_latch || {};
    return {
      ok: true,
      plan: planFromBundle(bundle),
      validation: validationFromBundle(bundle),
      stopLatchCleared: latch.cleared !== false,
      stopLatchDetail: latch.detail || '',
    };
  }

  async startExecution() {
    const bundle = this.lastPlanBundle;
    if (!bundle) return { ok: false, reasonCode: 'config.missing', detail: '계획이 없다' };
    const plan = bundle.plan || {};
    // 사용자가 누른 "실행 시작"이 서버의 결정 기록과 실행을 함께 만든다.
    const decision = await request('POST', '/v1/decision', {
      session_id: this.sessionId,
      request_id: bundle.request_id,
      plan_id: plan.plan_id,
      plan_hash: plan.plan_hash,
      decision: 'approve',
      note: '화면에서 실행 시작을 눌렀다',
    });
    if (!decision.ok || !decision.payload.ok) {
      return {
        ok: false,
        reasonCode: decision.payload.reason_code || null,
        detail: decision.payload.detail || decision.payload.error || '실행 결정을 기록할 수 없다',
      };
    }
    this.approvalId = decision.payload.approval_id;

    const execution = await request('POST', '/v1/execute', {
      session_id: this.sessionId,
      request_id: bundle.request_id,
      plan_id: plan.plan_id,
      approval_id: this.approvalId,
    });
    const payload = execution.payload;
    if (!payload.execution_id) {
      return {
        ok: false,
        reasonCode: payload.reason_code || null,
        detail: payload.detail || payload.error || '실행이 시작되지 않았다',
      };
    }
    return {
      ok: true,
      executionId: payload.execution_id,
      total: (plan.steps || []).length,
      final: payload.final || null,
      interrupted: payload.interrupted || null,
      steps: payload.steps || [],
    };
  }

  async cancelExecution(executionId) {
    const response = await request('POST', `/v1/executions/${executionId}/cancel`, {
      session_id: this.sessionId,
    });
    return {
      ok: Boolean(response.payload.ok),
      requested: true,
      reasonCode: response.payload.reason_code || null,
      detail: response.payload.detail || '',
    };
  }

  async stopAll() {
    const response = await request('POST', '/v1/stop', { session_id: this.sessionId });
    const payload = response.payload;
    // 전체 정지는 응답에서도 결과를 확인한다(이벤트가 늦게 올 수 있다).
    if (payload && payload.requested) {
      this.emit({
        kind: 'stop-result',
        record: {
          scope: 'global',
          requestTime: nowTime(),
          confirmedTime: payload.confirmed ? nowTime() : null,
          confirmed: Boolean(payload.confirmed),
          reasonCode: payload.reason_code || 'exec.stopped',
          result: payload.confirmed ? 'confirmed' : 'unconfirmed',
          affectedExecutionCount: payload.affected_execution_count || 0,
          yourExecutionIds: payload.your_execution_ids || [],
          detail: payload.detail || '',
        },
      });
    }
    return { ok: Boolean(payload.ok), requested: Boolean(payload.requested) };
  }

  // ── STT ─────────────────────────────────────────────────────────────
  async startVoice(onTranscript) {
    const audio = (this.config && this.config.audio) || {};
    if (!audio.sample_rate_hz) {
      return { ok: false, detail: '서버가 오디오 계약(sample rate)을 주지 않았다' };
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      return { ok: false, detail: `마이크를 쓸 수 없다: ${error.message}` };
    }
    const context = new AudioContext({ sampleRate: audio.sample_rate_hz });
    if (Math.round(context.sampleRate) !== Math.round(audio.sample_rate_hz)) {
      stream.getTracks().forEach((track) => track.stop());
      await context.close();
      return {
        ok: false,
        detail: `브라우저가 ${audio.sample_rate_hz} Hz를 주지 않았다 (${context.sampleRate} Hz). 리샘플링하지 않는다.`,
      };
    }
    await context.audioWorklet.addModule('/static/pcm-worklet.js');
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(
      `${scheme}//${location.host}/v1/stt?session_id=${encodeURIComponent(this.sessionId)}`,
    );
    socket.binaryType = 'arraybuffer';
    socket.onmessage = (message) => {
      let event = null;
      try {
        event = JSON.parse(message.data);
      } catch {
        return;
      }
      onTranscript(event);
    };
    const node = new AudioWorkletNode(context, 'pcm-frame-processor');
    node.port.onmessage = (message) => {
      if (socket.readyState === WebSocket.OPEN) socket.send(message.data);
    };
    context.createMediaStreamSource(stream).connect(node);
    this.stt = { socket, context, node, stream };
    return { ok: true };
  }

  async stopVoice() {
    const { socket, context, stream } = this.stt;
    if (socket && socket.readyState === WebSocket.OPEN) {
      // 남은 오디오를 확정시키고(flush) 닫는다.
      socket.send(JSON.stringify({ type: 'flush' }));
      socket.send(JSON.stringify({ type: 'close' }));
    }
    if (stream) stream.getTracks().forEach((track) => track.stop());
    if (context) await context.close();
    this.stt = { socket: null, context: null, node: null, stream: null };
    return { ok: true };
  }
}
