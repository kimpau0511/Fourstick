/** 화면 상태·시뮬레이션 백엔드 테스트 (node --test).
 *
 * 브라우저 없이 돌린다. 요구된 상태를 각각 확인한다:
 * PASS / BLOCK / ASK / 실행 중 / 실행 완료 / 개별 취소 / STOP 확인 / STOP 미확인.
 *
 *   node --test tests/web/state.test.mjs
 *   ./scripts/run_web_ui_tests.sh
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  EXECUTION,
  VERDICT,
  canCancel,
  canExecute,
  createStore,
  initialState,
  isActive,
  isStopped,
  progressPercent,
  reduce,
  verdictFromDecision,
} from '../../html/static/js/state.js';
import { SimulationBackend, scenarioFor } from '../../html/static/js/backend-sim.js';
import { planFromBundle, validationFromBundle } from '../../html/static/js/backend-http.js';
import {
  activeNodeIndex,
  describeStep,
  planResourceMapping,
  robotById,
  setResourceLabels,
  simNodesFromPlan,
  withWorkcell,
  workcellResources,
} from '../../html/static/js/catalog.js';

/** 테스트용 빠른 시간 설정. 화면 기본값(2.2초/1.2초)은 백엔드 상수다. */
const FAST = { stepMs: 40, confirmMs: 30, planMs: 10 };

// ── 도우미 ────────────────────────────────────────────────────────────
function waitFor(predicate, { timeout = 4000, interval = 20 } = {}) {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const timer = setInterval(() => {
      if (predicate()) {
        clearInterval(timer);
        resolve();
      } else if (Date.now() - started > timeout) {
        clearInterval(timer);
        reject(new Error('조건이 시간 안에 참이 되지 않았다'));
      }
    }, interval);
  });
}

/** 백엔드와 저장소를 실제 배선과 같은 방식으로 잇는다. */
function connect(backend, store) {
  backend.onEvent((event) => {
    if (event.kind === 'step-started') store.dispatch({ type: 'step-started', step: event.step });
    if (event.kind === 'step-result') store.dispatch({ type: 'step-result', result: event.result });
    if (event.kind === 'completed') store.dispatch({ type: 'execution-completed' });
    if (event.kind === 'failed') store.dispatch({ type: 'execution-failed' });
    if (event.kind === 'cancel-result') {
      store.dispatch({ type: 'cancel-result', record: event.record });
    }
    if (event.kind === 'stop-result') {
      store.dispatch({ type: 'stop-result', record: event.record });
    }
  });
}

async function planned(store, backend, utterance) {
  store.dispatch({ type: 'plan-requested' });
  const result = await backend.createPlan(utterance);
  store.dispatch({
    type: 'plan-received',
    plan: result.plan,
    validation: result.validation,
    stopLatchCleared: result.stopLatchCleared,
  });
  return result;
}

async function started(store, backend) {
  const result = await backend.startExecution();
  store.dispatch({ type: 'execution-started', executionId: result.executionId, step: 1 });
  return result;
}

// ── 판정 매핑 ─────────────────────────────────────────────────────────
test('서버 판정을 PASS/BLOCK/ASK로만 바꾼다', () => {
  assert.equal(verdictFromDecision('allow'), 'PASS');
  assert.equal(verdictFromDecision('block'), 'BLOCK');
  assert.equal(verdictFromDecision('ask'), 'ASK');
  assert.equal(verdictFromDecision('approved'), null);
});

test('시나리오 선택 규칙이 문구로 재현된다', () => {
  assert.equal(scenarioFor('1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘'), 'pass');
  assert.equal(scenarioFor('1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘'), 'block_skill');
  assert.equal(scenarioFor('3번 팔레트로 이동해줘'), 'block_resource');
  assert.equal(scenarioFor('그거 저기로 옮겨줘'), 'ask');
  assert.equal(scenarioFor('아무 말', 'ask'), 'ask');
});

