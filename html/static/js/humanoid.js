// G1 휴머노이드 카드 — 명령 결과 · 확인 카드 · 진행 · STOP · 3D(관측).
//
// - 명령은 `/v1/humanoid/*`로만 간다. FR3 경로(`/v1/sim-demo*`, `/v1/plan`)와 대화 맥락·확인
//   토큰을 공유하지 않는다.
// - 이동은 확인 카드에서 승인해야 실행된다. "멈춰"와 이 카드의 정지 버튼은 **확인 없이** 바로 간다.
// - 제어기가 꺼졌거나 시뮬레이션 시각이 멈췄으면 그대로 적는다 — 정상 실행처럼 보이지 않게.
// - 정지 뒤 상태는 "이동 목표 취소"와 "균형 제어(제자리 걸음)"를 따로 적는다. 서 있는 정지 자세가 아니다.

import { createG1View } from './humanoid-view.js';

const esc = (value) => String(value == null ? '' : value)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const pill = (color, icon, label) =>
  `<span class="pill ${color}"><span>${esc(icon)}</span>${esc(label)}</span>`;
const row = (label, valueHtml) =>
  `<div class="row"><span class="row-label">${esc(label)}</span><div class="row-value">${valueHtml}</div></div>`;

const BALANCE_TEXT = {
  stepping_in_place: '제자리 걸음으로 균형 유지 중(정책·PD 동작) — 서 있는 정지 자세가 아님',
  walking: '걷는 중(속도 명령 있음)',
  correcting_drift: '제자리 걸음 표류 보정 중 — 정지한 자리로 몇 걸음 되돌아감(이동 목표 아님)',
  fallen: '넘어짐 — 정책 정지, 기본 자세 PD만 유지',
};
const STEP_TEXT = { save_start: '출발 위치 저장', goto: '지점으로 이동', return: '출발 위치로 복귀' };
const STATUS_PILL = {
  pending: ['muted', '○', '대기'], running: ['info', '▶', '진행 중'], done: ['success', '✓', '관측 확인'],
  cancelled: ['warning', '■', '취소됨'], failed: ['danger', '✕', '실패'],
};

function holdText(hold) {
  if (!hold) return '—';
  if (!hold.anchor) return '감속 중 — 멈춘 자리를 기준으로 잡는 중';
  return `기준 (${hold.anchor.xy.map((v) => v.toFixed(2)).join(', ')}) m에서 ${hold.dist_m} m`
    + (hold.correcting ? ' · 표류 보정 중' : ' · 허용 범위 안(0.15 m)');
}

