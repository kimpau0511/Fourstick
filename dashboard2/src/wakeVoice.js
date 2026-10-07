import { useCallback, useEffect, useRef, useState } from 'react';

// 호출어 음성 명령(2026-10-07). 마이크 버튼은 켜고 끄는 스위치다.
//   OFF ─켜기→ WAKE_WORD_WAITING ─"{이름}야"→ (TTS '네, 말씀하세요.') → LISTENING → TRANSCRIBING → ANALYZING
//   → CONFIRMING(확인 카드의 실행·취소) → EXECUTING → 다시 WAKE_WORD_WAITING.  ERROR·STOPPED도 화면에 보인다.
// 계획 음성 안내 → 음성 승인(2026-10-07): 확인 카드가 뜨면 작업 요약을 읽고 "이대로 진행할까요?"를 묻는다.
//   안내가 끝난 **뒤의 새 발화만**(서버 발화 세대 epoch로 구분) 승인 응답으로 쓴다. 긍정 → 화면 '실행 승인'과 같은
//   sim.answer('confirm'), 부정 → sim.answer('cancel'), 섞였거나 불명확 → 다시 묻는다. 무응답은 카드 만료로 취소(자동 실행 없음).
//   승인은 지금 떠 있는 그 카드(pendingKey)에만, 한 번만. 정지어는 늘 먼저. 안내 음성 중에는 마이크를 보내지 않는다.
// 서버 STT 프로토콜(server/routes/stt.py):
//   호출어 대기 = /v1/stt?mode=wake — 계속 듣고, 발화마다 partial/final을 준다(요청으로 저장하지 않음).
//   명령 = /v1/stt(일반 모드) — 발화 하나를 final로 확정하고 요청으로 남긴다. 그 뒤 기존 명령 해석(sim.send)으로 보낸다.
// 마이크는 켜져 있는 동안 하나만 연다. 프레임은 지금 듣는 소켓 하나로만 보내고, TTS 중에는 보내지 않는다
// (시스템 음성이 다시 인식되지 않게). 긴급 정지 말('정지'·'멈춰'·'긴급 정지')은 호출어 없이 partial에서도 바로 처리한다.

export const VOICE_STATES = ['OFF', 'WAKE_WORD_WAITING', 'LISTENING', 'TRANSCRIBING', 'ANALYZING', 'CONFIRMING', 'EXECUTING', 'ERROR', 'STOPPED'];
export const DEFAULT_ROBOT_NAME = '지니';
const SESSION_KEY = 'forstick2.voice.session';
const STOP_WORDS = ['긴급정지', '정지', '멈춰'];
/** 호출어 뒤 말이 시작되기를 기다리는 시간(5~10초 안). */
export const COMMAND_START_SEC = 8;
/** 말을 시작했어도 이 시간이 지나면 지금까지로 확정한다. */
export const COMMAND_MAX_SEC = 10;
const ERROR_HOLD_MS = 2500;
/** 마이크 소리를 늘 이만큼 기억한다(호출어를 늦게 알아챈 사이 시작한 말을 잃지 않게). */
const RING_MS = 4000;
/** 중간 전사로 호출어를 받았을 때 명령 소켓에 앞붙일 소리 길이 — 호출어를 알아채기 전에 시작한 말. */
export const PREBUFFER_MS = 2500;
/** 중간 전사가 호출어만 담고 이만큼 바뀌지 않으면 바로 호출어로 본다 — 발화 끝(서버 무음 판정, 정책 3초)을 기다리지 않는다. */
export const WAKE_PARTIAL_HOLD_MS = 1200;
const REPLY = '네, 말씀하세요.';
/** 안내 음성이 끝난 뒤 스피커 잔향이 마이크에 남는 시간 — 이 동안은 듣지 않는다. */
export const POST_TTS_GUARD_MS = 400;
export const SAY = {
  heard: '요청하신 명령을 확인했습니다.',
  plan: (summary) => `다음과 같이 작업을 진행하려고 합니다. ${summary} 이대로 작업을 진행할까요?`,
  again: '작업을 진행할지 취소할지 다시 말씀해 주세요.',
  start: '네, 작업을 시작하겠습니다.',
  cancel: '알겠습니다. 작업을 취소하겠습니다.',
  expired: '확인 시간이 지나 작업을 취소했습니다.',
  rejected: (why) => `작업을 시작하지 못했습니다. ${why}`,
  ask: (why) => `추가 확인이 필요합니다. ${why}`,
  block: (why) => `작업을 진행할 수 없습니다. ${why}`,
  stale: '확인 카드가 바뀌었거나 만료되어 음성 승인을 적용하지 않았습니다.',
};
const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });
const shortWhy = (why) => (why ? String(why).replace(/\s+/g, ' ').slice(0, 120) : '사유를 받지 못했습니다.');

