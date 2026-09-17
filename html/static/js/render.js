/** 화면 그리기. 상태 → DOM. **여기서 상태를 바꾸지 않는다.**
 *
 * 원본 디자인: html/목업/src/App.tsx (레이아웃·색·문구 구조를 유지한다).
 * 정책 차이: 작업자 승인 단계가 없다. 안전 판단은 PASS/BLOCK/ASK만 보여주고,
 * PASS일 때만 실행 시작을 누를 수 있다.
 */

import {
  EVENT_LEVELS,
  ROBOTS,
  activeNodeIndex,
  describeStep,
  planResourceMapping,
  policyItems,
  resourceLabel,
  robotById,
  simNodesFromPlan,
  withWorkcell,
  workcellResources,
} from './catalog.js';
import {
  EXECUTION,
  VERDICT,
  canCancel,
  canExecute,
  isActive,
  isStopped,
  progressPercent,
} from './state.js';

const esc = (value) =>
  String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

const pill = (color, icon, label) =>
  `<span class="pill ${color}"><span>${esc(icon)}</span>${esc(label)}</span>`;

const row = (label, valueHtml) =>
  `<div class="row"><span class="row-label">${esc(label)}</span>`
  + `<div class="row-value">${valueHtml}</div></div>`;

const meta = (label, value, mono = false) =>
  `<div class="meta"><span class="meta-label">${esc(label)}</span>`
  + `<span class="meta-value${mono ? ' mono' : ''}">${esc(value)}</span></div>`;

const cardHeader = (title, rightHtml = '') =>
  `<div class="card-header"><h3 class="card-title">${esc(title)}</h3>${rightHtml}</div>`;

function clockText(seconds) {
  if (!seconds) return '—';
  return new Date(seconds * 1000).toLocaleTimeString('ko-KR', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

function expiryText(plan) {
  if (!plan || !plan.createdAt || !plan.ttlSec) return '—';
  const minutes = Math.round(plan.ttlSec / 60);
  return `${minutes}분 / ${clockText(plan.createdAt + plan.ttlSec)}까지`;
}

// ── 왼쪽: 로봇 및 정책 ────────────────────────────────────────────────
export function renderRobotCard(state) {
  // 서버가 붙어 있으면 **서버가 알려 준 작업 셀 값**으로 덮어쓴다.
  const robot = withWorkcell(robotById(state.robotId), state.serverWorkcell);
  const running = isActive(state);
  const badges = robot.badges.map((b) => pill(b.color, b.icon, b.label)).join('');
  const skills = robot.skills
    .map((skill) => `<span class="chip skill">${esc(skill)}</span>`)
    .join('');
  const offSkills = robot.disabledSkills
    .map((skill) => `<span class="chip skill off" title="비활성">${esc(skill)}</span>`)
    .join('');

  const disabledBlock = robot.disabledSkills.length
    ? `<div>
         <p class="label-caps">비활성 스킬</p>
         <div class="pill-row" style="margin-top:6px">${offSkills}</div>
         <p class="hint" style="margin-top:6px"><span class="hint-mark">✕</span>
           비활성 사유: ${esc(robot.disabledReason)}</p>
       </div>`
    : '';

  // 서버에 등록된 실행 어댑터. 선언된 구성(FR3-WMS)과 다르면 그 사실을 적는다.
  const server = state.serverRobot;
  const executionRow = server
    ? row(
        '실행 어댑터',
        `<span class="mono">${esc(server.robot_id || '—')}</span>`
          + (server.kind ? ` <span class="robot-pick-sub">(${esc(server.kind)})</span>` : ''),
      )
    : row('실행 어댑터', '<span class="robot-pick-sub">시뮬레이션 모드 (서버 미연결)</span>');
  const workcell = state.serverWorkcell;
  const workcellRows = workcell
    ? (workcell.registered
        ? row('작업 셀', `<span class="mono">${esc(workcell.workcell_id || '—')}`
            + ` ${esc(workcell.workcell_version || '')}</span>`)
          + row('Gazebo world', `<span class="mono">${esc(workcell.world || '—')}</span>`)
          + row('격리', `<span class="mono">${esc(workcell.gz_partition || '—')}`
            + ` · domain ${esc(String(workcell.ros_domain_id ?? '—'))}</span>`)
        : row('작업 셀', `<span class="hint">연결되지 않았습니다 — `
            + `${esc(workcell.detail || '이유 미기록')}</span>`))
    : '';
  const workcellNotice =
    workcell && workcell.registered
      ? `<div class="notice info">
           <span class="notice-mark">⬡</span>
           <p><strong>Gazebo 작업 셀에 연결됨</strong> — 실행은
           ${esc(workcell.world || '')} 시뮬레이터에서 수행됩니다.
           실제 하드웨어는 연결되지 않았습니다.</p>
         </div>`
      : '';

  const fakeNotice =
    server && server.kind === 'fake' && !(workcell && workcell.registered)
      ? `<div class="notice danger">
           <span class="notice-mark">⚠</span>
           <p><strong>개발용 Fake 실행</strong> — 실행은 개발용 Fake Adapter(${esc(
             server.robot_id || 'fake',
           )})로 수행됩니다. ${esc(robot.name)} 실기·Gazebo 경로는 아직 실행 경로에
           연결되지 않았습니다.</p>
         </div>`
      : '';

  const verification = robot.verification.length
    ? robot.verification.map((item) => row(item.label, `<span>${esc(item.value)}</span>`)).join('')
    : '';

  return `
    ${cardHeader(
      '로봇 및 정책',
      running ? pill('success', '▶', 'RUNNING') : pill('muted', '◉', 'IDLE'),
    )}
    <div class="card-body">
      <button class="robot-pick" data-action="open-robot" type="button">
        <span style="display:flex;align-items:center;gap:8px;min-width:0">
          <span style="font-size:16px">${esc(robot.icon)}</span>
          <span style="min-width:0">
            <span class="robot-pick-name truncate" style="display:block">${esc(robot.name)}</span>
            <span class="robot-pick-sub truncate" style="display:block">${esc(robot.summary)}</span>
          </span>
        </span>
        <span class="robot-pick-more">변경 ›</span>
      </button>

      <div class="pill-row">${badges}</div>

      <div class="rows">
        ${row('상태', `<span>${esc(robot.implemented ? '개발용 시뮬레이터' : '미구현')}</span>`)}
        ${row('어댑터', `<span class="mono">${esc(robot.adapter)}</span>`)}
        ${row('프로필', `<span class="mono">${esc(robot.profile)}</span>`)}
        ${row('페이로드', `<span class="mono">${esc(robot.payload)}</span>`)}
        ${row('도달거리', `<span class="mono">${esc(robot.reach)}</span>`)}
        ${row('환경', pill('info', '⬡', robot.environment))}
        ${executionRow}
        ${workcellRows}
      </div>

      <div>
        <p class="label-caps">지원 스킬</p>
        <div class="pill-row" style="margin-top:6px">${skills || '<span class="hint">없음</span>'}</div>
      </div>
      ${disabledBlock}

      <div class="notice warning">
        <span class="notice-mark">⚠</span>
        <p>현재 실행 결과는 시뮬레이션입니다. 실제 하드웨어는 검증되지 않았습니다.</p>
      </div>
      ${workcellNotice}
      ${fakeNotice}

      <button class="disclosure" type="button" data-action="toggle-policy"
        aria-expanded="${state.policyOpen ? 'true' : 'false'}">
        <span>정책 및 검증 상태 보기</span>
        <span class="disclosure-mark">▾</span>
      </button>
      ${
        state.policyOpen
          ? `<div class="disclosure-body">
               ${policyItems(state.serverConfig).map((item) =>
                 row(item.label, `<span>${esc(item.value)}</span>`),
               ).join('')}
               ${verification ? '<p class="label-caps">검증 상태</p>' + verification : ''}
             </div>`
          : ''
      }
    </div>`;
}

// ── 작업 셀 장면 영상 ─────────────────────────────────────────────────
/** 작업 셀 화면. **Gazebo 서버가 렌더링한 프레임**이다.
 *
 * 이 카드가 있는 이유: 이 환경의 Gazebo GUI는 소프트웨어 렌더링만 가능하고
 * 부하가 오르면 프레임을 완성하지 못한다. 같은 장면을 서버는 훨씬 적은 CPU로
 * 렌더링하므로 사용자가 셀을 보는 경로를 웹 화면에 둔다.
 *
 * 서버가 프레임을 주지 못하면 **빈 그림을 그리지 않는다** — 이유를 적는다.
 */
export function renderSceneCard(state) {
  if (state.sceneAvailable === false) {
    return `
      ${cardHeader('작업 셀 화면', pill('muted', '◉', '사용 불가'))}
      <div class="card-body">
        <p class="hint"><span class="hint-mark">✕</span>
          ${esc(state.sceneDetail || '장면 카메라를 쓸 수 없습니다.')}</p>
      </div>`;
  }
  if (state.sceneAvailable === null) {
    return `
      ${cardHeader('작업 셀 화면', pill('muted', '◉', '확인 중'))}
      <div class="card-body">
        <p class="hint"><span class="hint-mark">◉</span>장면 카메라를 확인하고 있습니다…</p>
      </div>`;
  }
  // **WebSocket이 프레임을 밀어 보낸다.** canvas에 그리므로 이미지 인코딩이
  // 없다. 이 함수는 canvas 틀만 만들고, 그리기는 main.js의 스트림이 한다.
  const live = state.sceneStream === 'open';
  const statusPill = live
    ? pill('success', '▶', `${state.sceneFps || 0} FPS`)
    : state.sceneStream === 'stalled'
      ? pill('warning', '!', '프레임 끊김')
      : pill('muted', '◉', '연결 중');
  return `
    ${cardHeader('작업 셀 화면', statusPill)}
    <div class="card-body">
      <canvas id="scene-canvas" width="480" height="360"
              style="width:100%;border-radius:8px;border:1px solid var(--border);
                     display:block;background:#0b1218"></canvas>
      <p class="hint"><span class="hint-mark">⬡</span>
        Gazebo 서버가 렌더링한 장면을 WebSocket으로 받습니다.
        GUI 창과 무관합니다.${
          state.sceneStreamDetail ? ` ${esc(state.sceneStreamDetail)}` : ''
        }</p>
    </div>`;
}

// ── 왼쪽: 작업 셀 자원 ────────────────────────────────────────────────
/** 발화에 쓸 수 있는 자원과 **씬의 무엇에 대응하는지**를 보여준다.
 *
 * 목록을 화면에 만들어 두지 않는다. 서버 대조표만 보여주고, 없으면 그 사실을
 * 적는다 — 없는 자원을 있는 것처럼 보이게 하지 않는다.
 */
export function renderWorkcellCard(state) {
  const workcell = state.serverWorkcell;
  const rows = workcellResources(workcell);
  if (!workcell || !workcell.registered) {
    return `
      ${cardHeader('작업 셀 자원', pill('muted', '◉', '미연결'))}
      <div class="card-body">
        <p class="hint"><span class="hint-mark">✕</span>
          작업 셀에 연결되지 않았습니다${
            workcell && workcell.detail ? ` — ${esc(workcell.detail)}` : ''
          }.</p>
      </div>`;
  }
  const group = (kind, title) => {
    const items = rows.filter((r) => r.kind === kind);
    if (!items.length) return '';
    return `<div>
        <p class="label-caps">${esc(title)}</p>
        <div class="rows" style="margin-top:6px">
          ${items
            .map(
              (item) => `<div class="row">
                 <span style="display:flex;align-items:center;gap:6px;min-width:0">
                   <span>${item.kind === 'location' ? '▣' : '▪'}</span>
                   <span class="truncate">${esc(item.korean)}</span>
                 </span>
                 <span class="mono robot-pick-sub truncate" title="${esc(item.id)} · ${esc(
                   item.model,
                 )} · ${esc(item.frame)}">${esc(item.id)}</span>
               </div>
               <p class="hint" style="margin:-4px 0 4px 18px">
                 모델 <span class="mono">${esc(item.model)}</span> ·
                 프레임 <span class="mono">${esc(item.frame)}</span>${
                   item.movePose
                     ? ` · 접근 <span class="mono">${esc(item.movePose)}</span>`
                     : ''
                 }</p>`,
            )
            .join('')}
        </div>
      </div>`;
  };
  return `
    ${cardHeader('작업 셀 자원', pill('info', '⬡', `${rows.length}개`))}
    <div class="card-body">
      ${group('location', '위치')}
      ${group('object', '자재')}
      <p class="hint"><span class="hint-mark">⬡</span>
        좌표는 카탈로그에 두지 않습니다. 자세는
        <span class="mono">config/workcell</span>의 검증된 값에서만 옵니다.</p>
    </div>`;
}