// ── PASS ──────────────────────────────────────────────────────────────
test('PASS: 실행 시작을 누를 수 있다', async () => {
  const store = createStore();
  const backend = new SimulationBackend(FAST);
  const result = await planned(store, backend, '1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘');

  assert.equal(result.validation.verdict, VERDICT.PASS);
  assert.deepEqual(result.validation.reasonCodes, []);
  assert.ok(store.get().plan.steps.length > 0);
  assert.equal(canExecute(store.get()), true);
  // PASS여도 자동 실행은 없다 — 상태는 여전히 idle이다.
  assert.equal(store.get().execution.status, EXECUTION.IDLE);
  // FR3에서 지원하지 않는 스킬이 계획에 없다.
  const skills = store.get().plan.steps.map((step) => step.skill);
  assert.ok(!skills.includes('pick'));
  assert.ok(!skills.includes('place'));
});

// ── BLOCK ─────────────────────────────────────────────────────────────
test('BLOCK: 차단 이유와 ReasonCode를 주고 실행을 막는다', async () => {
  const store = createStore();
  const backend = new SimulationBackend(FAST);
  const result = await planned(store, backend, '1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘');

  assert.equal(result.validation.verdict, VERDICT.BLOCK);
  assert.ok(result.validation.reasonCodes.includes('capability.skill_unsupported'));
  assert.match(result.validation.detail, /custom adapter/);
  assert.ok(result.validation.blocked.length >= 1);
  assert.equal(canExecute(store.get()), false);
});

test('BLOCK: 요청에 없는 자원은 다른 ReasonCode로 막는다', async () => {
  const store = createStore();
  const backend = new SimulationBackend(FAST);
  const result = await planned(store, backend, '3번 팔레트로 이동해줘');

  assert.equal(result.validation.verdict, VERDICT.BLOCK);
  assert.ok(result.validation.reasonCodes.includes('plan.resource_mismatch'));
  assert.equal(canExecute(store.get()), false);
  // 어느 행이 어긋났는지 화면에 줄 수 있어야 한다.
  assert.ok(store.get().plan.resources.some((row) => row.match === false));
});

// ── ASK ───────────────────────────────────────────────────────────────
test('ASK: 부족한 정보와 수정 방법을 주고 실행을 막는다', async () => {
  const store = createStore();
  const backend = new SimulationBackend(FAST);
  const result = await planned(store, backend, '그거 저기로 옮겨줘');

  assert.equal(result.validation.verdict, VERDICT.ASK);
  assert.ok(result.validation.reasonCodes.includes('plan.clarification_required'));
  assert.ok(result.validation.missing.length >= 1);
  assert.ok(result.validation.fixes.length >= 1);
  assert.equal(canExecute(store.get()), false);
});

// ── 실행 중 ───────────────────────────────────────────────────────────
test('실행 중: 단계가 진행되고 진행률이 올라간다', async () => {
  const store = createStore();
  const backend = new SimulationBackend(FAST);
  connect(backend, store);
  await planned(store, backend, '1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘');
  const execution = await started(store, backend);

  assert.equal(store.get().execution.status, EXECUTION.RUNNING);
  assert.equal(store.get().execution.id, execution.executionId);
  assert.equal(store.get().view, 'workspace');
  assert.equal(isActive(store.get()), true);
  assert.equal(canCancel(store.get()), true);
  // 실행이 시작된 뒤에는 다시 실행할 수 없다.
  assert.equal(canExecute(store.get()), false);

  await waitFor(() => store.get().execution.results.length >= 1);
  const total = store.get().plan.steps.length;
  assert.ok(progressPercent(store.get(), total) >= 0);
  assert.equal(store.get().execution.results[0].taskSuccess, true);
  backend.stopTimer();
});

