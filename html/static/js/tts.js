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
 * 쓸 수 없는 환경(SpeechSynthesis 미지원, 저장소 차단, speak 실패)은 **조용히**
 * 지나간다. 음성은 부가 기능이고, 여기서 난 오류가 명령·작업 기능을 막지 않는다.
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
/** 음성 안내기.
 *
 * `synth`·`storage`·`Utterance`를 주입할 수 있어 브라우저 없이 시험한다.
 * 어느 하나라도 없으면 `supported=false`이고, 모든 호출이 아무 일도 하지 않는다.
 */
export function createSpeaker({
  synth = typeof globalThis !== 'undefined' ? globalThis.speechSynthesis : null,
  storage = typeof globalThis !== 'undefined' ? globalThis.localStorage : null,
  Utterance = typeof globalThis !== 'undefined' ? globalThis.SpeechSynthesisUtterance : null,
  lang = 'ko-KR',
  rate = 1.05,
} = {}) {
  const supported = Boolean(synth && typeof synth.speak === 'function' && Utterance);
  // 저장소를 못 써도 켜고 끌 수는 있어야 한다. 값만 못 남긴다.
  let enabled = storage ? readStored(storage) : false;
  /** 마지막으로 읽은 문장. 테스트와 디버깅이 본다. */
  let lastSpoken = null;

  function cancel() {
    if (!supported) return;
    try {
      synth.cancel();
    } catch {
      // 취소 실패는 조용히 지나간다.
    }
  }

  function setEnabled(on) {
    const next = Boolean(on);
    if (next === enabled) return enabled;
    enabled = next;
    if (storage) writeStored(storage, enabled);
    // 끌 때는 읽던 문장을 바로 끊는다.
    if (!enabled) cancel();
    return enabled;
  }

  /** 사건 하나를 읽는다. 돌려주는 값은 실제로 읽은 문장(없으면 null).
   *
   * 읽기 전에 **이전 음성을 끊는다** — 안내는 항상 "지금 상태" 하나여야 한다.
   */
  function speak(event) {
    if (!supported || !enabled) return null;
    const text = speechFor(event);
    if (!text) return null;
    cancel();
    try {
      const utterance = new Utterance(text);
      utterance.lang = lang;
      utterance.rate = rate;
      synth.speak(utterance);
      lastSpoken = text;
      return text;
    } catch {
      // 합성 실패는 기능을 막지 않는다. 조용히 지나간다.
      return null;
    }
  }

  return {
    get supported() { return supported; },
    get enabled() { return enabled; },
    get lastSpoken() { return lastSpoken; },
    setEnabled,
    toggle: () => setEnabled(!enabled),
    speak,
    cancel,
  };
}
