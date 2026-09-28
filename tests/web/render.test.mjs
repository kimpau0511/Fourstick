/** 화면 그리기 테스트 (node --test). DOM 없이 HTML 문자열을 확인한다.
 *
 * 확인하는 것:
 *  - 승인 UI 문구·버튼이 **어디에도 없다**
 *  - 안전 판단은 PASS / BLOCK / ASK만 보여준다
 *  - PASS에서만 실행 시작 버튼이 열린다
 *  - BLOCK은 차단 이유와 Reason Code를, ASK는 부족한 정보와 수정 방법을 보여준다
 *  - 전체 정지와 개별 취소가 다른 버튼·다른 결과로 나온다
 *  - FR3-WMS 화면 데이터(구성 문구·스킬·비활성 사유·배지)가 그대로 나온다
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import {
  renderBlockCard,
  renderCommandCard,
  renderEventsCard,
  renderExecutionCard,
  renderModal,
  renderPlanCard,
  renderRobotCard,
  renderSimulationCard,
  renderStopCard,
  renderVerdictCard,
  renderVoiceCard,
  renderHardwareCard,
  renderWorkcellCard,
} from '../../html/static/js/render.js';
import { EXECUTION, createStore, initialState, makeEvent } from '../../html/static/js/state.js';
import { SimulationBackend } from '../../html/static/js/backend-sim.js';

const FAST = { stepMs: 40, confirmMs: 30, planMs: 10 };
const ROOT = new URL('../../', import.meta.url).pathname;

/** 화면에서 사라져야 하는 문구(사용자 요구). */
const FORBIDDEN = ['계획 승인', '승인 대기', '승인 완료', '계획 거부', '승인 필수', '승인됨'];

async function stateWith(utterance, { execute = false } = {}) {
  const store = createStore();
  const backend = new SimulationBackend(FAST);
  backend.onEvent((event) => {
    if (event.kind === 'step-started') store.dispatch({ type: 'step-started', step: event.step });
    if (event.kind === 'step-result') store.dispatch({ type: 'step-result', result: event.result });
    if (event.kind === 'completed') store.dispatch({ type: 'execution-completed' });
    if (event.kind === 'cancel-result') store.dispatch({ type: 'cancel-result', record: event.record });
    if (event.kind === 'stop-result') store.dispatch({ type: 'stop-result', record: event.record });
  });
  const result = await backend.createPlan(utterance);
  store.dispatch({
    type: 'plan-received',
    plan: result.plan,
    validation: result.validation,
    stopLatchCleared: true,
  });
  if (execute) {
    const execution = await backend.startExecution();
    store.dispatch({ type: 'execution-started', executionId: execution.executionId, step: 1 });
  }
  return { store, backend };
}

function screenHtml(state) {
  return [
    renderRobotCard(state),
    renderVoiceCard(state),
    renderEventsCard(state),
    renderCommandCard(state, 'simulation'),
    renderPlanCard(state),
    renderVerdictCard(state),
    renderSimulationCard(state),
    renderExecutionCard(state),
    renderStopCard(state.stopRecord),
    renderModal(state),
  ].join('\n');
}

// ── 승인 UI 제거 ──────────────────────────────────────────────────────
test('승인 관련 문구가 화면 어디에도 없다', async () => {
  const cases = [
    initialState(),
    (await stateWith('1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘')).store.get(),
    (await stateWith('1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘')).store.get(),
    (await stateWith('그거 저기로 옮겨줘')).store.get(),
    (await stateWith('1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘', { execute: true })).store.get(),
  ];
  for (const state of cases) {
    const html = screenHtml({ ...state, modal: 'execute' }) + screenHtml({ ...state, modal: 'robot' });
    for (const word of FORBIDDEN) {
      assert.ok(!html.includes(word), `"${word}"가 화면에 남아 있다`);
    }
  }
});

test('진입점 HTML과 스타일에도 승인 문구가 없다', () => {
  const index = readFileSync(`${ROOT}html/index.html`, 'utf-8');
  const css = readFileSync(`${ROOT}html/static/app.css`, 'utf-8');
  for (const word of FORBIDDEN) {
    assert.ok(!index.includes(word), `index.html에 "${word}"가 있다`);
    assert.ok(!css.includes(word), `app.css에 "${word}"가 있다`);
  }
  // 진입점은 목업 레이아웃의 컨테이너를 갖는다.
  ['card-robot', 'card-command', 'card-plan', 'card-verdict', 'card-sim', 'card-execution']
    .forEach((id) => assert.ok(index.includes(`id="${id}"`), `${id} 컨테이너가 없다`));
  assert.ok(index.includes('/static/js/main.js'));
  assert.ok(index.includes('/static/app.css'));
});

// ── PASS / BLOCK / ASK ────────────────────────────────────────────────
test('PASS 화면: 실행 시작이 열리고 PASS만 표시된다', async () => {
  const { store } = await stateWith('1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘');
  const html = renderVerdictCard(store.get());
  assert.ok(html.includes('>PASS<'));
  assert.ok(!html.includes('BLOCK'));
  assert.ok(!html.includes('ASK'));
  assert.ok(html.includes('▶ 실행 시작'));
  assert.ok(!html.includes('data-action="execute" disabled'));
  assert.match(html, /자동으로 실행되지 않습니다/);
});

test('BLOCK 화면: 차단 이유와 Reason Code가 보이고 실행 시작이 잠긴다', async () => {
  const { store } = await stateWith('1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘');
  const html = renderVerdictCard(store.get());
  assert.ok(html.includes('>BLOCK<'));
  assert.ok(html.includes('Reason Code'));
  assert.ok(html.includes('capability.skill_unsupported'));
  assert.match(html, /custom adapter/);
  assert.ok(html.includes('data-action="execute" disabled'));
  assert.match(html, /PASS 상태에서만 실행할 수 있습니다/);
});

test('ASK 화면: 부족한 정보와 수정 방법이 보이고 실행 시작이 잠긴다', async () => {
  const { store } = await stateWith('그거 저기로 옮겨줘');
  const html = renderVerdictCard(store.get());
  assert.ok(html.includes('>ASK<'));
  assert.ok(html.includes('부족한 정보'));
  assert.ok(html.includes('수정 방법'));
  assert.ok(html.includes('plan.clarification_required'));
  assert.ok(html.includes('data-action="execute" disabled'));
});

test('판정 전에는 실행 시작이 잠겨 있다', () => {
  const html = renderVerdictCard(initialState());
  assert.ok(html.includes('data-action="execute" disabled'));
  assert.match(html, /PASS \/ BLOCK \/ ASK/);
});

