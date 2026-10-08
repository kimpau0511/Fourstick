import { useCallback, useEffect, useRef, useState } from 'react';

// 음성 입력(STT). 옛 화면 html/static/js/backend-http.js의 startVoice/stopVoice와 같은 프로토콜이다.
//   POST /v1/sessions → wss://<host>/v1/stt?session_id=… 로 PCM16 mono 프레임 전송(pcm-worklet.js)
//   서버 메시지 kind: session(준비됨) · partial(중간 전사, 표시만) · final(확정, 명령으로 보냄)
//                     · clarify(신뢰도 낮음, 보내지 않고 되묻기) · error(reason_code, detail) · state 등
//   종료 = {type:'flush'} 로 남은 오디오를 확정시킨 뒤 final/clarify/error를 기다린다. 취소 = {type:'abort'}.
// 샘플레이트는 서버가 정한다(config.audio.sample_rate_hz). 브라우저가 못 주면 리샘플링하지 않고 오류로 알린다.

const SESSION_KEY = 'forstick2.voice.session';
// flush 뒤 기다리는 시간은 서버 STT 정책의 전사 마감(config.policies.stt.transcribe_deadline_sec)을 쓴다.
// 정책이 없으면 기한을 지어내지 않고, 서버가 소켓을 닫거나 error를 보낼 때까지 기다린다.

async function json(method, path, body) {
  const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  let payload = {};
  try { payload = await r.json(); } catch { /* JSON이 아닌 응답 */ }
  return { ok: r.ok, status: r.status, payload };
}

// 이 탭의 STT 세션. 있으면 이어받고(clients), 못 받으면 새로 만든다.
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