// ── 왼쪽: 음성 입력 ───────────────────────────────────────────────────
export function renderHardwareCard(state) {
  const readiness = state.hardwareReadiness;
  // Gazebo 시연 상태와 실기 준비 상태를 **나란히 두되 합치지 않는다.**
  const sim = state.simulationE2e;
  const simDone = !!(sim && sim.available && sim.completed);
  const simRow = row(
    'Gazebo pick/place 시연',
    simDone
      ? '<span class="chip skill">완료</span>'
      : '<span class="robot-pick-sub">기록 없음</span>',
  );

  if (!readiness || readiness.available === false) {
    return `
      ${cardHeader('실제 하드웨어 준비 상태', pill('muted', '◉', '판정 없음'))}
      <div class="card-body">
        <div class="rows">
          ${simRow}
          ${row('실제 pick/place 준비', '<span class="chip skill off">차단</span>')}
        </div>
        <p class="hint"><span class="hint-mark">✕</span>
          실기 준비 판정을 읽지 못했습니다${
            readiness && readiness.detail ? ` — ${esc(readiness.detail)}` : ''
          }.</p>
      </div>`;
  }

  const ready = readiness.real_hardware_ready === true;
  // 고정 상태 다섯 줄. 서버가 한 곳에서 만든 문장을 그대로 쓴다 —
  // 화면에서 숫자를 다시 세지 않는다(차단 검사 수와 섞이지 않게).
  const stateLines = readiness.state_lines || [];
  const inputs = readiness.inputs || {};
  const rows = inputs.inputs || [];
  const readyCount = (inputs.ready || []).length;
  const checklist = readiness.checklist || {};
  const guide = readiness.collection_guide || [];
  const missing = readiness.missing_labels || [];

  const pendingInputs = rows.filter((item) => !item.ready);
  const inputRows = pendingInputs
    .map(
      (item) => `<div class="row">
         <span class="truncate">${esc(item.label)}</span>
         <span class="mono robot-pick-sub truncate">${esc(
           item.measurement_method,
         )}${item.has_value ? ' · 값 있음' : ' · 값 없음'}</span>
       </div>
       <p class="hint" style="margin:-4px 0 4px 0">${esc(item.needed || '')}</p>`,
    )
    .join('');

  const guideRows = guide
    .slice(0, 6)
    .map(
      (item) => `<div class="row">
         <span class="truncate">${esc(item.label)}</span>
         <span class="mono robot-pick-sub">${esc(item.reason_code)}</span>
       </div>`,
    )
    .join('');

  return `
    ${cardHeader(
      '실제 하드웨어 준비 상태',
      ready
        ? pill('success', '✓', '근거 확보')
        : pill('danger', '✕', '차단'),
    )}
    <div class="card-body">
      <div class="notice danger">
        <span class="notice-mark">✕</span>
        <p><strong>실제 하드웨어는 아직 연결되지 않았습니다.</strong><br />
          이 화면은 읽기 전용이며, Gazebo 시연 결과는 실기 지원으로
          승격되지 않습니다.</p>
      </div>
      ${
        stateLines.length
          ? `<div>
               <p class="label-caps">지금 상태 (고정)</p>
               <pre class="mono" style="margin:6px 0 0;white-space:pre-wrap">${esc(
                 stateLines.join('\n'),
               )}</pre>
             </div>`
          : ''
      }
      <div class="rows">
        ${simRow}
        ${row('실제 pick/place 준비', ready
          ? '<span class="chip skill">근거 확보</span>'
          : '<span class="chip skill off">차단</span>')}
        ${row('실기 연결', readiness.real_hardware_connected
          ? '<span class="chip skill">연결됨</span>'
          : '<span class="chip skill off">미연결</span>')}
        ${row('실기 검증', readiness.real_hardware_verified
          ? '<span class="chip skill">완료</span>'
          : '<span class="chip skill off">미검증</span>')}
        ${row('근거 수집(입력)', `<span class="mono">${esc(String(readyCount))}/${esc(
          String(rows.length),
        )}</span> 확보`)}
        ${row('근거 수집(체크리스트)', `<span class="mono">${esc(
          String(checklist.blocking_done || 0),
        )}/${esc(String(checklist.blocking_total || 0))}</span> 통과`)}
        ${row('실기 어댑터 설정', readiness.adapter_configured
          ? '<span class="chip skill">선언됨</span>'
          : '<span class="chip skill off">없음</span>')}
      </div>
      ${
        missing.length
          ? `<div>
               <p class="label-caps">부족한 입력 (${esc(String(missing.length))})</p>
               <div class="rows" style="margin-top:6px">${guideRows}</div>
               ${
                 missing.length > 6
                   ? `<p class="hint"><span class="hint-mark">›</span>그 밖 ${esc(
                       String(missing.length - 6),
                     )}건은 보고서에서 확인합니다.</p>`
                   : ''
               }
             </div>`
          : ''
      }
      ${
        inputRows
          ? `<div>
               <p class="label-caps">무엇을 가져와야 하는가</p>
               <div class="rows" style="margin-top:6px">${inputRows}</div>
             </div>`
          : ''
      }
      <p class="hint"><span class="hint-mark">⬡</span>
        판정 관문 <span class="mono">validation/hardware_readiness.py</span> ·
        입력 설정 <span class="mono">config/hardware/</span></p>
    </div>`;
}