// ── 실행 중·완료 ──────────────────────────────────────────────────────
test('실행 중 화면: 진행률·타임라인·개별 취소 버튼이 나온다', async () => {
  const { store } = await stateWith('1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘', {
    execute: true,
  });
  const html = renderExecutionCard(store.get());
  assert.ok(html.includes('RUNNING'));
  assert.ok(html.includes('진행률'));
  assert.ok(html.includes('실행 타임라인'));
  assert.ok(html.includes('■ 이 실행 취소'));
  assert.ok(html.includes('EXECUTION SCOPE'));
  // 전체 정지는 이 카드의 버튼이 아니다(헤더에 있다).
  assert.ok(!html.includes('data-action="stop-all"'));
  assert.match(html, /상단 <strong style="color:var\(--danger\)">■ 전체 정지/);
  // 타임라인은 승인 대신 안전 판단을 적는다.
  assert.ok(html.includes('안전 판단 PASS'));
});

test('실행 완료 화면: 5축 결과와 시뮬레이션 표기가 나온다', async () => {
  const { store, backend } = await stateWith('1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘', {
    execute: true,
  });
  await new Promise((resolve) => {
    const timer = setInterval(() => {
      if (store.get().execution.status === EXECUTION.COMPLETED) {
        clearInterval(timer);
        resolve();
      }
    }, 20);
  });
  backend.stopTimer();
  const html = renderExecutionCard(store.get());
  assert.ok(html.includes('완료'));
  assert.ok(html.includes('최종 결과 (5축)'));
  assert.ok(html.includes('단계별 결과'));
  assert.match(html, /실제 로봇 실행으로 집계되지 않습니다/);
});

// ── 개별 취소 vs 전체 정지 ────────────────────────────────────────────
test('개별 취소 결과: execution 범위와 exec.canceled가 보인다', async () => {
  const { store, backend } = await stateWith('1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘', {
    execute: true,
  });
  store.dispatch({ type: 'cancel-requested' });
  await backend.cancelExecution(store.get().execution.id);
  await new Promise((resolve) => {
    const timer = setInterval(() => {
      if (store.get().cancelRecord) {
        clearInterval(timer);
        resolve();
      }
    }, 20);
  });
  const html = renderExecutionCard(store.get());
  assert.ok(html.includes('실행 취소됨'));
  assert.ok(html.includes('execution (이 실행만)'));
  assert.ok(html.includes('exec.canceled'));
  assert.ok(html.includes('취소 확인됨'));
  assert.match(html, /다른 실행과 로봇 전체에는 영향을 주지 않았습니다/);
  // 전체 정지 카드는 따로다 — 개별 취소로는 만들어지지 않는다.
  assert.equal(renderStopCard(store.get().stopRecord), '');
});

test('STOP 확인 결과: global 범위와 exec.stopped, 래치 안내가 보인다', async () => {
  const { store, backend } = await stateWith('1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘', {
    execute: true,
  });
  store.dispatch({ type: 'stop-requested' });
  await backend.stopAll();
  await new Promise((resolve) => {
    const timer = setInterval(() => {
      if (store.get().stopRecord) {
        clearInterval(timer);
        resolve();
      }
    }, 20);
  });
  const html = renderStopCard(store.get().stopRecord);
  assert.ok(html.includes('전체 정지 결과'));
  assert.ok(html.includes('global (전체 시스템)'));
  assert.ok(html.includes('exec.stopped'));
  assert.ok(html.includes('로봇 정지 확인됨'));
  assert.ok(html.includes('STOP 요청 전송됨'));
  assert.ok(html.includes('새 계획 생성 시까지 차단(래치)'));
  // 실행 카드에는 래치 안내가 아니라 전체 정지 상태가 나온다.
  assert.ok(renderVerdictCard(store.get()).includes('전체 정지가 걸려 있습니다'));
});

test('STOP 미확인 결과: 확인됨으로 표시하지 않는다', async () => {
  const store = createStore();
  const backend = new SimulationBackend({ ...FAST, scenario: 'stop_unconfirmed' });
  backend.onEvent((event) => {
    if (event.kind === 'stop-result') store.dispatch({ type: 'stop-result', record: event.record });
  });
  await backend.stopAll();
  await new Promise((resolve) => {
    const timer = setInterval(() => {
      if (store.get().stopRecord) {
        clearInterval(timer);
        resolve();
      }
    }, 20);
  });
  const html = renderStopCard(store.get().stopRecord);
  assert.ok(html.includes('정지 미확인'));
  assert.ok(!html.includes('로봇 정지 확인됨'));
  assert.match(html, /실제 정지 상태를 확인하지 못했습니다/);
});

// ── FR3 화면 데이터 ───────────────────────────────────────────────────
test('로봇 카드: FR3 구성·스킬·비활성 사유·배지가 그대로 나온다', () => {
  const html = renderRobotCard({ ...initialState(), policyOpen: true });
  assert.ok(html.includes('FAIRINO FR3-WMS + 2F-85 · Gazebo workcell simulation'));
  assert.ok(html.includes('MoveIt2 검증 완료'));
  assert.ok(html.includes('시뮬레이션 전용'));
  assert.ok(html.includes('실하드웨어 미검증'));
  assert.ok(
    html.includes('2F-85 장착 근거, 그리퍼 close 안정성, 파지 관측, pick/place 재검증 미완료'),
  );
  ['home', 'move', 'stop'].forEach((skill) =>
    assert.ok(html.includes(`<span class="chip skill">${skill}</span>`), `${skill} 활성 표시 없음`),
  );
  ['pick', 'place'].forEach((skill) =>
    assert.ok(html.includes(`chip skill off" title="비활성">${skill}`), `${skill} 비활성 표시 없음`),
  );
  // 검증되지 않은 것을 검증된 것처럼 적지 않는다.
  assert.match(html, /실제 하드웨어는 검증되지 않았습니다/);
  assert.ok(html.includes('미검증'));
});

test('로봇 선택 모달: 미구현 로봇은 버튼이 잠겨 있다', () => {
  const html = renderModal({ ...initialState(), modal: 'robot' });
  assert.ok(html.includes('FAIRINO FR3-WMS'));
  assert.ok(html.includes('미구현'));
  const disabledCount = (html.match(/robot-option[^>]*disabled/g) || []).length;
  assert.equal(disabledCount, 3);
  assert.match(html, /검증된 구성만 선택할 수 있습니다/);
});

