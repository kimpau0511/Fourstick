/** 웹 음성 안내(TTS) 테스트. 브라우저 없이 node로 돈다.
 *
 * 확인하는 것:
 *  - 읽을 문장은 **열거값에서만** 만들어진다(LLM 원문·STT partial·진행 로그 없음)
 *  - 기본은 꺼짐이고, 켜면 저장소에 남고 다시 만들면 복원된다
 *  - ASK/BLOCK · 확인 카드 · 작업 시작 · 완료/실패 · STOP · resume을 읽는다
 *  - 말하기 전에 이전 음성을 끊는다
 *  - SpeechSynthesis가 없거나 speak가 던져도 **조용히** 지나간다
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  STORAGE_KEY,
  createSpeaker,
  objectJosa,
  safeName,
  slotWords,
  speechFor,
} from '../../html/static/js/tts.js';
import { RESULT_LABELS } from '../../html/static/js/sim-demo.js';

// ── 대역 ──────────────────────────────────────────────────────────────
function fakeStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    dump: () => Object.fromEntries(map),
  };
}

function fakeSynth() {
  const calls = { spoken: [], cancels: 0 };
  return {
    calls,
    speak: (u) => calls.spoken.push(u.text),
    cancel: () => { calls.cancels += 1; },
  };
}

class FakeUtterance {
  constructor(text) { this.text = text; }
}

function speaker(extra = {}) {
  const synth = fakeSynth();
  const storage = fakeStorage(extra.stored);
  const s = createSpeaker({ synth, storage, Utterance: FakeUtterance, ...extra.options });
  return { s, synth, storage };
}

// ── 문장 제조 ─────────────────────────────────────────────────────────
test('PASS는 읽지 않고 BLOCK·ASK만 읽는다', () => {
  assert.equal(speechFor({ kind: 'plan-verdict', verdict: 'PASS' }), null);
  assert.match(speechFor({ kind: 'plan-verdict', verdict: 'BLOCK' }), /차단/);
  assert.match(speechFor({ kind: 'plan-verdict', verdict: 'ASK' }), /정보가 부족/);
  assert.match(speechFor({ kind: 'sim-decision', decision: 'ASK' }), /알아듣지 못했습니다/);
  assert.match(speechFor({ kind: 'sim-decision', decision: 'ASK', heard: '저쪽 벨트' }),
    /제가 이렇게 들었습니다.*저쪽 벨트/);
  assert.match(speechFor({ kind: 'sim-decision', decision: 'BLOCK' }), /할 수 없습니다/);
  assert.equal(speechFor({ kind: 'sim-decision', decision: 'RUN' }), null);
});

test('확인 카드·작업 시작은 동작과 자재 이름으로 만든다', () => {
  assert.equal(
    speechFor({ kind: 'confirm-pending', action: 'transfer', material: 'A자재' }),
    'A자재 컨베이어로 이송. 확인을 누르면 시뮬레이션에서 실행됩니다.',
  );
  assert.equal(
    speechFor({ kind: 'job-started', action: 'resume', material: 'C자재' }),
    'C자재 이어서 이송을 시작합니다.',
  );
  // 자재를 모르면 이름 없이 읽는다 — 만들어 내지 않는다.
  // 조사는 받침에 맞춘다("복귀을"이 아니라 "복귀를").
  assert.equal(
    speechFor({ kind: 'job-started', action: 'return', material: null }),
    '원래 자리로 복귀를 시작합니다.',
  );
  // 모르는 동작이면 일반 문장으로 떨어진다.
  assert.match(speechFor({ kind: 'job-started', action: 'ㅁㄴㅇ' }), /작업을 시작합니다/);
});

test('작업 결과는 화면 배지와 같은 표를 쓴다(resume 결과 포함)', () => {
  const cases = [
    ['simulation_transfer_completed', '이송 완료'],
    ['simulation_transfer_resumed_completed', '이어서 이송 완료'],
    ['returned_to_origin', '원래 슬롯 복귀'],
    ['simulation_transfer_stopped', '정지됨'],
    ['resume_stopped', 'resume 중 정지'],
    ['resume_blocked', 'resume 차단'],
    ['resume_failed', 'resume 실패'],
    ['simulation_transfer_incomplete', '이송 실패'],
  ];
  cases.forEach(([status, label]) => {
    assert.equal(RESULT_LABELS[status][2], label, `${status} 표가 어긋났다`);
    assert.equal(speechFor({ kind: 'job-finished', status }), `${label}.`);
  });
  // 모르는 결과는 지어내지 않고 확인을 요청한다.
  assert.match(speechFor({ kind: 'job-finished', status: 'ㅁㄴㅇ' }), /결과를 확인하세요/);
});

test('STOP은 요청과 확인/미확인을 구분해 읽는다', () => {
  assert.match(speechFor({ kind: 'stop-requested', scope: 'global' }), /전체 정지를 요청/);
  assert.match(speechFor({ kind: 'stop-requested', scope: 'sim' }), /시연 정지를 요청/);
  assert.match(speechFor({ kind: 'stop-result', result: 'confirmed' }), /확인되었습니다/);
  assert.match(speechFor({ kind: 'stop-result', result: 'unconfirmed' }), /확인하지 못했습니다/);
  // 모르는 결과는 **확인됨으로 올리지 않는다.**
  assert.match(speechFor({ kind: 'stop-result', result: 'ㅁㄴㅇ' }), /확인하지 못했습니다/);
});

test('읽지 않는 것: LLM 원문·STT partial·단계별 진행', () => {
  [
    { kind: 'intent-raw', raw: '{"intent":"transfer"}' },
    { kind: 'stt-partial', text: '주황색 거' },
    { kind: 'job-progress', no: 3, of: 12, label: 'pre-grasp' },
    { kind: 'log', message: '계획 생성 요청을 전송했습니다.' },
    null,
    'transfer',
  ].forEach((event) => assert.equal(speechFor(event), null, JSON.stringify(event)));
});

test('자재 이름은 길이와 글자 종류를 제한한다', () => {
  assert.equal(safeName('A자재'), 'A자재');
  assert.equal(safeName('{"intent": "transfer"}'), 'intent transfer');
  assert.equal(safeName('가'.repeat(80)).length, 24);
  assert.equal(safeName(null), '');
});

// ── 켬/끔과 저장 ──────────────────────────────────────────────────────
test('기본은 꺼짐이고, 꺼져 있으면 말하지 않는다', () => {
  const { s, synth } = speaker();
  assert.equal(s.enabled, false);
  assert.equal(s.supported, true);
  assert.equal(s.speak({ kind: 'plan-verdict', verdict: 'BLOCK' }), null);
  assert.deepEqual(synth.calls.spoken, []);
});

test('켜면 저장소에 남고, 다시 만들면 복원된다', () => {
  const { s, storage } = speaker();
  s.setEnabled(true);
  assert.equal(storage.dump()[STORAGE_KEY], '1');
  const again = createSpeaker({
    synth: fakeSynth(), storage, Utterance: FakeUtterance,
  });
  assert.equal(again.enabled, true);
  // 끄면 값도 따라 내려간다.
  again.setEnabled(false);
  assert.equal(storage.dump()[STORAGE_KEY], '0');
  assert.equal(createSpeaker({
    synth: fakeSynth(), storage, Utterance: FakeUtterance,
  }).enabled, false);
});

test('toggle은 상태를 뒤집고 끌 때 읽던 음성을 끊는다', () => {
  const { s, synth } = speaker();
  assert.equal(s.toggle(), true);
  s.speak({ kind: 'plan-verdict', verdict: 'ASK' });
  const cancelsBefore = synth.calls.cancels;
  assert.equal(s.toggle(), false);
  assert.equal(synth.calls.cancels, cancelsBefore + 1, '끌 때 끊지 않았다');
});

test('저장소를 쓸 수 없어도 켜고 끌 수 있다', () => {
  const blocked = {
    getItem() { throw new Error('denied'); },
    setItem() { throw new Error('denied'); },
  };
  const s = createSpeaker({ synth: fakeSynth(), storage: blocked, Utterance: FakeUtterance });
  assert.equal(s.enabled, false);
  assert.equal(s.setEnabled(true), true);
  assert.equal(s.speak({ kind: 'plan-verdict', verdict: 'BLOCK' }) !== null, true);
});

// ── 말하기 ────────────────────────────────────────────────────────────
test('켜면 각 사건을 한 문장씩 읽는다', () => {
  const { s, synth } = speaker();
  s.setEnabled(true);
  s.speak({ kind: 'sim-decision', decision: 'ASK' });
  s.speak({ kind: 'confirm-pending', action: 'transfer', material: 'A자재' });
  s.speak({ kind: 'job-started', action: 'transfer', material: 'A자재' });
  s.speak({ kind: 'job-finished', status: 'simulation_transfer_completed' });
  s.speak({ kind: 'stop-requested', scope: 'global' });
  s.speak({ kind: 'stop-result', result: 'confirmed' });
  assert.equal(synth.calls.spoken.length, 6);
  assert.match(synth.calls.spoken[1], /확인을 누르면/);
  assert.equal(s.lastSpoken, '정지가 확인되었습니다.');
});

test('말하기 전에 이전 음성을 끊는다(새 명령·새 작업·STOP)', () => {
  const { s, synth } = speaker();
  s.setEnabled(true);
  s.speak({ kind: 'confirm-pending', action: 'transfer', material: 'A자재' });
  assert.equal(synth.calls.cancels, 1);
  s.speak({ kind: 'job-started', action: 'transfer', material: 'A자재' });
  assert.equal(synth.calls.cancels, 2);
  s.speak({ kind: 'stop-requested', scope: 'global' });
  assert.equal(synth.calls.cancels, 3);
  // 명시적 취소도 센다(명령을 보내는 순간 main.js가 부른다).
  s.cancel();
  assert.equal(synth.calls.cancels, 4);
});

test('읽지 않는 사건은 이전 음성을 끊지도 않는다', () => {
  const { s, synth } = speaker();
  s.setEnabled(true);
  s.speak({ kind: 'plan-verdict', verdict: 'PASS' });
  s.speak({ kind: 'job-progress', no: 1, of: 12 });
  assert.deepEqual(synth.calls.spoken, []);
  assert.equal(synth.calls.cancels, 0);
});

// ── 못 쓰는 환경 ──────────────────────────────────────────────────────
test('SpeechSynthesis가 없으면 조용히 지나간다', () => {
  const s = createSpeaker({ synth: null, storage: fakeStorage(), Utterance: null });
  assert.equal(s.supported, false);
  assert.equal(s.setEnabled(true), true);
  assert.equal(s.speak({ kind: 'plan-verdict', verdict: 'BLOCK' }), null);
  assert.doesNotThrow(() => s.cancel());
});

test('speak가 던져도 삼키고 기능을 막지 않는다', () => {
  const synth = {
    speak() { throw new Error('synthesis failed'); },
    cancel() { throw new Error('cancel failed'); },
  };
  const s = createSpeaker({ synth, storage: fakeStorage(), Utterance: FakeUtterance });
  s.setEnabled(true);
  assert.doesNotThrow(() => {
    assert.equal(s.speak({ kind: 'plan-verdict', verdict: 'BLOCK' }), null);
  });
  assert.doesNotThrow(() => s.cancel());
});

// ── 슬롯 번호 ─────────────────────────────────────────────────────────
test('슬롯 번호는 읽되 좌표는 읽지 않는다', () => {
  assert.equal(
    speechFor({ kind: 'confirm-pending', action: 'transfer', material: 'A자재', slot: 'slot_2' }),
    'A자재 컨베이어 2번 위치로 이송. 확인을 누르면 시뮬레이션에서 실행됩니다.',
  );
  assert.equal(
    speechFor({ kind: 'job-started', action: 'transfer', material: 'B자재', slot: 'slot_3' }),
    'B자재 컨베이어 3번 위치로 이송을 시작합니다.',
  );
  assert.equal(
    speechFor({ kind: 'job-started', action: 'return', material: 'A자재', slot: 'slot_1' }),
    'A자재 컨베이어 1번 위치에서 원래 자리로 복귀를 시작합니다.',
  );
  assert.equal(
    speechFor({ kind: 'job-finished', status: 'simulation_transfer_completed', slot: 'slot_2' }),
    '컨베이어 2번 위치 이송 완료.',
  );
});

test('슬롯이 없으면 예전 문장 그대로다(호환)', () => {
  assert.equal(
    speechFor({ kind: 'job-started', action: 'transfer', material: 'A자재' }),
    'A자재 컨베이어로 이송을 시작합니다.',
  );
  assert.equal(
    speechFor({ kind: 'job-finished', status: 'simulation_transfer_completed' }),
    '이송 완료.',
  );
});

test('슬롯 이름이 열거값이 아니면 조용히 뺀다', () => {
  ['../../etc', 'slot_x', '', null, 42, { slot: 1 }, 'slot_0'].forEach((slot) => {
    assert.equal(slotWords(slot), '', String(slot));
    // 문장은 여전히 만들어지되 자리 이름만 빠진다.
    const said = speechFor({ kind: 'job-started', action: 'transfer', material: 'A자재', slot });
    assert.equal(said, 'A자재 컨베이어로 이송을 시작합니다.');
  });
});

test('조사는 받침에 맞춘다', () => {
  assert.equal(objectJosa('이송'), '을');
  assert.equal(objectJosa('복귀'), '를');
  assert.equal(objectJosa('복구'), '를');
  assert.equal(objectJosa(''), '을');
});