/** config: /v1/config, health: /health(선택). 서버가 STT를 꺼 두었으면 unavailable에 이유가 담긴다. */
//   2026-10-08 자동 보내기: final마다 이 녹음의 발화 id(서버 request_id, 없으면 녹음 번호)를 함께 넘긴다.
//   빈 final은 onEmpty로 이유를 알린다. 녹음 중 마이크가 끊기거나 화면을 벗어나면 취소(abort)한다 — 그 뒤 늦게 온 결과는 버린다.
let recordingSeq = 0;
export function useVoice({ config, health, onFinal, onClarify, onStart, onEmpty }) {
  const [status, setStatus] = useState('idle'); // idle | connecting | listening | finalizing | error
  const [partial, setPartial] = useState('');
  // 중간 결과의 신뢰도(서버 partial 이벤트의 confidence — 최근 window 구간 전사의 점수). 없으면 null.
  const [partialConfidence, setPartialConfidence] = useState(null);
  const [error, setError] = useState(null);
  const [clarify, setClarify] = useState(null);
  const rt = useRef(null); // 지금 열려 있는 입력 한 번분(소켓·마이크·컨텍스트)
  const finalRef = useRef(onFinal);
  const clarifyRef = useRef(onClarify);
  const startRef = useRef(onStart);
  const emptyRef = useRef(onEmpty);
  useEffect(() => { finalRef.current = onFinal; clarifyRef.current = onClarify; startRef.current = onStart; emptyRef.current = onEmpty; });
  // 취소 이유(마이크 끊김·화면 이탈 등). 사용자가 직접 취소했으면 null.
  const [notice, setNotice] = useState(null);

  const feature = (config && config.features && config.features.stt) || (health && health.features && health.features.stt) || null;
  const unavailable = feature && feature.available === false
    ? `음성 인식을 쓸 수 없습니다${feature.detail ? ` — ${feature.detail}` : ''}` : null;

  // 마이크·소켓·컨텍스트를 놓는다. 이미 닫힌 세션에 오디오를 더 보내지 않도록 프레임 전송부터 끊는다.
  const release = useCallback((closeMessage = 'close') => {
    const r = rt.current;
    rt.current = null;
    if (!r) return;
    clearTimeout(r.timer);
    if (r.node) r.node.port.onmessage = null;
    if (r.stream) r.stream.getTracks().forEach((t) => t.stop());
    if (r.socket) {
      r.socket.onclose = null;
      if (r.socket.readyState === WebSocket.OPEN) {
        try { r.socket.send(JSON.stringify({ type: closeMessage })); } catch { /* 이미 닫힘 */ }
      }
      r.socket.close();
    }
    if (r.context && r.context.state !== 'closed') r.context.close().catch(() => {});
  }, []);

  const fail = useCallback((message) => { release(); setPartial(''); setPartialConfidence(null); setError(message); setStatus('error'); }, [release]);

  const start = useCallback(async () => {
    if (unavailable || rt.current) return;
    setError(null); setClarify(null); setNotice(null); setPartial(''); setPartialConfidence(null); setStatus('connecting');
    startRef.current?.(); // 새 녹음 시작 — 화면이 이전 발화의 점수를 지운다
    recordingSeq += 1;
    const mine = { socket: null, context: null, node: null, stream: null, timer: null, id: `rec_${Date.now().toString(36)}_${recordingSeq}`, finalizing: false };
    rt.current = mine;
    const dead = () => rt.current !== mine; // 취소·언마운트 뒤에는 아무것도 하지 않는다
    try {
      const rate = config && config.audio && config.audio.sample_rate_hz;
      if (!rate) throw new Error('서버가 오디오 규격(샘플레이트)을 주지 않았습니다');
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) throw new Error('이 브라우저는 마이크 입력을 지원하지 않습니다');
      const sessionId = await ensureSession();
      if (dead()) return;
      try { mine.stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch (e) { throw new Error(micError(e)); }
      if (dead()) { mine.stream.getTracks().forEach((t) => t.stop()); return; }
      // 마이크가 녹음 도중 끊기면(장치 분리·권한 회수) 이 녹음을 버린다 — 정상 종료(stop)는 finalizing이라 해당 없음.
      mine.stream.getTracks().forEach((t) => { t.onended = () => { if (!dead() && !mine.finalizing) abortWith('마이크가 꺼져 음성 입력을 취소했습니다 — 보내지 않았습니다'); }; });
      mine.context = new AudioContext({ sampleRate: rate });
      if (Math.round(mine.context.sampleRate) !== Math.round(rate)) {
        throw new Error(`브라우저가 ${rate} Hz 오디오를 주지 않습니다(${mine.context.sampleRate} Hz). 리샘플링하지 않으므로 음성 입력을 쓸 수 없습니다`);
      }
      await mine.context.audioWorklet.addModule(new URL('./pcm-worklet.js', import.meta.url));
      if (dead()) return;
      const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const socket = new WebSocket(`${scheme}//${location.host}/v1/stt?session_id=${encodeURIComponent(sessionId)}`);
      socket.binaryType = 'arraybuffer';
      mine.socket = socket;
      socket.onmessage = (message) => {
        if (dead()) return;
        let ev;
        try { ev = JSON.parse(message.data); } catch { return; }
        if (ev.kind === 'session') setStatus('listening');
        else if (ev.kind === 'partial') { setPartial(ev.text || ''); setPartialConfidence(ev.confidence ?? null); }
        else if (ev.kind === 'final') {
          release(); setPartial(''); setPartialConfidence(null); setStatus('idle');
          // 정규화된 문장(text)을 명령으로, 원문·신뢰도는 기록용으로 함께 넘긴다.
          const text = (ev.text || ev.raw_text || '').trim();
          if (text) finalRef.current?.(text, { rawText: ev.raw_text || text, confidence: ev.confidence ?? null, requestId: ev.request_id || null,
            // 이번 발화 확정 때 서버가 이 세션의 STT 기록과 대조한다
            sessionId, persisted: ev.persisted ?? null, utteranceId: ev.request_id || mine.id });
          else emptyRef.current?.();
        } else if (ev.kind === 'clarify') {
          release(); setPartial(''); setPartialConfidence(null); setStatus('idle');
          setClarify(ev.detail || '잘 알아듣지 못했습니다. 다시 말해 주세요');
          clarifyRef.current?.({ confidence: ev.confidence ?? null });
        } else if (ev.kind === 'error') {
          fail(ev.reason_code === 'stt.no_speech' ? '말소리를 감지하지 못했습니다 — 마이크와 음량을 확인하고 다시 말해 주세요'
            : `음성 인식 오류${ev.detail ? ` — ${ev.detail}` : ''}`);
        }
      };
      socket.onclose = () => { if (!dead()) fail('음성 서버와 연결이 끊겼습니다'); };
      const node = new AudioWorkletNode(mine.context, 'pcm-frame-processor');
      node.port.onmessage = (m) => { if (socket.readyState === WebSocket.OPEN) socket.send(m.data); };
      mine.context.createMediaStreamSource(mine.stream).connect(node);
      mine.node = node;
    } catch (e) {
      if (!dead()) fail(e.message || String(e));
    }
  }, [config, unavailable, release, fail]);

  /** 말하기를 끝낸다: 마이크를 끄고 flush로 남은 오디오를 확정시킨 뒤 서버 답(final 등)을 기다린다. */
  const stop = useCallback(() => {
    const r = rt.current;
    if (!r || !r.socket || r.socket.readyState !== WebSocket.OPEN) return;
    if (r.node) r.node.port.onmessage = null;
    if (r.stream) r.stream.getTracks().forEach((t) => t.stop());
    r.finalizing = true;
    r.socket.send(JSON.stringify({ type: 'flush' }));
    setStatus('finalizing');
    const deadlineSec = config?.policies?.stt?.transcribe_deadline_sec;
    if (Number.isFinite(deadlineSec)) {
      r.timer = setTimeout(() => { if (rt.current === r) fail('음성 인식 결과가 오지 않았습니다 — 다시 시도해 주세요'); }, deadlineSec * 1000);
    }
  }, [config, fail]);

  /** 결과를 버리고 중단한다(서버에 abort). 이 뒤에 오는 결과는 무시한다(rt가 바뀌어 dead()). */
  const cancel = useCallback(() => { release('abort'); setPartial(''); setPartialConfidence(null); setError(null); setNotice(null); setStatus('idle'); }, [release]);
  // 이유를 남기며 취소(마이크 끊김·화면 이탈). start 안에서 쓰므로 ref로 최신 함수를 둔다.
  const abortRef = useRef(null);
  abortRef.current = (why) => { release('abort'); setPartial(''); setPartialConfidence(null); setError(null); setStatus('idle'); setNotice(why); };
  function abortWith(why) { abortRef.current?.(why); }

  // 화면을 벗어나면(탭 숨김·페이지 떠남) 듣던·확정 중이던 녹음을 버린다 — 보이지 않는 동안 결과가 자동으로 나가지 않게.
  useEffect(() => {
    const away = () => { if (rt.current) abortRef.current?.('화면을 벗어나 음성 입력을 취소했습니다 — 보내지 않았습니다'); };
    const onVisibility = () => { if (document.visibilityState === 'hidden') away(); };
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('pagehide', away);
    return () => { document.removeEventListener('visibilitychange', onVisibility); window.removeEventListener('pagehide', away); };
  }, []);

  useEffect(() => () => release(), [release]);

  return { status, partial, partialConfidence, error, clarify, notice, unavailable, start, stop, cancel };
}