// ── 계획 카드 ─────────────────────────────────────────────────────────
test('계획 카드: 단계·자원 대조·규칙 코드가 나온다', async () => {
  const { store } = await stateWith('1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘');
  const html = renderPlanCard(store.get());
  assert.ok(html.includes('생성된 작업 계획'));
  assert.ok(html.includes('작업 단계'));
  assert.ok(html.includes('요청과 계획의 자원 대조'));
  assert.ok(html.includes('E-SEQ-001'));
  assert.ok(html.includes('>PASS<'));
  assert.ok(html.includes('Plan hash'));
});

test('계획 카드: BLOCK이면 차단 문구가 표에 붙는다', async () => {
  const { store } = await stateWith('3번 팔레트로 이동해줘');
  const html = renderPlanCard(store.get());
  assert.ok(html.includes('>BLOCK<'));
  assert.ok(html.includes('resource-utterance bad'));
  assert.match(html, /요청에 없는 자원이 계획에 들어 있다/);
});

test('명령 카드: 실행 조건을 안내하고 계획 생성 버튼을 잠근다', () => {
  const empty = renderCommandCard(initialState(), 'simulation');
  assert.ok(empty.includes('data-action="generate" disabled'));
  assert.match(empty, /계획 생성은 로봇을 실행하지 않습니다/);
  assert.match(empty, /안전 판단이 PASS일 때/);
  const filled = renderCommandCard({ ...initialState(), command: '이동해줘' }, 'server');
  assert.ok(!filled.includes('data-action="generate" disabled'));
  // 시뮬레이션 안내는 시뮬레이션 모드에서만 나온다.
  assert.ok(empty.includes('시뮬레이션 모드 예시'));
  assert.ok(!filled.includes('시뮬레이션 모드 예시'));
});

// ── 이벤트·시뮬레이션 ─────────────────────────────────────────────────
test('이벤트 카드: 필터와 항목이 나온다', () => {
  const state = {
    ...initialState(),
    events: [makeEvent('info', '시스템이 초기화되었습니다.', '14:32:10')],
  };
  const html = renderEventsCard(state);
  assert.ok(html.includes('시스템이 초기화되었습니다.'));
  assert.ok(html.includes('data-filter="execution"'));
  assert.ok(html.includes('aria-pressed="true"'));
});

test('시뮬레이션 카드: 실행 전에는 안내, 정지 후에는 덮개가 나온다', async () => {
  const { store, backend } = await stateWith('1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘');
  assert.match(renderSimulationCard(store.get()), /실행 시작 후 단계가 표시됩니다/);
  const execution = await backend.startExecution();
  store.dispatch({ type: 'execution-started', executionId: execution.executionId, step: 1 });
  backend.stopTimer();
  const running = renderSimulationCard(store.get());
  assert.ok(running.includes('Simulator / Gazebo'));
  assert.ok(running.includes('progress-step current'));
  store.dispatch({
    type: 'cancel-result',
    record: { scope: 'execution', result: 'confirmed', executionId: execution.executionId },
  });
  assert.ok(renderSimulationCard(store.get()).includes('실행 취소됨'));
  store.dispatch({ type: 'stop-result', record: { scope: 'global', result: 'confirmed' } });
  assert.ok(renderSimulationCard(store.get()).includes('전체 정지됨'));
});

test('HTML 삽입 값은 이스케이프한다', () => {
  const state = {
    ...initialState(),
    events: [makeEvent('info', '<img src=x onerror=alert(1)>', '00:00:00')],
  };
  const html = renderEventsCard(state);
  assert.ok(!html.includes('<img src=x'));
  assert.ok(html.includes('&lt;img src=x'));
});

// ── 진입점과 렌더러의 컨테이너 id가 맞는가 ────────────────────────────
test('render()가 index.html의 컨테이너를 모두 채운다', async () => {
  const index = readFileSync(`${ROOT}html/index.html`, 'utf-8');
  const ids = [...index.matchAll(/id="([^"]+)"/g)].map((match) => match[1]);
  const nodes = new Map();
  ids.forEach((id) =>
    nodes.set(id, {
      id,
      innerHTML: '',
      textContent: '',
      classList: {
        set: new Set(),
        toggle(name, on) {
          if (on) this.set.add(name);
          else this.set.delete(name);
        },
      },
      scrollTop: 0,
      scrollHeight: 100,
      value: '',
    }),
  );
  // 최소 DOM 대역. 실제 브라우저 대신 id 연결만 확인한다.
  globalThis.document = { getElementById: (id) => nodes.get(id) || null };

  const { render } = await import('../../html/static/js/render.js');
  const { store } = await stateWith('1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘', {
    execute: true,
  });
  // 실제 시작 순서와 같게 세션·연결 상태를 넣는다.
  store.dispatch({ type: 'session', session: { session_id: 'sim_abc', client_id: 'c1' } });
  store.dispatch({ type: 'connection', state: 'ok', detail: '시뮬레이션 모드' });
  render(store.get(), 'simulation');

  const filled = [
    'card-robot', 'card-voice', 'card-events', 'card-command', 'card-plan',
    'card-verdict', 'card-sim', 'card-execution',
  ];
  filled.forEach((id) => {
    assert.ok(nodes.get(id), `${id}가 index.html에 없다`);
    assert.ok(nodes.get(id).innerHTML.length > 50, `${id}가 채워지지 않았다`);
  });
  // 실행 중이면 워크스페이스로 이동 버튼이 생긴다.
  assert.match(nodes.get('goto-workspace-slot').innerHTML, /실행 워크스페이스로 이동/);
  // 헤더 연결 상태와 세션 표시가 채워진다.
  assert.match(nodes.get('conn').innerHTML, /시뮬레이션 모드/);
  assert.ok(nodes.get('session-id').textContent.length > 0);
  // 실행 중에는 워크스페이스가 보이고 계획 화면이 숨는다.
  assert.ok(nodes.get('view-plan').classList.set.has('hidden'));
  assert.ok(!nodes.get('view-workspace').classList.set.has('hidden'));
  delete globalThis.document;
});