test('실행 중: 경과 시간은 tick으로만 올라간다', () => {
  let state = initialState();
  state = reduce(state, { type: 'execution-started', executionId: 'exec_1', step: 1 });
  state = reduce(state, { type: 'tick' });
  state = reduce(state, { type: 'tick' });
  assert.equal(state.execution.elapsedSec, 2);
  // 멈춘 실행에서는 시간이 흐르지 않는다.
  state = reduce(state, { type: 'execution-completed' });
  state = reduce(state, { type: 'tick' });
  assert.equal(state.execution.elapsedSec, 2);
});

// ── 실행 완료 ─────────────────────────────────────────────────────────
test('실행 완료: 모든 단계가 결과로 남고 진행률이 100이다', async () => {
  const store = createStore();
  const backend = new SimulationBackend(FAST);
  connect(backend, store);
  await planned(store, backend, '1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘');
  await started(store, backend);

  await waitFor(() => store.get().execution.status === EXECUTION.COMPLETED, { timeout: 15000 });
  const state = store.get();
  assert.equal(state.execution.results.length, state.plan.steps.length);
  assert.equal(progressPercent(state, state.plan.steps.length), 100);
  assert.ok(state.execution.results.every((result) => result.status === 'done'));
  assert.equal(isStopped(state), false);
});

// ── 개별 실행 취소 ────────────────────────────────────────────────────
test('개별 취소: 이 실행만 멈추고 결과가 따로 남는다', async () => {
  const store = createStore();
  const backend = new SimulationBackend(FAST);
  connect(backend, store);
  await planned(store, backend, '1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘');
  const execution = await started(store, backend);

  store.dispatch({ type: 'cancel-requested' });
  assert.equal(store.get().execution.status, EXECUTION.CANCELLING);
  await backend.cancelExecution(execution.executionId);
  await waitFor(() => store.get().execution.status === EXECUTION.CANCELLED);

  const state = store.get();
  assert.equal(state.cancelRecord.scope, 'execution');
  assert.equal(state.cancelRecord.reasonCode, 'exec.canceled');
  assert.equal(state.cancelRecord.result, 'confirmed');
  assert.equal(state.cancelRecord.executionId, execution.executionId);
  assert.ok(state.cancelRecord.stoppedAtStep >= 1);
  // 개별 취소는 전체 정지가 아니다 — 래치도, 전체 정지 기록도 없다.
  assert.equal(state.stopLatched, false);
  assert.equal(state.stopRecord, null);
  assert.equal(isStopped(state), true);
  // 취소된 단계는 성공으로 기록되지 않는다.
  const cancelled = state.execution.results.find((r) => r.status === 'cancelled');
  assert.ok(cancelled);
  assert.equal(cancelled.taskSuccess, false);
  assert.equal(cancelled.reasonCode, 'exec.canceled');
});

test('개별 취소 미확인: 확인 시각이 없고 확인됨으로 표시되지 않는다', async () => {
  const store = createStore();
  const backend = new SimulationBackend({ ...FAST, scenario: 'cancel_unconfirmed' });
  connect(backend, store);
  await planned(store, backend, '1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘');
  const execution = await started(store, backend);
  await backend.cancelExecution(execution.executionId);
  await waitFor(() => store.get().cancelRecord !== null);

  assert.equal(store.get().cancelRecord.result, 'unconfirmed');
  assert.equal(store.get().cancelRecord.confirmedTime, null);
});

