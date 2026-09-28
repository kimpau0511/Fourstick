/** 시뮬레이션 작업 명령(텍스트·STT) 화면 테스트 (node --test).
 *
 * 확인하는 것:
 *  - **모드 토글이 없다** — 작업 셀에 붙으면 시뮬레이션 명령이 기본이다
 *  - 작업 셀에 붙으면 제목이 "시뮬레이션 작업 명령"이고 시뮬레이션 문구가 항상 보인다
 *  - RUN/ASK/BLOCK/STOP 결과와 job id·진행 단계·사유가 결과 영역에 나온다
 *  - 상태 전이가 결과·진행을 그대로 담는다
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import { renderCommandCard, renderSimCommandArea } from '../../html/static/js/render.js';
import { initialState, reduce } from '../../html/static/js/state.js';

const NOTICE = 'Gazebo 시뮬레이션 · 실제 로봇 아님';

function withMode(_unused, extra = {}) {
  return { ...initialState(), command: 'A 자재를 컨베이어로 옮겨줘',
    serverWorkcell: { registered: true, world: 'forstick2_fr3_2f85_workcell' },
    simDemo: { busy: false, result: null, job: null, ...extra } };
}

test('모드 토글이 없다', () => {
  const html = renderCommandCard(withMode(), 'server');
  assert.doesNotMatch(html, /toggle-sim-demo-mode/);
  assert.doesNotMatch(html, /시뮬레이션 시연 모드/);
  assert.doesNotMatch(html, /type="checkbox"/);
});

test('작업 셀에 붙으면 제목과 안내가 시뮬레이션 기준이다', () => {
  const html = renderCommandCard(withMode(), 'server');
  assert.match(html, /시뮬레이션 작업 명령/);
  assert.match(html, new RegExp(NOTICE));
  assert.match(html, /명령 보내기/);
  assert.match(html, /원래 자리로 돌려놔/);
  assert.match(html, /기존 계획 생성으로 갑니다/);
});

test('작업 셀에 붙지 않으면 기존 제목을 쓴다', () => {
  const state = { ...withMode(), serverWorkcell: null };
  const html = renderCommandCard(state, 'simulation');
  assert.match(html, /작업 명령/);
  assert.doesNotMatch(html, /시뮬레이션 작업 명령/);
});

test('RUN 결과는 작업 id와 진행 단계를 보여준다', () => {
  const state = withMode(null, {
    result: { decision: 'RUN', intent: 'transfer', material: 'material_a',
      utterance: 'A 자재를 컨베이어로 옮겨줘', simulation_notice: NOTICE,
      job: { job_id: 'simjob_1', status: 'running' } },
    job: { job_id: 'simjob_1', status: 'running',
      progress: [{ no: 7, of: 12, label: 'lift', reached: true }] },
  });
  const html = renderSimCommandArea(state);
  assert.match(html, /작업 생성/);
  assert.match(html, /simjob_1/);
  assert.match(html, /7\/12 lift/);
  assert.match(html, /material_a/);
  assert.match(html, new RegExp(NOTICE));
});

test('ASK·BLOCK은 사유만 보여주고 작업 정보가 없다', () => {
  for (const decision of ['ASK', 'BLOCK']) {
    const html = renderSimCommandArea(withMode(null, {
      result: { decision, utterance: '자재를 옮겨줘', reason: '어느 자재인지 알 수 없습니다',
        simulation_notice: NOTICE, job: null },
    }));
    assert.match(html, /어느 자재인지 알 수 없습니다/);
    assert.doesNotMatch(html, /simjob_/);
    assert.match(html, new RegExp(decision === 'ASK' ? 'ASK' : 'BLOCK'));
  }
});

test('STOP 결과는 정지 요청 상태를 보여준다', () => {
  const html = renderSimCommandArea(withMode(null, {
    result: { decision: 'STOP', intent: 'stop', utterance: '멈춰',
      simulation_notice: NOTICE, job: null, stop: { requested: true } },
  }));
  assert.match(html, /정지 요청/);
  assert.match(html, /체크포인트가 남습니다/);
});

test('완료된 작업은 결과 상태를 보여준다', () => {
  const html = renderSimCommandArea(withMode(null, {
    result: { decision: 'RUN', utterance: 'A 자재를 컨베이어로 옮겨줘',
      simulation_notice: NOTICE, job: { job_id: 'simjob_2' } },
    job: { job_id: 'simjob_2', status: 'finished', exit_code: 0, progress: [],
      report: { status: 'simulation_transfer_completed' } },
  }));
  assert.match(html, /simulation_transfer_completed/);
});

test('상태 전이가 결과·진행을 담는다', () => {
  let state = initialState();
  assert.equal(state.simDemo.mode, undefined);
  state = reduce(state, { type: 'sim-demo-busy', busy: true });
  assert.equal(state.simDemo.busy, true);
  const result = { decision: 'RUN', job: { job_id: 'simjob_3' } };
  state = reduce(state, { type: 'sim-demo-result', result });
  assert.equal(state.simDemo.busy, false);
  assert.equal(state.simDemo.result, result);
  assert.equal(state.simDemo.job.job_id, 'simjob_3');
  state = reduce(state, { type: 'sim-demo-job', job: { job_id: 'simjob_3', status: 'finished' } });
  assert.equal(state.simDemo.job.status, 'finished');
});