test('render(): 타이핑 중에는 명령 textarea를 갈아 끼우지 않는다', async () => {
  const textarea = { id: 'command-input', value: '', tagName: 'TEXTAREA' };
  const button = { disabled: true, innerHTML: '◈ 계획 생성' };
  const hints = [{}, {}];
  const card = {
    innerHTML: '',
    querySelector: (sel) =>
      sel === '#command-input' ? textarea : sel === '[data-action="generate"]' ? button : null,
    querySelectorAll: (sel) => (sel === '.hint' ? hints : []),
  };
  const nodes = new Map([['card-command', card], ['command-input', textarea]]);
  const ids = [
    'card-robot', 'card-scene', 'card-workcell', 'card-hardware', 'card-voice', 'card-events',
    'card-plan', 'card-verdict', 'card-block', 'goto-workspace-slot', 'stop-slot-plan',
    'card-sim', 'card-execution', 'stop-slot-workspace', 'modal-root', 'event-list',
  ];
  ids.forEach((id) => nodes.set(id, { innerHTML: '', hidden: false, scrollTop: 0, scrollHeight: 0 }));
  globalThis.document = {
    getElementById: (id) => nodes.get(id) || null,
    activeElement: textarea,
    createElement: () => {
      const fresh = { innerHTML: '' };
      // 새로 그린 HTML에서 버튼 상태만 읽는다.
      fresh.querySelector = (sel) =>
        sel === '[data-action="generate"]'
          ? { disabled: /data-action="generate" disabled/.test(fresh.innerHTML), innerHTML: '◈ 계획 생성' }
          : null;
      fresh.querySelectorAll = (sel) =>
        sel === '.hint' ? Array.from(fresh.innerHTML.matchAll(/class="hint"/g)) : [];
      return fresh;
    },
  };
  const { render } = await import('../../html/static/js/render.js');
  const store = createStore(initialState());
  store.dispatch({ type: 'connection', state: 'ok', detail: '' });
  // 첫 글자 — 카드의 innerHTML은 그대로이고 버튼만 풀린다.
  store.dispatch({ type: 'command', command: '1' });
  textarea.value = '1';
  render(store.get(), 'server');
  assert.equal(card.innerHTML, '');
  assert.equal(button.disabled, false);
  assert.equal(textarea.value, '1');
  // 조합 중인 글자도 값이 되돌아가지 않는다.
  store.dispatch({ type: 'command', command: '1번 팔ㄹ' });
  textarea.value = '1번 팔ㄹ';
  render(store.get(), 'server');
  assert.equal(card.innerHTML, '');
  assert.equal(textarea.value, '1번 팔ㄹ');
  // 지우기 — 값은 상태를 따라가고 버튼은 다시 잠긴다.
  store.dispatch({ type: 'command', command: '' });
  render(store.get(), 'server');
  assert.equal(textarea.value, '');
  assert.equal(button.disabled, true);
  delete globalThis.document;
});

// ── 정책·자원 수치의 출처 ─────────────────────────────────────────────
test('정책 항목 수치는 서버 설정에서만 온다', async () => {
  const { policyItems } = await import('../../html/static/js/catalog.js');
  const offline = policyItems(null);
  assert.ok(offline.some((item) => item.value.includes('미연결')));
  const online = policyItems({
    session: { plan_ttl_sec: 300 },
    policies: { safety: { policy_version: 'safety-1.0' }, stop: { policy_version: 'stop-1.0', hold_sec: 2 } },
    catalogs: { locations: [1, 2, 3], objects: [1, 2], skills: [1, 2, 3, 4, 5] },
  });
  const byLabel = Object.fromEntries(online.map((item) => [item.label, item.value]));
  assert.equal(byLabel['위치 카탈로그'], '3개');
  assert.equal(byLabel['자재 카탈로그'], '2개');
  assert.equal(byLabel['스킬 카탈로그'], '5개');
  assert.equal(byLabel['안전 정책'], 'safety-1.0');
  assert.match(byLabel['계획 유효 시간'], /5분/);
  // 승인 항목이 없다.
  assert.ok(!online.some((item) => item.label.includes('승인')));
});

test('서버 모드에서는 실제 실행 어댑터를 밝힌다', () => {
  const html = renderRobotCard({
    ...initialState(),
    serverRobot: { robot_id: 'fake_dev', kind: 'fake', is_simulated: true },
  });
  assert.ok(html.includes('실행 어댑터'));
  assert.ok(html.includes('fake_dev'));
  assert.ok(html.includes('개발용 Fake 실행'));
  // 선언된 구성은 그대로 보여주되, 실행이 FR3에서 되는 것처럼 적지 않는다.
  assert.ok(html.includes('FAIRINO FR3-WMS'));
  assert.match(html, /실행 경로에\s*\n?\s*연결되지 않았습니다/);
});

// ── 작업 셀 화면 (8-08 우선순위 6) ─────────────────────────────────────
test('작업 셀 카드: 연결 전에는 값을 만들지 않는다', () => {
  const html = renderWorkcellCard(initialState());
  assert.ok(html.includes('미연결'));
  assert.ok(html.includes('작업 셀에 연결되지 않았습니다'));
  // 없는 자원을 있는 것처럼 보여주지 않는다.
  assert.ok(!html.includes('pallet_1'));
});

test('작업 셀 카드: 서버 대조표를 한국어·자원 id·모델·프레임으로 보여준다', () => {
  const html = renderWorkcellCard({
    ...initialState(),
    serverWorkcell: {
      registered: true,
      world: 'forstick2_fr3_2f85_workcell',
      resources: {
        loc_pallet_1: { korean: '1번 팔레트', gazebo_model: 'pallet_1', frame: 'pallet_1_frame' },
        loc_pallet_2: { korean: '2번 팔레트', gazebo_model: 'pallet_2', frame: 'pallet_2_frame' },
        loc_pallet_3: { korean: '3번 팔레트', gazebo_model: 'pallet_3', frame: 'pallet_3_frame' },
        loc_conveyor: { korean: '컨베이어', gazebo_model: 'conveyor', frame: 'conveyor_frame' },
        mat_a: { korean: 'A자재', gazebo_model: 'material_a', frame: 'material_a_frame' },
        mat_b: { korean: 'B자재', gazebo_model: 'material_b', frame: 'material_b_frame' },
        mat_c: { korean: 'C자재', gazebo_model: 'material_c', frame: 'material_c_frame' },
      },
      move_poses: { loc_pallet_1: 'pallet_1_approach', loc_conveyor: 'conveyor_approach' },
    },
  });
  ['1번 팔레트', '2번 팔레트', '3번 팔레트', '컨베이어', 'A자재', 'B자재', 'C자재'].forEach(
    (label) => assert.ok(html.includes(label), `${label} 표시 없음`),
  );
  ['pallet_1_frame', 'conveyor_frame', 'material_c_frame'].forEach((frame) =>
    assert.ok(html.includes(frame), `${frame} 표시 없음`),
  );
  assert.ok(html.includes('pallet_1_approach'));
  assert.ok(html.includes('7개'));
});