// ── 전체 정지 ─────────────────────────────────────────────────────────
test('STOP 확인: 전체 정지가 확인되고 래치가 걸린다', async () => {
  const store = createStore();
  const backend = new SimulationBackend(FAST);
  connect(backend, store);
  await planned(store, backend, '1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘');
  await started(store, backend);

  store.dispatch({ type: 'stop-requested' });
  assert.equal(store.get().execution.status, EXECUTION.STOPPING);
  assert.equal(store.get().stopLatched, true);
  await backend.stopAll();
  await waitFor(() => store.get().stopRecord !== null);

  const state = store.get();
  assert.equal(state.execution.status, EXECUTION.GLOBAL_STOPPED);
  assert.equal(state.stopRecord.scope, 'global');
  assert.equal(state.stopRecord.result, 'confirmed');
  assert.equal(state.stopRecord.reasonCode, 'exec.stopped');
  assert.ok(state.stopRecord.confirmedTime);
  assert.equal(state.stopRecord.affectedExecutionCount, 1);
  // 전체 정지는 개별 취소 기록을 만들지 않는다.
  assert.equal(state.cancelRecord, null);
  // 래치가 걸린 동안 실행할 수 없다.
  assert.equal(canExecute(state), false);
});

test('STOP 미확인: 요청은 전송됐지만 확인되지 않은 상태를 성공으로 표시하지 않는다', async () => {
  const store = createStore();
  const backend = new SimulationBackend({ ...FAST, scenario: 'stop_unconfirmed' });
  connect(backend, store);
  await planned(store, backend, '1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘');
  await started(store, backend);
  await backend.stopAll();
  await waitFor(() => store.get().stopRecord !== null);

  const record = store.get().stopRecord;
  assert.equal(record.result, 'unconfirmed');
  assert.equal(record.confirmed, false);
  assert.equal(record.confirmedTime, null);
  assert.match(record.detail, /확인하지 못했다/);
  // 확인 실패도 래치는 걸려 있어야 한다(실행을 열어 주지 않는다).
  assert.equal(store.get().stopLatched, true);
  assert.equal(canExecute(store.get()), false);
});

test('정지 래치는 새 계획을 받을 때만 풀린다', async () => {
  const store = createStore();
  const backend = new SimulationBackend(FAST);
  connect(backend, store);
  await planned(store, backend, '1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘');
  await started(store, backend);
  await backend.stopAll();
  await waitFor(() => store.get().stopRecord !== null);
  assert.equal(store.get().stopLatched, true);

  await planned(store, backend, '1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘');
  assert.equal(store.get().stopLatched, false);
  assert.equal(canExecute(store.get()), true);
});

// ── 상태 경계 ─────────────────────────────────────────────────────────
test('실행 중에는 로봇을 바꾸지 않는다', () => {
  let state = initialState();
  state = reduce(state, { type: 'execution-started', executionId: 'exec_1', step: 1 });
  const next = reduce(state, { type: 'robot', robotId: 'ur5e' });
  assert.equal(next.robotId, 'fairino_fr3');
});

test('로봇을 바꾸면 계획과 판정이 사라진다(재생성이 필요하다)', async () => {
  const store = createStore();
  const backend = new SimulationBackend(FAST);
  await planned(store, backend, '1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘');
  assert.ok(store.get().plan);
  store.dispatch({ type: 'robot', robotId: 'ur5e' });
  assert.equal(store.get().plan, null);
  assert.equal(store.get().validation, null);
  assert.equal(canExecute(store.get()), false);
});

// ── FR3 화면 데이터 ───────────────────────────────────────────────────
test('FR3-WMS + 2F-85 작업 셀 데이터가 요구된 문구와 배지를 그대로 갖는다', () => {
  const robot = robotById('fairino_fr3');
  assert.equal(robot.summary, 'FAIRINO FR3-WMS + 2F-85 · Gazebo workcell simulation');
  assert.equal(robot.adapter, 'FR3 GAZEBO');
  assert.deepEqual(robot.skills, ['home', 'move', 'stop']);
  assert.deepEqual(robot.disabledSkills, ['pick', 'place']);
  assert.equal(
    robot.disabledReason,
    '2F-85 장착 근거, 그리퍼 close 안정성, 파지 관측, pick/place 재검증 미완료',
  );
  const labels = robot.badges.map((badge) => badge.label);
  assert.deepEqual(labels, ['MoveIt2 검증 완료', '시뮬레이션 전용', '실하드웨어 미검증']);
});

