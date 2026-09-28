/** 시뮬레이션 시연 카드 — 이송(상태 유지)·원래 슬롯 복귀·resume·복구·시연 정지.
 *
 * **시뮬레이터 전용이다.** 서버의 `/v1/sim-demo*`가 검증된 시연 스크립트를 별도
 * 프로세스 작업으로 띄운다. 일반 명령(계획 → 실행)의 pick/place 차단과 무관하다.
 *
 * - 버튼 활성화는 서버가 준 **안내**다. 실제 허용 여부는 시연 스크립트가 관측으로
 *   다시 검증하고, 막히면 결과에 이유가 남는다.
 * - 로봇이 움직이는 동작은 누를 때 한 번 더 확인한다. 누르지 않으면 아무것도
 *   움직이지 않는다.
 * - 시연 정지와 헤더의 전체 정지는 모두 서버에 **정지 요청**을 보낸다. 시연
 *   스크립트가 기존 STOP 절차(취소 → 정지 확인 → 체크포인트)로 멈춘다.
 *
 * 다른 카드와 상태 저장소를 공유하지 않는다 — 자기 카드(`#card-sim-demo`)만 그린다.
 */

const esc = (value) =>
  String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

const pill = (color, icon, label) =>
  `<span class="pill ${color}"><span>${esc(icon)}</span>${esc(label)}</span>`;

const header = (title, rightHtml) =>
  `<div class="card-header"><h3 class="card-title">${esc(title)}</h3>${rightHtml}</div>`;

/** 화면 갱신 주기(ms). 작업이 돌 때만 자주 본다. */
const POLL_RUNNING_MS = 1500;
const POLL_IDLE_MS = 6000;

/** 로봇이 움직이는 동작. 누를 때 확인을 받는다. */
const MOVING_ACTIONS = new Set(['transfer', 'return', 'resume']);

const RECORD_LABELS = {
  held_on_target: ['info', '▣', '컨베이어에 유지'],
  stopped_unrestored: ['danger', '■', '정지됨 · 복구 필요'],
  fault_unrestored: ['danger', '✕', '실패 · 복구 필요'],
  return_stopped: ['danger', '■', '복귀 중 정지 · 복구 필요'],
  return_failed: ['danger', '✕', '복귀 실패 · 복구 필요'],
};

const RESULT_LABELS = {
  simulation_transfer_completed: ['success', '✓', '이송 완료'],
  simulation_transfer_resumed_completed: ['success', '✓', '이어서 이송 완료'],
  returned_to_origin: ['success', '✓', '원래 슬롯 복귀'],
  simulation_transfer_stopped: ['danger', '■', '정지됨'],
  resume_stopped: ['danger', '■', 'resume 중 정지'],
  return_stopped: ['danger', '■', '복귀 중 정지'],
  resume_blocked: ['warning', '!', 'resume 차단'],
  return_not_started: ['warning', '!', '복귀 시작 안 함'],
  simulation_transfer_not_started: ['warning', '!', '이송 시작 안 함'],
  simulation_transfer_incomplete: ['danger', '✕', '이송 실패'],
  resume_failed: ['danger', '✕', 'resume 실패'],
  return_failed: ['danger', '✕', '복귀 실패'],
};

async function call(method, path, body) {
  const options = { method, headers: { 'content-type': 'application/json' } };
  if (body !== undefined) options.body = JSON.stringify(body);
  const response = await fetch(path, options);
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  return { ok: response.ok, status: response.status, payload };
}

