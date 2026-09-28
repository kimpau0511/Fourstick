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
  assert.deepEqual(open.sort(), ['material_a:transfer', 'material_b:restore',
    'material_b:resume', 'material_b:resume_preflight'].sort());
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
  assert.match(yes.confirms[0], /실제 로봇이 아닙니다/);

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