export function renderVoiceCard(state) {
  const { recording, partial, final, confidence, available, detail } = state.stt;
  const statusText = available ? 'STT 연결됨' : 'STT 미사용';
  const confidenceClass =
    confidence == null
      ? ''
      : confidence >= 0.9
        ? 'success'
        : confidence >= 0.7
          ? 'warning'
          : 'danger';
  const confidenceLabel =
    confidence == null ? '' : confidence >= 0.9 ? '높음' : confidence >= 0.7 ? '보통' : '낮음';

  return `
    ${cardHeader(
      '음성 입력',
      `<div class="conn"><span class="dot sm ${available ? 'success' : ''}"></span>`
        + `<span class="robot-pick-sub">${esc(statusText)}</span></div>`,
    )}
    <div class="card-body">
      <div class="row">
        <button class="btn sm ${recording ? 'danger' : 'primary'}" type="button"
          data-action="toggle-voice" ${available ? '' : 'disabled'}>
          ${recording ? '● 녹음 중지' : '◎ 녹음 시작'}
        </button>
        <span class="row-value" style="color:${recording ? 'var(--danger)' : 'var(--muted)'}">
          ${recording ? '⏺ 녹음 중...' : available ? '마이크 준비' : esc(detail || '서버 STT 없음')}
        </span>
      </div>
      <div class="transcript">
        ${
          partial
            ? `<div><span class="label-caps">PARTIAL</span>`
              + `<p class="transcript-partial">${esc(partial)}</p></div>`
            : ''
        }
        ${
          final
            ? `<div><span class="label-caps" style="color:var(--success)">FINAL</span>`
              + `<p class="transcript-final">${esc(final)}</p></div>`
            : ''
        }
        ${!partial && !final ? '<p class="transcript-empty">전사 결과가 여기에 표시됩니다.</p>' : ''}
      </div>
      ${
        confidence == null
          ? ''
          : `<div class="row"><span class="row-label">신뢰도</span>
               <span class="row-value mono" style="color:var(--${confidenceClass})">
                 ${confidence.toFixed(2)} · ${esc(confidenceLabel)}</span></div>`
      }
    </div>`;
}

// ── 왼쪽: 이벤트 ──────────────────────────────────────────────────────
export function renderEventsCard(state) {
  const filters = ['all', 'info', 'warning', 'error', 'execution']
    .map((key) => {
      const label = key === 'all' ? '전체' : EVENT_LEVELS[key].label;
      const on = state.eventFilter === key;
      return `<button class="event-filter" type="button" data-action="event-filter"
        data-filter="${key}" aria-pressed="${on}">${esc(label)}</button>`;
    })
    .join('');
  const visible =
    state.eventFilter === 'all'
      ? state.events
      : state.events.filter((event) => event.level === state.eventFilter);
  const items = visible
    .map(
      (event) => `<div class="event">
        <span class="event-time">${esc(event.time)}</span>
        <span class="dot sm event-dot ${EVENT_LEVELS[event.level].dot}"></span>
        <span class="event-msg">${esc(event.message)}</span>
      </div>`,
    )
    .join('');

  return `
    ${cardHeader('이벤트', `<span class="robot-pick-sub mono">${state.events.length}</span>`)}
    <div class="event-filters">${filters}</div>
    <div class="event-list" id="event-list">${items}</div>`;
}

// ── 가운데: 작업 명령 ─────────────────────────────────────────────────
export function renderCommandCard(state, backendKind) {
  const disabled = !state.command.trim() || state.planLoading || isActive(state);
  const simHint =
    backendKind === 'simulation'
      ? `<p class="hint"><span class="hint-mark">⬡</span>
           시뮬레이션 모드 예시 — PASS: "1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘" ·
           BLOCK: "1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘" ·
           ASK: "그거 저기로 옮겨줘"</p>`
      : '';
  return `
    ${cardHeader('작업 명령')}
    <div class="card-body">
      <textarea class="textarea" id="command-input" rows="3" data-action="command-input"
        placeholder="작업 명령을 입력하거나 음성으로 입력하세요.">${esc(state.command)}</textarea>
      <div class="btn-row">
        <button class="btn primary" type="button" data-action="generate" ${disabled ? 'disabled' : ''}>
          ${state.planLoading ? '<span class="spinner"></span>' : '◈'} 계획 생성
        </button>
        <button class="btn ghost" type="button" data-action="clear-command">지우기</button>
      </div>
      <p class="hint"><span class="hint-mark">ℹ</span>
        계획 생성은 로봇을 실행하지 않습니다. 안전 판단이 PASS일 때
        <strong>실행 시작</strong>을 눌러야 실행됩니다.</p>
      ${simHint}
    </div>`;
}

// ── 가운데: 생성된 작업 계획 ──────────────────────────────────────────
/** 발화 자원 → 씬 id → 프레임 대조. 대조표에 없으면 **모른다고 적는다.** */
function renderResourceMapping(state) {
  const rows = planResourceMapping(state.plan, state.serverWorkcell);
  if (!rows.length) return '';
  return `
    <div>
      <p class="label-caps">자원 대조 (발화 → 씬 → 프레임)</p>
      <div class="rows" style="margin-top:6px">
        ${rows
          .map((item) =>
            item.known
              ? `<div class="row">
                   <span class="truncate">${esc(item.label)}</span>
                   <span class="mono robot-pick-sub truncate">${esc(item.resource)}
                     → ${esc(item.model)} → ${esc(item.frame)}${
                       item.pose ? ` → ${esc(item.pose)}` : ''
                     }</span>
                 </div>`
              : `<div class="row">
                   <span class="truncate">${esc(item.label)}</span>
                   <span class="robot-pick-sub">씬 대조표에 없습니다
                     (<span class="mono">${esc(item.resource)}</span>)</span>
                 </div>`,
          )
          .join('')}
      </div>
    </div>`;
}

export function renderPlanCard(state) {
  if (!state.plan || !state.validation) {
    return `${cardHeader('생성된 작업 계획')}
      <div class="card-empty">계획이 아직 생성되지 않았습니다.</div>`;
  }
  const { plan, validation } = state;
  const verdictPill =
    validation.verdict === VERDICT.PASS
      ? pill('success', '✓', 'PASS')
      : validation.verdict === VERDICT.BLOCK
        ? pill('danger', '✕', 'BLOCK')
        : pill('warning', '?', 'ASK');

  const steps = plan.steps
    .map(
      (step) => `<div class="step">
        <span class="step-no">${step.no}</span>
        <span class="step-skill">${esc(step.skill)}</span>
        <span class="step-desc">${esc(step.description)}</span>
      </div>`,
    )
    .join('');

  const ruleChips = (validation.rules || [])
    .map((rule) => {
      const cls =
        rule.status === 'block' ? 'chip reason' : rule.status === 'pass' ? 'chip' : 'chip ask';
      return `<span class="${cls}" title="${esc(rule.message || '')}">${esc(rule.code)}</span>`;
    })
    .join('');

  const summaryPills = [
    (validation.rules || []).every((r) => r.status !== 'block')
      ? pill('success', '✓', '형식·안전 규칙 통과')
      : pill('danger', '✕', '안전 규칙 차단'),
    (validation.rules || []).some((r) => r.status === 'insufficient_data')
      ? pill('warning', '!', '확인 필요 항목 있음')
      : pill('success', '✓', '요청-계획 자원 일치'),
  ].join('');

  const resourceRows = (plan.resources || [])
    .map(
      (entry) => `<div class="resource-row">
        <span class="resource-utterance${entry.match ? '' : ' bad'}">${esc(entry.utterance)}</span>
        <div class="resource-plan"><span>${esc(entry.plan)}</span>
          ${entry.match ? '<span class="resource-ok">✓</span>' : '<span class="resource-bad">✕</span>'}
        </div>
      </div>`,
    )
    .join('');

  const blockNotice =
    validation.verdict === VERDICT.BLOCK
      ? `<div class="notice danger" style="margin-top:8px"><span class="notice-mark">✕</span>
           <p>${esc(validation.detail || '실행할 수 없는 계획입니다.')}</p></div>`
      : '';

  return `
    ${cardHeader(
      '생성된 작업 계획',
      `<div class="pill-row">${verdictPill}`
        + `<span class="robot-pick-sub mono">${esc(plan.planId || '')}</span></div>`,
    )}
    <div class="card-body">
      <div class="meta-grid">
        ${meta('요청 ID', plan.requestId || '—')}
        ${meta('생성 시각', clockText(plan.createdAt))}
        ${meta('Plan hash', plan.planHash || '—', true)}
        ${meta('유효 시간', expiryText(plan))}
      </div>
      <div>
        <p class="label-caps">작업 단계</p>
        <div class="step-list" style="margin-top:8px">${steps}</div>
      </div>
      ${renderResourceMapping(state)}
      <div>
        <p class="label-caps">안전 검증</p>
        <div class="pill-row" style="margin:8px 0">${summaryPills}</div>
        <div class="pill-row">${ruleChips}</div>
      </div>
      <div>
        <p class="label-caps">요청과 계획의 자원 대조</p>
        <div class="resource-table" style="margin-top:8px">
          <div class="resource-head"><span>작업자 발화</span><span>계획 자원</span></div>
          ${resourceRows || '<div class="resource-row"><span class="resource-utterance">자원을 쓰지 않는 계획</span><div class="resource-plan"><span>—</span></div></div>'}
        </div>
        ${blockNotice}
      </div>
    </div>`;
}