export function createSimDemoCard({ root, fetchImpl = null, confirmImpl = null } = {}) {
  const state = {
    available: null, // null=확인 전, false=서버 없음
    status: null,
    job: null, // 마지막으로 본 작업 상세
    notice: '',
    busy: false,
  };
  let timer = null;
  const request = fetchImpl || call;
  const confirmAction = confirmImpl || ((text) => window.confirm(text));

  function draw() {
    if (!root) return;
    root.innerHTML = renderCard(state);
  }

  async function refresh() {
    try {
      const response = await request('GET', '/v1/sim-demo');
      if (!response.ok) {
        state.available = false;
        state.notice = `상태를 읽지 못했습니다 (${response.status})`;
      } else {
        state.available = true;
        state.status = response.payload;
        const running = response.payload.running_job;
        const jobId = running ? running.job_id : state.job && state.job.job_id;
        if (jobId && (running || (state.job && state.job.status === 'running'))) {
          const detail = await request('GET', `/v1/sim-demo/jobs/${encodeURIComponent(jobId)}`);
          if (detail.ok) state.job = detail.payload;
        }
      }
    } catch {
      state.available = false;
      state.notice = '서버에 연결되어 있을 때만 쓸 수 있습니다.';
    }
    draw();
    schedule();
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    const running = state.status && state.status.running_job;
    timer = setTimeout(refresh, running ? POLL_RUNNING_MS : POLL_IDLE_MS);
  }

  async function start(action, material) {
    const status = state.status || {};
    const labels = status.action_labels || {};
    const row = (status.materials || []).find((m) => m.model === material) || {};
    const checkpoint = (status.state || {}).checkpoint || null;
    const body = { action, material };
    if (action === 'resume' || action === 'resume_preflight') {
      if (!checkpoint || checkpoint.model !== material) {
        state.notice = '이 자재의 체크포인트가 없습니다.';
        draw();
        return;
      }
      body.checkpoint_id = checkpoint.checkpoint_id;
    }
    if (MOVING_ACTIONS.has(action)) {
      const text = `[시뮬레이터] ${row.korean || material}: ${labels[action] || action}\n`
        + 'Gazebo 시뮬레이터에서 로봇이 움직입니다. 실제 로봇이 아닙니다. 진행할까요?';
      if (!confirmAction(text)) return;
    }
    state.busy = true;
    draw();
    const response = await request('POST', '/v1/sim-demo/jobs', body);
    state.busy = false;
    if (response.ok) {
      state.job = response.payload;
      state.notice = '';
    } else {
      state.notice = response.payload.detail || response.payload.message
        || `시작하지 못했습니다 (${response.status})`;
    }
    await refresh();
  }

  async function stop() {
    const response = await request('POST', '/v1/sim-demo/stop', {});
    state.notice = response.ok && response.payload.requested
      ? '정지를 요청했습니다. 시뮬레이터가 정지를 확인하면 체크포인트가 남습니다.'
      : (response.payload.detail || '실행 중인 시연 작업이 없습니다.');
    await refresh();
  }

  function onClick(event) {
    const target = event.target.closest('[data-sim-action]');
    if (!target || !root.contains(target) || target.disabled) return;
    const action = target.getAttribute('data-sim-action');
    if (action === 'stop') stop();
    else start(action, target.getAttribute('data-material'));
  }

  if (root) root.addEventListener('click', onClick);
  draw();
  return { refresh, state, start, stop };
}

// ── 그리기 ────────────────────────────────────────────────────────────
function renderCard(state) {
  if (state.available === null) {
    return `${header('시뮬레이션 시연', pill('muted', '◌', '확인 중'))}
      <div class="card-body"><p class="hint">상태를 읽는 중…</p></div>`;
  }
  if (state.available === false || !state.status) {
    return `${header('시뮬레이션 시연', pill('muted', '◉', '사용 불가'))}
      <div class="card-body"><p class="hint"><span class="hint-mark">✕</span>
        ${esc(state.notice || '서버에 연결되어 있을 때만 쓸 수 있습니다.')}</p></div>`;
  }
  const status = state.status;
  if (!status.enabled) {
    return `${header('시뮬레이션 시연', pill('muted', '◉', '꺼짐'))}
      <div class="card-body">
        <p class="hint"><span class="hint-mark">✕</span>
          웹 시연이 꺼져 있습니다 — ${esc(status.reason || '이유 미기록')}</p>
        <p class="hint">서버를 <span class="mono">FORSTICK2_SIM_DEMO_WEB=1</span>로
          띄우면 시뮬레이터 시연을 여기서 실행할 수 있습니다.</p>
      </div>`;
  }
  const demo = status.state || {};
  const running = status.running_job;
  const badge = running
    ? pill('success', '●', '실행 중')
    : demo.state_hold_active
      ? pill('info', '▣', demo.display_label || '상태 유지')
      : pill('info', '⬡', '시뮬레이터 전용');
  return `${header('시뮬레이션 시연', badge)}
    <div class="card-body">
      <p class="hint"><span class="hint-mark">⬡</span>
        Gazebo 시뮬레이터 안의 이송입니다. 실제 로봇 결과가 아니며
        (<span class="mono">is_simulated=true</span>), 일반 명령의 집기·놓기는 계속 차단됩니다.</p>
      ${renderRunning(state, running)}
      ${renderMaterials(status, Boolean(running) || state.busy)}
      ${renderCheckpoint(demo)}
      ${renderLastJob(state.job, running)}
      ${state.notice ? `<p class="hint" role="status"><span class="hint-mark">!</span>${esc(state.notice)}</p>` : ''}
    </div>`;
}

