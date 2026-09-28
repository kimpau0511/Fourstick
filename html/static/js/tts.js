/** 웹 음성 안내(TTS). 브라우저 내장 `SpeechSynthesis`만 쓴다.
 *
 * 무엇을 읽는가 — **신뢰된 결과만, 짧게.**
 *  1. 안전 판단 ASK / BLOCK, 시뮬레이션 명령 ASK / BLOCK
 *  2. Qwen 확인 카드가 떴을 때
 *  3. 확인을 눌러 작업이 시작됐을 때
 *  4. 작업 완료 / 실패
 *  5. STOP 요청과 그 확인(confirmed) / 미확인(unconfirmed) 결과
 *  6. resume 결과
 *
 * 무엇을 읽지 않는가 — **LLM 원문, STT partial, 단계별 진행 로그.**
 * 읽는 문장은 `speechFor()`가 **열거값에서만** 만든다. 모델이 낸 글자를 그대로
 * 읽지 않는다. 자재 이름은 작업 셀 설정에서 온 값만 쓰고, 그마저도 길이와
 * 글자 종류를 제한한다(`safeName`).
 *
 * 기본은 **꺼짐**이다. 사용자가 켜면 브라우저 저장소에 남는다(그 브라우저에만).
 *
 * 쓸 수 없는 환경(SpeechSynthesis 미지원, 저장소 차단, speak 실패)이 명령·작업
 * 기능을 막지 않는다. 다만 **실패를 숨기지 않는다** — 재생 단계와 오류를 `status`로
 * 알리고 화면이 보인다(소리가 안 났는데 성공으로 두지 않는다).
 *
 * DOM을 모른다. `speechFor()`는 순수 함수라 브라우저 없이 node로 시험한다
 * (`tests/web/tts.test.mjs`).
 */

import { RESULT_LABELS } from './sim-demo.js';

/** 켬/끔을 남기는 자리. 브라우저마다 따로 남는다. */
export const STORAGE_KEY = 'forstick.tts.enabled';

/** 동작 → 말로 읽을 짧은 이름. **열거값이다** — 서버 문자열을 그대로 읽지 않는다.
 *  (서버의 `action_label`은 "컨베이어로 이송(상태 유지)"처럼 괄호가 있어
 *   소리로 듣기에 적합하지 않다.) */
const ACTION_WORDS = {
  transfer: '컨베이어로 이송',
  return: '원래 자리로 복귀',
  resume: '이어서 이송',
  restore: '복구',
};

/** 정지 결과 → 읽을 말. 확인된 정지와 미확인 정지를 **구분해서** 읽는다. */
const STOP_RESULTS = {
  confirmed: '정지가 확인되었습니다.',
  unconfirmed: '정지를 확인하지 못했습니다. 화면을 확인하세요.',
};

/** 자재 이름에 허용하는 글자. 한글·영문·숫자·공백만 남기고 길이를 제한한다.
 *
 * 이 값은 작업 셀 설정(`config/workcell`)에서 오지만, 소리로 나가는 문자열을
 * 만들 때는 출처를 믿지 않고 한 번 더 깎는다. */
const NAME_LIMIT = 24;

