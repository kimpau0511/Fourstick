/** 화면 상태 스냅샷을 한 장의 HTML로 만든다(검토용).
 *
 *   node scripts/render_screen_states.mjs
 *   → reports/web/screen_states.html
 *
 * 브라우저 없이 만든 정적 스냅샷이다. 실제 화면은
 * http://127.0.0.1:8092 (또는 `?backend=sim`)에서 본다.
 */

import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

import { SimulationBackend } from '../html/static/js/backend-sim.js';
import { createStore, makeEvent } from '../html/static/js/state.js';
import {
  renderCommandCard,
  renderEventsCard,
  renderExecutionCard,
  renderPlanCard,
  renderRobotCard,
  renderSimulationCard,
  renderStopCard,
  renderVerdictCard,
  renderVoiceCard,
} from '../html/static/js/render.js';

const ROOT = resolve(dirname(new URL(import.meta.url).pathname), '..');
const FAST = { stepMs: 30, confirmMs: 20, planMs: 5 };
const PASS_TEXT = '1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘';

function wire(backend, store) {
  backend.onEvent((event) => {
    if (event.kind === 'step-started') store.dispatch({ type: 'step-started', step: event.step });
    if (event.kind === 'step-result') store.dispatch({ type: 'step-result', result: event.result });
    if (event.kind === 'completed') store.dispatch({ type: 'execution-completed' });
    if (event.kind === 'cancel-result') store.dispatch({ type: 'cancel-result', record: event.record });
    if (event.kind === 'stop-result') store.dispatch({ type: 'stop-result', record: event.record });
  });
}

function until(predicate, timeout = 4000) {
  return new Promise((ok, fail) => {
    const started = Date.now();
    const timer = setInterval(() => {
      if (predicate()) {
        clearInterval(timer);
        ok();
      } else if (Date.now() - started > timeout) {
        clearInterval(timer);
        fail(new Error('시간 초과'));
      }
    }, 10);
  });
}

async function scenario(name, utterance, options = {}) {
  const store = createStore();
  const backend = new SimulationBackend({ ...FAST, scenario: options.scenario });
  wire(backend, store);
  store.dispatch({ type: 'session', session: { session_id: 'sim_snapshot', client_id: 'c1' } });
  store.dispatch({ type: 'connection', state: 'ok', detail: '시뮬레이션 모드' });
  store.dispatch({ type: 'event', event: makeEvent('info', '시스템이 초기화되었습니다.', '00:00:00') });
  const result = await backend.createPlan(utterance);
  store.dispatch({
    type: 'plan-received',
    plan: result.plan,
    validation: result.validation,
    stopLatchCleared: true,
  });
  store.dispatch({ type: 'command', command: utterance });

  if (options.execute) {
    const execution = await backend.startExecution();
    store.dispatch({ type: 'execution-started', executionId: execution.executionId, step: 1 });
    if (options.finish) {
      await until(() => store.get().execution.status === 'completed', 8000);
    } else {
      await until(() => store.get().execution.results.length >= 1);
    }
    if (options.cancel) {
      store.dispatch({ type: 'cancel-requested' });
      await backend.cancelExecution(store.get().execution.id);
      await until(() => store.get().cancelRecord !== null);
    }
    if (options.stop) {
      store.dispatch({ type: 'stop-requested' });
      await backend.stopAll();
      await until(() => store.get().stopRecord !== null);
    }
  }
  backend.stopTimer();
  return { name, state: store.get() };
}

function section({ name, state }) {
  const plan = `
    <div class="grid-plan">
      <div class="col col-left">
        <section class="card">${renderRobotCard(state)}</section>
        <section class="card">${renderVoiceCard(state)}</section>
        <section class="card">${renderEventsCard(state)}</section>
      </div>
      <div class="col col-center">
        <section class="card">${renderCommandCard(state, 'simulation')}</section>
        <section class="card">${renderPlanCard(state)}</section>
      </div>
      <div class="col col-right">
        <section class="card">${renderVerdictCard(state)}</section>
      </div>
    </div>`;
  const workspace =
    state.execution.status === 'idle'
      ? ''
      : `<div class="grid-workspace" style="margin-top:16px">
           <section class="card">${renderSimulationCard(state)}</section>
           <div class="col">${renderExecutionCard(state)}</div>
         </div>`;
  return `
    <h2 style="max-width:1440px;margin:32px auto 8px;padding:0 16px;font-size:14px;
      color:#8BA1B4;font-family:'JetBrains Mono',monospace;text-transform:uppercase;
      letter-spacing:.15em">${name}</h2>
    <div class="shell">${plan}${workspace}
      <div style="margin-top:16px">${renderStopCard(state.stopRecord)}</div>
    </div>`;
}

const states = [
  await scenario('1. PASS — 실행 시작 가능', PASS_TEXT),
  await scenario('2. BLOCK — pick/place 미지원', '1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘'),
  await scenario('3. BLOCK — 요청에 없는 자원', '3번 팔레트로 이동해줘'),
  await scenario('4. ASK — 정보 부족', '그거 저기로 옮겨줘'),
  await scenario('5. 실행 중', PASS_TEXT, { execute: true }),
  await scenario('6. 실행 완료', PASS_TEXT, { execute: true, finish: true }),
  await scenario('7. 개별 실행 취소', PASS_TEXT, { execute: true, cancel: true }),
  await scenario('8. 전체 정지 — 확인됨', PASS_TEXT, { execute: true, stop: true }),
  await scenario('9. 전체 정지 — 미확인', PASS_TEXT, {
    execute: true,
    stop: true,
    scenario: 'stop_unconfirmed',
  }),
];

const css = readFileSync(`${ROOT}/html/static/app.css`, 'utf-8');
const html = `<!doctype html>
<html lang="ko"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Forstick 화면 상태 스냅샷</title>
<style>${css}</style></head>
<body><div class="page">
  <header class="app-header">
    <div class="brand"><div class="brand-badge">🤖</div>
      <div class="brand-text"><span class="brand-name">Forstick</span>
        <span class="brand-sub">Robot Command Console — 상태 스냅샷</span></div></div>
    <div class="spacer"></div>
    <span class="session-id">정적 스냅샷 · 버튼은 동작하지 않는다</span>
    <button class="estop" type="button"><span>■</span><span>전체 정지</span></button>
  </header>
  ${states.map(section).join('\n')}
</div></body></html>`;

mkdirSync(`${ROOT}/reports/web`, { recursive: true });
writeFileSync(`${ROOT}/reports/web/screen_states.html`, html, 'utf-8');
console.log(`reports/web/screen_states.html (${html.length} bytes, ${states.length} 상태)`);
states.forEach(({ name, state }) => {
  console.log(
    `  ${name} — verdict=${state.validation ? state.validation.verdict : '—'}`
      + ` execution=${state.execution.status}`
      + ` cancel=${state.cancelRecord ? state.cancelRecord.result : '—'}`
      + ` stop=${state.stopRecord ? state.stopRecord.result : '—'}`,
  );
});