function renderRunning(state, running) {
  if (!running) return '';
  const job = state.job && state.job.job_id === running.job_id ? state.job : running;
  const progress = job.progress || [];
  const last = progress[progress.length - 1];
  const text = last ? `${last.no}/${last.of} ${last.label}` : '시작 준비 중';
  return `<div class="rows" style="margin-bottom:8px">
      <div class="row"><span class="row-label">작업</span>
        <div class="row-value">${esc(running.action_label)} · <span class="mono">${esc(running.material)}</span></div></div>
      <div class="row"><span class="row-label">진행</span>
        <div class="row-value mono">${esc(text)}</div></div>
      ${running.stop_requested ? '<div class="row"><span class="row-label">정지</span><div class="row-value">요청됨 — 확인 대기</div></div>' : ''}
    </div>
    <button class="btn danger block" type="button" data-sim-action="stop">■ 시연 정지</button>`;
}

function renderMaterials(status, locked) {
  const labels = status.action_labels || {};
  const order = ['transfer', 'return', 'resume_preflight', 'resume', 'restore'];
  return (status.materials || [])
    .map((material) => {
      const record = material.record;
      const tag = record && RECORD_LABELS[record.state]
        ? pill(...RECORD_LABELS[record.state])
        : pill('muted', '▪', '원래 자리');
      const buttons = order
        .map((action) => {
          const enabled = !locked && material.actions && material.actions[action];
          return `<button class="btn sm${action === 'transfer' || action === 'resume' ? ' primary' : ''}"
                    type="button" data-sim-action="${action}" data-material="${esc(material.model)}"
                    ${enabled ? '' : 'disabled'} title="${esc(labels[action] || action)}">${esc(labels[action] || action)}</button>`;
        })
        .join('');
      return `<div style="margin-top:10px">
          <div class="row"><span class="truncate">${esc(material.korean)} <span class="mono robot-pick-sub">${esc(material.model)}</span></span>${tag}</div>
          <div class="pill-row" style="margin-top:6px">${buttons}</div>
        </div>`;
    })
    .join('');
}

function renderCheckpoint(demo) {
  const checkpoint = demo.checkpoint;
  const preflight = demo.resume_preflight;
  if (!checkpoint) return '';
  return `<div class="rows" style="margin-top:12px">
      <p class="label-caps">STOP 체크포인트</p>
      <div class="row"><span class="row-label">자재 · 상태</span>
        <div class="row-value"><span class="mono">${esc(checkpoint.model)}</span> · ${esc(checkpoint.object_state)}</div></div>
      <div class="row"><span class="row-label">정지 단계</span>
        <div class="row-value mono">${esc(checkpoint.stopped_stage)}</div></div>
      <div class="row"><span class="row-label">체크포인트</span>
        <div class="row-value mono truncate" title="${esc(checkpoint.checkpoint_id)}">${esc(checkpoint.checkpoint_id)}</div></div>
      ${preflight ? `<div class="row"><span class="row-label">사전검증</span>
        <div class="row-value">${preflight.allowed ? pill('success', '✓', '통과') : pill('warning', '!', '차단')}</div></div>` : ''}
      <p class="hint">resume은 실행 직전에 scene·자재·고정·관절·goal을 다시 관측해 검증합니다.</p>
    </div>`;
}

function renderLastJob(job, running) {
  if (!job || (running && running.job_id === job.job_id) || job.status === 'running') return '';
  const report = job.report || {};
  const result = RESULT_LABELS[report.status];
  const reasons = report.reasons || report.reason_codes || [];
  return `<div class="rows" style="margin-top:12px">
      <p class="label-caps">마지막 작업</p>
      <div class="row"><span class="row-label">${esc(job.action_label)}</span>
        <div class="row-value">${result ? pill(...result) : esc(report.status || `종료 코드 ${job.exit_code}`)}</div></div>
      ${reasons.length ? `<p class="hint">${reasons.map((r) => esc(r)).join('<br>')}</p>` : ''}
      ${report.raw_log_path ? `<p class="hint">원본 로그 <span class="mono">${esc(report.raw_log_path)}</span></p>` : ''}
    </div>`;
}

export { renderCard as renderSimDemoCard };