test('로봇 카드: 작업 셀에 붙으면 world·격리를 보여주고 실하드웨어가 아님을 적는다', () => {
  const html = renderRobotCard({
    ...initialState(),
    serverRobot: { robot_id: 'fr3wms_2f85_workcell', kind: 'gazebo' },
    serverWorkcell: {
      registered: true,
      workcell_id: 'fr3_2f85_workcell',
      workcell_version: '0.1.0-design',
      world: 'forstick2_fr3_2f85_workcell',
      gz_partition: 'forstick2_fr3_workcell',
      ros_domain_id: 44,
      supported_skills: ['home', 'move', 'stop'],
    },
  });
  assert.ok(html.includes('forstick2_fr3_2f85_workcell'));
  assert.ok(html.includes('forstick2_fr3_workcell'));
  assert.ok(html.includes('domain 44'));
  assert.ok(html.includes('Gazebo 작업 셀에 연결됨'));
  assert.match(html, /실제 하드웨어는 연결되지 않았습니다/);
  // 작업 셀에 붙었으면 Fake 경고를 띄우지 않는다.
  assert.ok(!html.includes('개발용 Fake 실행'));
  // pick/place는 계속 비활성이다.
  ['pick', 'place'].forEach((skill) =>
    assert.ok(html.includes(`chip skill off" title="비활성">${skill}`), `${skill} 비활성 없음`),
  );
});

test('로봇 카드: 작업 셀 연결 실패는 이유를 그대로 보여준다', () => {
  const html = renderRobotCard({
    ...initialState(),
    serverWorkcell: { registered: false, detail: 'ValueError: 매니페스트의 enabled가 꺼져 있다' },
  });
  assert.ok(html.includes('연결되지 않았습니다'));
  assert.ok(html.includes('매니페스트의 enabled가 꺼져 있다'));
});

test('계획 카드: 발화 자원을 씬 id·프레임과 대조해 보여준다', () => {
  const state = {
    ...initialState(),
    plan: {
      planId: 'plan-1',
      requestId: 'req-1',
      planHash: 'h',
      createdAt: 0,
      ttlSec: 600,
      steps: [
        { no: 1, skill: 'move', args: { to: 'loc_pallet_1' }, description: '이동' },
      ],
    },
    validation: { verdict: 'PASS', reasonCodes: [], detail: '', rules: [], blocked: [], missing: [], fixes: [] },
    serverWorkcell: {
      registered: true,
      resources: {
        loc_pallet_1: { korean: '1번 팔레트', gazebo_model: 'pallet_1', frame: 'pallet_1_frame' },
      },
      move_poses: { loc_pallet_1: 'pallet_1_approach' },
    },
  };
  const html = renderPlanCard(state);
  assert.ok(html.includes('자원 대조 (발화 → 씬 → 프레임)'));
  assert.ok(html.includes('loc_pallet_1'));
  assert.ok(html.includes('pallet_1_frame'));
  assert.ok(html.includes('pallet_1_approach'));
});

test('실행 카드: 관측값이 없으면 없다고 적는다', () => {
  const state = { ...initialState() };
  state.execution = { ...state.execution, status: 'running', id: 'exec-1' };
  const html = renderExecutionCard(state);
  assert.ok(html.includes('실행 관측'));
  assert.ok(html.includes('아직 관측값이 없습니다'));
});

test('실행 카드: 어댑터 근거의 관측값만 보여준다', () => {
  const state = { ...initialState() };
  state.execution = {
    status: 'running',
    id: 'exec-1',
    currentStep: 1,
    elapsedSec: 3,
    results: [
      {
        no: 1,
        skill: 'move',
        status: 'done',
        args: { to: 'loc_pallet_1' },
        evidence: {
          pose: 'pallet_1_approach',
          is_simulated: true,
          max_error_rad: 0.0031,
          tolerance_rad: 0.05,
          observed_joint_rad: { j1: 0.3812, j2: -1.2 },
          controllers: {
            joint_state_broadcaster: 'active',
            arm_trajectory_controller: 'active',
            gripper_trajectory_controller: 'active',
          },
          gripper: { observed: true, joint_rad: 0.0, aperture_m: 0.084997 },
          planning_scene: { checked: true, valid: true, contacts: [], snapshot_id: 'scene', content_hash: 'abc123' },
        },
      },
    ],
  };
  const html = renderExecutionCard(state);
  assert.ok(html.includes('pallet_1_approach'));
  assert.ok(html.includes('충돌 없음'));
  assert.ok(html.includes('abc123'));
  assert.ok(html.includes('gripper_trajectory_controller=active'));
  assert.ok(html.includes('j1 0.3812'));
  assert.ok(html.includes('85.0 mm'));
  assert.ok(html.includes('0.0031 rad'));
  assert.match(html, /Gazebo simulation 관측값입니다/);
});

test('실행 카드: 개구 모델이 없으면 개구를 주장하지 않는다', () => {
  const state = { ...initialState() };
  state.execution = {
    status: 'running', id: 'e', currentStep: 1, elapsedSec: 1,
    results: [{
      no: 1, skill: 'move', status: 'done',
      evidence: {
        pose: 'p',
        gripper: { observed: true, joint_rad: 0.2, aperture_m: null,
                   aperture_detail: '개구 모델이 없다 — 개구를 주장하지 않는다' },
      },
    }],
  };
  const html = renderExecutionCard(state);
  assert.ok(html.includes('개구를 주장하지 않는다'));
  assert.ok(!html.includes(' mm ('));
});

test('실행 카드: 검증 불가 종료를 성공으로 바꾸지 않는다', () => {
  const state = { ...initialState() };
  state.execution = {
    status: 'unverified',
    id: 'exec-1',
    currentStep: 2,
    elapsedSec: 9,
    unverifiedReason: 'exec.unverifiable',
    unverifiedDetail: '파지 상태를 관측할 수 없습니다.',
    results: [],
  };
  const html = renderExecutionCard(state);
  assert.ok(html.includes('검증 불가'));
  assert.ok(html.includes('모션은 끝났지만 결과를 확인하지 못했습니다'));
  assert.ok(html.includes('exec.unverifiable'));
  assert.ok(html.includes('성공으로 바꾸지 않습니다'));
  // 완료라고 적지 않는다.
  assert.ok(!html.includes('실행 완료'));
});

// ── 차단된 요청 화면 (8-09 우선 작업 2) ────────────────────────────────
test('차단 카드: 기록이 없으면 아무것도 그리지 않는다', () => {
  assert.equal(renderBlockCard(initialState()), '');
});

