import { useEffect, useRef, useState } from 'react';

// forstick2 백엔드 읽기 API. 개발 서버(vite.config.js)가 읽기 경로와 해석 미리보기만 넘긴다.
// **값을 만들어 채우지 않는다** — 받지 못한 값은 null이고, 화면은 '미연동'으로 그린다.

async function getJson(path) {
  const started = performance.now();
  const response = await fetch(path, { cache: 'no-store' });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || body.detail || `HTTP ${response.status}`);
  return { body, rttMs: Math.round(performance.now() - started) };
}

/** 주기적으로 GET한다. { data, error, at, rttMs }를 돌려준다. 탭이 안 보이면 쉬다. */
export function usePoll(path, intervalMs) {
  const [state, setState] = useState({ data: null, error: null, at: null, rttMs: null });
  const timer = useRef(null);
  useEffect(() => {
    let disposed = false;
    async function tick() {
      if (document.visibilityState === 'visible') {
        try {
          const { body, rttMs } = await getJson(path);
          if (!disposed) setState({ data: body, error: null, at: Date.now(), rttMs });
        } catch (error) {
          if (!disposed) setState((prev) => ({ ...prev, error: error.message, at: Date.now() }));
        }
      }
      if (!disposed) timer.current = setTimeout(tick, intervalMs);
    }
    tick();
    return () => { disposed = true; clearTimeout(timer.current); };
  }, [path, intervalMs]);
  return state;
}

export const MATERIAL_KO = { material_a: 'A자재', material_b: 'B자재', material_c: 'C자재' };

export function materialName(model, materials) {
  const found = (materials || []).find((m) => m.model === model);
  return found?.korean || MATERIAL_KO[model] || model || '—';
}

/** epoch 초 또는 ISO 문자열 → 'MM.DD HH:MM:SS'. */
export function stamp(value) {
  if (value == null) return '—';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  const p = (n) => String(n).padStart(2, '0');
  return `${p(date.getMonth() + 1)}.${p(date.getDate())} ${p(date.getHours())}:${p(date.getMinutes())}:${p(date.getSeconds())}`;
}

export function ago(value) {
  if (value == null) return '';
  const ms = Date.now() - (typeof value === 'number' ? value * 1000 : new Date(value).getTime());
  if (Number.isNaN(ms)) return '';
  const min = Math.floor(ms / 60000);
  if (min < 1) return '방금';
  if (min < 60) return `${min}분 전`;
  const h = Math.floor(min / 60);
  return h < 24 ? `${h}시간 전` : `${Math.floor(h / 24)}일 전`;
}

/** 판정 → 화면 톤·문구. 실제 명령 결과를 그린다(서버가 정한 판정을 옮길 뿐이다). */
export const DECISION = {
  RUN: { tone: 'info', label: '실행 (RUN)', text: '서버가 작업을 시작했습니다' },
  CONFIRM: { tone: 'warn', label: '확인 필요 (CONFIRM)', text: '확인을 눌러야 실행됩니다' },
  CONFIRM_GOAL: { tone: 'warn', label: '목표 확인 필요', text: '확인을 눌러야 순서대로 실행됩니다' },
  ASK: { tone: 'warn', label: '되묻기 (ASK)', text: '실행하지 않았습니다' },
  BLOCK: { tone: 'danger', label: '차단 (BLOCK)', text: '실행하지 않았습니다' },
  STOP: { tone: 'danger', label: '정지 (STOP)', text: '정지를 요청했습니다(확인 없이)' },
  NOOP: { tone: 'ok', label: '할 일 없음 (NOOP)', text: '이미 그 상태입니다' },
  ENVIRONMENT: { tone: 'info', label: '환경 정보', text: '셀 작업 환경을 바꿨습니다' },
  CANCELLED: { tone: 'idle', label: '취소', text: '작업을 만들지 않았습니다' },
  PASS_THROUGH: { tone: 'idle', label: '시연 명령 아님', text: '자유 계획 생성은 이 화면에서 하지 않습니다(테스트 화면 8443)' },
};