// ── 승인 응답 분류: 긍정·부정 낱말이 둘 다 있으면 불명확(다시 묻는다). '해줘'처럼 홀로 뜻이 없는 말은 세지 않는다. ──
const YES = ['네', '예', '응', '그래', '진행', '좋아', '좋습니다', '시작', '실행', '오케이', '하자', '부탁', '맞아'];
const NO = ['아니', '아뇨', '취소', '하지마', '하지말', '그만', '안해', '안할', '안돼', '싫어', '말아', '됐어'];
/** 'approve' | 'cancel' | 'unclear' */
export function classifyAnswer(text) {
  const compact = hangul(text);
  if (!compact) return 'unclear';
  const no = NO.some((w) => compact.includes(w));
  const yes = YES.some((w) => compact.includes(w));
  if (yes && no) return 'unclear';
  return yes ? 'approve' : no ? 'cancel' : 'unclear';
}

// ── 호출어 맞추기: 한글 자모로 풀어 1글자(자모 1개) 차이까지 같은 말로 본다(지니야 ≈ 진이야 ≈ 지니아). ──
const CHO = 'ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ';
const JUNG = 'ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ';
const JONG = ['', 'ㄱ', 'ㄲ', 'ㄳ', 'ㄴ', 'ㄵ', 'ㄶ', 'ㄷ', 'ㄹ', 'ㄺ', 'ㄻ', 'ㄼ', 'ㄽ', 'ㄾ', 'ㄿ', 'ㅀ', 'ㅁ', 'ㅂ', 'ㅄ', 'ㅅ', 'ㅆ', 'ㅇ', 'ㅈ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ'];
function jamo(text) {
  let out = '';
  for (const ch of text) {
    const c = ch.charCodeAt(0) - 0xac00;
    if (c < 0 || c > 11171) { out += ch; continue; }
    out += CHO[Math.floor(c / 588)] + JUNG[Math.floor((c % 588) / 28)] + JONG[c % 28];
  }
  return out;
}
function distance(a, b) {
  const row = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i += 1) {
    let prev = row[0];
    row[0] = i;
    for (let j = 1; j <= b.length; j += 1) {
      const keep = row[j];
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, prev + (a[i - 1] === b[j - 1] ? 0 : 1));
      prev = keep;
    }
  }
  return row[b.length];
}
const hangul = (text) => (text || '').replace(/[^가-힣]/g, '');
export const wakeWordOf = (name) => `${name || DEFAULT_ROBOT_NAME}야`;

/** 문장에서 호출어를 찾는다 → 없으면 null, 있으면 { rest: 호출어 뒤 말 }. maxStart: 호출어가 시작할 수 있는 최대 글자 위치. */
export function matchWake(text, name, { maxStart = Infinity } = {}) {
  const words = (text || '').trim();
  const target = jamo(wakeWordOf(name));
  const compact = hangul(words);
  const n = hangul(wakeWordOf(name)).length;
  for (let len = n - 1; len <= n + 1; len += 1) {
    for (let i = 0; i + len <= compact.length && i <= maxStart; i += 1) {
      if (len < 2 || distance(jamo(compact.slice(i, i + len)), target) > 1) continue;
      // 호출어 뒤 말: 원문에서 그 글자들이 끝나는 자리 뒤
      let seen = 0;
      let cut = words.length;
      for (let k = 0; k < words.length; k += 1) {
        if (/[가-힣]/.test(words[k])) { seen += 1; if (seen === i + len) { cut = k + 1; break; } }
      }
      return { rest: words.slice(cut).replace(/^[\s,.!?~]+/, '').trim() };
    }
  }
  return null;
}

/**
 * 명령 소켓의 확정 문장 → 명령. 호출어 직후·안내 중 소리도 명령 소켓에 넣으므로(앞부분 잘림 방지) 문장 앞에
 * 호출어("지니야")와 안내 음성의 되울림("네, 말씀하세요")이 붙을 수 있다 — 앞에서만 떼어 낸다. 남는 말이 없으면 ''.
 */