test('서버가 준 작업 셀 값이 로봇 카드 데이터를 덮어쓴다', () => {
  const robot = robotById('fairino_fr3');
  // 붙지 않았으면 그대로 둔다 — 없는 연결을 있는 것처럼 만들지 않는다.
  assert.equal(withWorkcell(robot, null), robot);
  assert.equal(withWorkcell(robot, { registered: false }), robot);
  const merged = withWorkcell(robot, {
    registered: true,
    world: 'forstick2_fr3_2f85_workcell',
    supported_skills: ['home', 'move', 'stop'],
  });
  assert.deepEqual(merged.skills, ['home', 'move', 'stop']);
  assert.deepEqual(merged.disabledSkills, ['pick', 'place']);
  assert.match(merged.environment, /forstick2_fr3_2f85_workcell/);
});

test('작업 셀 자원 목록은 서버 대조표에서만 온다', () => {
  assert.deepEqual(workcellResources(null), []);
  assert.deepEqual(workcellResources({ registered: true }), []);
  const rows = workcellResources({
    registered: true,
    resources: {
      loc_pallet_1: { korean: '1번 팔레트', gazebo_model: 'pallet_1', frame: 'pallet_1_frame' },
      mat_a: { korean: 'A자재', gazebo_model: 'material_a', frame: 'material_a_frame' },
    },
    move_poses: { loc_pallet_1: 'pallet_1_approach' },
  });
  assert.equal(rows.length, 2);
  // 위치가 자재보다 먼저 온다.
  assert.equal(rows[0].kind, 'location');
  assert.equal(rows[0].movePose, 'pallet_1_approach');
  assert.equal(rows[1].id, 'mat_a');
  assert.equal(rows[1].movePose, null);
});

test('계획 자원 대조는 대조표에 없는 자원을 모른다고 표시한다', () => {
  const plan = {
    steps: [
      { no: 1, skill: 'move', args: { to: 'loc_pallet_1' } },
      { no: 2, skill: 'move', args: { to: 'loc_nowhere' } },
    ],
  };
  const rows = planResourceMapping(plan, {
    resources: {
      loc_pallet_1: { korean: '1번 팔레트', gazebo_model: 'pallet_1', frame: 'pallet_1_frame' },
    },
    move_poses: { loc_pallet_1: 'pallet_1_approach' },
  });
  assert.equal(rows.length, 2);
  assert.equal(rows[0].known, true);
  assert.equal(rows[0].model, 'pallet_1');
  assert.equal(rows[0].frame, 'pallet_1_frame');
  assert.equal(rows[1].known, false);
  assert.equal(rows[1].model, null);
});

test('미구현 로봇은 수치를 만들지 않고 선택도 되지 않는다', () => {
  ['doosan_m1013', 'kinova_gen3', 'ur5e'].forEach((id) => {
    const robot = robotById(id);
    assert.equal(robot.implemented, false);
    assert.equal(robot.payload, '미확보');
    assert.equal(robot.reach, '미확보');
    assert.deepEqual(robot.skills, []);
  });
});

// ── 계획 표현 ─────────────────────────────────────────────────────────
test('스텝 문구는 스킬과 인자에서 만든다', () => {
  // 자원 이름은 서버 카탈로그에서 온다. 화면에 목록을 두지 않는다.
  setResourceLabels([
    { resource_id: 'loc_pallet_1', display_name: '1번 팔레트' },
    { resource_id: 'obj_a', display_name: 'A자재' },
    { resource_id: 'loc_conveyor', display_name: '컨베이어' },
  ]);
  assert.equal(describeStep('home', {}), '안전 위치 이동');
  assert.equal(describeStep('move', { to: 'loc_pallet_1' }), '1번 팔레트 이동');
  assert.equal(describeStep('pick', { object: 'obj_a', from: 'loc_pallet_1' }), 'A자재 집기');
  assert.equal(describeStep('place', { object: 'obj_a', to: 'loc_conveyor' }), '컨베이어에 배치');
});