test('차단 카드: 발화 자원·계획 초안·Reason Code·부족한 입력을 함께 보여준다', () => {
  const html = renderBlockCard({
    ...initialState(),
    planBlock: {
      utterance: '1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘',
      reasonCode: 'capability.profile_incomplete',
      detail: 'pick, place: …',
      slots: {
        matches: [
          { surface: '1번팔레트', resource_id: 'loc_pallet_1', kind: 'location' },
          { surface: 'a자재', resource_id: 'mat_a', kind: 'object' },
          { surface: '컨베이어', resource_id: 'loc_conveyor', kind: 'location' },
        ],
      },
      draftSteps: [
        { no: 1, skill: 'pick', args: { object: 'mat_a', from: 'loc_pallet_1' } },
        { no: 2, skill: 'move', args: { to: 'loc_conveyor' } },
        { no: 3, skill: 'place', args: { object: 'mat_a', to: 'loc_conveyor' } },
        { no: 4, skill: 'home', args: {} },
      ],
      blocked: {
        skills: ['pick', 'place'],
        reason_code: 'capability.profile_incomplete',
        message: '2F-85 장착 근거, 그리퍼 close 안정성, 파지 관측, pick/place 재검증이 완료되지 않았습니다.',
        unmet: ['장착 transform과 yaw 근거', '커플링 질량·관성 근거', '파지 상태 관측 수단'],
        gate: 'validation/pick_place_gate.py',
      },
    },
  });
  // 요청과 이유 코드
  assert.ok(html.includes('차단된 요청'));
  assert.ok(html.includes('capability.profile_incomplete'));
  assert.ok(html.includes('2F-85 장착 근거'));
  // 발화 자원
  assert.ok(html.includes('발화 자원'));
  ['loc_pallet_1', 'mat_a', 'loc_conveyor'].forEach((id) =>
    assert.ok(html.includes(id), `${id} 표시 없음`),
  );
  // 계획 초안 — 실행하지 않았다고 적는다
  assert.ok(html.includes('계획 초안 (실행하지 않았습니다)'));
  assert.ok(html.includes('pick'));
  assert.ok(html.includes('place'));
  // 부족한 입력
  assert.ok(html.includes('부족한 입력'));
  assert.ok(html.includes('파지 상태 관측 수단'));
  assert.ok(html.includes('validation/pick_place_gate.py'));
});

test('차단 카드: 모호 명령은 되묻기 문구를 보여준다', () => {
  const html = renderBlockCard({
    ...initialState(),
    planBlock: {
      utterance: '그거 저기로 옮겨줘',
      reasonCode: 'plan.clarification_required',
      detail: '',
      clarification: '확인 필요: 이동할 물체와 목적지 위치가 명확하지 않습니다.',
      slots: { matches: [] },
      draftSteps: [],
      blocked: null,
    },
  });
  assert.ok(html.includes('plan.clarification_required'));
  assert.ok(html.includes('이동할 물체와 목적지 위치가 명확하지 않습니다'));
  assert.ok(html.includes('발화에서 확인된 자원이 없습니다'));
  assert.ok(html.includes('계획 초안이 없습니다'));
});

// ── 실제 하드웨어 준비 상태 표시 (8-12) ────────────────────────────────
function readiness(overrides = {}) {
  return {
    available: true,
    real_hardware_ready: false,
    real_hardware_verified: false,
    real_hardware_connected: false,
    adapter_configured: false,
    missing_labels: ['장착 yaw 근거', '커플링 실측 질량', '커플링 관성 텐서'],
    collection_guide: [
      { label: '장착 yaw 근거', reason_code: 'hardware.input_missing',
        needed: '도면의 핀 위치 또는 조립 사진이 필요합니다' },
      { label: '커플링 실측 질량', reason_code: 'hardware.input_missing',
        needed: '저울 실측값(g)과 측정 사진이 필요합니다' },
    ],
    inputs: {
      ready: [],
      pending: ['mounting_yaw_evidence', 'coupling_measured_mass'],
      inputs: [
        { key: 'mounting_yaw_evidence', label: '장착 yaw 근거', ready: false,
          has_value: false, measurement_method: 'none',
          needed: '도면의 핀 위치 또는 조립 사진이 필요합니다' },
        { key: 'coupling_measured_mass', label: '커플링 실측 질량', ready: false,
          has_value: false, measurement_method: 'none',
          needed: '저울 실측값(g)과 측정 사진이 필요합니다' },
      ],
    },
    checklist: { blocking_total: 11, blocking_done: 0, counts: { not_started: 11 } },
    state: {
      simulation_e2e: true, real_hardware_ready: false,
      real_hardware_verified: false,
      hardware_evidence: '0/18', hardware_checklist: '0/11',
    },
    state_lines: [
      'simulation_e2e=true',
      'real_hardware_ready=false',
      'real_hardware_verified=false',
      'hardware_evidence=0/18',
      'hardware_checklist=0/11',
    ],
    ...overrides,
  };
}

test('하드웨어 카드: Gazebo 시연과 실기 준비를 나란히 두되 합치지 않는다', () => {
  const html = renderHardwareCard({
    ...initialState(),
    simulationE2e: { available: true, completed: true, real_hardware_ready: false },
    hardwareReadiness: readiness(),
  });
  // 두 줄이 **따로** 있다.
  const simRow = html.match(
    /<span class="row-label">Gazebo pick\/place 시연<\/span><div class="row-value">([\s\S]*?)<\/div>/,
  );
  const realRow = html.match(
    /<span class="row-label">실제 pick\/place 준비<\/span><div class="row-value">([\s\S]*?)<\/div>/,
  );
  assert.ok(simRow, 'Gazebo 시연 행이 없다');
  assert.ok(realRow, '실제 준비 행이 없다');
  assert.ok(simRow[1].includes('완료'));
  assert.ok(realRow[1].includes('차단'));
  // 같은 배지로 합쳐지지 않는다.
  assert.ok(!simRow[1].includes('차단'));
  assert.ok(!realRow[1].includes('완료'));
});

test('하드웨어 카드: 고정 상태 다섯 줄을 서버 문장 그대로 보여준다', () => {
  const html = renderHardwareCard({
    ...initialState(),
    simulationE2e: { available: true, completed: true },
    hardwareReadiness: readiness(),
  });
  assert.ok(html.includes('지금 상태 (고정)'));
  [
    'simulation_e2e=true',
    'real_hardware_ready=false',
    'real_hardware_verified=false',
    'hardware_evidence=0/18',
    'hardware_checklist=0/11',
  ].forEach((line) => assert.ok(html.includes(line), `${line} 없음`));
  // 근거 수집 수(0/18)가 "검증됨"으로 읽히지 않는다.
  assert.ok(!html.includes('real_hardware_verified=18'));
  assert.ok(!html.includes('real_hardware_verified=true'));
});

