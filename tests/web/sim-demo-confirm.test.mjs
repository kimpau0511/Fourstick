/** 확인 카드(Qwen 해석 → 사용자 확인) 화면 테스트. 브라우저 없이 돈다.
 *
 * 확인하는 것:
 *  - CONFIRM 응답은 확인 대기를 세우고 **job을 세우지 않는다**
 *  - 확인 카드에 확인·취소 버튼, 해석 근거, 현재 자재 상태, 시뮬레이션 안내가 있다
 *  - 남은 시간이 tick마다 줄고, 0이 되면 카드가 사라진다
 *  - 취소·만료 뒤에는 확인 버튼이 화면에 없다
 *  - 시뮬레이션 표시 배지는 화면 어디에도 없다(대체 배지·문구도 없다)
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  renderCommandCard,
  renderConfirmCard,
  renderSceneBadges,
  renderSceneCard,
  renderSceneStrip,
  renderSimExecution,
  renderSimReadiness,
  renderSimDecision,
  renderSlotStrip,
  renderVerdictCard,
  renderDetect,
} from '../../html/static/js/render.js';
import { HttpBackend } from '../../html/static/js/backend-http.js';
import { createStore, initialState, reduce } from '../../html/static/js/state.js';

test('음성 확인 카드에서 STT 원문·정규화·해석을 각각 보여준다', () => {
  const store = createStore();
  store.dispatch({ type: 'sim-demo-result', result: {
    decision: 'CONFIRM', source: 'stt_final',
    raw_transcript: '에이 자재를 벨트로 갖다 놔',
    normalized_transcript: 'A 자재를 컨베이어로 옮겨',
    stt_confidence: 0.82,
    confirmation: {
      token: 'voice_1', utterance: 'A 자재를 컨베이어로 옮겨',
      action: 'transfer', material: 'material_a', summary: 'A 자재 이송',
      remaining_sec: 30, evidence: { material_korean: 'A 자재' },
    },
    job: null,
  } });
  const html = renderConfirmCard(store.get());
  assert.match(html, /인식한 명령.*에이 자재를 벨트로 갖다 놔/s);
  assert.match(html, /정규화 결과.*A 자재를 컨베이어로 옮겨/s);
  assert.match(html, /STT confidence.*0\.82/s);
  assert.match(html, /확인 — 시뮬레이션에서 실행/);
  assert.equal(store.get().simDemo.job, null);
});

const NOTICE = 'Gazebo 시뮬레이션 · 실제 로봇 아님';

function confirmation(overrides = {}) {
  return {
    token: 'simconfirm_abc123',
    summary: 'A자재를 컨베이어로 옮기겠습니다.',
    action: 'transfer',
    material: 'material_a',
    utterance: '그거 컨베이어로 좀 옮겨줘',
    ttl_sec: 60,
    remaining_sec: 60,
    is_simulated: true,
    evidence: {
      rule_decision: 'PASS_THROUGH',
      material_korean: 'A자재',
      classifier: {
        intent: 'transfer',
        material_id: 'material_a',
        confidence: 0.91,
        model_id: 'qwen3-8b-awq',
        min_confidence: 0.7,
        ok: true,
      },
      state: [
        { model: 'material_a', korean: 'A자재', state: null, has_checkpoint: false },
        { model: 'material_b', korean: 'B자재', state: 'held_on_target', has_checkpoint: false },
      ],
    },
    ...overrides,
  };
}

function storeWithConfirmation(extra = {}) {
  const store = createStore();
  store.dispatch({
    type: 'sim-demo-result',
    result: {
      decision: 'CONFIRM',
      utterance: '그거 컨베이어로 좀 옮겨줘',
      intent: 'transfer',
      material: 'material_a',
      job: null,
      simulation_notice: NOTICE,
      confirmation: confirmation(extra),
    },
  });
  return store;
}

// ── 상태 ──────────────────────────────────────────────────────────────
test('CONFIRM 응답은 확인 대기를 세우고 작업을 세우지 않는다', () => {
  const state = storeWithConfirmation().get();
  assert.equal(state.simDemo.confirmation.token, 'simconfirm_abc123');
  assert.equal(state.simDemo.job, null, '확인 전에 job이 생겼다');
  assert.equal(state.simDemo.remainingSec, 60);
  assert.equal(state.simDemo.busy, false);
});

test('tick마다 남은 시간이 줄고, 만료되면 확인 대기가 사라진다', () => {
  const store = storeWithConfirmation({ remaining_sec: 3 });
  store.dispatch({ type: 'tick' });
  assert.equal(store.get().simDemo.remainingSec, 2);
  store.dispatch({ type: 'tick' });
  assert.equal(store.get().simDemo.remainingSec, 1);
  store.dispatch({ type: 'tick' });
  assert.equal(store.get().simDemo.confirmation, null, '만료 후에도 카드가 남았다');
  assert.equal(store.get().simDemo.remainingSec, null);
  assert.equal(store.get().simDemo.job, null);
});

test('확인을 지우면 작업이 생기지 않는다(취소 경로)', () => {
  const store = storeWithConfirmation();
  store.dispatch({ type: 'sim-demo-confirm-cleared' });
  assert.equal(store.get().simDemo.confirmation, null);
  assert.equal(store.get().simDemo.job, null);
});

test('전체 복귀 목표 확인은 서버 순서와 goal id를 보여주며 child job은 없다', () => {
  const store = createStore();
  store.dispatch({ type: 'sim-demo-result', result: {
    decision: 'CONFIRM', intent: 'return_all_to_origin', job: null,
    confirmation: {
      kind: 'goal', goal_id: 'simgoal_1', action: 'return_all_to_origin',
      utterance: '컨베이어에 있는 자재를 모두 제자리에 가져다놔',
      summary: '컨베이어의 자재 2개를 순서대로 원래 자리로 돌려놓습니다.',
      plan: [
        { step: 1, material: 'material_c', material_label: 'C자재',
          from: 'slot_1', from_label: '컨베이어 1번 위치', to_label: '원래 자리' },
        { step: 2, material: 'material_a', material_label: 'A자재',
          from: 'slot_3', from_label: '컨베이어 3번 위치', to_label: '원래 자리' },
      ],
    },
  } });

  const state = store.get();
  const html = renderConfirmCard(state);
  assert.equal(state.simDemo.job, null);
  assert.equal(state.simDemo.confirmation.goal_id, 'simgoal_1');
  assert.match(html, /return_all_to_origin/);
  assert.match(html, /실행 순서/);
  assert.match(html, /STEP 01.*C자재.*컨베이어 1번 위치.*원래 자리/s);
  assert.match(html, /STEP 02.*A자재.*컨베이어 3번 위치.*원래 자리/s);
  assert.match(html, /확인을 누르기 전에는 하위 작업이 만들어지지 않습니다/);
  assert.match(html, /data-action="sim-confirm"/);
  assert.match(html, /data-action="sim-cancel"/);
});

test('텍스트 목표 계획 뒤 확인·취소는 기존 goal confirm endpoint를 쓴다', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path, options) => {
    const requestBody = options.body ? JSON.parse(options.body) : null;
    calls.push({ path, method: options.method, body: requestBody });
    if (path === '/v1/sim-demo/command') {
      return {
        ok: true, status: 200,
        json: async () => ({
          decision: 'CONFIRM', intent: 'return_all_to_origin', job: null,
          confirmation: {
            kind: 'goal', goal_id: 'simgoal_web', action: 'return_all_to_origin',
            plan: [{ step: 1, material: 'material_a' }],
          },
        }),
      };
    }
    return {
      ok: true, status: 202,
      json: async () => ({
        goal_id: 'simgoal_web',
        status: requestBody.action === 'confirm' ? 'running' : 'cancelled',
        plan: [{ step: 1, material: 'material_a', job_id: null }],
      }),
    };
  };

  try {
    const backend = new HttpBackend();
    const planned = await backend.simDemoCommand(
      '컨베이어에 있는 자재를 모두 제자리에 가져다놔', 'text');
    assert.equal(planned.decision, 'CONFIRM');
    assert.equal(planned.job, null, '확인 전에 child job이 생겼다');

    const cancelled = await backend.simDemoGoalConfirm(
      planned.confirmation.goal_id, 'cancel');
    const confirmed = await backend.simDemoGoalConfirm(
      planned.confirmation.goal_id, 'confirm');
    assert.equal(cancelled.decision, 'CANCELLED');
    assert.equal(cancelled.status, 'cancelled');
    assert.equal(confirmed.decision, 'RUN');
    assert.equal(confirmed.status, 'running');
    assert.deepEqual(calls.map((call) => [call.path, call.body]), [
      ['/v1/sim-demo/command', {
        mode: 'simulation_demo',
        utterance: '컨베이어에 있는 자재를 모두 제자리에 가져다놔',
        source: 'text',
        // 대화 맥락 분리용 브라우저 세션 id(인증 아님). 이 테스트는 세션을 열지 않았다.
        session_id: null,
      }],
      ['/v1/sim-demo/goals/simgoal_web/confirm', { action: 'cancel' }],
      ['/v1/sim-demo/goals/simgoal_web/confirm', { action: 'confirm' }],
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('서버 상태의 확인 대기를 그대로 복원한다(새로고침)', () => {
  const state = reduce(initialState(), {
    type: 'sim-demo-confirmation', confirmation: confirmation({ remaining_sec: 41 }),
  });
  assert.equal(state.simDemo.confirmation.token, 'simconfirm_abc123');
  assert.equal(state.simDemo.remainingSec, 41);
});

// ── 확인 카드 ─────────────────────────────────────────────────────────
test('확인 카드: 확인·취소 버튼과 해석 근거, 자재 상태, 시뮬레이션 안내가 있다', () => {
  const html = renderConfirmCard(storeWithConfirmation().get());
  assert.match(html, /A자재를 컨베이어로 옮기겠습니다\./);
  assert.match(html, /data-action="sim-confirm"/);
  assert.match(html, /data-action="sim-cancel"/);
  assert.match(html, /확인 만료까지 60초/);
  // 해석 근거 — 모델이 무엇을 냈는지 숨기지 않는다.
  assert.match(html, /해석 근거/);
  assert.match(html, /qwen3-8b-awq/);
  assert.match(html, /transfer/);
  assert.match(html, /0\.91/);
  assert.match(html, /기준 0\.70/);
  // 현재 자재 상태
  assert.match(html, /현재 자재 상태/);
  assert.match(html, /B자재/);
  assert.match(html, /컨베이어에 유지/);
  // 배지는 없고, "확인 전에는 실행 안 됨" 문장은 남는다.
  assert.doesNotMatch(html, />SIM</);
  assert.match(html, /확인을 누르기 전에는 작업이 만들어지지 않습니다/);
});

test('확인 카드: 확인 대기가 없으면 아무것도 그리지 않는다', () => {
  assert.equal(renderConfirmCard(initialState()), '');
});

test('명령 카드: 확인 대기가 있으면 명령 결과 영역에 확인 카드가 들어간다', () => {
  const state = storeWithConfirmation().get();
  const html = renderCommandCard(state, 'server');
  assert.match(html, /id="sim-command-area"/);
  assert.match(html, /data-action="sim-confirm"/);
  // 목업의 명령 칸 구성: 텍스트 + 마이크 on/off + 명령 보내기/지우기 + TTS on/off
  assert.match(html, /id="command-input"/);
  assert.match(html, /data-action="toggle-voice"/);
  assert.match(html, /data-action="generate"/);
  assert.match(html, /data-action="clear-command"/);
  assert.match(html, /TTS 음성/);
});

test('명령 카드: TTS 토글은 기본 꺼짐이고 켜면 on으로 바뀐다', () => {
  const base = initialState();
  // 브라우저가 SpeechSynthesis를 지원하지 않으면 잠긴 채로 그 사실을 적는다.
  const none = renderCommandCard(base, 'server');
  assert.match(none, /data-action="toggle-tts"[^>]*disabled/);
  assert.match(none, /TTS 음성 없음/);

  const off = renderCommandCard(
    { ...base, tts: { supported: true, enabled: false } }, 'server');
  assert.doesNotMatch(off, /data-action="toggle-tts"[^>]*disabled/);
  assert.match(off, /TTS 음성 off/);
  assert.match(off, /aria-pressed="false"/);

  const on = renderCommandCard(
    { ...base, tts: { supported: true, enabled: true } }, 'server');
  assert.match(on, /TTS 음성 on/);
  assert.match(on, /aria-pressed="true"/);
  // 무엇을 읽는지 화면이 적는다.
  assert.match(on, /판단·확인·작업 시작·완료·정지 결과만 짧게 읽습니다/);
});

test('명령 카드: 확인 대기가 없으면 확인 버튼이 화면에 없다', () => {
  const html = renderCommandCard(initialState(), 'server');
  assert.doesNotMatch(html, /data-action="sim-confirm"/);
});

test('명령 카드 머리말에 시뮬레이션 표시 배지를 두지 않는다', () => {
  for (const kind of ['server', 'simulation']) {
    const html = renderCommandCard(initialState(), kind);
    assert.doesNotMatch(html, />SIM</);
    assert.doesNotMatch(html, /sim-badge/);
  }
});

test('화면에는 긴 시뮬레이션 문구를 반복하지 않는다(배지의 title에만 남는다)', () => {
  const state = storeWithConfirmation().get();
  [renderCommandCard(state, 'server'), renderConfirmCard(state),
   renderSceneCard(state), renderSceneBadges(state)].forEach((html) => {
    // 눈에 보이는 본문에는 없고, title 속성에만 있다.
    const visible = html.replace(/title="[^"]*"/g, '');
    assert.doesNotMatch(visible, new RegExp(NOTICE));
  });
});

// ── 가제보 화면 ───────────────────────────────────────────────────────
test('가제보 화면: 시뮬레이션 배지는 없고 작업·정지 배지만 남는다', () => {
  const base = initialState();
  const cases = [
    base,
    { ...base, stopLatched: true },
    {
      ...base,
      simDemo: {
        ...base.simDemo,
        status: {
          enabled: true,
          running_job: { job_id: 'j1', action_label: '이송', material: 'material_a' },
          materials: [],
        },
      },
    },
  ];
  cases.forEach((state) => {
    const html = renderSceneBadges(state);
    assert.doesNotMatch(html, /sim-badge/);
    assert.doesNotMatch(html, />SIM</);
  });
  // 실행 중 작업과 전체 정지 배지는 그대로 남는다.
  assert.match(renderSceneBadges(cases[1]), /전체 정지/);
  assert.match(renderSceneBadges(cases[2]), /이송/);
  assert.doesNotMatch(renderSceneCard(base), />SIM</);
});

test('가제보 화면 아래에 현재 자재 상태 띠가 나온다', () => {
  const base = initialState();
  const state = {
    ...base,
    simDemo: {
      ...base.simDemo,
      status: {
        enabled: true,
        running_job: null,
        materials: [
          { model: 'material_a', korean: 'A자재', record: null },
          { model: 'material_b', korean: 'B자재', record: { state: 'held_on_target' } },
        ],
      },
    },
  };
  const html = renderSceneStrip(state);
  assert.match(html, /현재 자재 상태/);
  assert.match(html, /A자재/);
  assert.match(html, /컨베이어에 유지/);
});

// ── 안전 판단 및 실행: 시뮬레이션 작업 구역 ───────────────────────────
test('안전 판단 및 실행 칸에 시뮬레이션 작업 진행과 시연 정지가 나온다', () => {
  const base = initialState();
  const state = {
    ...base,
    simDemo: {
      ...base.simDemo,
      status: {
        enabled: true,
        running_job: {
          job_id: 'j1', action_label: '컨베이어로 이송', material: 'material_a',
          progress: [{ no: 2, of: 5, label: '집기' }],
        },
        materials: [],
        state: {},
      },
      job: null,
    },
  };
  const html = renderSimExecution(state);
  assert.match(html, /컨베이어로 이송/);
  assert.match(html, /2\/5 집기/);
  assert.match(html, /data-action="sim-stop"/);
  assert.doesNotMatch(html, />SIM</);
});

test('시뮬레이션 시연이 꺼져 있으면 작업 구역을 그리지 않는다', () => {
  assert.equal(renderSimExecution(initialState()), '');
});

// ── 명령 감지(머리말) ─────────────────────────────────────────────────
test('명령 감지는 지금 무엇을 받고 있는지 그대로 보여준다', () => {
  const base = initialState();
  assert.match(renderDetect(base), /명령 감지 대기/);
  assert.match(renderDetect({ ...base, command: '옮겨' }), /입력 중/);
  assert.match(
    renderDetect({ ...base, stt: { ...base.stt, recording: true } }),
    /음성 듣는 중/,
  );
  assert.match(
    renderDetect({ ...base, simDemo: { ...base.simDemo, busy: true } }),
    /명령 해석 중/,
  );
  assert.match(
    renderDetect({ ...base, simDemo: { ...base.simDemo, confirmation: confirmation() } }),
    /확인 대기/,
  );
});

// ── 컨베이어 슬롯 ─────────────────────────────────────────────────────
function withSlots(overrides = {}) {
  const base = initialState();
  return {
    ...base,
    simDemo: {
      ...base.simDemo,
      status: {
        enabled: true,
        running_job: null,
        state: {},
        materials: [],
        conveyor: {
          enabled: true,
          slot_count: 3,
          next_slot: 'slot_2',
          next_slot_label: '컨베이어 2번 위치',
          full: false,
          slots: [
            { slot: 'slot_1', label: '컨베이어 1번 위치', number: 1,
              occupied: true, model: 'material_a', korean: 'A자재' },
            { slot: 'slot_2', label: '컨베이어 2번 위치', number: 2,
              occupied: false, model: null, korean: null },
            { slot: 'slot_3', label: '컨베이어 3번 위치', number: 3,
              occupied: false, model: null, korean: null },
          ],
          ...overrides,
        },
      },
    },
  };
}

test('슬롯 현황 띠: 번호와 점유 자재를 보여준다', () => {
  const html = renderSlotStrip(withSlots());
  assert.match(html, /컨베이어 위치/);
  assert.match(html, /A자재/);
  assert.match(html, /비어 있음/);
  assert.match(html, /다음 자리: 컨베이어 2번 위치/);
  // 번호가 칩마다 붙는다.
  ['1', '2', '3'].forEach((n) =>
    assert.match(html, new RegExp(`class="slot-no">${n}<`)));
});

test('슬롯 현황 띠: 모두 차면 차단 예고를 적는다', () => {
  const html = renderSlotStrip(withSlots({
    full: true, next_slot: null, next_slot_label: null,
    slots: [
      { slot: 'slot_1', label: '컨베이어 1번 위치', number: 1, occupied: true, korean: 'A자재' },
      { slot: 'slot_2', label: '컨베이어 2번 위치', number: 2, occupied: true, korean: 'B자재' },
      { slot: 'slot_3', label: '컨베이어 3번 위치', number: 3, occupied: true, korean: 'C자재' },
    ],
  }));
  assert.match(html, /모두 참 — 다음 이송은 차단됩니다/);
  assert.doesNotMatch(html, /비어 있음/);
});

test('슬롯 기능이 꺼져 있으면 띠를 그리지 않는다', () => {
  assert.equal(renderSlotStrip(initialState()), '');
  assert.equal(renderSlotStrip(withSlots({ enabled: false, slots: [] })), '');
});

test('확인 카드: 서버가 배정한 슬롯을 사용자에게 보인다', () => {
  const store = createStore();
  store.dispatch({
    type: 'sim-demo-result',
    result: {
      decision: 'CONFIRM', utterance: '그거 컨베이어로 좀 옮겨줘',
      job: null, simulation_notice: NOTICE,
      slot: 'slot_2', slot_label: '컨베이어 2번 위치',
      confirmation: confirmation({
        summary: 'A자재를 컨베이어 2번 위치로 옮기겠습니다.',
        slot: 'slot_2',
        evidence: {
          material_korean: 'A자재', slot: 'slot_2',
          slot_label: '컨베이어 2번 위치',
          classifier: { intent: 'transfer', material_id: 'material_a',
                        confidence: 0.95, model_id: 'qwen3-8b-awq', ok: true },
          state: [],
        },
      }),
    },
  });
  const html = renderConfirmCard(store.get());
  assert.match(html, /A자재를 컨베이어 2번 위치로 옮기겠습니다\./);
  assert.match(html, /대상 위치/);
  assert.match(html, /<strong>컨베이어 2번 위치<\/strong>/);
  assert.match(html, /서버가 배정/);
});

// ── "안전 판단 및 실행" 카드의 시뮬레이션 실행 준비 ────────────────────
function readiness(overrides = {}) {
  return {
    action: 'transfer', material: 'material_a', material_korean: 'A자재',
    origin: '원래 팔레트', target: '컨베이어 1번 위치',
    slot: 'slot_1', slot_label: '컨베이어 1번 위치',
    status: 'ready', status_label: '실행 준비됨',
    checks: [
      // 서버가 실제로 내는 사유다 — 슬롯 이름을 되풀이하지 않는다.
      { key: 'geometry', label: 'geometry (슬롯 자세)', ok: true, recheck: false,
        detail: '배정된 자리의 자세가 MoveIt 검증을 통과한 값' },
      { key: 'occupancy', label: '슬롯 점유', ok: true, recheck: true,
        detail: '배정된 자리가 비어 있음을 기록으로 확인' },
      { key: 'scene', label: 'scene hash · 충돌 · 자재 pose', ok: null, recheck: true,
        detail: '작업 시작 직후 관측으로 다시 검사한다' },
    ],
    ...overrides,
  };
}

function readyState(extra = {}) {
  const store = createStore();
  store.dispatch({
    type: 'sim-demo-result',
    result: {
      decision: 'CONFIRM', utterance: '그거 컨베이어로 좀 옮겨줘',
      job: null, simulation_notice: NOTICE, slot: 'slot_1',
      confirmation: confirmation({
        summary: 'A자재를 컨베이어 1번 위치로 옮기겠습니다.',
        slot: 'slot_1',
        evidence: { material_korean: 'A자재', slot: 'slot_1',
                    slot_label: '컨베이어 1번 위치',
                    classifier: { intent: 'transfer', material_id: 'material_a',
                                  confidence: 0.95, model_id: 'q', ok: true },
                    state: [], readiness: readiness(extra) },
      }),
    },
  });
  return store.get();
}

test('안전 판단 카드: 자재·출발/도착·슬롯·검증 상태·실행 준비됨을 보여준다', () => {
  const html = renderVerdictCard(readyState());
  assert.match(html, /실행 준비됨/);
  assert.match(html, /A자재/);
  assert.match(html, /원래 팔레트/);
  assert.match(html, /컨베이어 1번 위치/);
  assert.match(html, /배정 슬롯/);
  assert.match(html, /검증 상태/);
  assert.match(html, /geometry \(슬롯 자세\)/);
  assert.match(html, /슬롯 점유/);
  assert.match(html, /scene hash/);
  // 확인 전이라는 것을 분명히 적는다.
  assert.match(html, /확인을 눌러야 시작됩니다/);
  // 상태 배지는 머리말에 한 번만 나온다(본문에서 되풀이하지 않는다).
  assert.equal((html.match(/실행 준비됨/g) || []).length, 1);
  // 시뮬레이션 표시 배지는 화면 어디에도 없다.
  assert.doesNotMatch(html, />SIM</);
});

test('안전 판단 카드: "검증 상태" 제목은 어떤 상태에서도 한 번만 나온다', () => {
  // 제목이 두 번 찍히면 같은 근거 묶음이 둘로 보인다. 준비 구역이 유일한
  // 출처이므로, 계획 판정·작업 진행·정지 래치가 같은 카드에 겹쳐도 늘지 않는다.
  const heads = (html) => (html.match(/검증 상태/g) || []).length;
  const ready = readyState();
  assert.equal(heads(renderVerdictCard(ready)), 1);
  assert.equal(heads(renderSimReadiness(ready)), 1);

  const withPlan = (verdict) => ({
    ...ready, plan: { steps: [] },
    validation: { verdict, detail: '판정 문구', clarification: '무엇을 옮길까요',
                  reasonCodes: ['capability.profile_incomplete'],
                  rules: [], blocked: [], missing: [], fixes: [] },
  });
  for (const verdict of ['PASS', 'BLOCK', 'ASK']) {
    assert.equal(heads(renderVerdictCard(withPlan(verdict))), 1, verdict);
  }

  const job = { job_id: 'simjob_1', action: 'transfer', action_label: '컨베이어로 이송',
                material: 'material_a', status: 'finished', exit_code: 0, slot: 'slot_1',
                progress: [{ no: 12, of: 12, label: '안전 home 복귀' }],
                report: { status: 'simulation_transfer_completed', reason_codes: [] } };
  const running = { job_id: 'simjob_1', action: 'transfer', action_label: '컨베이어로 이송',
                    material: 'material_a',
                    progress: [{ no: 5, of: 12, label: 'pick 접근' }] };
  const done = { ...ready, simDemo: { ...ready.simDemo, job } };
  const live = { ...ready, simDemo: { ...ready.simDemo, job: running,
                                      status: { running_job: running } } };
  assert.equal(heads(renderVerdictCard(done)), 1);
  assert.equal(heads(renderVerdictCard(live)), 1);
  assert.equal(heads(renderVerdictCard({ ...ready, stopLatched: true })), 1);

  // 준비 구역이 없으면 제목도 없다 — 빈 제목만 남지 않는다.
  assert.equal(heads(renderVerdictCard(initialState())), 0);
});

test('안전 판단 카드: 아직 보지 않은 검사를 통과로 적지 않는다', () => {
  const html = renderSimReadiness(readyState());
  // scene은 ok:null → "실행 직전 검사"로만 보인다.
  assert.match(html, /실행 직전 검사/);
  assert.match(html, /실행 직전 재검증/);
  // 통과 배지가 검사 수만큼 무한정 찍히지 않는다(ok:true 둘뿐).
  assert.equal((html.match(/>통과</g) || []).length, 2);
});

test('안전 판단 카드: 전체 정지 래치에는 정지 해제 단추가 함께 나온다', () => {
  // 래치 해제 지점은 예전에 "새 계획 수락" 하나뿐이었다. 이 화면은 계획을
  // 만들지 않으므로 단추가 없으면 정지를 풀 길이 없다(실측 2026-09-22).
  const base = initialState();
  const latched = renderVerdictCard({ ...base, stopLatched: true });
  assert.match(latched, /전체 정지가 걸려 있습니다/);
  assert.match(latched, /data-action="release-stop"/);
  assert.match(latched, /정지 해제/);
  // "새 계획을 생성하면 풀립니다"는 이 화면에서 사실이 아니다 — 남기지 않는다.
  assert.doesNotMatch(latched, /새 계획을 생성하면/);
  // 진행 중인 동작이 있으면 서버가 거부한다는 것을 적는다.
  assert.match(latched, /진행 중인 동작이 있으면/);

  // 래치가 없으면 해제 단추도 없다.
  const clear = renderVerdictCard(base);
  assert.doesNotMatch(clear, /data-action="release-stop"/);
  assert.doesNotMatch(clear, /전체 정지가 걸려 있습니다/);
});

test('정지 해제: 서버가 풀었을 때만 래치가 내려간다', () => {
  const stopped = reduce(
    reduce(initialState(), { type: 'stop-requested' }),
    { type: 'stop-result', record: { scope: 'global', confirmed: true } },
  );
  assert.equal(stopped.stopLatched, true);
  assert.ok(stopped.stopRecord);

  const released = reduce(stopped, { type: 'stop-released' });
  assert.equal(released.stopLatched, false);
  assert.equal(released.stopRecord, null);

  // 카드를 닫는 것(✕)은 래치를 풀지 않는다 — 둘은 다른 동작이다.
  const dismissed = reduce(stopped, { type: 'stop-dismissed' });
  assert.equal(dismissed.stopLatched, true);
  assert.equal(dismissed.stopRecord, null);
});

test('안전 판단 카드: 확인 대기가 없으면 준비 구역을 그리지 않는다', () => {
  assert.equal(renderSimReadiness(initialState()), '');
  assert.doesNotMatch(renderVerdictCard(initialState()), /실행 준비됨/);
});

test('안전 판단 카드: 시뮬레이션 ASK/BLOCK 사유를 같은 카드에 보인다', () => {
  const base = initialState();
  const ask = { ...base, simDemo: { ...base.simDemo,
    result: { decision: 'ASK', reason: '어느 자재인지 알 수 없습니다' } } };
  const askHtml = renderVerdictCard(ask);
  assert.match(askHtml, /시뮬레이션 작업 ASK/);
  assert.match(askHtml, /어느 자재인지 알 수 없습니다/);
  assert.match(askHtml, /A\/B\/C 자재와 대상 위치를 지정하세요/);

  const full = { ...base, simDemo: { ...base.simDemo,
    result: { decision: 'BLOCK', reason: '컨베이어 3개 위치가 모두 찼습니다' },
    status: { conveyor: { full: true } } } };
  const blockHtml = renderVerdictCard(full);
  assert.match(blockHtml, /시뮬레이션 작업 BLOCK/);
  assert.match(blockHtml, /하나를 원래 자리로 돌려놓으면/);
});

test('안전 판단 카드: 확인 대기가 있으면 ASK/BLOCK 구역을 겹쳐 그리지 않는다', () => {
  assert.equal(renderSimDecision(readyState()), '');
});

test('자유 pick/place BLOCK에는 할 수 있는 길을 안내한다', () => {
  const base = initialState();
  const blocked = { ...base, validation: {
    verdict: 'BLOCK', detail: '집기·놓기는 지원하지 않습니다',
    reasonCodes: ['capability.skill_unsupported'], blocked: [] } };
  const html = renderVerdictCard(blocked);
  assert.match(html, /BLOCK/);
  assert.match(html, /capability\.skill_unsupported/);
  assert.match(html, /시뮬레이션 작업 명령에서 A\/B\/C 자재와 대상 위치를 지정하세요/);
  assert.match(html, /자유 집기·놓기 계획은 그대로 차단됩니다/);
});

test('안전 판단 카드: 준비 근거가 없으면 실행 준비됨이라고 말하지 않는다', () => {
  // 서버가 readiness를 주지 않는 구성(옛 서버 등). 머리말만 바뀌고 본문이
  // 비는 일이 없어야 한다 — 실측으로 겪었다.
  const store = createStore();
  store.dispatch({
    type: 'sim-demo-result',
    result: { decision: 'CONFIRM', job: null,
      confirmation: confirmation({ evidence: { material_korean: 'A자재',
        classifier: { intent: 'transfer', ok: true }, state: [] } }) },
  });
  const html = renderVerdictCard(store.get());
  assert.doesNotMatch(html, /실행 준비됨/);
  assert.equal(renderSimReadiness(store.get()), '');
  // 계획 경로의 실행 버튼은 그대로 있다.
  assert.match(html, /data-action="execute"/);
});

test('안전 판단 카드: 시뮬레이션 준비 중에는 계획용 실행 버튼을 띄우지 않는다', () => {
  const html = renderVerdictCard(readyState());
  assert.doesNotMatch(html, /data-action="execute"/);
  assert.doesNotMatch(html, /계획이 없습니다/);
});

test('계획 경로로 넘어가면 낡은 시뮬레이션 ASK를 남기지 않는다', () => {
  const store = createStore();
  store.dispatch({ type: 'sim-demo-result',
    result: { decision: 'ASK', reason: '어느 자재인지 알 수 없습니다' } });
  assert.match(renderVerdictCard(store.get()), /시뮬레이션 작업 ASK/);
  // 다음 발화가 시뮬레이션 명령이 아니었다.
  store.dispatch({ type: 'sim-demo-passed-through' });
  assert.equal(store.get().simDemo.result, null);
  assert.equal(store.get().simDemo.busy, false);
  const html = renderVerdictCard(store.get());
  assert.doesNotMatch(html, /시뮬레이션 작업 ASK/);
  // 계획 안내가 다시 자리를 갖는다.
  assert.match(html, /계획을 생성하면/);
});

test('시뮬레이션 판단이 답이면 계획 안내·실행 버튼을 겹쳐 띄우지 않는다', () => {
  const base = initialState();
  const state = { ...base, simDemo: { ...base.simDemo,
    result: { decision: 'ASK', reason: '시뮬레이션 작업 명령에서 A/B/C 자재와 대상 위치를 지정하세요' } } };
  const html = renderVerdictCard(state);
  assert.match(html, /시뮬레이션 작업 ASK/);
  assert.doesNotMatch(html, /계획을 생성하면/);
  assert.doesNotMatch(html, /data-action="execute"/);
  assert.doesNotMatch(html, /계획이 없습니다/);
  // 사유가 이미 안내를 담고 있으면 안내를 되풀이하지 않는다.
  assert.equal((html.match(/대상 위치를 지정하세요/g) || []).length, 1);
});

test('실행 준비 카드: 같은 검증 근거를 중복 렌더하지 않는다', () => {
  const html = renderSimReadiness(readyState());
  // 점유 근거는 한 번만 나온다.
  assert.equal((html.match(/비어 있음을 기록으로 확인/g) || []).length, 1);
  // 검사별 사유도 각각 한 번씩이다.
  ['배정된 자리의 자세가 MoveIt 검증을 통과한 값',
   '작업 시작 직후 관측으로 다시 검사한다'].forEach((detail) => {
    assert.equal((html.match(new RegExp(detail, 'g')) || []).length, 1, detail);
  });
  // 검사 행도 셋뿐이다(같은 key가 두 번 그려지지 않는다).
  assert.equal((html.match(/class="row-label"/g) || []).length, 3 + 3);
});

test('실행 준비 카드: 슬롯 이름을 사유마다 되풀이하지 않는다', () => {
  const html = renderSimReadiness(readyState());
  // "배정 슬롯" 행과 "출발 → 도착" 행이 이름을 갖는다 — 사유는 갖지 않는다.
  assert.equal((html.match(/컨베이어 1번 위치/g) || []).length, 2);
});

test('실행 준비 카드: 자연어로 해석한 출발·도착 자원과 배정 슬롯을 한 번씩 표시한다', () => {
  const html = renderSimReadiness(readyState({
    origin: '1번 팔레트', target: '컨베이어 2번 위치',
    source_resource: 'loc_pallet_1', destination_resource: 'slot_2',
    slot: 'slot_2', slot_label: '컨베이어 2번 위치',
  }));
  assert.equal((html.match(/class="row-label">출발 → 도착/g) || []).length, 1);
  assert.equal((html.match(/class="row-label">배정 슬롯/g) || []).length, 1);
  assert.equal((html.match(/1번 팔레트/g) || []).length, 1);
  assert.equal((html.match(/loc_pallet_1/g) || []).length, 1);
  assert.equal((html.match(/slot_2/g) || []).length, 1);
  // 도착 이름은 경로와 배정 슬롯 행에 각각 한 번씩만 나온다.
  assert.equal((html.match(/컨베이어 2번 위치/g) || []).length, 2);
});

test('실행 준비 카드: 자원 ID가 없는 옛 응답도 그리고 ID는 이스케이프한다', () => {
  assert.doesNotMatch(renderSimReadiness(readyState()), /undefined|null/);
  const html = renderSimReadiness(readyState({ source_resource: '<source>' }));
  assert.match(html, /&lt;source&gt;/);
  assert.doesNotMatch(html, /<source>/);
});

test('화면 어디에도 시뮬레이션 표시 배지가 없다', () => {
  const states = [initialState(), readyState(), withSlots()];
  const render = [renderCommandCard, renderConfirmCard, renderSceneCard,
                  renderSceneBadges, renderSceneStrip, renderSimExecution,
                  renderSimReadiness, renderSimDecision, renderVerdictCard];
  states.forEach((state) => {
    render.forEach((fn) => {
      const html = fn.length > 1 ? fn(state, 'server') : fn(state);
      assert.doesNotMatch(html, /sim-badge/, fn.name);
      assert.doesNotMatch(html, />SIM</, fn.name);
      // 대체 배지·대체 문구도 넣지 않는다.
      assert.doesNotMatch(html, new RegExp(NOTICE), fn.name);
    });
  });
});