export function safeName(value) {
  return String(value == null ? '' : value)
    .replace(/[^0-9A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, NAME_LIMIT);
}

/** 슬롯 번호를 읽을 말로. **번호만** 읽는다 — 좌표를 읽지 않는다.
 *  슬롯 이름은 서버가 준 열거값(`slot_1`…)이고, 모르는 값이면 조용히 뺀다. */
export function slotWords(slot) {
  if (typeof slot !== 'string') return '';
  const match = /^slot_([1-9]\d?)$/.exec(slot);
  return match ? `컨베이어 ${match[1]}번 위치` : '';
}

/** 앞 글자 받침에 맞는 목적격 조사. "복귀을"이 아니라 "복귀를"로 읽는다. */
export function objectJosa(word) {
  const last = String(word || '').trim().slice(-1);
  const code = last.charCodeAt(0);
  if (!last || Number.isNaN(code) || code < 0xac00 || code > 0xd7a3) return '을';
  return (code - 0xac00) % 28 === 0 ? '를' : '을';
}

function slotWork(work, slot) {
  const where = slotWords(slot);
  if (!where) return work;
  // "컨베이어로 이송"은 자리 이름으로 바꿔 읽는다 — 중복을 피한다.
  return work === ACTION_WORDS.transfer ? `${where}로 이송` : `${where}에서 ${work}`;
}

function withName(name, sentence) {
  const clean = safeName(name);
  return clean ? `${clean} ${sentence}` : sentence;
}

/** 사건 하나 → 읽을 문장. 읽지 않을 사건이면 `null`이다.
 *
 * **여기가 유일한 문장 제조기다.** 호출하는 쪽이 문자열을 만들어 넘기지 않는다.
 */
export function speechFor(event) {
  if (!event || typeof event !== 'object') return null;
  switch (event.kind) {
    // ── 1. 안전 판단 / 시뮬레이션 명령 판단 ────────────────────────────
    case 'plan-verdict':
      // PASS는 읽지 않는다 — 사용자가 실행 버튼을 누르는 자리라 방해가 된다.
      if (event.verdict === 'BLOCK') return '실행이 차단되었습니다. 차단 사유를 확인하세요.';
      if (event.verdict === 'ASK') return '정보가 부족합니다. 명령을 더 구체적으로 말해 주세요.';
      return null;

    case 'sim-decision':
      if (event.decision === 'BLOCK') return '지금 상태에서는 할 수 없습니다.';
      if (event.decision === 'ASK') return event.heard
        ? `제가 이렇게 들었습니다. ${safeName(event.heard)}. 화면을 확인하고 다시 말해 주세요.`
        : '명령을 알아듣지 못했습니다. 다시 말해 주세요.';
      return null;

    // ── 2. 확인 카드 ──────────────────────────────────────────────────
    case 'confirm-pending': {
      const work = ACTION_WORDS[event.action];
      if (!work) return '확인이 필요합니다. 확인 버튼을 눌러 주세요.';
      return withName(event.material,
        `${slotWork(work, event.slot)}. 확인을 누르면 시뮬레이션에서 실행됩니다.`);
    }

    case 'confirm-cancelled':
      return '취소했습니다. 작업을 만들지 않았습니다.';

    // ── 3. 작업 시작 ──────────────────────────────────────────────────
    case 'job-started': {
      const work = ACTION_WORDS[event.action];
      if (!work) return '시뮬레이션 작업을 시작합니다.';
      const phrase = slotWork(work, event.slot);
      return withName(event.material, `${phrase}${objectJosa(phrase)} 시작합니다.`);
    }

    // ── 4·6. 작업 완료 / 실패 / resume 결과 ───────────────────────────
    case 'job-finished': {
      // 화면 배지와 **같은 출처**를 쓴다(`sim-demo.js`의 RESULT_LABELS).
      // 여기에 문구를 따로 만들면 화면과 소리가 갈라진다.
      const label = RESULT_LABELS[event.status];
      const where = slotWords(event.slot);
      if (label) return where ? `${where} ${label[2]}.` : `${label[2]}.`;
      return '시뮬레이션 작업이 끝났습니다. 결과를 확인하세요.';
    }

    // ── 5. 정지 ───────────────────────────────────────────────────────
    case 'stop-requested':
      return event.scope === 'sim'
        ? '시연 정지를 요청했습니다.'
        : '전체 정지를 요청했습니다.';

    case 'stop-result':
      return STOP_RESULTS[event.result] || STOP_RESULTS.unconfirmed;

    case 'tts-test':
      return '음성 안내 시험입니다. 이 문장이 들리면 음성 안내가 동작합니다.';

    default:
      return null;
  }
}

// ── 저장소 ────────────────────────────────────────────────────────────
function readStored(storage) {
  try {
    return storage.getItem(STORAGE_KEY) === '1';
  } catch {
    // 사생활 보호 모드·저장소 차단. 기본값(꺼짐)으로 둔다.
    return false;
  }
}

function writeStored(storage, on) {
  try {
    storage.setItem(STORAGE_KEY, on ? '1' : '0');
  } catch {
    // 남기지 못해도 이번 세션 동안은 동작한다. 조용히 지나간다.
  }
}

// ── 말하기 ────────────────────────────────────────────────────────────
/** 브라우저가 재생을 시작했다고 알려 오기를 기다리는 시간(ms). 이 안에
 *  `onstart`가 오지 않으면 **소리가 나지 않은 것으로** 적는다(성공으로 두지 않는다). */
export const START_TIMEOUT_MS = 4000;

/** 이전 음성을 끊은 뒤 새 문장을 넣기까지 쉬는 시간(ms).
 *  Chrome은 `cancel()` 바로 뒤의 `speak()`를 떨어뜨리는 일이 있다. */
export const AFTER_CANCEL_MS = 120;

/** 문장 길이로 어림한 재생 시간(ms). 끝 알림이 이보다 훨씬 빨리 오면 소리 없이 끝난 것일 수 있다. */
export function expectedMs(text, rate = 1.05) {
  const chars = String(text || '').replace(/\s+/g, '').length;
  return Math.round((chars * 150) / rate);
}

/** 목록에서 한국어 음성을 고른다. 없으면 null — 브라우저 기본 음성에 맡긴다. */
export function pickKoreanVoice(voices) {
  const list = Array.isArray(voices) ? voices : [];
  const ko = list.filter((v) => /^ko([-_]|$)/i.test(String((v && v.lang) || '')));
  return ko.find((v) => v.localService) || ko[0] || null;
}

/** 음성 안내기.
 *
 * `synth`·`storage`·`Utterance`·타이머를 주입할 수 있어 브라우저 없이 시험한다.
 * 어느 하나라도 없으면 `supported=false`이고, 모든 호출이 아무 일도 하지 않는다.
 *
 * **재생 상태를 따로 적는다**(`status`, `onStatus`). 문장 생성 → 음성 엔진(목록·
 * 한국어 음성) → 재생 호출 → 시작(`onstart`) → 끝(`onend`) / 오류(`onerror`,
 * 시작 제한시간)를 구분한다. `onend`는 브라우저가 "재생을 마쳤다"고 알린 것일 뿐
 * **귀에 들렸다는 증거가 아니다**(출력 장치·음량은 브라우저가 모른다).
 */
export function createSpeaker({
  synth = typeof globalThis !== 'undefined' ? globalThis.speechSynthesis : null,
  storage = typeof globalThis !== 'undefined' ? globalThis.localStorage : null,
  Utterance = typeof globalThis !== 'undefined' ? globalThis.SpeechSynthesisUtterance : null,
  lang = 'ko-KR',
  rate = 1.05,
  setTimer = typeof globalThis !== 'undefined' && globalThis.setTimeout
    ? globalThis.setTimeout.bind(globalThis) : null,
  clearTimer = typeof globalThis !== 'undefined' && globalThis.clearTimeout
    ? globalThis.clearTimeout.bind(globalThis) : null,
  onStatus = null,
  now = () => Date.now(),
} = {}) {
  const supported = Boolean(synth && typeof synth.speak === 'function' && Utterance);
  // 저장소를 못 써도 켜고 끌 수는 있어야 한다. 값만 못 남긴다.
  let enabled = storage ? readStored(storage) : false;
  /** 마지막으로 재생을 요청한 문장. 테스트와 디버깅이 본다. */
  let lastSpoken = null;
  /** 재생 중인 발화. **참조를 쥐고 있어야 한다** — 놓치면 Chrome이 발화를 수거해
   *  `onend`가 오지 않는다. */
  let current = null;
  let serial = 0;
  let voice = null;
  let voiceCount = 0;
  let status = { phase: supported ? 'idle' : 'unsupported', text: null, error: null,
                 voice: null, voices: 0, at: Date.now() };

  function report(fields) {
    status = { ...status, ...fields, voice: voice ? `${voice.name} (${voice.lang})` : null,
               voices: voiceCount, at: Date.now() };
    if (typeof onStatus === 'function') {
      try { onStatus(status); } catch { /* 화면 갱신 실패가 음성을 막지 않는다 */ }
    }
  }

  function loadVoices() {
    if (!supported || typeof synth.getVoices !== 'function') return;
    try {
      const voices = synth.getVoices() || [];
      voiceCount = voices.length;
      voice = pickKoreanVoice(voices);
    } catch {
      voiceCount = 0;
      voice = null;
    }
  }
  loadVoices();
  if (supported) {
    // 목록은 늦게 온다(Chrome). 오면 다시 고른다.
    try {
      if (typeof synth.addEventListener === 'function') {
        synth.addEventListener('voiceschanged', () => { loadVoices(); report({}); });
      } else if ('onvoiceschanged' in synth) {
        synth.onvoiceschanged = () => { loadVoices(); report({}); };
      }
    } catch { /* 목록 알림을 못 받아도 기본 음성으로 읽는다 */ }
  }

  function cancel() {
    if (!supported) return;
    serial += 1;              // 기다리던 재생 예약도 무효로 한다
    current = null;
    try {
      synth.cancel();
    } catch {
      // 취소 실패는 기능을 막지 않는다.
    }
  }

  function setEnabled(on) {
    const next = Boolean(on);
    if (next === enabled) return enabled;
    enabled = next;
    if (storage) writeStored(storage, enabled);
    // 끌 때는 읽던 문장을 바로 끊는다.
    if (!enabled) {
      cancel();
      report({ phase: 'off', error: null });
    }
    return enabled;
  }

  function play(text, id) {
    if (id !== serial) return;          // 그 사이 새 문장이나 취소가 왔다
    if (!voice) loadVoices();
    let utterance;
    try {
      utterance = new Utterance(text);
      utterance.lang = voice ? voice.lang : lang;
      if (voice) utterance.voice = voice;
      utterance.rate = rate;
    } catch (error) {
      report({ phase: 'error', text, error: `발화 생성 실패: ${error && error.message}` });
      return;
    }
    let started = false;
    let timer = null;
    let startedAt = null;
    const expect = expectedMs(text, rate);
    const mine = () => id === serial && current === utterance;
    utterance.onstart = () => {
      started = true;
      startedAt = now();
      if (timer !== null && clearTimer) clearTimer(timer);
      if (!mine()) return;
      report({ phase: 'speaking', text, error: null, expected_ms: expect, elapsed_ms: null, heard: null });
      // 끝 알림이 오지 않는 엔진(일부 안드로이드)도 있다 — 예상 시간의 3배 + 5 s 뒤에도 안 오면 적는다.
      if (setTimer) {
        timer = setTimer(() => {
          if (!mine()) return;
          report({ phase: 'error', text,
                   error: `끝 알림이 오지 않았습니다(${Math.round((expect * 3 + 5000) / 1000)} s) — 엔진 speaking=${Boolean(synth.speaking)}` });
        }, expect * 3 + 5000);
      }
    };
    utterance.onend = () => {
      if (timer !== null && clearTimer) clearTimer(timer);
      if (!mine()) return;
      current = null;
      const elapsed = startedAt === null ? null : now() - startedAt;
      // 시작 알림 없이 끝났으면 실제 재생을 확인하지 못한 것이다. 너무 빨리 끝나도 소리 없이 끝났을 수 있다.
      if (!started) {
        report({ phase: 'error', text, error: '재생 시작 알림 없이 끝났습니다', elapsed_ms: elapsed });
      } else if (elapsed !== null && elapsed < expect * 0.3) {
        report({ phase: 'error', text, elapsed_ms: elapsed, expected_ms: expect,
                 error: `재생이 너무 빨리 끝났습니다(${elapsed} ms, 예상 약 ${expect} ms) — 소리 없이 끝났을 수 있습니다` });
      } else {
        report({ phase: 'ended', text, error: null, elapsed_ms: elapsed, expected_ms: expect });
      }
    };
    utterance.onerror = (event) => {
      if (timer !== null && clearTimer) clearTimer(timer);
      const code = (event && event.error) || 'unknown';
      // 우리가 끊은 것(interrupted/canceled)은 오류가 아니다.
      if (code === 'interrupted' || code === 'canceled') return;
      if (!mine()) return;
      current = null;
      const hint = code === 'not-allowed'
        ? ' — 브라우저가 사용자 조작 전 재생을 막았습니다. 화면을 한 번 누른 뒤 음성 시험을 눌러 주세요.'
        : code === 'language-unavailable' || code === 'voice-unavailable'
          ? ' — 이 브라우저에 한국어 음성이 없습니다.' : '';
      report({ phase: 'error', text, error: `음성 엔진 오류: ${code}${hint}` });
    };
    current = utterance;
    try {
      if (synth.paused && typeof synth.resume === 'function') synth.resume();
      synth.speak(utterance);
    } catch (error) {
      current = null;
      report({ phase: 'error', text, error: `재생 호출 실패: ${error && error.message}` });
      return;
    }
    report({ phase: 'requested', text, error: null });
    if (setTimer) {
      timer = setTimer(() => {
        if (started || !mine()) return;
        current = null;
        report({ phase: 'error', text,
                 error: `${START_TIMEOUT_MS / 1000}초 안에 재생이 시작되지 않았습니다`
                   + (voiceCount === 0 ? ' — 브라우저 음성 목록이 비어 있습니다' : '') });
      }, START_TIMEOUT_MS);
    }
  }

  function say(text) {
    const busy = Boolean(synth.speaking || synth.pending);
    cancel();
    const id = serial;
    lastSpoken = text;
    report({ phase: 'queued', text, error: null, heard: null, elapsed_ms: null, expected_ms: null });
    if (busy && setTimer) {
      setTimer(() => play(text, id), AFTER_CANCEL_MS);
      return text;
    }
    play(text, id);
    // 호출 자체가 실패했으면 요청한 문장도 없다.
    return status.phase === 'error' ? null : text;
  }

  /** 사건 하나를 읽는다. 돌려주는 값은 재생을 **요청한** 문장(없으면 null).
   *  요청이 곧 재생 성공은 아니다 — 결과는 `status`로 따로 온다.
   *
   * 읽기 전에 **이전 음성을 끊는다** — 안내는 항상 "지금 상태" 하나여야 한다.
   */
  function speak(event) {
    const text = speechFor(event);
    if (!text) return null;
    if (!supported) {
      report({ phase: 'unsupported', text, error: '이 브라우저는 음성 합성을 지원하지 않습니다' });
      return null;
    }
    if (!enabled) {
      // 꺼져 있으면 읽지 않는다. 화면에는 읽었을 문장을 남긴다.
      report({ phase: 'off', text, error: null });
      return null;
    }
    return say(text);
  }

  /** 사용자가 귀로 확인한 결과. **이것만이 실제 청취 확인이다** — 브라우저 알림은 소리를 보장하지 않는다. */
  function confirmHeard(heard) {
    report({ heard: Boolean(heard), heard_at: now() });
    return status;
  }

  /** 음성 시험. **사용자가 누른 단추에서만** 부른다(브라우저 재생 허가를 얻는 자리). */
  function test() {
    if (!supported) return null;
    return say(speechFor({ kind: 'tts-test' }));
  }

  return {
    get supported() { return supported; },
    get enabled() { return enabled; },
    get lastSpoken() { return lastSpoken; },
    get status() { return status; },
    setEnabled,
    toggle: () => setEnabled(!enabled),
    speak,
    test,
    confirmHeard,
    cancel,
  };
}