async function call(method, path, body) {
  const response = await fetch(path, {
    method, headers: { 'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined, cache: 'no-store',
  });
  const payload = await response.json().catch(() => ({}));
  return { status: response.status, payload };
}

export function createHumanoidPanel({ root, getSessionId, log }) {
  const st = { status: null, result: null, pending: null, job: null, stop: null,
    busy: false, visible: false, view: null };

  root.innerHTML = `
    <div class="card-header"><h3 class="card-title">G1 휴머노이드 · Gazebo 시뮬레이션</h3>
      <div class="btn-row"><button class="btn sm orange" type="button" data-g1="stop">■ G1 멈춰</button></div></div>
    <div class="card-body">
      <div class="pill-row" id="g1-health"></div>
      <div class="scene-frame sim3d-frame" id="g1-frame">
        <div class="scene-badges"><span class="badge-overlay" id="g1-3d-status">연결 중…</span></div>
      </div>
      <p class="hint"><span class="hint-mark">⬢</span><strong>다리 12관절 제어 · 팔·허리 고정.</strong>
        Gazebo가 관측한 골반 위치·방향과 다리 관절만 그립니다(읽기 전용). 초록 원판 = 선언된 안전 지점,
        주황 선 = 컨베이어 금지선. 시뮬레이션 시각과 벽시계 시간을 따로 적습니다.</p>
      <div id="g1-time" class="hint"></div>
      <div id="g1-result"></div>
      <div id="g1-job"></div>
    </div>`;
  const $ = (id) => root.querySelector(`#${id}`);
  st.view = createG1View({ frame: $('g1-frame'), statusEl: $('g1-3d-status') });

  root.addEventListener('click', (event) => {
    const target = event.target.closest('[data-g1]');
    if (!target) return;
    const action = target.dataset.g1;
    if (action === 'stop') stop('G1 카드 정지 버튼');
    if (action === 'confirm' || action === 'cancel') answer(action);
  });

  function renderHealth() {
    const h = (st.status || {}).health || {};
    const parts = [];
    if (!st.status) parts.push(pill('muted', '○', '상태 모름'));
    else if (h.controller === 'alive') parts.push(pill('success', '●', '제어기 동작'));
    else if (h.controller === 'stale') parts.push(pill('danger', '■', '제어기 응답 없음'));
    else parts.push(pill('danger', '■', '제어기 꺼짐'));
    if (h.controller === 'alive') {
      parts.push(h.sim_advancing ? pill('success', '▶', '시뮬레이션 진행 중')
        : pill('danger', '❚❚', '시뮬레이션 멈춤'));
    }
    if (h.balance) parts.push(pill(h.balance === 'fallen' ? 'danger' : 'info', '⬢', BALANCE_TEXT[h.balance] || h.balance));
    if (h.hold) parts.push(pill('muted', '⌖', `자리 유지: ${holdText(h.hold)}`));
    else if (h.nav && h.nav.stage === 'holding') {
      parts.push(pill('muted', '⌖', `자리 유지: 도착 지점(${(h.nav.goal || {}).name || ''}) 유지 중 — 제자리 걸음 표류 보정`));
    }
    if (h.ready === false && h.reason) parts.push(pill('warning', '!', `실행 불가: ${h.reason}`));
    $('g1-health').innerHTML = parts.join(' ');
    const t = [];
    if (h.sim_time_s != null) t.push(`시뮬레이션 시각 ${Number(h.sim_time_s).toFixed(2)} s`);
    if (h.sim_rate_measured != null) {
      t.push(`시뮬레이션 속도 ${h.sim_rate_measured.toFixed(2)}× 실제 시간${h.sim_rate_measured < 0.95 ? ' (실시간 아님)' : ''}`);
    }
    if (h.status_age_wall_s != null) t.push(`제어기 상태 나이 ${Math.round(h.status_age_wall_s * 1000)} ms(벽시계)`);
    $('g1-time').textContent = t.join(' · ');
  }

  function renderResult() {
    const r = st.result;
    let html = '';
    if (st.pending) {
      const p = st.pending;
      html += `<div class="confirm">
        <div class="confirm-head">${pill('decide', '◈', '확인 필요 — G1')}</div>
        <p class="confirm-summary">${esc(p.summary)}</p>
        <div class="confirm-actions">
          <button class="btn decide" type="button" data-g1="confirm" ${st.busy ? 'disabled' : ''}>✓ 확인 — Gazebo에서 실행</button>
          <button class="btn outline" type="button" data-g1="cancel" ${st.busy ? 'disabled' : ''}>취소</button>
        </div>
        <div class="confirm-evidence"><p class="label-caps">실행 순서</p>${p.steps
          .map((s, i) => row(`STEP ${String(i + 1).padStart(2, '0')}`, `<span>${esc(s.text)}</span>`)).join('')}</div>
        <div class="confirm-evidence"><p class="label-caps">판단 근거</p>${p.evidence
          .map((e) => row('근거', `<span>${esc(e)}</span>`)).join('')}
          ${row('도착 판정', `<span>${esc(p.arrival_rule)}</span>`)}</div>
        <p class="hint"><span class="hint-mark">⬡</span><strong>확인을 누르기 전에는 G1을 움직이지 않습니다.</strong>
          실행 중 "멈춰"는 확인 없이 바로 전달됩니다. 웹은 세계 초기화를 하지 않습니다.</p></div>`;
    } else if (r && ['ASK', 'BLOCK', 'CANCELLED', 'NOOP'].includes(r.decision)) {
      const [color, icon, label] = {
        ASK: ['warning', '?', '되묻기'], BLOCK: ['danger', '✕', '차단'],
        CANCELLED: ['muted', '✕', '취소'], NOOP: ['muted', '=', '이동 없음'],
      }[r.decision];
      html += `<div class="rows">${row('판단', pill(color, icon, label))}
        ${r.utterance ? row('인식한 명령', `<strong>${esc(r.utterance)}</strong>`) : ''}
        ${row('이유', `<span>${esc(r.reason || '')}</span>`)}</div>`;
    }
    if (st.stop) {
      const s = st.stop;
      html += `<div class="rows">
        ${row('정지', s.applied ? pill('warning', '■', `제어기 적용(${s.sent_to_applied_wall_ms} ms 벽시계, 시뮬레이션 ${s.applied_sim_s} s)`)
          : pill('danger', '✕', `적용 확인 안 됨${s.error ? ` — ${s.error}` : ''}`))}
        ${row('이동 목표', `<span>${s.goal_cancelled ? `취소됨 (${esc(s.goal_cancelled)})` : '진행 중인 이동 목표 없음'}</span>`)}
        ${row('균형 상태', `<span>${esc(BALANCE_TEXT[s.balance] || s.balance || '알 수 없음')}</span>`)}
        ${row('자리 유지', `<span>${esc(holdText(((st.status || {}).health || {}).hold || s.hold))}</span>`)}</div>`;
    }
    $('g1-result').innerHTML = html;
  }

  function renderJob() {
    const j = st.job;
    if (!j) { $('g1-job').innerHTML = ''; return; }
    const head = {
      running: pill('info', '▶', `실행 중 · ${STEP_TEXT[j.stage] || j.stage}`),
      completed: pill('success', '✓', '완료 — Gazebo 관측으로 도착·복귀 확인'),
      stopped: pill('warning', '■', '정지 — 이동 목표 취소, 균형 제어(제자리 걸음) 유지'),
      failed: pill('danger', '✕', `실패 — ${j.reason || ''}`),
    }[j.status] || pill('muted', '○', j.status);
    const rows = j.steps.map((s, i) => {
      const [c, icon, label] = STATUS_PILL[s.status] || ['muted', '○', s.status];
      const detail = s.observed_max_dist_m != null
        ? ` · 관측 최대 오차 ${s.observed_max_dist_m} m / ${s.observed_max_yaw_err_rad} rad · 소요 ${s.duration_sim_s} s(시뮬레이션)`
        : s.reason ? ` · ${s.reason}` : '';
      return row(`STEP ${i + 1}`, `${pill(c, icon, label)} <span>${esc(STEP_TEXT[s.step] || s.step)}${esc(detail)}</span>`);
    }).join('');
    const wall = j.ended_wall && j.started_wall ? (j.ended_wall - j.started_wall).toFixed(1) : null;
    const sim = j.ended_sim != null && j.started_sim != null ? (j.ended_sim - j.started_sim).toFixed(1) : null;
    $('g1-job').innerHTML = `<div class="rows">${row('G1 작업', `${head} <span class="mono">${esc(j.job_id)}</span>`)}
      ${rows}${wall ? row('걸린 시간', `<span>시뮬레이션 ${sim} s · 벽시계 ${wall} s</span>`) : ''}</div>`;
  }

  function draw() { renderHealth(); renderResult(); renderJob(); }

  async function refresh() {
    try {
      const { payload } = await call('GET', `/v1/humanoid?session_id=${encodeURIComponent(getSessionId() || '')}`);
      st.status = payload;
      if (!st.job && payload.last_job) st.job = payload.last_job;
      if (payload.running_job) st.job = payload.running_job;
    } catch { st.status = null; }
    draw();
  }

  async function pollJob(jobId) {
    const { status, payload } = await call('GET', `/v1/humanoid/jobs/${encodeURIComponent(jobId)}`);
    if (status !== 200) return;
    st.job = payload;
    draw();
    if (payload.status === 'running') { setTimeout(() => pollJob(jobId), 500); return; }
    log(payload.status === 'completed' ? 'info' : 'warning',
      `G1 작업 ${payload.status}: ${payload.reason || '관측으로 도착·복귀 확인'}`);
  }

  async function command(text, source = 'text') {
    st.busy = true; st.stop = null;
    const { payload } = await call('POST', '/v1/humanoid/command',
      { session_id: getSessionId(), utterance: text, source });
    st.busy = false;
    st.result = payload;
    st.pending = payload.decision === 'CONFIRM' ? payload.confirmation : null;
    if (payload.decision === 'STOP') st.stop = payload.stop;
    log(payload.decision === 'BLOCK' ? 'warning' : 'info',
      `G1 명령 판단 ${payload.decision}${payload.reason ? ` — ${payload.reason}` : ''}`);
    draw();
    return payload;
  }

  async function answer(action) {
    if (!st.pending) return;
    st.busy = true; draw();
    const { payload } = await call('POST', '/v1/humanoid/confirm',
      { session_id: getSessionId(), token: st.pending.token, action });
    st.busy = false; st.pending = null; st.result = payload;
    if (payload.decision === 'RUN' && payload.job) {
      st.job = payload.job;
      log('execution', `G1 작업 시작: ${payload.job.job_id}`);
      pollJob(payload.job.job_id);
    } else {
      log('warning', `G1 확인 결과 ${payload.decision}: ${payload.reason || ''}`);
    }
    draw();
  }

  async function stop(reason = '멈춰') {
    // 확인 카드를 기다리지 않는다.
    const { payload } = await call('POST', '/v1/humanoid/stop', { session_id: getSessionId(), reason });
    st.stop = payload.stop; st.pending = null;
    log('warning', `G1 정지: 목표 ${payload.stop && payload.stop.goal_cancelled ? '취소' : '없음'} · 균형 제어 유지`);
    draw();
    refresh();
    return payload;
  }

  let timer = null;
  return {
    command, stop,
    show(visible) {
      st.visible = visible;
      root.hidden = !visible;
      st.view.setVisible(visible);
      if (visible) {
        st.view.start();
        refresh();
        if (!timer) timer = setInterval(() => { if (st.visible) refresh(); }, 1000);
      }
    },
    state: () => ({ ...st, view: st.view.snapshot() }),
  };
}