test('이름을 모르는 자원은 id를 그대로 보여준다(추측하지 않는다)', () => {
  assert.equal(describeStep('move', { to: 'loc_unknown_9' }), 'loc_unknown_9 이동');
});

test('시뮬레이션 노드를 계획에서 만든다(좌표를 코드에 박지 않는다)', async () => {
  const backend = new SimulationBackend(FAST);
  const { plan } = await backend.createPlan('1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘');
  const nodes = simNodesFromPlan(plan);
  assert.equal(nodes.length, 4);
  assert.deepEqual(
    nodes.map((node) => node.sub),
    ['loc_home', 'loc_pallet_1', 'loc_conveyor', 'loc_home'],
  );
  assert.equal(activeNodeIndex(nodes, 2), 1);
  assert.equal(activeNodeIndex(nodes, 0), -1);
});

// ── 서버 응답 매핑 ────────────────────────────────────────────────────
test('서버 계획 묶음을 화면 모양으로 바꾼다', () => {
  const bundle = {
    request_id: 'req_1',
    plan: {
      plan_id: 'plan_1',
      plan_hash: 'abc123',
      created_at: 1000,
      ttl_sec: 300,
      utterance: '이동해줘',
      steps: [
        { index: 1, skill: 'home', args: {} },
        { index: 2, skill: 'move', args: { to: 'loc_conveyor' } },
      ],
    },
    consistency: {
      request_resources: [{ surface: '컨베이어', resource_id: 'loc_conveyor', kind: 'location' }],
      plan_resources: ['loc_conveyor'],
      only_in_plan: [],
    },
  };
  const plan = planFromBundle(bundle);
  assert.equal(plan.planId, 'plan_1');
  assert.equal(plan.steps.length, 2);
  assert.equal(plan.steps[1].description, '컨베이어 이동');
  assert.equal(plan.resources[0].match, true);
});

test('서버 관문 판정을 PASS/BLOCK/ASK와 ReasonCode로 옮긴다', () => {
  const allow = validationFromBundle({
    validation: { decision: 'allow', reason_code: null, detail: '통과' },
    safety: { rules: [{ code: 'E-SEQ-001', status: 'pass', message: 'ok' }] },
  });
  assert.equal(allow.verdict, 'PASS');
  assert.deepEqual(allow.reasonCodes, []);

  const block = validationFromBundle({
    validation: { decision: 'block', reason_code: 'plan.resource_mismatch', detail: '차단' },
    safety: {
      rules: [
        {
          code: 'E-REQ-001',
          status: 'block',
          message: '자원 불일치',
          reason_code: 'plan.resource_mismatch',
        },
      ],
    },
  });
  assert.equal(block.verdict, 'BLOCK');
  assert.deepEqual(block.reasonCodes, ['plan.resource_mismatch']);
  assert.equal(block.blocked.length, 1);

  const ask = validationFromBundle({
    validation: {
      decision: 'ask',
      reason_code: 'geometry.environment_unavailable',
      detail: '정보 부족',
      geometry: { decision: 'ask' },
    },
    safety: {
      rules: [
        {
          code: 'E-GEOM-001',
          status: 'insufficient_data',
          message: '환경 없음',
          reason_code: 'geometry.environment_unavailable',
        },
      ],
    },
  });
  assert.equal(ask.verdict, 'ASK');
  assert.ok(ask.missing.length >= 1);
  assert.ok(ask.fixes.length >= 1);
});

test('관문 판정이 없으면 통과로 만들지 않는다', () => {
  const result = validationFromBundle({ plan: {}, safety: { rules: [] } });
  assert.equal(result.verdict, 'ASK');
  assert.ok(result.missing.length >= 1);
});