test('하드웨어 카드: 근거 수집 수를 검증 수로 읽히게 두지 않는다', () => {
  const html = renderHardwareCard({
    ...initialState(),
    hardwareReadiness: readiness(),
  });
  // 행 이름이 '근거 수집'임을 밝힌다.
  assert.ok(html.includes('근거 수집(입력)'));
  assert.ok(html.includes('근거 수집(체크리스트)'));
  // 실기 검증 행은 숫자가 아니라 미검증이다.
  const verifiedRow = html.match(
    /<span class="row-label">실기 검증<\/span><div class="row-value">([\s\S]*?)<\/div>/,
  );
  assert.ok(verifiedRow, '실기 검증 행이 없다');
  assert.ok(verifiedRow[1].includes('미검증'));
  assert.ok(!/\d/.test(verifiedRow[1]), '실기 검증 행에 숫자가 있다');
});

test('하드웨어 카드: 실제 하드웨어가 연결되지 않았음을 밝힌다', () => {
  const html = renderHardwareCard({
    ...initialState(),
    simulationE2e: { available: true, completed: true },
    hardwareReadiness: readiness(),
  });
  assert.ok(html.includes('실제 하드웨어는 아직 연결되지 않았습니다'));
  assert.ok(html.includes('승격되지 않습니다'));
  const connectRow = html.match(
    /<span class="row-label">실기 연결<\/span><div class="row-value">([\s\S]*?)<\/div>/,
  );
  assert.ok(connectRow && connectRow[1].includes('미연결'));
});

test('하드웨어 카드: 부족한 입력과 수집 방법을 보여준다', () => {
  const html = renderHardwareCard({
    ...initialState(),
    simulationE2e: { available: true, completed: true },
    hardwareReadiness: readiness(),
  });
  assert.ok(html.includes('부족한 입력'));
  assert.ok(html.includes('장착 yaw 근거'));
  assert.ok(html.includes('커플링 실측 질량'));
  assert.ok(html.includes('hardware.input_missing'));
  // 사람이 바로 수집할 수 있는 문장
  assert.ok(html.includes('저울 실측값(g)과 측정 사진이 필요합니다'));
  // 진행 상황
  assert.ok(html.includes('0/11'));
});

test('하드웨어 카드: 실기 어댑터 설정 없음을 표시한다', () => {
  const html = renderHardwareCard({
    ...initialState(),
    hardwareReadiness: readiness(),
  });
  const row = html.match(
    /<span class="row-label">실기 어댑터 설정<\/span><div class="row-value">([\s\S]*?)<\/div>/,
  );
  assert.ok(row && row[1].includes('없음'));
});

test('하드웨어 카드: 판정이 없으면 준비를 차단으로 표시한다', () => {
  const html = renderHardwareCard({
    ...initialState(),
    hardwareReadiness: { available: false, detail: '매니페스트가 없다' },
  });
  assert.ok(html.includes('판정 없음'));
  assert.ok(html.includes('차단'));
  assert.ok(html.includes('매니페스트가 없다'));
});

test('하드웨어 카드: Gazebo 시연 기록이 없으면 완료로 적지 않는다', () => {
  const html = renderHardwareCard({
    ...initialState(),
    simulationE2e: { available: false },
    hardwareReadiness: readiness(),
  });
  const simRow = html.match(
    /<span class="row-label">Gazebo pick\/place 시연<\/span><div class="row-value">([\s\S]*?)<\/div>/,
  );
  assert.ok(simRow && simRow[1].includes('기록 없음'));
  assert.ok(!simRow[1].includes('완료'));
});

test('하드웨어 카드: 근거가 모두 채워지면 근거 확보로 표시한다', () => {
  const html = renderHardwareCard({
    ...initialState(),
    hardwareReadiness: readiness({
      real_hardware_ready: true,
      adapter_configured: true,
      missing_labels: [],
      collection_guide: [],
      inputs: { ready: ['a'], pending: [], inputs: [
        { key: 'a', label: '합성', ready: true, has_value: true,
          measurement_method: 'scale', needed: '' }] },
      checklist: { blocking_total: 11, blocking_done: 11, counts: { passed: 11 } },
    }),
  });
  assert.ok(html.includes('근거 확보'));
  // 준비가 끝나도 **검증·연결은 여전히 미완료**다.
  const verifiedRow = html.match(
    /<span class="row-label">실기 검증<\/span><div class="row-value">([\s\S]*?)<\/div>/,
  );
  assert.ok(verifiedRow && verifiedRow[1].includes('미검증'));
});

// ── pick/place 계획 사전 검증 표시 (8-10) ──────────────────────────────
function planValidation(overrides = {}) {
  return {
    available: true,
    plan_verified: true,
    execution_allowed: false,
    stage_count: 12,
    checks_passed: 12,
    resources_matched: true,
    scene_stable: true,
    snapshot: { snapshot_id: 'scene-1', content_hash: 'abcdef012345678' },
    grasp_observation: { availability: 'unavailable', held: null },
    reason_codes: [],
    findings: [],
    resource_rows: [
      {
        resource_id: 'mat_a', role: 'object', korean: 'A자재',
        scene_model: 'material_a', frame: 'material_a_frame',
        in_utterance: true, utterance_surface: 'a자재', known: true, matched: true,
      },
      {
        resource_id: 'loc_pallet_1', role: 'source', korean: '1번 팔레트',
        scene_model: 'pallet_1', frame: 'pallet_1_frame',
        in_utterance: true, utterance_surface: '1번팔레트', known: true, matched: true,
      },
    ],
    stages: [
      { no: 1, stage: 'home_start', label: '안전 home', kind: 'arm_motion',
        pose_name: 'workcell_safe_home', holds_object: null },
      { no: 5, stage: 'grasp_approach', label: 'pick 접근', kind: 'arm_motion',
        pose_name: 'material_a_grasp', holds_object: null },
      { no: 7, stage: 'lift', label: 'lift', kind: 'arm_motion',
        pose_name: 'pallet_1_approach', holds_object: 'mat_a' },
    ],
    checks: [
      { stage: 'home_start', passed: true, collisions: [] },
      { stage: 'grasp_approach', passed: true, collisions: [] },
      { stage: 'lift', passed: true, collisions: [] },
    ],
    ...overrides,
  };
}