// ── 오른쪽: 안전 판단 및 실행 ────────────────────────────────────────
/** 차단된 요청 카드. **무엇을 하려 했는지 + 왜 막혔는지**를 함께 보여준다.
 *
 * 발화 자원 → 계획 초안 → Reason Code → 부족한 입력 순서다. 화면에 목록을
 * 만들어 두지 않는다 — 서버가 준 구조만 보여준다.
 */
export function renderBlockCard(state) {
  const block = state.planBlock;
  if (!block) return '';
  const info = block.blocked || null;
  const matches = (block.slots && block.slots.matches) || [];
  const steps = block.draftSteps || [];

  const resourceRows = matches.length
    ? matches
        .map(
          (m) => `<div class="row">
             <span class="truncate">${esc(resourceLabel(m.resource_id))}</span>
             <span class="mono robot-pick-sub truncate">${esc(m.resource_id)}
               (${esc(m.kind || '')})</span>
           </div>`,
        )
        .join('')
    : '<p class="hint"><span class="hint-mark">✕</span>발화에서 확인된 자원이 없습니다.</p>';

  const stepRows = steps.length
    ? steps
        .map(
          (s) => `<div class="row">
             <span><span class="mono">${esc(String(s.no))}</span>
               ${esc(describeStep(s.skill, s.args || {}))}</span>
             <span class="mono robot-pick-sub truncate">${esc(s.skill)}
               ${esc(JSON.stringify(s.args || {}))}</span>
           </div>`,
        )
        .join('')
    : '<p class="hint"><span class="hint-mark">◉</span>계획 초안이 없습니다.</p>';

  const unmet = (info && info.unmet) || [];
  const check = block.planValidation || null;
  return `
    ${cardHeader(
      '차단된 요청',
      pill('danger', '✕', block.reasonCode || 'BLOCK'),
    )}
    <div class="card-body">
      ${
        block.utterance
          ? `<p class="hint"><span class="hint-mark">›</span>요청: “${esc(
              block.utterance,
            )}”</p>`
          : ''
      }
      <div class="notice danger">
        <span class="notice-mark">✕</span>
        <p><strong>${esc((info && info.message) || block.detail || '실행할 수 없습니다.')}</strong>
        ${
          block.clarification
            ? `<br />${esc(block.clarification)}`
            : ''
        }</p>
      </div>
      <div class="rows">
        ${row('Reason Code', `<span class="mono">${esc(block.reasonCode || '—')}</span>`)}
        ${
          info && info.skills && info.skills.length
            ? row('막힌 스킬', info.skills
                .map((s) => `<span class="chip skill off">${esc(s)}</span>`)
                .join(' '))
            : ''
        }
        ${
          info && info.gate
            ? row('판정 관문', `<span class="mono">${esc(info.gate)}</span>`)
            : ''
        }
      </div>
      <div>
        <p class="label-caps">발화 자원</p>
        <div class="rows" style="margin-top:6px">${resourceRows}</div>
      </div>
      <div>
        <p class="label-caps">계획 초안 (실행하지 않았습니다)</p>
        <div class="rows" style="margin-top:6px">${stepRows}</div>
      </div>
      ${
        info && info.met && info.met.length
          ? `<div>
               <p class="label-caps">확인된 항목</p>
               <div class="rows" style="margin-top:6px">
                 ${info.met
                   .map(
                     (item) => `<div class="row">
                        <span class="truncate">${esc(item)}</span>
                        <span class="chip skill">확인</span>
                      </div>`,
                   )
                   .join('')}
               </div>
             </div>`
          : ''
      }
      ${renderPlanValidation(check)}
      ${
        unmet.length
          ? `<div>
               <p class="label-caps">부족한 입력</p>
               <div class="rows" style="margin-top:6px">
                 ${unmet
                   .map(
                     (item) => `<div class="row">
                        <span class="truncate">${esc(item)}</span>
                        <span class="robot-pick-sub">미확보</span>
                      </div>`,
                   )
                   .join('')}
               </div>
             </div>`
          : ''
      }
    </div>`;
}

function renderPlanValidation(check) {
  if (!check) return '';
  if (check.available === false) {
    return `<div class="notice">
        <span class="notice-mark">◉</span>
        <p>계획 사전 검증을 돌리지 못했습니다: ${esc(check.detail || '')}
        <br /><span class="mono">${esc(check.reason_code || '')}</span></p>
      </div>`;
  }
  const stages = check.stages || [];
  const checks = check.checks || [];
  const rows = check.resource_rows || [];
  const findings = check.findings || [];
  const byStage = {};
  checks.forEach((item) => {
    byStage[item.stage] = item;
  });

  const stageRows = stages
    .map((stage) => {
      const result = byStage[stage.stage] || null;
      const mark = !result
        ? '<span class="robot-pick-sub">미검사</span>'
        : result.passed
          ? '<span class="chip skill">통과</span>'
          : `<span class="chip skill off">${esc(result.collisions && result.collisions.length ? '충돌' : '미통과')}</span>`;
      const held = stage.holds_object ? ` · 적재 ${esc(stage.holds_object)}` : '';
      return `<div class="row">
          <span><span class="mono">${esc(String(stage.no))}</span>
            ${esc(stage.label)}${held}</span>
          <span class="robot-pick-sub truncate">${esc(stage.pose_name || '')}
            ${mark}</span>
        </div>`;
    })
    .join('');

  const resourceRows = rows
    .map(
      (r) => `<div class="row">
         <span class="truncate">${esc(r.korean || r.resource_id)}
           <span class="robot-pick-sub">${esc(r.role)}</span></span>
         <span class="mono robot-pick-sub truncate">
           ${esc(r.utterance_surface || '발화에 없음')} →
           ${esc(r.resource_id)} → ${esc(r.scene_model || '모델 없음')} →
           ${esc(r.frame || '프레임 없음')}
           ${r.matched ? '<span class="chip skill">일치</span>' : '<span class="chip skill off">불일치</span>'}
         </span>
       </div>`,
    )
    .join('');

  const findingRows = findings
    .map(
      (f) => `<div class="row">
         <span class="truncate">${esc(f.detail)}</span>
         <span class="mono robot-pick-sub">${esc(f.reason_code)}</span>
       </div>`,
    )
    .join('');

  const passed = check.checks_passed || 0;
  const total = check.stage_count || 0;
  const snapshot = check.snapshot || {};
  return `
    <div>
      <p class="label-caps">계획 사전 검증 (실행 허가가 아닙니다)</p>
      <div class="notice ${check.plan_verified ? 'success' : 'danger'}"
           style="margin-top:6px">
        <span class="notice-mark">${check.plan_verified ? '✓' : '✕'}</span>
        <p><strong>자원·도달·관절 제한·충돌 검사 ${esc(String(passed))}/${esc(String(total))}
          단계 ${check.plan_verified ? '통과' : '미통과'}</strong><br />
          이 결과는 <strong>실행 가능을 뜻하지 않습니다.</strong>
          pick/place 실행은 아래 부족한 입력 때문에 계속 차단됩니다.</p>
      </div>
      <div class="rows" style="margin-top:6px">
        ${row('계획 검증', check.plan_verified
          ? '<span class="chip skill">통과</span>'
          : '<span class="chip skill off">미통과</span>')}
        ${row('실행 가능', '<span class="chip skill off">아니오</span>')}
        ${row('planning scene', `<span class="mono truncate">${esc(
          (snapshot.content_hash || '').slice(0, 12) || '없음')}</span>
          ${check.scene_stable === true
            ? '<span class="chip skill">검사 중 변경 없음</span>'
            : '<span class="chip skill off">검사 중 변경됨</span>'}`)}
        ${row('파지 관측', `<span class="mono">${esc(
          (check.grasp_observation || {}).availability || 'unavailable')}</span>`)}
      </div>
      <p class="label-caps" style="margin-top:10px">발화 ↔ 계획 ↔ 모델 ↔ 프레임</p>
      <div class="rows" style="margin-top:6px">${resourceRows}</div>
      <p class="label-caps" style="margin-top:10px">계획 단계</p>
      <div class="rows" style="margin-top:6px">${stageRows}</div>
      ${
        findingRows
          ? `<p class="label-caps" style="margin-top:10px">검증에서 걸린 항목</p>
             <div class="rows" style="margin-top:6px">${findingRows}</div>`
          : ''
      }
    </div>`;
}