/** 서버 intent 값 → 한글. 모르는 값은 그대로 둔다. */
export const INTENT_KO = {
  transfer: '이송', return: '원래 자리로 복귀', move_slot: '컨베이어 칸 이동', environment: '작업 환경 변경',
  stop: '정지', resume: '이어하기', arrange: '목표 배치', return_all: '전체 복귀', status: '상태 조회',
};

/** 시연 작업 결과 상태 → 톤. */
export function jobTone(job) {
  if (!job) return 'idle';
  if (job.stop_requested || /stopped/.test(job.status || '')) return 'danger';
  if ((job.reason_codes || []).length || /not_started|failed|error/.test(job.status || '')) return 'warn';
  if (job.completed || /completed|returned|arrived|moved/.test(job.status || '')) return 'ok';
  return 'info';
}

// ── 명령(제어) ────────────────────────────────────────────────────────
// 시뮬레이션 명령·확인·정지·음성. 모든 결과는 Gazebo 시뮬레이션이다(is_simulated).

async function post(path, body) {
  const response = await fetch(path, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body || {}),
  });
  const payload = await response.json().catch(() => ({}));
  return { status: response.status, ok: response.ok, ...payload };
}

const SESSION_KEY = 'forstick2.dashboard2.session_id';

/** 서버가 발급한 세션. 탭마다 하나(sessionStorage). 인증이 아니다 — 대화 맥락·확인 카드를 묶는다. */
let sessionPromise = null;
export function ensureSession() {
  // 한 페이지에서 한 번만 발급한다(React 개발 모드의 이중 마운트에도).
  sessionPromise ||= claimOrCreateSession().catch((e) => { sessionPromise = null; throw e; });
  return sessionPromise;
}

/** 세션이 만료(1시간 유휴)·종료되면 새로 받는다. */
export function renewSession() {
  try { sessionStorage.removeItem(SESSION_KEY); } catch { /* 저장소 없음 */ }
  sessionPromise = null;
  return ensureSession();
}

/** 서버가 세션을 거절한 응답인가(없음 404 · 만료/종료 409, 문구에 '세션'). */
export function isSessionRejected(r) {
  return (r.status === 404 || r.status === 409) && /세션/.test(r.detail || r.error || '');
}

async function claimOrCreateSession() {
  let stored = null;
  try { stored = sessionStorage.getItem(SESSION_KEY); } catch { /* 저장소 없음 */ }
  if (stored) {
    const claimed = await post(`/v1/sessions/${encodeURIComponent(stored)}/clients`, {});
    if (claimed.ok) return stored;
  }
  const created = await post('/v1/sessions', { origin: 'dashboard2' });
  if (!created.ok || !created.session_id) throw new Error(created.detail || created.error || `세션 발급 실패 HTTP ${created.status}`);
  try { sessionStorage.setItem(SESSION_KEY, created.session_id); } catch { /* 저장소 없음 */ }
  return created.session_id;
}

/** 로봇별 명령 경로. FR3와 G1은 대화 맥락·확인 토큰을 공유하지 않는다(서버 설계). */
export const COMMANDS = {
  fr3: {
    command: (sid, utterance, source, stt = {}) => post('/v1/sim-demo/command', {
      mode: 'simulation_demo', utterance, source, session_id: sid,
      ...(source === 'stt_final' ? { raw_transcript: stt.rawText, stt_confidence: stt.confidence } : {}),
    }),
    confirm: (sid, pending, action) => (pending.kind === 'goal'
      ? post(`/v1/sim-demo/goals/${encodeURIComponent(pending.goal_id)}/confirm`, { action })
      : post('/v1/sim-demo/confirm', { token: pending.token, action })),
    stop: () => post('/v1/sim-demo/stop', {}),
  },
  g1: {
    command: (sid, utterance, source) => post('/v1/humanoid/command', { session_id: sid, utterance, source }),
    confirm: (sid, pending, action) => post('/v1/humanoid/confirm', { session_id: sid, token: pending.token, action }),
    stop: (sid) => post('/v1/humanoid/stop', { session_id: sid, reason: '정지 버튼(dashboard2)' }),
  },
};