export function commandText(text, name) {
  let t = (text || '').trim();
  const hit = matchWake(t, name, { maxStart: 3 });
  if (hit) t = hit.rest;
  t = t.replace(/^(네[,.!?\s]+)?(말씀\s*하세요[,.!?\s]*)?/, '').trim();
  return hangul(t).length >= 2 ? t : '';
}

/** 긴급 정지 말이 들어 있는가(호출어 없이). 서버 STT 정책의 정지어도 함께 본다. */
export function isStopWord(text, extra = []) {
  const compact = hangul(text);
  return !!compact && [...STOP_WORDS, ...extra.map(hangul)].some((w) => w && compact.includes(w));
}

async function json(method, path, body) {
  const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  let payload = {};
  try { payload = await r.json(); } catch { /* JSON 아님 */ }
  return { ok: r.ok, status: r.status, payload };
}
async function ensureSession() {
  let stored = null;
  try { stored = sessionStorage.getItem(SESSION_KEY); } catch { /* 저장소 차단 */ }
  if (stored && (await json('POST', `/v1/sessions/${encodeURIComponent(stored)}/clients`)).ok) return stored;
  const created = await json('POST', '/v1/sessions', { origin: 'web-console' });
  if (!created.ok || !created.payload.session_id) throw new Error('음성 세션을 만들지 못했습니다');
  try { sessionStorage.setItem(SESSION_KEY, created.payload.session_id); } catch { /* 저장소 차단 */ }
  return created.payload.session_id;
}
function micError(error) {
  if (error && (error.name === 'NotAllowedError' || error.name === 'SecurityError')) return '마이크 권한이 거부되었습니다 — 브라우저 주소창의 권한 설정에서 허용해 주세요';
  if (error && error.name === 'NotFoundError') return '사용할 수 있는 마이크가 없습니다';
  return `마이크를 쓸 수 없습니다: ${(error && error.message) || error}`;
}

/** TTS. 끝나거나(실패해도) 문장 길이에 맞춘 최대 시간 뒤에 resolve — 음성이 없는 브라우저에서도 흐름이 멈추지 않는다. */
function speak(text, rt) {
  return new Promise((resolve) => {
    const synth = typeof window !== 'undefined' ? window.speechSynthesis : null;
    let done = false;
    const finish = () => { if (!done) { done = true; clearTimeout(rt.ttsTimer); resolve(); } };
    rt.ttsTimer = setTimeout(finish, Math.min(20000, 1500 + text.length * 180));
    if (!synth || typeof SpeechSynthesisUtterance === 'undefined') { setTimeout(finish, 300); return; }
    try {
      synth.cancel();
      const u = new SpeechSynthesisUtterance(text);
      u.lang = 'ko-KR';
      u.onend = finish;
      u.onerror = finish;
      synth.speak(u);
    } catch { finish(); }
  });
}

/**
 * name: 로봇 이름. flow: 명령 흐름({ busy, phase, result, error }) — 분석·확인·실행 단계를 따라간다.
 * onCommand(text, stt): 명령 해석 요청(기존 sim.send). onStop(): 긴급 정지(헤더와 같은 전체 정지).
 */
