/** 시뮬레이션 시연 카드 테스트 (node --test). DOM 없이 HTML 문자열·호출을 본다.
 *
 * 확인하는 것:
 *  - 꺼져 있으면 이유를 보여주고 버튼이 없다
 *  - 안내된 동작만 버튼이 열리고, 작업이 돌면 모두 잠기며 시연 정지 버튼이 나온다
 *  - 로봇이 움직이는 동작은 확인을 받고, 거절하면 요청을 보내지 않는다
 *  - resume은 그 자재의 체크포인트 id를 함께 보낸다
 *  - 시뮬레이터 전용·일반 명령 차단 안내가 보인다
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import { createSimDemoCard, renderSimDemoCard } from '../../html/static/js/sim-demo.js';

const MATERIALS = [
  { model: 'material_a', korean: 'A자재', support_model: 'pallet_1',
    korean_colors: ['주황', '오렌지'],
    record: null, actions: { transfer: true, return: false, resume_preflight: false,
      resume: false, restore: false } },
  { model: 'material_b', korean: 'B자재', support_model: 'pallet_2',
    record: { state: 'stopped_unrestored' }, actions: { transfer: false, return: false,
      resume_preflight: true, resume: true, restore: true } },
];

function status(extra = {}) {
  return {
    enabled: true, is_simulated: true, running_job: null, recent_jobs: [],
    materials: MATERIALS,
    action_labels: { transfer: '컨베이어로 이송(상태 유지)', return: '원래 슬롯 복귀',
      resume_preflight: 'resume 사전검증', resume: '체크포인트에서 이어서 이송',
      restore: '원래 자리로 복구(순간 이동)' },
    state: { checkpoint: { model: 'material_b', checkpoint_id: 'simckpt_b1',
      object_state: 'held', stopped_stage: 'place_approach' },
    checkpoints: ['material_b'], resume_preflight: null },
    ...extra,
  };
}

const card = (s) => renderSimDemoCard({ available: true, status: s, job: null, notice: '', busy: false });

function buttons(html) {
  const out = [];
  const pattern = /<button[^>]*data-sim-action="([^"]+)"(?:[^>]*data-material="([^"]+)")?([^>]*)>/g;
  for (const match of html.matchAll(pattern)) {
    out.push({ action: match[1], material: match[2] || null,
      disabled: /\bdisabled\b/.test(match[0]) });
  }
  return out;
}

test('꺼져 있으면 이유만 보이고 버튼이 없다', () => {
  const html = card({ enabled: false, reason: 'FORSTICK2_SIM_DEMO_WEB이 꺼져 있다' });
  assert.match(html, /FORSTICK2_SIM_DEMO_WEB/);
  assert.equal(buttons(html).length, 0);
});

test('서버가 없으면 사용 불가로 보인다', () => {
  const html = renderSimDemoCard({ available: false, status: null, notice: '' });
  assert.match(html, /사용 불가/);
  assert.equal(buttons(html).length, 0);
});

test('안내된 동작만 열리고 시뮬레이터 전용 안내가 있다', () => {
  const html = card(status());
  const list = buttons(html);
  const open = list.filter((b) => !b.disabled).map((b) => `${b.material}:${b.action}`);
  // 정합 버튼은 자재를 고르지 않는 셀 전체 동작이다(관측만 한다).
  assert.deepEqual(open.sort(), ['material_a:transfer', 'material_b:restore',
    'material_b:resume', 'material_b:resume_preflight', 'null:reconcile',
    'null:return-all-goal'].sort());
  assert.match(html, /is_simulated=true/);
  assert.match(html, /일반 명령의 집기·놓기는 계속 차단/);
  assert.match(html, /STOP 체크포인트/);
  assert.match(html, /simckpt_b1/);
  assert.match(html, /정지됨 · 복구 필요/);
});

test('작업이 돌면 모든 동작이 잠기고 시연 정지가 나온다', () => {
  const html = card(status({ running_job: { job_id: 'simjob_1', action_label: '컨베이어로 이송(상태 유지)',
    material: 'material_a', progress: [] } }));
  const list = buttons(html);
  assert.ok(list.some((b) => b.action === 'stop' && !b.disabled));
  assert.ok(list.filter((b) => b.action !== 'stop').every((b) => b.disabled));
  assert.match(html, /실행 중/);
});

test('활성 목표가 있으면 STOP만 열리고 나머지 동작은 잠긴다', () => {
  const goal = {
    goal_id: 'simgoal_1', status: 'running', current_step: 1,
    progress: { completed: 0, total: 2, percent: 0 },
    plan: [
      { step: 1, material_label: 'A자재', from_label: '컨베이어 1번',
        to_label: '원래 자리', status: 'running' },
      { step: 2, material_label: 'B자재', from_label: '컨베이어 2번',
        to_label: '원래 자리', status: 'pending' },
    ],
  };
  const list = buttons(card(status({ goal })));
  assert.ok(list.some((b) => b.action === 'stop' && !b.disabled));
  assert.ok(list.filter((b) => b.action !== 'stop').every((b) => b.disabled));
});

function harness({ confirm = true } = {}) {
  const calls = [];
  const confirms = [];
  const fetchImpl = async (method, path, body) => {
    calls.push({ method, path, body });
    if (method === 'GET' && path === '/v1/sim-demo') return { ok: true, status: 200, payload: status() };
    if (method === 'POST' && path === '/v1/sim-demo/jobs') {
      return { ok: true, status: 202, payload: { job_id: 'simjob_x', status: 'running' } };
    }
    if (method === 'POST' && path === '/v1/sim-demo/stop') return { ok: true, status: 200, payload: { requested: true } };
    return { ok: true, status: 200, payload: { job_id: 'simjob_x', status: 'running', progress: [] } };
  };
  const root = { innerHTML: '', addEventListener() {}, contains: () => true };
  globalThis.setTimeout = () => 0;
  globalThis.clearTimeout = () => {};
  const api = createSimDemoCard({ root, fetchImpl,
    confirmImpl: (text) => { confirms.push(text); return confirm; } });
  return { api, calls, confirms, root };
}

test('이송은 확인 뒤에만 요청을 보낸다', async () => {
  const yes = harness({ confirm: true });
  await yes.api.refresh();
  await yes.api.start('transfer', 'material_a');
  const posted = yes.calls.find((c) => c.method === 'POST');
  assert.deepEqual(posted.body, { action: 'transfer', material: 'material_a' });
  assert.match(yes.confirms[0], /시뮬레이터/);
  assert.match(yes.confirms[0], /Gazebo 시뮬레이터에서 로봇이 움직입니다/);

  const no = harness({ confirm: false });
  await no.api.refresh();
  await no.api.start('transfer', 'material_a');
  assert.equal(no.calls.filter((c) => c.method === 'POST').length, 0);
});

test('resume은 그 자재의 체크포인트 id를 보낸다', async () => {
  const h = harness();
  await h.api.refresh();
  await h.api.start('resume', 'material_b');
  const posted = h.calls.find((c) => c.method === 'POST' && c.path === '/v1/sim-demo/jobs');
  assert.deepEqual(posted.body, { action: 'resume', material: 'material_b', checkpoint_id: 'simckpt_b1' });
});

test('체크포인트가 없는 자재의 resume은 보내지 않는다', async () => {
  const h = harness();
  await h.api.refresh();
  await h.api.start('resume', 'material_a');
  assert.equal(h.calls.filter((c) => c.method === 'POST').length, 0);
  assert.match(h.api.state.notice, /체크포인트가 없습니다/);
});

test('사전검증은 확인 없이 보내고, 시연 정지는 정지 요청을 보낸다', async () => {
  const h = harness();
  await h.api.refresh();
  await h.api.start('resume_preflight', 'material_b');
  assert.equal(h.confirms.length, 0);
  await h.api.stop();
  assert.ok(h.calls.some((c) => c.method === 'POST' && c.path === '/v1/sim-demo/stop'));
  assert.match(h.api.state.notice, /정지를 요청했습니다/);
});

test('자재 목록에 서버가 보낸 색 이름이 보인다', () => {
  const html = card(status());
  // 선언이 있는 자재만 색 이름이 붙는다 — 화면이 색 이름을 만들어 내지 않는다.
  assert.match(html, /A자재[\s\S]*?주황 · 오렌지/);
  assert.doesNotMatch(html, /B자재[\s\S]*?\(.*?·.*?\)<\/span> <span class="mono"/);
});

test('정합 칸은 맞출 기록과 마지막 결과를 보여준다', () => {
  const html = card(status({
    reconcile: { needed: true, available: true,
      last: { at: '2026-09-21T15:00:00+0900', cleared: ['material_a'] } },
  }));
  assert.match(html, /기록 정합/);
  assert.match(html, /맞출 기록이 있습니다/);
  assert.match(html, /material_a/);
  assert.match(html, /data-sim-action="reconcile"/);
});

test('작업이 돌면 정합 버튼도 잠긴다', () => {
  const html = card(status({ running_job: { job_id: 'simjob_9', action_label: '원래 슬롯 복귀',
    material: 'material_a', progress: [] } }));
  const row = buttons(html).find((b) => b.action === 'reconcile');
  assert.ok(row && row.disabled);
});

test('정합 요청은 전용 경로로 간다', async () => {
  const calls = [];
  const fetchImpl = async (method, path, body) => {
    calls.push({ method, path, body });
    if (path === '/v1/sim-demo') return { ok: true, status: 200, payload: status() };
    return { ok: true, status: 202, payload: { job_id: 'simjob_r1', action: 'reconcile' } };
  };
  const root = { innerHTML: '', addEventListener() {}, contains: () => true };
  const cardApi = createSimDemoCard({ root, fetchImpl, confirmImpl: () => true });
  await cardApi.reconcile();
  const posted = calls.find((c) => c.method === 'POST');
  assert.equal(posted.path, '/v1/sim-demo/reconcile');
});

test('전체 복귀는 계획을 요청하고 보여준 뒤 그 계획을 확인한다', async () => {
  const calls = [];
  const confirms = [];
  const plan = {
    goal_id: 'simgoal_plan', goal: 'return_all_to_origin', status: 'planned',
    summary: '컨베이어의 자재 2개를 순서대로 원래 자리로 돌려놓습니다.',
    progress: { completed: 0, total: 2, percent: 0 },
    plan: [
      { step: 1, material_label: 'A자재', from_label: '컨베이어 1번',
        to_label: '원래 자리', status: 'pending' },
      { step: 2, material_label: 'B자재', from_label: '컨베이어 2번',
        to_label: '원래 자리', status: 'pending' },
    ],
  };
  let currentGoal = null;
  const fetchImpl = async (method, path, body) => {
    calls.push({ method, path, body });
    if (method === 'POST' && path === '/v1/sim-demo/goals') {
      currentGoal = plan;
      return { ok: true, status: 201, payload: plan };
    }
    if (method === 'POST' && path === '/v1/sim-demo/goals/simgoal_plan/confirm') {
      currentGoal = { ...plan, status: 'running', confirmation_required: false };
      return { ok: true, status: 202, payload: currentGoal };
    }
    if (method === 'GET' && path === '/v1/sim-demo') {
      return { ok: true, status: 200, payload: status({ goal: currentGoal }) };
    }
    return { ok: false, status: 404, payload: {} };
  };
  const root = { innerHTML: '', addEventListener() {}, contains: () => true };
  globalThis.setTimeout = () => 0;
  globalThis.clearTimeout = () => {};
  const api = createSimDemoCard({ root, fetchImpl,
    confirmImpl: (text) => { confirms.push(text); return true; } });

  await api.refresh();
  await api.startReturnAllGoal();

  assert.deepEqual(calls.find((c) => c.path === '/v1/sim-demo/goals').body,
    { goal: 'return_all_to_origin' });
  assert.deepEqual(calls.find((c) => c.path.endsWith('/confirm')).body,
    { action: 'confirm' });
  assert.match(confirms[0], /1\. A자재: 컨베이어 1번 → 원래 자리/);
  assert.match(confirms[0], /2\. B자재: 컨베이어 2번 → 원래 자리/);
});

test('목표 계획과 순서가 보이고 목표 STOP을 요청할 수 있다', async () => {
  const goal = {
    goal_id: 'simgoal_progress', status: 'running', current_step: 2,
    progress: { completed: 1, total: 2, percent: 50 },
    plan: [
      { step: 1, material_label: 'A자재', from_label: '컨베이어 1번',
        to_label: '원래 자리', status: 'completed' },
      { step: 2, material_label: 'B자재', from_label: '컨베이어 2번',
        to_label: '원래 자리', status: 'running' },
    ],
  };
  const html = card(status({ goal }));
  assert.ok(html.indexOf('1. A자재') < html.indexOf('2. B자재'));
  assert.match(html, /1 \/ 2 \(50%\) · running/);
  assert.match(html, /현재 단계[\s\S]*?2/);
  assert.match(html, /data-sim-action="stop"[^>]*>■ 목표 STOP/);

  const h = harness();
  await h.api.refresh();
  h.api.state.status = status({ goal });
  await h.api.stop();
  assert.ok(h.calls.some((c) => c.method === 'POST' && c.path === '/v1/sim-demo/stop'));
});