export function renderVerdictCard(state) {
  const { validation } = state;
  const verdict = validation ? validation.verdict : null;
  const headerPill =
    verdict === VERDICT.PASS
      ? pill('success', '✓', 'PASS')
      : verdict === VERDICT.BLOCK
        ? pill('danger', '✕', 'BLOCK')
        : verdict === VERDICT.ASK
          ? pill('warning', '?', 'ASK')
          : pill('muted', '◉', '판단 없음');

  let body;
  if (!validation) {
    body = `<div class="notice">
        <span class="notice-mark">🛡</span>
        <p>계획을 생성하면 안전 판단(PASS / BLOCK / ASK)이 여기에 표시됩니다.</p>
      </div>`;
  } else if (verdict === VERDICT.PASS) {
    body = `<div class="notice success">
        <span class="notice-mark">✓</span>
        <p><strong>PASS</strong> — ${esc(validation.detail || '안전 판단을 통과했습니다.')}<br />
        실행 시작을 누르면 실행됩니다. 자동으로 실행되지 않습니다.</p>
      </div>`;
  } else if (verdict === VERDICT.BLOCK) {
    const reasons = (validation.reasonCodes || [])
      .map((code) => `<span class="chip reason">${esc(code)}</span>`)
      .join('');
    const items = (validation.blocked || [])
      .map((line) => `<li>${esc(line)}</li>`)
      .join('');
    body = `<div class="notice danger">
        <span class="notice-mark">✕</span>
        <p><strong>BLOCK</strong> — 실행이 차단되었습니다.<br />${esc(validation.detail || '')}</p>
      </div>
      ${items ? `<ul style="margin:0;padding-left:18px;font-size:12px;color:var(--muted)">${items}</ul>` : ''}
      <div>
        <p class="label-caps">Reason Code</p>
        <div class="pill-row" style="margin-top:6px">${reasons || '<span class="hint">—</span>'}</div>
      </div>`;
  } else {
    const missing = (validation.missing || []).map((line) => `<li>${esc(line)}</li>`).join('');
    const fixes = (validation.fixes || []).map((line) => `<li>${esc(line)}</li>`).join('');
    const reasons = (validation.reasonCodes || [])
      .map((code) => `<span class="chip ask">${esc(code)}</span>`)
      .join('');
    body = `<div class="notice warning">
        <span class="notice-mark">?</span>
        <p><strong>ASK</strong> — 정보가 부족해 실행할 수 없습니다.<br />
        ${esc(validation.clarification || validation.detail || '')}</p>
      </div>
      <div>
        <p class="label-caps">부족한 정보</p>
        <ul style="margin:6px 0 0;padding-left:18px;font-size:12px;color:var(--muted)">
          ${missing || '<li>서버가 항목을 특정하지 않았습니다.</li>'}
        </ul>
      </div>
      <div>
        <p class="label-caps">수정 방법</p>
        <ul style="margin:6px 0 0;padding-left:18px;font-size:12px;color:var(--muted)">
          ${fixes || '<li>요청을 더 구체적으로 다시 입력합니다.</li>'}
        </ul>
      </div>
      <div>
        <p class="label-caps">Reason Code</p>
        <div class="pill-row" style="margin-top:6px">${reasons || '<span class="hint">—</span>'}</div>
      </div>`;
  }

  const latchNotice = state.stopLatched
    ? `<div class="notice danger"><span class="notice-mark">■</span>
         <p>전체 정지가 걸려 있습니다. 새 계획을 생성하면 정지 상태가 풀립니다.</p></div>`
    : '';

  const executeDisabled = canExecute(state) ? '' : 'disabled';
  const reason = !state.plan
    ? '계획이 없습니다.'
    : verdict !== VERDICT.PASS
      ? 'PASS 상태에서만 실행할 수 있습니다.'
      : state.stopLatched
        ? '전체 정지 래치가 걸려 있습니다.'
        : state.execution.status !== EXECUTION.IDLE
          ? '이미 실행이 시작되었습니다.'
          : '';

  return `
    ${cardHeader('안전 판단 및 실행', headerPill)}
    <div class="card-body">
      ${body}
      ${latchNotice}
      <button class="btn primary block" type="button" data-action="execute" ${executeDisabled}>
        ▶ 실행 시작
      </button>
      ${reason ? `<p class="hint"><span class="hint-mark">ℹ</span>${esc(reason)}</p>` : ''}
    </div>`;
}

// ── 워크스페이스: 시뮬레이션 ──────────────────────────────────────────
export function renderSimulationCard(state) {
  const nodes = simNodesFromPlan(state.plan);
  const currentStep = state.execution.currentStep;
  const active = activeNodeIndex(nodes, currentStep);
  const running = isActive(state);
  // 전체 정지는 시스템 수준 사건이라 실행 상태와 별개로 표시한다.
  const globalStopped = Boolean(state.stopRecord)
    || state.execution.status === EXECUTION.GLOBAL_STOPPED;
  const cancelledOnly = state.execution.status === EXECUTION.CANCELLED && !globalStopped;
  const stopped = globalStopped || cancelledOnly || isStopped(state);
  const done = state.execution.results.filter((r) => r.status === 'done');
  const hasMaterial =
    done.some((r) => r.skill === 'pick') && !done.some((r) => r.skill === 'place');

  const gridLines = [
    ...[1, 2, 3, 4, 5, 6, 7].map(
      (i) => `<line x1="${i * 50}" y1="0" x2="${i * 50}" y2="200" stroke="#1E2C38" stroke-width="0.5" />`,
    ),
    ...[1, 2, 3].map(
      (i) => `<line x1="0" y1="${i * 50}" x2="400" y2="${i * 50}" stroke="#1E2C38" stroke-width="0.5" />`,
    ),
  ].join('');

  const paths = nodes
    .slice(0, -1)
    .map((node, index) => {
      const from = node;
      const to = nodes[index + 1];
      const fx = from.x * 4;
      const fy = from.y * 2;
      const tx = to.x * 4;
      const ty = to.y * 2;
      const on = running && active >= index && active <= index + 1;
      return `<line x1="${fx}" y1="${fy}" x2="${tx}" y2="${ty}"
        stroke="${on ? '#60A5FA' : '#1E2C38'}" stroke-width="${on ? 2 : 1}"
        stroke-dasharray="${on ? '4 2' : '2 2'}" />`;
    })
    .join('');

  const nodeShapes = nodes
    .map((node, index) => {
      const cx = node.x * 4;
      const cy = node.y * 2;
      const isActive_ = index === active;
      const isPast = index < active;
      const fill = isActive_ ? '#162030' : isPast ? '#0d2e22' : '#101922';
      const stroke = isActive_ ? '#60A5FA' : isPast ? '#6EE7B7' : '#1E2C38';
      const glyph = isActive_ && running ? '◉' : isPast ? '✓' : '○';
      const color = isActive_ ? '#60A5FA' : isPast ? '#6EE7B7' : '#8BA1B4';
      return `<g>
        <circle cx="${cx}" cy="${cy}" r="${isActive_ ? 16 : 12}" fill="${fill}"
          stroke="${stroke}" stroke-width="${isActive_ ? 2 : 1}" />
        ${
          isActive_
            ? `<circle cx="${cx}" cy="${cy}" r="20" fill="none" stroke="#60A5FA"
                 stroke-width="1" opacity="0.3">
                 <animate attributeName="r" values="16;24;16" dur="2s" repeatCount="indefinite" />
                 <animate attributeName="opacity" values="0.4;0;0.4" dur="2s" repeatCount="indefinite" />
               </circle>`
            : ''
        }
        <text x="${cx}" y="${cy + 1}" text-anchor="middle" dominant-baseline="middle"
          font-size="9" fill="${color}" font-family="monospace">${glyph}</text>
        <text x="${cx}" y="${cy + 22}" text-anchor="middle" font-size="7"
          fill="${isActive_ ? '#E6EDF3' : '#8BA1B4'}" font-family="monospace">${esc(node.label)}</text>
        <text x="${cx}" y="${cy + 31}" text-anchor="middle" font-size="6"
          fill="#4a6070" font-family="monospace">${esc(node.sub)}</text>
      </g>`;
    })
    .join('');

  const stepList = state.plan
    ? state.plan.steps
        .map((step) => {
          const isDone = state.execution.results.some(
            (r) => r.no === step.no && r.status === 'done',
          );
          const isCurrent = step.no === currentStep && !isDone;
          const cls = isCurrent ? 'current' : isDone ? 'done' : '';
          return `<div class="progress-step ${cls}">
            <span class="progress-mark">${isDone ? '✓' : step.no}</span>
            <span class="progress-skill">${esc(step.skill)}</span>
            <span class="progress-desc">${esc(step.description)}</span>
            ${isCurrent && running ? '<span class="progress-live"><span class="dot info pulse"></span></span>' : ''}
          </div>`;
        })
        .join('')
    : '';

  const overlay = stopped
    ? `<div class="sim-overlay ${cancelledOnly ? 'orange' : ''}">
         <div class="sim-overlay-box"><span>■</span>
           <span>${cancelledOnly ? '실행 취소됨' : '전체 정지됨'}</span>
         </div>
       </div>`
    : '';

  return `
    ${cardHeader(
      '시뮬레이션',
      `<div class="pill-row">${pill('info', '⬡', 'Simulator / Gazebo')}`
        + `${hasMaterial ? pill('warning', '📦', '자재 보유 중') : ''}</div>`,
    )}
    <div class="card-body">
      <div class="sim-canvas">
        <svg viewBox="0 0 400 200" preserveAspectRatio="xMidYMid meet">
          ${gridLines}${paths}${nodeShapes}
        </svg>
        ${overlay}
      </div>
      <div>
        <div class="row">
          <p class="label-caps">현재 단계</p>
          ${
            currentStep && state.plan
              ? `<span class="robot-pick-sub mono">${currentStep} / ${state.plan.steps.length}</span>`
              : ''
          }
        </div>
        ${
          currentStep
            ? `<div class="step-list" style="margin-top:8px">${stepList}</div>`
            : '<p class="hint" style="justify-content:center;padding:16px 0">실행 시작 후 단계가 표시됩니다.</p>'
        }
      </div>
    </div>`;
}