export function useWakeVoice({ config, health, name, flow, onCommand, onStop, onAnswer }) {
  const [state, setState] = useState('OFF');
  const [heard, setHeard] = useState('');        // 인식된 명령 원문
  const [partial, setPartial] = useState('');
  const [note, setNote] = useState(null);        // { tone, text } — 오류·시간 초과·취소 등
  const [speaking, setSpeaking] = useState(false);   // 안내 음성 재생 중(이때 마이크는 보내지 않는다)
  const [approval, setApproval] = useState(null);    // 음성 승인 단계: speaking | listening | processing
  const [answer, setAnswer] = useState('');          // 들은 승인 응답
  const rt = useRef(null);
  const stateRef = useRef('OFF');
  const nameRef = useRef(name);
  const cbRef = useRef({ onCommand, onStop, onAnswer });
  const flowRef = useRef(flow || {});
  useEffect(() => { nameRef.current = name; cbRef.current = { onCommand, onStop, onAnswer }; flowRef.current = flow || {}; });
  const stopWordsRef = useRef([]);
  stopWordsRef.current = (config && config.policies && config.policies.stt && config.policies.stt.stop_keywords) || [];

  const feature = (config && config.features && config.features.stt) || (health && health.features && health.features.stt) || null;
  const unavailable = feature && feature.available === false
    ? `음성 인식을 쓸 수 없습니다${feature.detail ? ` — ${feature.detail}` : ''}` : null;

  const go = useCallback((next) => { stateRef.current = next; setState(next); }, []);
  const startRef = useRef(null);

  const closeSocket = (socket, message) => {
    if (!socket) return;
    socket.onmessage = null; socket.onclose = null;
    if (socket.readyState === WebSocket.OPEN) { try { socket.send(JSON.stringify({ type: message })); } catch { /* 닫힘 */ } }
    try { socket.close(); } catch { /* 이미 닫힘 */ }
  };
  /** 마이크 프레임을 보낼 곳: 명령을 듣는 중이면 명령 소켓, 아니면 호출어 대기 소켓. 안내 음성 중에는 없음. */
  const listenTarget = (r) => (r.speaking > 0 ? null
    : ['LISTENING', 'TRANSCRIBING'].includes(stateRef.current) && r.cmd ? r.cmd : r.wake);
  /** 호출어 대기 소켓의 지금 발화를 버린다 — 서버가 발화 세대(epoch)를 올린다. */
  const abortWake = (r) => {
    if (r.wake && r.wake.readyState === WebSocket.OPEN) { r.wake.send(JSON.stringify({ type: 'abort' })); r.epoch += 1; }
  };
  /** 안내 음성(차례대로). 재생 중에는 마이크를 보내지 않고, 끝나면 그 사이 발화를 버린 뒤 잔향 시간 뒤에 다시 듣는다. */
  const say = useCallback((r, text) => {
    const gen = r.speechGen;
    r.speech = (r.speech || Promise.resolve()).then(async () => {
      if (rt.current !== r || r.speechGen !== gen) return;
      r.speaking += 1; setSpeaking(true); r.route = null;
      await speak(text, r);
      if (r.speechGen !== gen) return;               // 정지·끄기가 안내를 끊었다 — 카운트는 그쪽에서 0으로 맞췄다
      r.speaking -= 1;
      if (r.speaking > 0 || rt.current !== r) return;
      setSpeaking(false);
      abortWake(r);
      await sleep(POST_TTS_GUARD_MS);
      if (rt.current !== r || r.speaking > 0) return;
      r.route = listenTarget(r);
    });
    return r.speech;
  }, []);  
  const clearTimers = (r) => { ['cmdTimer', 'cmdMaxTimer', 'backTimer', 'ttsTimer', 'wakeTimer'].forEach((k) => { clearTimeout(r[k]); r[k] = null; }); };

  /** 전부 정리(OFF). 녹음·호출어 감지·대기 타이머·TTS를 모두 멈춘다. */
  const off = useCallback(() => {
    const r = rt.current;
    rt.current = null;
    if (r) {
      clearTimers(r);
      r.route = null;
      if (r.node) r.node.port.onmessage = null;
      closeSocket(r.cmd, 'abort');
      closeSocket(r.wake, 'close');
      if (r.stream) r.stream.getTracks().forEach((t) => t.stop());
      if (r.context && r.context.state !== 'closed') r.context.close().catch(() => {});
    }
    if (r) { r.speechGen += 1; r.speaking = 0; r.appr = null; r.capture = null; }
    try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch { /* 없음 */ }
    setPartial(''); setHeard(''); setSpeaking(false); setApproval(null); setAnswer('');
    go('OFF');
  }, [go]);

  const toWaiting = useCallback((r, message = null) => {
    if (!r || rt.current !== r) return;
    clearTimers(r);
    closeSocket(r.cmd, 'abort'); r.cmd = null;
    r.appr = null; setApproval(null); r.capture = null;
    if (!r.speaking) r.route = r.wake;
    setPartial('');
    if (message) setNote(message);
    go('WAKE_WORD_WAITING');
  }, [go]);

  const fail = useCallback((r, text) => {
    if (!r || rt.current !== r) return;
    clearTimers(r);
    closeSocket(r.cmd, 'abort'); r.cmd = null;
    r.appr = null; setApproval(null); r.capture = null;
    if (!r.speaking) r.route = r.wake;
    setPartial('');
    setNote({ tone: 'danger', text });
    go('ERROR');
    r.backTimer = setTimeout(() => { if (rt.current === r && stateRef.current === 'ERROR') go('WAKE_WORD_WAITING'); }, ERROR_HOLD_MS);
  }, [go]);

  const emergencyStop = useCallback((r, text) => {
    if (!r || rt.current !== r) return;
    clearTimers(r);
    closeSocket(r.cmd, 'abort'); r.cmd = null;
    // 정지가 먼저다: 남은 안내 음성·승인 대기를 모두 버린다.
    r.speechGen += 1; r.speaking = 0; setSpeaking(false);
    r.appr = null; setApproval(null); r.capture = null;
    try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch { /* 없음 */ }
    r.route = r.wake;
    setPartial('');
    setNote({ tone: 'danger', text: `긴급 정지를 요청했습니다(“${text}”)` });
    go('STOPPED');
    cbRef.current.onStop?.();
  }, [go]);

  const openSocket = (r, mode) => {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const q = `session_id=${encodeURIComponent(r.sessionId)}${mode === 'wake' ? '&mode=wake' : ''}`;
    const socket = new WebSocket(`${scheme}//${location.host}/v1/stt?${q}`);
    socket.binaryType = 'arraybuffer';
    return socket;
  };

  // 호출어 대기 소켓: 켜져 있는 동안 계속. 정지어는 어느 단계에서든, 호출어는 대기·오류 단계에서만 받는다.
  const onWakeEvent = useCallback((r, ev) => {
    if (rt.current !== r) return;
    const text = ev.text || ev.raw_text || '';
    if (!text) return;
    if ((ev.kind === 'partial' || ev.kind === 'final' || ev.kind === 'clarify') && isStopWord(text, stopWordsRef.current)) {
      emergencyStop(r, text);
      return;
    }
    if (stateRef.current === 'CONFIRMING') {   // 음성 승인: 안내가 끝난 뒤의 새 발화(세대)만
      const a = r.appr;
      if (!a || a.stage !== 'listening' || (ev.kind !== 'final' && ev.kind !== 'clarify')) return;
      if (typeof ev.epoch !== 'number' || ev.epoch < a.epoch) return;
      let verdict = classifyAnswer(text);
      // 신뢰도가 낮은 말(clarify)로는 실행하지 않는다 — 취소는 받는다(안전한 쪽).
      if (ev.kind === 'clarify' && verdict === 'approve') verdict = 'unclear';
      setAnswer(text);
      if (verdict === 'unclear') {
        a.stage = 'speaking'; setApproval('speaking');
        say(r, SAY.again).then(() => {
          if (rt.current === r && r.appr === a && stateRef.current === 'CONFIRMING') { a.stage = 'listening'; a.epoch = r.epoch; setApproval('listening'); }
        });
        return;
      }
      const f = flowRef.current;
      if (f.pendingKey !== a.key || f.expired || f.busy) {
        a.stage = 'done'; setApproval(null);
        say(r, SAY.stale);
        return;
      }
      a.stage = 'processing'; setApproval('processing');
      cbRef.current.onAnswer?.(verdict === 'approve' ? 'confirm' : 'cancel', a.key);
      return;
    }
    if (!['WAKE_WORD_WAITING', 'ERROR'].includes(stateRef.current)) return;   // 실행·확인 중에는 새 명령을 받지 않는다
    clearTimeout(r.wakeTimer); r.wakeTimer = null;
    const hit = matchWake(text, nameRef.current);
    if (ev.kind === 'partial') {
      // "지니야"만 들리고 말이 이어지지 않으면 곧바로 받는다. 이어지면(뒤 말이 붙으면) 발화 끝(final)을 기다린다.
      if (hit && !hit.rest) {
        r.wakeTimer = setTimeout(() => {
          if (rt.current !== r || !['WAKE_WORD_WAITING', 'ERROR'].includes(stateRef.current)) return;
          // 서버의 이 발화는 버린다(abort) — 나중에 같은 "지니야" final이 또 오지 않게.
          abortWake(r);
          startRef.current?.(r, '', true);
        }, WAKE_PARTIAL_HOLD_MS);
      }
      return;
    }
    if (ev.kind !== 'final' && ev.kind !== 'clarify') return;
    if (hit) startRef.current?.(r, hit.rest, false);
  }, [emergencyStop, say]);  

  const sendCommand = useCallback((r, text, stt) => {
    setHeard(text);
    setPartial('');
    clearTimers(r);
    closeSocket(r.cmd, 'close'); r.cmd = null;
    if (!r.speaking) r.route = r.wake;
    if (isStopWord(text, stopWordsRef.current)) { emergencyStop(r, text); return; }
    r.sawBusy = false;
    r.sentAt = Date.now();
    setAnswer('');
    go('ANALYZING');
    say(r, SAY.heard);                               // 해석은 기다리지 않고 바로 보낸다
    cbRef.current.onCommand?.(text, stt);
  }, [emergencyStop, go, say]);

  /**
   * 명령 소켓으로 듣는다. withCapture: 그동안 모은 소리(호출어 직전·안내 중)를 먼저 보낸다 — 앞부분 잘림 방지.
   * 확정 문장에서 호출어·안내 되울림을 떼고, 남는 말이 없으면 한 번만 다시 듣는다.
   */
  const listenCommand = useCallback((r, { withCapture }) => {
    if (!withCapture) r.capture = null;
    const socket = openSocket(r, 'command');
    r.cmd = socket;
    socket.onmessage = (message) => {
      if (rt.current !== r || r.cmd !== socket) return;
      let ev;
      try { ev = JSON.parse(message.data); } catch { return; }
      if (ev.kind === 'session') {
        if (r.capture) { r.capture.forEach((frame) => socket.send(frame)); r.capture = null; }
        if (!r.speaking) r.route = socket;
        return;
      }
      if (ev.kind === 'speech_start') { clearTimeout(r.cmdTimer); r.cmdTimer = null; return; }
      if (ev.kind === 'state' && ev.state === 'finalizing') { go('TRANSCRIBING'); return; }
      if (ev.kind === 'partial') {
        setPartial(ev.text || '');
        if (isStopWord(ev.text, stopWordsRef.current)) emergencyStop(r, ev.text);
        return;
      }
      if (ev.kind === 'final') {
        const raw = (ev.text || ev.raw_text || '').trim();
        if (isStopWord(raw, stopWordsRef.current)) { emergencyStop(r, raw); return; }
        const text = commandText(raw, nameRef.current);
        if (!text) {
          if (raw && !r.relistened) {               // "지니야"만 들렸다 — 명령을 한 번 더 듣는다
            r.relistened = true;
            clearTimers(r);
            closeSocket(socket, 'close');
            r.cmd = null;
            go('LISTENING');
            listenCommand(r, { withCapture: false });
            return;
          }
          fail(r, '음성을 알아듣지 못했습니다 — 호출어부터 다시 말씀해 주세요');
          return;
        }
        sendCommand(r, text, { rawText: ev.raw_text || raw, confidence: ev.confidence ?? null });
        return;
      }
      if (ev.kind === 'clarify') { fail(r, '잘 알아듣지 못했습니다 — 호출어부터 다시 말씀해 주세요'); return; }
      if (ev.kind === 'error') {
        fail(r, ev.reason_code === 'stt.no_speech' ? '말소리를 감지하지 못했습니다 — 호출어부터 다시 말씀해 주세요'
          : `음성 인식 오류${ev.detail ? ` — ${ev.detail}` : ''}`);
      }
    };
    socket.onclose = () => { if (rt.current === r && r.cmd === socket) fail(r, '음성 서버와 연결이 끊겼습니다(명령)'); };
    // 제한 시간: 말이 시작되지 않으면 대기로, 말이 길면 지금까지로 확정한다.
    r.cmdTimer = setTimeout(() => {
      if (rt.current === r && r.cmd === socket && stateRef.current === 'LISTENING') toWaiting(r, { tone: 'warn', text: `${COMMAND_START_SEC}초 안에 명령이 없어 호출어 대기로 돌아갑니다` });
    }, COMMAND_START_SEC * 1000);
    r.cmdMaxTimer = setTimeout(() => {
      if (rt.current === r && r.cmd === socket && socket.readyState === WebSocket.OPEN && stateRef.current === 'LISTENING') {
        r.route = null;
        socket.send(JSON.stringify({ type: 'flush' }));
        go('TRANSCRIBING');
      }
    }, COMMAND_MAX_SEC * 1000);
  }, [go, sendCommand, fail, toWaiting, emergencyStop]);  

  /**
   * 호출어를 들었다: '네, 말씀하세요.' → 끝난 뒤 명령 소켓으로 듣는다. 안내 중에는 소켓으로 보내지 않지만 버리지도 않고
   * 모아 두었다가(capture) 명령 소켓에 먼저 넣는다. early(중간 전사로 받음)면 호출어를 알아채기 전 소리도 앞붙인다.
   */
  const startCommand = useCallback(async (r, rest, early = false) => {
    setNote(null); setHeard('');
    r.route = null;
    r.relistened = false;
    go('LISTENING');
    if (rest && rest.length >= 2) {                  // "지니야, A자재 옮겨줘"처럼 한 번에 말했으면 그 뒤 말을 명령으로
      r.capture = null;
      sendCommand(r, rest, { rawText: rest, confidence: null });
      return;
    }
    const now = performance.now();
    r.capture = early ? r.ring.filter(([t]) => now - t <= PREBUFFER_MS).map(([, frame]) => frame) : [];
    await say(r, REPLY);
    if (rt.current !== r || stateRef.current !== 'LISTENING') { r.capture = null; return; }
    listenCommand(r, { withCapture: true });
  }, [go, sendCommand, say, listenCommand]);
  useEffect(() => { startRef.current = startCommand; });

  /** 켜기: 마이크 하나 + 호출어 대기 소켓. */
  const on = useCallback(async () => {
    if (unavailable || rt.current) return;
    const r = { route: null, wake: null, cmd: null, stream: null, context: null, node: null, sessionId: null,
      epoch: 0, speaking: 0, speechGen: 0, speech: null, appr: null, ring: [], capture: null, relistened: false };
    rt.current = r;
    setNote(null); setHeard(''); setPartial('');
    go('WAKE_WORD_WAITING');
    const dead = () => rt.current !== r;
    try {
      const rate = config && config.audio && config.audio.sample_rate_hz;
      if (!rate) throw new Error('서버가 오디오 규격(샘플레이트)을 주지 않았습니다');
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) throw new Error('이 브라우저는 마이크 입력을 지원하지 않습니다');
      r.sessionId = await ensureSession();
      if (dead()) return;
      try { r.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); } catch (e) { throw new Error(micError(e)); }
      if (dead()) { r.stream.getTracks().forEach((t) => t.stop()); return; }
      r.context = new AudioContext({ sampleRate: rate });
      if (Math.round(r.context.sampleRate) !== Math.round(rate)) {
        throw new Error(`브라우저가 ${rate} Hz 오디오를 주지 않습니다(${r.context.sampleRate} Hz). 리샘플링하지 않으므로 음성 입력을 쓸 수 없습니다`);
      }
      await r.context.audioWorklet.addModule(new URL('./pcm-worklet.js', import.meta.url));
      if (dead()) return;
      const wake = openSocket(r, 'wake');
      r.wake = wake;
      wake.onmessage = (message) => {
        let ev;
        try { ev = JSON.parse(message.data); } catch { return; }
        if (ev.kind === 'session') { if (r.route == null && !r.speaking && ['WAKE_WORD_WAITING', 'ERROR', 'STOPPED', 'CONFIRMING', 'EXECUTING', 'ANALYZING'].includes(stateRef.current)) r.route = wake; return; }
        if (ev.kind === 'error' && ev.reason_code && !['stt.no_speech', 'stt.stream_aborted', 'exec.stopped'].includes(ev.reason_code)) {
          setNote({ tone: 'warn', text: `호출어 인식 오류${ev.detail ? ` — ${ev.detail}` : ''}` });
          return;
        }
        onWakeEvent(r, ev);
      };
      wake.onclose = () => {
        if (rt.current !== r) return;
        off();
        setNote({ tone: 'danger', text: '음성 서버와 연결이 끊겨 음성 명령을 껐습니다' });
      };
      const node = new AudioWorkletNode(r.context, 'pcm-frame-processor');
      node.port.onmessage = (m) => {
        // 최근 소리는 늘 기억하고(RING_MS), 명령을 기다리는 동안에는 모은다(capture) — 보낼 곳이 없을 때도 잃지 않게.
        const now = performance.now();
        r.ring.push([now, m.data]);
        while (r.ring.length && now - r.ring[0][0] > RING_MS) r.ring.shift();
        if (r.capture) r.capture.push(m.data);
        const s = r.route;
        if (s && s.readyState === WebSocket.OPEN) s.send(m.data);
      };
      r.context.createMediaStreamSource(r.stream).connect(node);
      r.node = node;
    } catch (e) {
      if (!dead()) { off(); setNote({ tone: 'danger', text: e.message || String(e) }); }
    }
  }, [config, unavailable, go, off, onWakeEvent]);

  /** 확인 카드가 떴다: 작업 요약을 읽고 묻는다. 안내가 끝난 뒤에야 승인 응답을 듣는다. */
  const startApproval = useCallback((r, key, summary) => {
    const a = { key, stage: 'speaking', epoch: Infinity };
    r.appr = a; setApproval('speaking'); setAnswer('');
    say(r, SAY.plan(summary || '작업 계획을 확인해 주세요.')).then(() => {
      if (rt.current === r && r.appr === a && stateRef.current === 'CONFIRMING' && a.stage === 'speaking') {
        a.stage = 'listening'; a.epoch = r.epoch; setApproval('listening');
      }
    });
  }, [say]);

  // 명령 흐름 따라가기: 분석 → 확인(음성 안내·승인) → 실행 → 끝나면 호출어 대기. 결과 안내는 서버 응답을 본 뒤에만 한다.
  const busy = !!(flow && flow.busy);
  const phase = flow ? flow.phase : 'idle';
  const f = flow || {};
  useEffect(() => {
    const r = rt.current;
    if (!r) return;
    const s = stateRef.current;
    if (s === 'ANALYZING') {
      if (busy) { r.sawBusy = true; return; }
      if (!r.sawBusy && phase === 'idle' && Date.now() - (r.sentAt || 0) < 1500) return;   // 아직 보내는 중
      if (phase === 'confirm') { go('CONFIRMING'); startApproval(r, f.pendingKey, f.summary); }
      else if (phase === 'running' || f.accepted) go('EXECUTING');
      else if (phase === 'ask') { say(r, SAY.ask(shortWhy(f.reason))); fail(r, `추가 확인이 필요합니다${f.reason ? ` — ${f.reason}` : ''} — 호출어부터 다시 말씀해 주세요`); }
      else if (phase === 'other') { say(r, SAY.block(shortWhy(f.reason))); fail(r, `명령을 실행할 수 없습니다${f.reason ? ` — ${f.reason}` : ''}`); }
      else if (phase === 'done') toWaiting(r);
      else if (r.sawBusy) fail(r, '명령 해석 결과를 받지 못했습니다');
    } else if (s === 'CONFIRMING') {
      // 승인 응답 사이의 중간 상태로 판단하지 않는다 — 서버가 시작·취소·거부를 알려 준 뒤에만 말하고 옮긴다.
      if (f.accepted || phase === 'done') {
        r.appr = null; setApproval(null); r.capture = null;
        say(r, SAY.start);
        if (phase === 'done') toWaiting(r, { tone: 'muted', text: '작업이 끝났습니다 — 호출어 대기로 돌아갑니다' }); else go('EXECUTING');
      } else if (f.cancelled) {
        say(r, SAY.cancel);
        toWaiting(r, { tone: 'muted', text: '작업을 취소했습니다 — 호출어 대기로 돌아갑니다' });
      } else if (f.rejected) {
        say(r, SAY.rejected(shortWhy(f.reason)));
        fail(r, `작업을 시작하지 못했습니다 — ${f.reason || '사유 없음'}`);
      } else if (f.expired) {
        say(r, SAY.expired);
        toWaiting(r, { tone: 'warn', text: '확인 시간이 지나 실행하지 않았습니다 — 호출어 대기로 돌아갑니다' });
      } else if (phase === 'idle' && !busy) toWaiting(r, { tone: 'muted', text: '실행하지 않았습니다 — 호출어 대기로 돌아갑니다' });
    } else if (s === 'EXECUTING') {
      if (f.rejected) { say(r, SAY.rejected(shortWhy(f.reason))); fail(r, `작업을 시작하지 못했습니다 — ${f.reason || '사유 없음'}`); }
      else if (phase === 'done' || phase === 'other' || phase === 'idle') toWaiting(r, { tone: 'muted', text: '작업이 끝났습니다 — 호출어 대기로 돌아갑니다' });
    }
  }, [busy, phase, f.reason, f.accepted, f.cancelled, f.rejected, f.expired, f.pendingKey, go, fail, toWaiting, say, startApproval]); // eslint-disable-line react-hooks/exhaustive-deps

  const leaveStopped = useCallback(() => { const r = rt.current; if (r && stateRef.current === 'STOPPED') toWaiting(r); }, [toWaiting]);

  useEffect(() => () => off(), []); // eslint-disable-line react-hooks/exhaustive-deps

  const ready = !!(config && config.audio && config.audio.sample_rate_hz);   // 서버 오디오 규격을 받기 전에는 켜지 않는다
  return { state, heard, partial, note, unavailable, ready, speaking, approval, answer, on, off, leaveStopped, wakeWord: wakeWordOf(name) };
}