test('차단 카드: 계획 단계·자원 대조·사전 검증 결과를 함께 보여준다', () => {
  const html = renderBlockCard({
    ...initialState(),
    planBlock: {
      utterance: '1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘',
      reasonCode: 'capability.profile_incomplete',
      detail: '',
      slots: { matches: [] },
      draftSteps: [
        { no: 1, skill: 'pick', args: { object: 'mat_a', from: 'loc_pallet_1' } },
      ],
      blocked: { skills: ['pick', 'place'], unmet: ['장착 transform과 yaw 근거'] },
      planValidation: planValidation(),
    },
  });
  // 통과한 사전 검증 항목
  assert.ok(html.includes('계획 사전 검증'));
  assert.ok(html.includes('12/12'));
  // 계획 단계
  assert.ok(html.includes('pick 접근'));
  assert.ok(html.includes('material_a_grasp'));
  assert.ok(html.includes('lift'));
  // 발화 ↔ 계획 ↔ 모델 ↔ 프레임 대조
  assert.ok(html.includes('material_a_frame'));
  assert.ok(html.includes('1번팔레트'));
  // planning scene snapshot
  assert.ok(html.includes('abcdef012345'));
  assert.ok(html.includes('검사 중 변경 없음'));
  // 파지 관측
  assert.ok(html.includes('unavailable'));
  // 미충족 조건
  assert.ok(html.includes('장착 transform과 yaw 근거'));
});

test('차단 카드: 계획 검증 통과를 실행 가능으로 표시하지 않는다', () => {
  const html = renderBlockCard({
    ...initialState(),
    planBlock: {
      utterance: 'x', reasonCode: 'capability.profile_incomplete', detail: '',
      slots: { matches: [] }, draftSteps: [], blocked: null,
      planValidation: planValidation(),
    },
  });
  assert.ok(html.includes('실행 허가가 아닙니다'));
  assert.ok(html.includes('실행 가능을 뜻하지 않습니다'));
  // "실행 가능" 행은 반드시 "아니오"다.
  const rowMatch = html.match(
    /<span class="row-label">실행 가능<\/span><div class="row-value">([\s\S]*?)<\/div>/,
  );
  assert.ok(rowMatch, '실행 가능 행이 없다');
  assert.ok(rowMatch[1].includes('아니오'), '실행 가능이 아니오로 표시되지 않았다');
  assert.ok(!rowMatch[1].includes('예'), '실행 가능이 예로 표시됐다');
});

test('차단 카드: 사전 검증이 걸리면 이유 코드를 단계와 함께 보여준다', () => {
  const html = renderBlockCard({
    ...initialState(),
    planBlock: {
      utterance: '1번 팔레트에서 C자재를 집어서 컨베이어에 올려줘',
      reasonCode: 'capability.profile_incomplete', detail: '',
      slots: { matches: [] }, draftSteps: [], blocked: null,
      planValidation: planValidation({
        plan_verified: false,
        checks_passed: 6,
        reason_codes: ['plan.resource_mismatch'],
        findings: [{
          key: 'support_mismatch:mat_c',
          reason_code: 'plan.resource_mismatch',
          detail: 'mat_c은(는) loc_pallet_3에 놓여 있다고 선언됐는데 계획은 loc_pallet_1에서 집으려 한다',
          stage: null,
        }],
        checks: [
          { stage: 'home_start', passed: true, collisions: [] },
          { stage: 'grasp_approach', passed: false,
            collisions: [['robotiq_85_base_link', 'material_c']] },
          { stage: 'lift', passed: true, collisions: [] },
        ],
      }),
    },
  });
  assert.ok(html.includes('검증에서 걸린 항목'));
  assert.ok(html.includes('plan.resource_mismatch'));
  assert.ok(html.includes('loc_pallet_3'));
  assert.ok(html.includes('미통과'));
  assert.ok(html.includes('충돌'));
  assert.ok(html.includes('6/12'));
});

test('차단 카드: 계획 검증을 돌리지 못하면 그 사실을 보여준다', () => {
  const html = renderBlockCard({
    ...initialState(),
    planBlock: {
      utterance: 'x', reasonCode: 'capability.profile_incomplete', detail: '',
      slots: { matches: [] }, draftSteps: [], blocked: null,
      planValidation: {
        available: false,
        detail: '이 로봇에는 pick/place 계획 검증에 쓸 셀 선언이 없다',
        reason_code: 'geometry.validator_unavailable',
      },
    },
  });
  assert.ok(html.includes('계획 사전 검증을 돌리지 못했습니다'));
  assert.ok(html.includes('geometry.validator_unavailable'));
});

test('차단 카드: 계획 검증 결과가 없으면 그 구역을 만들지 않는다', () => {
  const html = renderBlockCard({
    ...initialState(),
    planBlock: {
      utterance: 'x', reasonCode: 'plan.clarification_required', detail: '',
      slots: { matches: [] }, draftSteps: [], blocked: null,
    },
  });
  assert.ok(!html.includes('계획 사전 검증'));
});

// ── 실행 결과 판정 표시 (8-09 우선 작업 1) ─────────────────────────────
test('실행 카드: 파지 관측 요구 여부와 근거를 보여준다', () => {
  const state = { ...initialState() };
  state.execution = {
    status: 'running', id: 'e', currentStep: 1, elapsedSec: 1,
    results: [{
      no: 1, skill: 'move', status: 'done',
      evidence: {
        pose: 'pallet_1_approach',
        hold_requirement: {
          hold_required: false,
          basis: '계획이 목표 파지 상태를 명시하지 않았고, 카탈로그가 물체로 선언한 인자를 쓰는 스텝도 없다',
        },
        hold_observation_performed: false,
      },
    }],
  };
  const html = renderExecutionCard(state);
  assert.ok(html.includes('파지 관측'));
  assert.ok(html.includes('요구되지 않음'));
  assert.ok(html.includes('관측하지 않았습니다'));
  assert.ok(html.includes('카탈로그가 물체로 선언한 인자를 쓰는 스텝도 없다'));
});

test('실행 카드: 정지 확인으로 끝난 실행을 실패로 보이지 않게 한다', () => {
  const state = { ...initialState() };
  state.execution = {
    status: 'stop_confirmed', id: 'e', currentStep: 1, elapsedSec: 2, results: [],
  };
  const html = renderExecutionCard(state);
  assert.ok(html.includes('정지 확인'));
  assert.ok(!html.includes('실행 실패'));
});