// ── 워크스페이스: 실행 상태 ───────────────────────────────────────────
const STATUS_VIEW = {
  [EXECUTION.IDLE]: { color: 'muted', icon: '◉', label: '대기', border: '' },
  [EXECUTION.RUNNING]: { color: 'success', icon: '▶', label: 'RUNNING', border: 'success' },
  [EXECUTION.CANCELLING]: { color: 'orange', icon: '⏹', label: 'CANCELLING', border: 'orange' },
  [EXECUTION.CANCELLED]: { color: 'orange', icon: '■', label: '실행 취소됨', border: 'orange' },
  [EXECUTION.STOPPING]: { color: 'danger', icon: '⏹', label: 'STOPPING', border: 'red' },
  [EXECUTION.GLOBAL_STOPPED]: { color: 'danger', icon: '■', label: '전체 정지', border: 'red' },
  [EXECUTION.COMPLETED]: { color: 'info', icon: '✓', label: '완료', border: 'info' },
  [EXECUTION.FAILED]: { color: 'danger', icon: '✕', label: '실패', border: 'red' },
  [EXECUTION.UNVERIFIED]: {
    label: '검증 불가', color: 'warning', icon: '?', border: 'warn',
  },
  [EXECUTION.STOP_CONFIRMED]: {
    label: '정지 확인', color: 'danger', icon: '■', border: 'danger',
  },
};

function renderTimeline(state) {
  const items = [];
  const verdict = state.validation ? state.validation.verdict : null;
  items.push({ label: `안전 판단 ${verdict || '—'}`, done: verdict === VERDICT.PASS });
  items.push({
    label: '실행 허가 확인됨',
    done: state.execution.status !== EXECUTION.IDLE,
  });
  items.push({
    label: '실행 시작됨',
    done: state.execution.status !== EXECUTION.IDLE,
  });
  (state.plan ? state.plan.steps : []).forEach((step) => {
    const done = state.execution.results.some((r) => r.no === step.no && r.status === 'done');
    const active = step.no === state.execution.currentStep && isActive(state) && !done;
    items.push({
      label: done
        ? `${step.no}단계 완료: ${step.skill}`
        : active
          ? `${step.no}단계 실행 중: ${step.skill}`
          : `${step.no}단계 대기`,
      done,
      active,
    });
  });
  if (state.execution.status === EXECUTION.CANCELLED) {
    items.push({ label: '실행 취소 확인됨 (scope: execution)', done: true });
  }
  if (state.execution.status === EXECUTION.GLOBAL_STOPPED) {
    items.push({ label: '전체 정지 (scope: global)', done: true });
  }
  return items
    .map(
      (item) => `<div class="timeline-item ${item.active ? 'active' : item.done ? 'done' : ''}">
        <span class="timeline-mark"></span><span>${esc(item.label)}</span>
      </div>`,
    )
    .join('');
}

function renderCancelRecord(record) {
  if (!record) return '';
  const confirmed = record.result === 'confirmed';
  return `<div class="notice orange" style="flex-direction:column;gap:8px">
      <div class="pill-row" style="align-items:center">
        <span style="font-weight:700">■ 실행 취소됨</span>
        ${confirmed ? pill('success', '✓', '취소 확인됨') : pill('danger', '!', '취소 미확인')}
      </div>
      <div class="meta-grid" style="width:100%">
        ${meta('범위', 'execution (이 실행만)')}
        ${meta('실행 ID', record.executionId || '—', true)}
        ${meta('취소 요청 시각', record.requestTime || '—')}
        ${meta('취소 확인 시각', record.confirmedTime || '—')}
        ${meta('취소 시점 단계', record.stoppedAtStep ? `${record.stoppedAtStep}단계` : '—')}
        ${meta('Reason Code', record.reasonCode || 'exec.canceled', true)}
      </div>
      ${
        confirmed
          ? '<p>다른 실행과 로봇 전체에는 영향을 주지 않았습니다. 다음 실행은 막히지 않습니다.</p>'
          : '<p>취소 요청은 전송했지만 실제 정지를 확인하지 못했습니다.</p>'
      }
    </div>`;
}

/** 실행 중 관측값. **어댑터 근거에 있는 것만 보여준다.**
 *
 * 값이 없으면 "관측 없음"으로 적는다 — 빈 칸을 그럴듯한 값으로 채우지 않는다.
 */
function renderObservation(state) {
  const results = state.execution.results || [];
  const latest = [...results].reverse().find((r) => r.evidence);
  const evidence = latest ? latest.evidence : null;
  if (!evidence) {
    return `<div>
        <p class="label-caps">실행 관측</p>
        <p class="hint" style="margin-top:6px"><span class="hint-mark">◉</span>
          아직 관측값이 없습니다.</p>
      </div>`;
  }
  const scene = evidence.planning_scene || null;
  const gripper = evidence.gripper || null;
  const controllers = evidence.controllers || null;
  const joints = evidence.observed_joint_rad || null;
  const stop = state.stopRecord || null;

  const controllerText = controllers
    ? Object.entries(controllers)
        .map(([name, value]) => `${name}=${value}`)
        .join(' · ')
    : '관측 없음';
  const sceneText = scene
    ? scene.valid
      ? `충돌 없음 · snapshot ${scene.snapshot_id || '—'}`
        + (scene.content_hash ? ` (${scene.content_hash})` : '')
      : `충돌 ${(scene.contacts || []).length}쌍 — ${(scene.contacts || [])
          .slice(0, 2)
          .map((pair) => pair.join(' ↔ '))
          .join(', ')}`
    : '검사 기록 없음';
  const apertureText = gripper
    ? gripper.observed
      ? gripper.aperture_m === null || gripper.aperture_m === undefined
        ? `관절 ${gripper.joint_rad} rad · 개구 ${esc(
            gripper.aperture_detail || '주장하지 않음',
          )}`
        : `${(gripper.aperture_m * 1000).toFixed(1)} mm (관절 ${gripper.joint_rad} rad)`
      : `관측 없음 — ${esc(gripper.detail || '')}`
    : '관측 없음';
  const jointText = joints
    ? Object.entries(joints)
        .map(([name, value]) => `${name} ${Number(value).toFixed(4)}`)
        .join(' · ')
    : '관측 없음';
  const stopText = stop
    ? stop.confirmed
      ? `정지 확인 (팔 ${stop.armPeak ?? '—'} · 그리퍼 ${stop.gripperPeak ?? '—'} rad/s)`
      : `정지 미확인 — ${esc(stop.detail || stop.reasonCode || '')}`
    : '정지 요청 없음';

  return `
    <div>
      <p class="label-caps">실행 관측</p>
      <div class="rows" style="margin-top:6px">
        ${row('목표 위치', `<span class="mono">${esc(
          evidence.pose || (latest.args && (latest.args.to || latest.args.from)) || '—',
        )}</span>`)}
        ${row('planning scene', `<span>${sceneText}</span>`)}
        ${row('컨트롤러', `<span class="mono" style="font-size:10px">${esc(
          controllerText,
        )}</span>`)}
        ${row('관측 관절값', `<span class="mono" style="font-size:10px">${esc(
          jointText,
        )}</span>`)}
        ${row('그리퍼 개구', `<span class="mono">${apertureText}</span>`)}
        ${row('관절 오차', `<span class="mono">${
          evidence.max_error_rad === undefined || evidence.max_error_rad === null
            ? '관측 없음'
            : `${evidence.max_error_rad} rad (허용 ${evidence.tolerance_rad ?? '—'})`
        }</span>`)}
        ${row('STOP 확인', `<span>${stopText}</span>`)}
        ${
          evidence.hold_requirement
            ? row(
                '파지 관측',
                `<span>${
                  evidence.hold_requirement.hold_required
                    ? '요구됨'
                    : '요구되지 않음'
                }${
                  evidence.hold_observation_performed === false
                    ? ' · 관측하지 않았습니다'
                    : evidence.hold_observation_performed === true
                      ? ' · 관측했습니다'
                      : ''
                }</span>`,
              )
              + `<p class="hint" style="margin:-4px 0 4px 0">
                   <span class="hint-mark">⬡</span>${esc(
                     evidence.hold_requirement.basis || '',
                   )}</p>`
            : ''
        }
      </div>
      ${
        evidence.is_simulated
          ? `<p class="hint" style="margin-top:6px"><span class="hint-mark">⬡</span>
               Gazebo simulation 관측값입니다. 실하드웨어 미검증.</p>`
          : ''
      }
    </div>`;
}