/** 전체 정지 — FR3(/v1/stop: 범용 실행 + 시연 작업)와 G1을 함께 멈춘다. 확인을 묻지 않는다. */
export async function stopAll(sid, g1Enabled) {
  const [fr3, g1] = await Promise.all([
    post('/v1/stop', { session_id: sid }).catch((e) => ({ ok: false, error: e.message })),
    g1Enabled ? COMMANDS.g1.stop(sid).catch((e) => ({ ok: false, error: e.message })) : null,
  ]);
  return { fr3, g1 };
}

/** 정지 래치 해제. 서버가 released:true로 답할 때만 풀린 것이다. */
export function releaseStop(sid) {
  return post('/v1/stop/release', { session_id: sid });
}

// ── 음성 입력 ──────────────────────────────────────────────────────────
// 8443과 같은 계약: 서버가 정한 sample rate로 PCM16 프레임을 /v1/stt WebSocket에 보낸다.
// 리샘플링하지 않는다. partial은 표시만, final만 명령으로 보낸다.

/** 마이크를 켜고 전사 이벤트를 onEvent로 넘긴다. 돌려준 release()로 마이크를 놓는다. */
export async function startVoice(sid, sampleRateHz, onEvent, onReady) {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    throw new Error('https 주소에서만 마이크를 쓸 수 있습니다');
  }
  if (!sampleRateHz) throw new Error('서버가 오디오 계약(sample rate)을 주지 않았습니다');
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const context = new AudioContext({ sampleRate: sampleRateHz });
  if (Math.round(context.sampleRate) !== Math.round(sampleRateHz)) {
    stream.getTracks().forEach((t) => t.stop());
    await context.close();
    throw new Error(`브라우저가 ${sampleRateHz} Hz를 주지 않았습니다 (${context.sampleRate} Hz)`);
  }
  await context.audioWorklet.addModule('/static/pcm-worklet.js');
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${scheme}//${location.host}/v1/stt?session_id=${encodeURIComponent(sid)}`);
  socket.binaryType = 'arraybuffer';
  socket.onopen = () => {
    try { socket.send(JSON.stringify({ type: 'client_info', user_agent: navigator.userAgent, context_sample_rate: context.sampleRate, client: 'dashboard2' })); } catch { /* 닫힘 */ }
    onReady?.();
  };
  socket.onmessage = (message) => {
    try { onEvent(JSON.parse(message.data)); } catch { /* JSON 아님 */ }
  };
  socket.onerror = () => onEvent({ kind: 'error', reason_code: 'stt.socket', detail: '음성 연결 오류' });
  const node = new AudioWorkletNode(context, 'pcm-frame-processor');
  node.port.onmessage = (m) => { if (socket.readyState === WebSocket.OPEN) socket.send(m.data); };
  context.createMediaStreamSource(stream).connect(node);

  let released = false;
  /** flush=true: 남은 오디오를 확정시키고 닫는다(사용자가 끔). false: 서버가 이미 확정했다. */
  async function release(flush = false) {
    if (released) return;
    released = true;
    node.port.onmessage = null;
    stream.getTracks().forEach((t) => t.stop());
    if (socket.readyState === WebSocket.OPEN) {
      try {
        if (flush) socket.send(JSON.stringify({ type: 'flush' }));
        socket.send(JSON.stringify({ type: 'close' }));
      } catch { /* 이미 닫힘 */ }
    }
    if (context.state !== 'closed') await context.close().catch(() => {});
  }
  return { release };
}
