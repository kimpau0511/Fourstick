import { useCallback, useEffect, useRef, useState } from 'react';

// 호출어 음성 명령(2026-10-07). 마이크 버튼은 켜고 끄는 스위치다.
//   OFF ─켜기→ WAKE_WORD_WAITING ─"{이름}야"→ (TTS '네, 말씀하세요.') → LISTENING → TRANSCRIBING → ANALYZING
//   → CONFIRMING(확인 카드의 실행·취소) → EXECUTING → 다시 WAKE_WORD_WAITING.  ERROR·STOPPED도 화면에 보인다.
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
/** 중간 전사가 호출어만 담고 이만큼 바뀌지 않으면 바로 호출어로 본다 — 발화 끝(서버 무음 판정, 정책 3초)을 기다리지 않는다. */
export const WAKE_PARTIAL_HOLD_MS = 1200;
const TTS_MAX_MS = 4000;
const REPLY = '네, 말씀하세요.';

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

/** 문장에서 호출어를 찾는다 → 없으면 null, 있으면 { rest: 호출어 뒤 말 }. */
export function matchWake(text, name) {
  const words = (text || '').trim();
  const target = jamo(wakeWordOf(name));
  const compact = hangul(words);
  const n = hangul(wakeWordOf(name)).length;
  for (let len = n - 1; len <= n + 1; len += 1) {
    for (let i = 0; i + len <= compact.length; i += 1) {
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

/** TTS. 끝나거나(실패해도) 최대 TTS_MAX_MS 뒤에 resolve — 음성이 없는 브라우저에서도 흐름이 멈추지 않는다. */
function speak(text, rt) {
  return new Promise((resolve) => {
    const synth = typeof window !== 'undefined' ? window.speechSynthesis : null;
    let done = false;
    const finish = () => { if (!done) { done = true; clearTimeout(rt.ttsTimer); resolve(); } };
    rt.ttsTimer = setTimeout(finish, TTS_MAX_MS);
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
export function useWakeVoice({ config, health, name, flow, onCommand, onStop }) {
  const [state, setState] = useState('OFF');
  const [heard, setHeard] = useState('');        // 인식된 명령 원문
  const [partial, setPartial] = useState('');
  const [note, setNote] = useState(null);        // { tone, text } — 오류·시간 초과·취소 등
  const rt = useRef(null);
  const stateRef = useRef('OFF');
  const nameRef = useRef(name);
  const cbRef = useRef({ onCommand, onStop });
  useEffect(() => { nameRef.current = name; cbRef.current = { onCommand, onStop }; });
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
    try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch { /* 없음 */ }
    setPartial(''); setHeard('');
    go('OFF');
  }, [go]);

  const toWaiting = useCallback((r, message = null) => {
    if (!r || rt.current !== r) return;
    clearTimers(r);
    closeSocket(r.cmd, 'abort'); r.cmd = null;
    r.route = r.wake;
    setPartial('');
    if (message) setNote(message);
    go('WAKE_WORD_WAITING');
  }, [go]);

  const fail = useCallback((r, text) => {
    if (!r || rt.current !== r) return;
    clearTimers(r);
    closeSocket(r.cmd, 'abort'); r.cmd = null;
    r.route = r.wake;
    setPartial('');
    setNote({ tone: 'danger', text });
    go('ERROR');
    r.backTimer = setTimeout(() => { if (rt.current === r && stateRef.current === 'ERROR') go('WAKE_WORD_WAITING'); }, ERROR_HOLD_MS);
  }, [go]);

  const emergencyStop = useCallback((r, text) => {
    if (!r || rt.current !== r) return;
    clearTimers(r);
    closeSocket(r.cmd, 'abort'); r.cmd = null;
    r.route = r.wake;
    try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch { /* 없음 */ }
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
    if (!['WAKE_WORD_WAITING', 'ERROR'].includes(stateRef.current)) return;   // 실행·확인 중에는 새 명령을 받지 않는다
    clearTimeout(r.wakeTimer); r.wakeTimer = null;
    const hit = matchWake(text, nameRef.current);
    if (ev.kind === 'partial') {
      // "지니야"만 들리고 말이 이어지지 않으면 곧바로 받는다. 이어지면(뒤 말이 붙으면) 발화 끝(final)을 기다린다.
      if (hit && !hit.rest) {
        r.wakeTimer = setTimeout(() => {
          if (rt.current !== r || !['WAKE_WORD_WAITING', 'ERROR'].includes(stateRef.current)) return;
          // 서버의 이 발화는 버린다(abort) — 나중에 같은 "지니야" final이 또 오지 않게.
          if (r.wake && r.wake.readyState === WebSocket.OPEN) r.wake.send(JSON.stringify({ type: 'abort' }));
          startRef.current?.(r, '');
        }, WAKE_PARTIAL_HOLD_MS);
      }
      return;
    }
    if (ev.kind !== 'final' && ev.kind !== 'clarify') return;
    if (hit) startRef.current?.(r, hit.rest);
  }, [emergencyStop]);

  const sendCommand = useCallback((r, text, stt) => {
    setHeard(text);
    setPartial('');
    clearTimers(r);
    closeSocket(r.cmd, 'close'); r.cmd = null;
    r.route = r.wake;
    if (isStopWord(text, stopWordsRef.current)) { emergencyStop(r, text); return; }
    r.sawBusy = false;
    r.sentAt = Date.now();
    go('ANALYZING');
    cbRef.current.onCommand?.(text, stt);
  }, [emergencyStop, go]);

  /** 호출어를 들었다: 프레임을 멈추고 '네, 말씀하세요.' → 끝난 뒤 명령 소켓으로 듣는다. */
  const startCommand = useCallback(async (r, rest) => {
    setNote(null); setHeard('');
    r.route = null;                                  // TTS 동안 마이크 프레임을 어디에도 보내지 않는다
    go('LISTENING');
    if (rest && rest.length >= 2) {                  // "지니야, A자재 옮겨줘"처럼 한 번에 말했으면 그 뒤 말을 명령으로
      sendCommand(r, rest, { rawText: rest, confidence: null });
      return;
    }
    await speak(REPLY, r);
    if (rt.current !== r || stateRef.current !== 'LISTENING') return;
    const socket = openSocket(r, 'command');
    r.cmd = socket;
    socket.onmessage = (message) => {
      if (rt.current !== r || r.cmd !== socket) return;
      let ev;
      try { ev = JSON.parse(message.data); } catch { return; }
      if (ev.kind === 'session') { r.route = socket; return; }
      if (ev.kind === 'speech_start') { clearTimeout(r.cmdTimer); r.cmdTimer = null; return; }
      if (ev.kind === 'state' && ev.state === 'finalizing') { go('TRANSCRIBING'); return; }
      if (ev.kind === 'partial') {
        setPartial(ev.text || '');
        if (isStopWord(ev.text, stopWordsRef.current)) emergencyStop(r, ev.text);
        return;
      }
      if (ev.kind === 'final') {
        const text = (ev.text || ev.raw_text || '').trim();
        if (!text) { fail(r, '음성을 알아듣지 못했습니다 — 호출어부터 다시 말씀해 주세요'); return; }
        sendCommand(r, text, { rawText: ev.raw_text || text, confidence: ev.confidence ?? null });
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
  useEffect(() => { startRef.current = startCommand; });

  /** 켜기: 마이크 하나 + 호출어 대기 소켓. */
  const on = useCallback(async () => {
    if (unavailable || rt.current) return;
    const r = { route: null, wake: null, cmd: null, stream: null, context: null, node: null, sessionId: null };
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
        if (ev.kind === 'session') { if (r.route == null && ['WAKE_WORD_WAITING', 'ERROR', 'STOPPED', 'CONFIRMING', 'EXECUTING', 'ANALYZING'].includes(stateRef.current)) r.route = wake; return; }
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
      node.port.onmessage = (m) => { const s = r.route; if (s && s.readyState === WebSocket.OPEN) s.send(m.data); };
      r.context.createMediaStreamSource(r.stream).connect(node);
      r.node = node;
    } catch (e) {
      if (!dead()) { off(); setNote({ tone: 'danger', text: e.message || String(e) }); }
    }
  }, [config, unavailable, go, off, onWakeEvent]);

  // 명령 흐름 따라가기: 분석 → 확인 대기 → 실행 → 끝나면 호출어 대기.
  const busy = !!(flow && flow.busy);
  const phase = flow ? flow.phase : 'idle';
  useEffect(() => {
    const r = rt.current;
    if (!r) return;
    const s = stateRef.current;
    if (s === 'ANALYZING') {
      if (busy) { r.sawBusy = true; return; }
      if (!r.sawBusy && phase === 'idle' && Date.now() - (r.sentAt || 0) < 1500) return;   // 아직 보내는 중
      if (phase === 'confirm') go('CONFIRMING');
      else if (phase === 'running') go('EXECUTING');
      else if (phase === 'ask') fail(r, `추가 확인이 필요합니다${flow.reason ? ` — ${flow.reason}` : ''} — 호출어부터 다시 말씀해 주세요`);
      else if (phase === 'other') fail(r, `명령을 실행할 수 없습니다${flow.reason ? ` — ${flow.reason}` : ''}`);
      else if (phase === 'done') toWaiting(r);
      else if (r.sawBusy) fail(r, '명령 해석 결과를 받지 못했습니다');
    } else if (s === 'CONFIRMING') {
      // 승인 응답 사이에 잠깐 보이는 중간 상태로 판단하지 않는다 — 작업 시작·취소·차단·초기화가 확인될 때만 옮긴다.
      if (phase === 'done') toWaiting(r, { tone: 'muted', text: '작업이 끝났습니다 — 호출어 대기로 돌아갑니다' });
      else if (phase === 'running' || (flow && flow.started)) go('EXECUTING');
      else if (!busy && ((flow && flow.ended) || phase === 'idle')) toWaiting(r, { tone: 'muted', text: '실행하지 않았습니다 — 호출어 대기로 돌아갑니다' });
    } else if (s === 'EXECUTING') {
      if (phase === 'done' || phase === 'other' || phase === 'idle') toWaiting(r, { tone: 'muted', text: '작업이 끝났습니다 — 호출어 대기로 돌아갑니다' });
    }
  }, [busy, phase, flow && flow.reason, flow && flow.started, flow && flow.ended, go, fail, toWaiting]); // eslint-disable-line react-hooks/exhaustive-deps

  const leaveStopped = useCallback(() => { const r = rt.current; if (r && stateRef.current === 'STOPPED') toWaiting(r); }, [toWaiting]);

  useEffect(() => () => off(), []); // eslint-disable-line react-hooks/exhaustive-deps

  const ready = !!(config && config.audio && config.audio.sample_rate_hz);   // 서버 오디오 규격을 받기 전에는 켜지 않는다
  return { state, heard, partial, note, unavailable, ready, on, off, leaveStopped, wakeWord: wakeWordOf(name) };
}