export function renderExecutionCard(state) {
  const status = state.execution.status;
  const view = STATUS_VIEW[status];
  const total = state.plan ? state.plan.steps.length : 0;
  const percent = progressPercent(state, total);
  const fillClass =
    status === EXECUTION.CANCELLED || status === EXECUTION.CANCELLING
      ? 'orange'
      : status === EXECUTION.GLOBAL_STOPPED || status === EXECUTION.STOPPING
        ? 'danger'
        : '';

  const idleBody = `<div style="padding:24px 0;text-align:center;display:flex;flex-direction:column;gap:12px;align-items:center">
      <p style="margin:0;color:var(--muted)">
        ${
          canExecute(state)
            ? '안전 판단 PASS · 실행 시작을 누르면 실행됩니다.'
            : '실행할 수 있는 계획이 없습니다. 계획을 생성하고 PASS를 확인하세요.'
        }
      </p>
      <button class="btn primary" type="button" data-action="execute"
        ${canExecute(state) ? '' : 'disabled'}>▶ 실행 시작</button>
    </div>`;

  const cancelBox =
    status === EXECUTION.RUNNING
      ? `<div class="notice orange" style="flex-direction:column;gap:8px">
           <div class="row" style="width:100%">
             <span style="display:flex;align-items:center;gap:6px;font-weight:600">
               <span>⚡</span>실행 단위 취소</span>
             <span class="robot-pick-sub mono">EXECUTION SCOPE</span>
           </div>
           <button class="btn orange block" type="button" data-action="cancel"
             ${canCancel(state) ? '' : 'disabled'}>■ 이 실행 취소</button>
           <p style="color:#6b4020">이 실행만 취소합니다. 다른 실행에는 영향을 주지 않습니다.</p>
           <p style="color:var(--dim);font-size:10px">
             로봇 전체 긴급 정지 → 상단 <strong style="color:var(--danger)">■ 전체 정지</strong></p>
         </div>`
      : '';

  const runningBody = `
      <div class="meta-grid">
        ${meta('실행 ID', state.execution.id || '—', true)}
        ${meta('경과 시간', `${state.execution.elapsedSec}s`)}
        ${meta('현재 단계', total ? `${state.execution.currentStep} / ${total}` : '—')}
        ${meta(
          '현재 스킬',
          state.plan && state.plan.steps[state.execution.currentStep - 1]
            ? state.plan.steps[state.execution.currentStep - 1].skill
            : '—',
        )}
      </div>
      <div>
        <div class="progress-meta"><span>진행률</span><span class="mono">${percent}%</span></div>
        <div class="progress-bar"><div class="progress-fill ${fillClass}" style="width:${percent}%"></div></div>
        ${
          status === EXECUTION.CANCELLING
            ? '<p class="hint" style="color:var(--orange);margin-top:6px"><span class="dot sm orange pulse"></span>취소 요청 전송됨 · 실제 정지 확인 중…</p>'
            : ''
        }
        ${
          status === EXECUTION.STOPPING
            ? '<p class="hint" style="color:var(--danger);margin-top:6px"><span class="dot sm danger pulse"></span>전체 정지 요청 전송됨 · 실제 정지 확인 중…</p>'
            : ''
        }
      </div>
      ${cancelBox}
      ${renderCancelRecord(state.cancelRecord)}
      ${
        status === EXECUTION.GLOBAL_STOPPED
          ? `<div class="notice danger"><span class="notice-mark">■</span>
               <p><strong>전체 정지 — 모든 실행 중단됨</strong><br />
               결과는 아래 "전체 정지 결과"에 따로 남습니다.</p></div>`
          : ''
      }
      ${
        status === EXECUTION.UNVERIFIED
          ? `<div class="notice warning"><span class="notice-mark">?</span>
               <p><strong>모션은 끝났지만 결과를 확인하지 못했습니다</strong><br />
               ${esc(state.execution.unverifiedDetail || '')}
               ${
                 state.execution.unverifiedReason
                   ? `<span class="mono"> (${esc(state.execution.unverifiedReason)})</span>`
                   : ''
               }<br />
               성공으로 바꾸지 않습니다. 각 단계의 관측값은 아래에 있습니다.</p>
             </div>`
          : ''
      }
      ${renderObservation(state)}
      <div>
        <p class="label-caps">실행 타임라인</p>
        <div class="timeline" style="margin-top:8px">${renderTimeline(state)}</div>
      </div>`;

  const statusCard = `<section class="card ${view.border}">
      ${cardHeader('실행 상태', pill(view.color, view.icon, view.label))}
      <div class="card-body">${status === EXECUTION.IDLE ? idleBody : runningBody}</div>
    </section>`;

  const results = state.execution.results;
  const mark = (value) =>
    value == null
      ? '<span class="axis-none">—</span>'
      : value
        ? '<span class="axis-yes">✓</span>'
        : '<span class="axis-no">✕</span>';
  const statusPill = (result) => {
    if (result.status === 'done') return pill('success', '✓', '완료');
    if (result.status === 'cancelled') return pill('orange', '■', '취소');
    if (result.status === 'stopped') return pill('danger', '■', '정지');
    if (result.status === 'running') return pill('info', '▶', '실행중');
    if (result.status === 'failed') return pill('danger', '✕', '실패');
    return pill('muted', '◉', '대기');
  };

  const resultCard = results.length
    ? `<section class="card">
        ${cardHeader(
          '단계별 결과',
          `<span class="robot-pick-sub mono">${results.length} / ${total}</span>`,
        )}
        <div class="table-scroll">
          <table class="result-table">
            <thead><tr>
              <th>#</th><th>스킬</th><th>상태</th><th>수락</th><th>모션</th>
              <th>목표</th><th>성공</th><th>Reason</th><th>재시도</th>
            </tr></thead>
            <tbody>
              ${results
                .map(
                  (result) => `<tr>
                    <td class="num">${result.no}</td>
                    <td class="skill">${esc(result.skill)}</td>
                    <td>${statusPill(result)}</td>
                    <td class="center">${mark(result.requestAccepted)}</td>
                    <td class="center">${mark(result.motionDone)}</td>
                    <td class="center">${mark(result.goalReached)}</td>
                    <td class="center">${mark(result.taskSuccess)}</td>
                    <td class="num">${esc(result.reasonCode || '—')}</td>
                    <td class="center num">${result.retry}</td>
                  </tr>`,
                )
                .join('')}
            </tbody>
          </table>
        </div>
        ${
          status === EXECUTION.COMPLETED
            ? `<div class="card-note">
                 <p class="label-caps">최종 결과 (5축)</p>
                 <div class="axis-grid" style="margin-top:12px">
                   ${['요청 수락', '모션 완료', '목표 도달', '작업 성공', '확인 불가']
                     .map(
                       (label, index) => `<div class="axis-cell">
                         <span class="axis-mark ${index === 4 ? 'unknown' : ''}">${index === 4 ? '?' : '✓'}</span>
                         <span class="axis-label">${esc(label)}</span>
                       </div>`,
                     )
                     .join('')}
                 </div>
                 <p class="hint" style="margin-top:8px"><span class="hint-mark">⬡</span>
                   시뮬레이션 실행 결과입니다. 실제 로봇 실행으로 집계되지 않습니다.</p>
               </div>`
            : ''
        }
      </section>`
    : '';

  return statusCard + resultCard;
}

// ── 전체 정지 결과 ───────────────────────────────────────────────────
export function renderStopCard(record) {
  if (!record) return '';
  const confirmed = record.result === 'confirmed';
  return `<section class="card red">
      <div class="card-header">
        <div style="display:flex;align-items:center;gap:8px">
          <span style="color:var(--danger)">■</span>
          <h3 class="card-title">전체 정지 결과</h3>
        </div>
        <div class="pill-row" style="align-items:center">
          ${pill('danger', '■', 'STOP 요청 전송됨')}
          ${confirmed ? pill('success', '✓', '로봇 정지 확인됨') : pill('danger', '!', '정지 미확인')}
          <button class="modal-close" type="button" data-action="dismiss-stop">✕</button>
        </div>
      </div>
      <div class="card-body tight">
        <div class="meta-grid four">
          ${meta('범위', 'global (전체 시스템)')}
          ${meta('요청 시각', record.requestTime || '—')}
          ${meta('정지 확인 시각', record.confirmedTime || '—')}
          ${meta('영향받은 실행 수', String(record.affectedExecutionCount ?? 0))}
          ${meta('내 실행', (record.yourExecutionIds || []).join(', ') || '—', true)}
          ${meta('Reason Code', record.reasonCode || 'exec.stopped', true)}
          ${meta('실제 정지 확인', confirmed ? '확인됨' : '실패')}
          ${meta('다음 실행', '새 계획 생성 시까지 차단(래치)')}
        </div>
        ${
          confirmed
            ? ''
            : `<div class="notice danger"><span class="notice-mark">!</span>
                 <p>정지 요청은 전송되었지만 실제 정지 상태를 확인하지 못했습니다.
                 ${esc(record.detail || '')}</p></div>`
        }
      </div>
    </section>`;
}

// ── 모달 ──────────────────────────────────────────────────────────────
export function renderModal(state) {
  if (!state.modal) return '';
  if (state.modal === 'execute') {
    const robot = robotById(state.robotId);
    const plan = state.plan || {};
    return `<div class="modal-backdrop" data-action="modal-backdrop">
        <div class="modal" role="dialog" aria-modal="true">
          <h2>실행을 시작하시겠습니까?</h2>
          <p class="modal-sub">아래 정보를 확인하고 실행을 시작하세요.</p>
          <div class="modal-panel">
            ${row('로봇', `<span>${esc(robot.name)}</span>`)}
            ${row('안전 판단', pill('success', '✓', 'PASS'))}
            ${row('계획 ID', `<span class="mono" style="color:var(--info)">${esc(plan.planId || '')}</span>`)}
            ${row('단계 수', `<span>${(plan.steps || []).length}단계</span>`)}
            ${row('유효 시간', `<span>${esc(expiryText(plan))}</span>`)}
          </div>
          <div class="btn-row">
            <button class="btn primary grow" type="button" data-action="execute-confirm">▶ 실행 확인</button>
            <button class="btn ghost grow" type="button" data-action="close-modal">취소</button>
          </div>
        </div>
      </div>`;
  }
  if (state.modal === 'cancel') {
    const plan = state.plan || {};
    const step = state.execution.currentStep;
    const current = (plan.steps || [])[step - 1];
    return `<div class="modal-backdrop" data-action="modal-backdrop">
        <div class="modal orange" role="dialog" aria-modal="true">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
            <span style="color:var(--orange);font-weight:700">■</span>
            <h2 style="margin:0">이 실행을 취소할까요?</h2>
          </div>
          <p class="modal-sub">현재 실행만 취소합니다. 다른 실행과 로봇 전체에는 영향을 주지 않습니다.</p>
          <div class="modal-panel">
            ${row('실행 ID', `<span class="mono">${esc(state.execution.id || '')}</span>`)}
            ${row('Plan ID', `<span class="mono" style="color:var(--info)">${esc(plan.planId || '')}</span>`)}
            ${row(
              '현재 단계',
              `<span>${step} / ${(plan.steps || []).length} — ${esc(current ? current.description : '')}</span>`,
            )}
            ${row('범위', '<span class="mono">execution</span>')}
            ${row('Reason Code', '<span class="mono">exec.canceled</span>')}
          </div>
          <div class="notice orange" style="margin-bottom:16px">
            <span class="notice-mark">⚡</span>
            <p>취소 요청 후 실제 정지 확인까지 수 초가 걸릴 수 있습니다. (CANCELLING → 취소됨)</p>
          </div>
          <div class="btn-row">
            <button class="btn orange grow" type="button" data-action="cancel-confirm">■ 실행 취소</button>
            <button class="btn ghost grow" type="button" data-action="close-modal">닫기</button>
          </div>
        </div>
      </div>`;
  }
  if (state.modal === 'robot') {
    const options = ROBOTS.map((robot) => {
      const selected = robot.id === state.robotId;
      const skills = [...robot.skills, ...robot.disabledSkills]
        .map(
          (skill) =>
            `<span class="chip skill${robot.disabledSkills.includes(skill) ? ' off' : ''}">${esc(skill)}</span>`,
        )
        .join('');
      return `<button class="robot-option ${selected ? 'selected' : ''}" type="button"
          data-action="select-robot" data-robot="${esc(robot.id)}"
          ${robot.implemented ? '' : 'disabled'}>
          <div class="robot-option-top">
            <div class="robot-option-id">
              <span class="robot-option-icon">${esc(robot.icon)}</span>
              <div>
                <p class="robot-option-name">${esc(robot.name)}</p>
                <p class="robot-option-model">${esc(robot.model)} · ${robot.dof}축</p>
              </div>
            </div>
            ${selected ? '<span class="robot-option-check">✓</span>' : ''}
          </div>
          <div class="robot-option-specs">
            <div><span>페이로드</span><p>${esc(robot.payload)}</p></div>
            <div><span>도달거리</span><p>${esc(robot.reach)}</p></div>
            <div><span>어댑터</span><p>${esc(robot.adapter)}</p></div>
            <div><span>프로필</span><p>${esc(robot.profile)}</p></div>
          </div>
          <div class="pill-row">${skills || '<span class="hint">지원 스킬 없음</span>'}</div>
          <div class="pill-row" style="margin-top:8px">
            ${robot.badges.map((b) => pill(b.color, b.icon, b.label)).join('')}
          </div>
          <p class="robot-option-notes">${esc(robot.notes)}</p>
        </button>`;
    }).join('');

    return `<div class="modal-backdrop" data-action="modal-backdrop">
        <div class="modal wide" role="dialog" aria-modal="true">
          <div class="modal-head">
            <div>
              <h2>로봇 선택</h2>
              <p>검증된 구성만 선택할 수 있습니다. 미구현 로봇은 선택되지 않습니다.</p>
            </div>
            <button class="modal-close" type="button" data-action="close-modal">✕</button>
          </div>
          <div class="robot-grid">${options}</div>
          <div class="modal-foot">
            <p>로봇 변경은 계획 생성 전에만 가능합니다. 실행 중에는 바꿀 수 없습니다.</p>
            <button class="btn ghost sm" type="button" data-action="close-modal">닫기</button>
          </div>
        </div>
      </div>`;
  }
  return '';
}

// ── 전체 ──────────────────────────────────────────────────────────────
export function render(state, backendKind) {
  const set = (id, html) => {
    const node = document.getElementById(id);
    if (node && node.innerHTML !== html) node.innerHTML = html;
  };

  // 헤더
  const conn = document.getElementById('conn');
  if (conn) {
    const { state: connState, detail } = state.connection;
    const cls = connState === 'ok' ? '' : connState === 'lost' ? 'bad' : 'warn';
    const text =
      connState === 'ok'
        ? backendKind === 'simulation'
          ? '시뮬레이션 모드'
          : '서버 연결됨'
        : connState === 'lost'
          ? '연결 끊김'
          : '연결 중…';
    conn.innerHTML = `<span class="conn-dot ${cls} ${connState === 'ok' ? 'pulse' : ''}"></span>`
      + `<span class="conn-text ${cls} sm-only-inline" title="${esc(detail)}">${esc(text)}</span>`;
  }
  const sessionNode = document.getElementById('session-id');
  if (sessionNode) {
    sessionNode.textContent = state.session ? state.session.session_id : '세션 없음';
  }

  // 계획 화면
  set('card-robot', renderRobotCard(state));
  // **canvas를 지우지 않는다.** 카드가 이미 있으면 머리말만 갈아 끼운다 —
  // innerHTML을 다시 쓰면 그려 둔 프레임이 사라진다.
  const sceneNode = document.getElementById('card-scene');
  // 테스트의 최소 DOM 스텁에는 querySelector가 없다. 있을 때만 쓴다.
  const canQuery = sceneNode && typeof sceneNode.querySelector === 'function';
  const hasCanvas = canQuery && sceneNode.querySelector('#scene-canvas');
  if (!hasCanvas) {
    set('card-scene', renderSceneCard(state));
  } else if (typeof document.createElement === 'function') {
    const header = sceneNode.querySelector('.card-header');
    const fresh = document.createElement('div');
    fresh.innerHTML = renderSceneCard(state);
    const freshHeader = fresh.querySelector
      ? fresh.querySelector('.card-header') : null;
    if (header && freshHeader && header.innerHTML !== freshHeader.innerHTML) {
      header.innerHTML = freshHeader.innerHTML;
    }
  }
  set('card-workcell', renderWorkcellCard(state));
  set('card-hardware', renderHardwareCard(state));
  set('card-voice', renderVoiceCard(state));
  set('card-events', renderEventsCard(state));
  set('card-command', renderCommandCard(state, backendKind));
  set('card-plan', renderPlanCard(state));
  set('card-verdict', renderVerdictCard(state));
  const blockHtml = renderBlockCard(state);
  set('card-block', blockHtml);
  const blockNode = document.getElementById('card-block');
  // 차단 기록이 없으면 빈 카드 상자를 남기지 않는다.
  if (blockNode) blockNode.hidden = !blockHtml;
  set(
    'goto-workspace-slot',
    isActive(state) || isStopped(state) || state.execution.status === EXECUTION.COMPLETED
      ? `<button class="goto-workspace" type="button" data-action="goto-workspace">
           <span>→</span> 실행 워크스페이스로 이동
           ${isActive(state) ? '<span class="dot success pulse"></span>' : ''}
         </button>`
      : '',
  );
  set('stop-slot-plan', state.view === 'plan' ? renderStopCard(state.stopRecord) : '');

  // 워크스페이스
  set('card-sim', renderSimulationCard(state));
  set('card-execution', renderExecutionCard(state));
  set('stop-slot-workspace', state.view === 'workspace' ? renderStopCard(state.stopRecord) : '');
  const verdictPill = document.getElementById('sub-verdict');
  if (verdictPill) {
    const verdict = state.validation ? state.validation.verdict : null;
    verdictPill.innerHTML = verdict
      ? verdict === VERDICT.PASS
        ? pill('success', '✓', 'PASS')
        : verdict === VERDICT.BLOCK
          ? pill('danger', '✕', 'BLOCK')
          : pill('warning', '?', 'ASK')
      : '';
  }
  const subPlan = document.getElementById('sub-plan-id');
  if (subPlan) subPlan.textContent = state.plan ? state.plan.planId : '';

  // 화면 전환
  const planView = document.getElementById('view-plan');
  const workspaceView = document.getElementById('view-workspace');
  if (planView && workspaceView) {
    planView.classList.toggle('hidden', state.view !== 'plan');
    workspaceView.classList.toggle('hidden', state.view !== 'workspace');
  }

  // 모달
  set('modal-root', renderModal(state));

  // 명령 입력 값 복원(다시 그릴 때 커서를 잃지 않게 값만 맞춘다)
  const input = document.getElementById('command-input');
  if (input && input.value !== state.command) input.value = state.command;

  // 이벤트 목록은 최신이 보이게 내린다.
  const list = document.getElementById('event-list');
  if (list) list.scrollTop = list.scrollHeight;
}
