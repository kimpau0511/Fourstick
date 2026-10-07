/** 화면 그리기. 상태 → DOM. **여기서 상태를 바꾸지 않는다.**
 *
 * 배치: `html/내가원하는구성.png` — 위 줄에 로봇 선택·연결·명령 감지·전체 정지,
 * 왼쪽 큰 칸에 가제보 화면과 시뮬레이션 작업 명령, 오른쪽 칸에 생성된 작업 계획 ·
 * 안전 판단 및 실행 · 차단된 요청.
 * 디자인: `html/design.md` — 모노크롬 잉크/캔버스, 스타디움 필, 그림자 없음.
 * 강조색(파랑)은 **결정을 요구하는 확인 카드에만** 쓴다.
 *
 * 정책 차이: 작업자 승인 단계가 없다. 안전 판단은 PASS/BLOCK/ASK만 보여주고,
 * PASS일 때만 실행 시작을 누를 수 있다.
 *
 * 시뮬레이션 표시 배지는 화면에 두지 않는다. 실제 하드웨어처럼 보이는 문구·
 * 선택지도 쓰지 않는다 — 내부 `is_simulated`와 안전 검증은 그대로다.
 *
 * 실기 준비도·검증 상태는 화면에 두지 않는다 — 내부 판정 코드와 설정은 그대로
 * 있고, 공개 응답에서만 빠진다(`server/routes/common.py`).
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
import { RECORD_LABELS, RESULT_LABELS } from './sim-demo.js';
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
           ${esc(workcell.world || '')} 시뮬레이터에서 수행됩니다.</p>
         </div>`
      : '';

  const fakeNotice =
    server && server.kind === 'fake' && !(workcell && workcell.registered)
      ? `<div class="notice danger">
           <span class="notice-mark">⚠</span>
           <p><strong>개발용 Fake 실행</strong> — 실행은 개발용 Fake Adapter(${esc(
             server.robot_id || 'fake',
           )})로 수행됩니다. Gazebo 작업 셀에 연결되지 않았습니다.</p>
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
        <p>실행 결과는 모두 Gazebo 시뮬레이션 결과입니다.</p>
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

// ── 왼쪽 큰 칸: 가제보 화면 ───────────────────────────────────────────
/* 시뮬레이션 표시 배지는 화면에 두지 않는다(사용자 요구). 대체 배지·대체
 * 문구도 넣지 않는다. **표시만 없앤 것이고 검증은 그대로다** — 서버 응답의
 * `is_simulated`·`simulation_notice`와 모든 안전 검증은 변하지 않는다.
 */
/** 자유 pick/place가 막혔을 때 **할 수 있는 길**을 알려 주는 한 문장.
 *  화면 여러 곳이 같은 말을 쓰도록 한 곳에 둔다. */
export const SIM_PICK_PLACE_HINT =
  '시뮬레이션 작업 명령에서 A/B/C 자재와 대상 위치를 지정하세요.';
/** 영상 위 배지들. 실행 중인 시연 작업과 전체 정지를 함께 얹는다. */
export function renderSceneBadges(state) {
  const status = (state.simDemo || {}).status;
  const running = status && status.running_job;
  return [
    running
      ? `<span class="badge-overlay">▶ ${esc(running.action_label || '작업')} ·`
        + ` ${esc(running.material || '')}</span>`
      : '',
    state.stopLatched ? '<span class="badge-overlay stop">■ 전체 정지</span>' : '',
  ].join('');
}

/** 가제보 화면 아래 자재 상태 띠. 확인 카드가 말하는 "현재 자재 상태"를
 *  명령을 보내기 전에도 볼 수 있게 상시로 둔다. 기록에서만 온다. */
export function renderSceneStrip(state) {
  const status = (state.simDemo || {}).status;
  if (!status || !status.enabled || !(status.materials || []).length) {
    return `<p class="hint"><span class="hint-mark">⬡</span>
      ${esc(status && status.reason
        ? `시뮬레이션 시연을 쓸 수 없습니다 — ${status.reason}`
        : '시뮬레이션 작업 셀에 연결되면 자재 상태가 여기에 표시됩니다.')}</p>`;
  }
  const chips = (status.materials || [])
    .map((material) => {
      const record = material.record;
      const label = record && RECORD_LABELS[record.state]
        ? RECORD_LABELS[record.state][2] : '원래 자리';
      const tone = record && RECORD_LABELS[record.state]
        ? RECORD_LABELS[record.state][0] : 'muted';
      return `<span class="material-chip">
          <span class="material-swatch"></span>
          <span>${esc(material.korean || material.model)}</span>
          ${pill(tone, record && RECORD_LABELS[record.state]
            ? RECORD_LABELS[record.state][1] : '▪', label)}
        </span>`;
    })
    .join('');
  return `<div>
      <p class="label-caps">현재 자재 상태</p>
      <div class="material-strip" style="margin-top:8px">${chips}</div>
    </div>
    ${renderSlotStrip(state)}`;
}

/** 컨베이어 슬롯별 점유 현황. 서버가 준 것만 보여준다 — 화면이 자리를 만들지
 *  않는다. 슬롯 기능이 꺼져 있으면(검증된 슬롯 없음) 아무것도 그리지 않는다. */
export function renderSlotStrip(state) {
  const conveyor = ((state.simDemo || {}).status || {}).conveyor;
  if (!conveyor || !conveyor.enabled || !(conveyor.slots || []).length) return '';
  const chips = conveyor.slots
    .map((slot) => `<span class="material-chip${slot.occupied ? ' taken' : ''}">
        <span class="slot-no">${esc(String(slot.number ?? '?'))}</span>
        <span>${esc(slot.occupied ? (slot.korean || slot.model) : '비어 있음')}</span>
      </span>`)
    .join('');
  const note = conveyor.full
    ? pill('warning', '!', '모두 참 — 다음 이송은 차단됩니다')
    : pill('muted', '▫', `다음 자리: ${conveyor.next_slot_label || '—'}`);
  return `<div style="margin-top:12px">
      <p class="label-caps">컨베이어 위치 ${note}</p>
      <div class="material-strip" style="margin-top:8px">${chips}</div>
    </div>`;
}

/** 영상 위 안내문. 프레임이 오고 있으면 **아무것도 그리지 않는다.** */
export function renderSceneEmpty(state) {
  if (state.sceneAvailable === false) {
    return `<div class="scene-empty"><span>✕</span><span>${
      esc(state.sceneDetail || '장면 카메라를 쓸 수 없습니다.')}</span></div>`;
  }
  if (state.sceneStream === 'open') return '';
  if (state.sceneStream === 'snapshot') {
    return state.sceneSnapshotAt ? '' : `<div class="scene-empty"><span>◷</span><span>${
      esc('실시간 영상이 없어 서버 스냅샷을 받고 있습니다…')}</span></div>`;
  }
  const text = state.sceneAvailable === null
    ? '장면 카메라를 확인하고 있습니다…'
    : state.sceneStream === 'stalled'
      ? '프레임이 끊겼습니다 — 만들어 그리지 않습니다.'
      : '장면 스트림에 연결하는 중입니다…';
  return `<div class="scene-empty"><span>◉</span><span>${esc(text)}</span></div>`;
}

function sceneStatusPill(state) {
  if (state.sceneAvailable === false) return pill('muted', '◉', '사용 불가');
  if (state.sceneAvailable === null) return pill('muted', '◉', '확인 중');
  switch (state.sceneStream) {
    case 'open': return pill('success', '▶', `${state.sceneFps || 0} FPS`);
    case 'snapshot': {
      // 스냅샷이 멈춰도 그림은 남는다. 시각을 적어 낡았는지 보이게 한다.
      const at = state.sceneSnapshotAt
        ? ` · ${new Date(state.sceneSnapshotAt).toLocaleTimeString('ko-KR', { hour12: false })}`
        : '';
      return pill('warning', '◷', `스냅샷 · 실시간 아님${at}`);
    }
    case 'stalled': return pill('warning', '!', '프레임 끊김');
    default: return pill('muted', '◉', '연결 중');
  }
}

/** 작업 셀 화면. **Gazebo 서버가 렌더링한 프레임**이다.
 *
 * 이 카드가 있는 이유: 이 환경의 Gazebo GUI는 소프트웨어 렌더링만 가능하고
 * 부하가 오르면 프레임을 완성하지 못한다. 같은 장면을 서버는 훨씬 적은 CPU로
 * 렌더링하므로 사용자가 셀을 보는 경로를 웹 화면에 둔다.
 *
 * 서버가 프레임을 주지 못하면 **빈 그림을 그리지 않는다** — 이유를 적는다.
 */
export function renderSceneCard(state) {
  const statusPill = sceneStatusPill(state);
  // 프레임을 받을 수 없으면 canvas를 두지 않는다 — 빈 화면을 영상처럼 보이게
  // 하지 않는다. 안내문은 **canvas와 따로 갱신한다**(아래 renderSceneEmpty) —
  // 카드 전체를 다시 그리면 그려 둔 프레임이 사라지기 때문에, 첫 프레임이 온
  // 뒤에도 "확인하고 있습니다"가 남아 있었다(실측).
  const frame = state.sceneAvailable === false
    ? `<div class="scene-frame">
         <div class="scene-badges" id="scene-badges">${renderSceneBadges(state)}</div>
         <div id="scene-empty-slot">${renderSceneEmpty(state)}</div>
       </div>`
    : `<div class="scene-frame">
         <canvas id="scene-canvas" width="640" height="360"></canvas>
         <div class="scene-badges" id="scene-badges">${renderSceneBadges(state)}</div>
         <div id="scene-empty-slot">${renderSceneEmpty(state)}</div>
       </div>`;
  return `
    ${cardHeader('가제보 화면', statusPill)}
    <div class="card-body">
      ${frame}
      <div id="scene-strip">${renderSceneStrip(state)}</div>
      <p class="hint"><span class="hint-mark">⬡</span>
        Gazebo 서버가 렌더링한 장면을 WebSocket으로 받습니다. GUI 창과 무관합니다.${
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
// 실기(실제 하드웨어) 준비 상태 카드는 화면에서 걷어냈다. 지금 서비스는 Gazebo
// 시뮬레이션 전용이라, 실기 준비도·검증 체크리스트를 보여 주면 실제 로봇을 고르거나
// 돌릴 수 있는 것처럼 읽힌다. 판정 코드(`validation/hardware_readiness.py`)와 설정·
// 기준 데이터는 **그대로 있고** 계속 계산된다 — 공개 응답과 화면에서만 빠진다
// (`server/routes/common.py`의 `public_payload`).

export function renderVoiceCard(state) {
  const { recording, partial, final, normalized, confidence, available, detail } = state.stt;
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
      <p class="hint"><span class="hint-mark">◎</span>
        마이크 on/off는 <strong>시뮬레이션 작업 명령</strong> 칸에 있습니다.
        여기는 전사 결과와 신뢰도만 보여줍니다.</p>
      <div class="row">
        <span class="row-label">상태</span>
        <span class="row-value">
          ${recording ? '⏺ 듣는 중' : available ? '마이크 준비' : esc(detail || '서버 STT 없음')}
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
        ${final && normalized ? row('정규화 결과', esc(normalized)) : ''}
        ${!partial && !final ? '<p class="transcript-empty">전사 결과가 여기에 표시됩니다.</p>' : ''}
      </div>
      ${
        confidence == null
          ? ''
          : `<div class="row"><span class="row-label">신뢰도</span>
               <span class="row-value mono ${confidenceClass}">
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

// ── 왼쪽 큰 칸 아래: 시뮬레이션 작업 명령 ────────────────────────────
/** 명령 결과 영역. 규칙 판단(RUN/STOP/ASK/BLOCK)과 **확인 카드**가 여기 나온다.
 *
 * 모드 토글은 없다 — Gazebo 작업 셀에서는 자재 이송·복귀·정지·이어서가
 * **기본 동작**이고, 그 밖의 발화는 서버가 `PASS_THROUGH`로 돌려 기존 계획
 * 생성으로 간다. */
export function renderSimCommandArea(state) {
  // G1을 골랐으면 FR3 시연 확인 카드·결과를 여기 그리지 않는다(로봇 맥락 분리).
  // G1의 확인 카드·진행은 G1 카드(#card-humanoid)가 그린다.
  if (state.robotId === 'unitree_g1') return '';
  const sim = state.simDemo || {};
  if (sim.confirmation) return renderConfirmCard(state);
  const result = sim.result;
  if (!result) return '';
  return `<div class="rows">${renderSimCommandResult(state, result)}</div>`;
}

function simDecisionPill(decision) {
  if (decision === 'RUN') return pill('success', '▶', '작업 생성');
  if (decision === 'CONFIRM') return pill('decide', '◈', '확인 필요');
  if (decision === 'CANCELLED') return pill('muted', '✕', '취소됨');
  if (decision === 'STOP') return pill('danger', '■', '정지 요청');
  if (decision === 'ASK') return pill('warning', '?', 'ASK');
  if (decision === 'PASS_THROUGH') return pill('muted', '→', '계획 생성으로');
  if (decision === 'NOOP') return pill('muted', '✓', '이미 목표 상태');
  return pill('danger', '✕', 'BLOCK');
}

/** 남은 시간 표시. 서버가 만료를 강제하고, 화면은 그 숫자를 **보여만 준다.** */
function countdownText(seconds) {
  if (seconds == null) return '';
  return `확인 만료까지 ${Math.max(0, Math.ceil(seconds))}초`;
}

/** Qwen이 낸 해석의 **근거**. 모델이 무엇을 냈는지 숨기지 않는다. */
function renderIntentEvidence(info, minConfidence) {
  if (!info) return '';
  const confidence = typeof info.confidence === 'number'
    ? info.confidence.toFixed(2) : '—';
  const threshold = typeof minConfidence === 'number'
    ? minConfidence.toFixed(2) : (typeof info.min_confidence === 'number'
      ? info.min_confidence.toFixed(2) : '—');
  return `<div class="confirm-evidence">
      <p class="label-caps">해석 근거 · Qwen 후보</p>
      ${row('해석기', `<span class="mono">${esc(info.model_id || 'Qwen 분류기')}</span>`)}
      ${row('intent', `<span class="mono">${esc(info.intent || '—')}</span>`)}
      ${row('material_id', `<span class="mono">${esc(info.material_id || 'null')}</span>`)}
      ${row('confidence', `<span class="mono">${esc(confidence)}</span>`
        + ` <span class="robot-pick-sub">(기준 ${esc(threshold)})</span>`)}
      <p class="hint"><span class="hint-mark">ℹ</span>
        모델은 위 JSON만 냅니다. 계획을 만들지 않으며, 서버가 스키마·자재 후보·
        지금 시뮬레이션 상태를 다시 검증합니다.</p>
    </div>`;
}

/** 확인 대기가 말하는 **현재 자재 상태**. 서버가 해석 시점에 본 기록이다. */
function renderConfirmState(rows) {
  if (!rows || !rows.length) return '';
  const items = rows
    .map((item) => row(
      item.korean || item.model,
      `<span>${esc(item.state
        ? (RECORD_LABELS[item.state] ? RECORD_LABELS[item.state][2] : item.state)
        : '원래 자리')}</span>`
      + (item.has_checkpoint ? ' <span class="robot-pick-sub">· 체크포인트 있음</span>' : ''),
    ))
    .join('');
  return `<div class="confirm-evidence">
      <p class="label-caps">현재 자재 상태</p>${items}</div>`;
}

/** 확인 카드 — **이 화면에서 결정을 요구하는 유일한 자리.**
 *
 * design.md: 강조색은 결정을 요구할 때만 나온다. 확인을 누르기 전까지 작업은
 * 만들어지지 않는다. 취소·만료·상태 변경이면 그대로 사라진다.
 *
 * 음성 final 명령도 여기서 확인을 기다린다.
 */
/** 계획 단계의 공통 transfer 동작 이름(표시용). */
const STEP_ACTION_LABELS = { move: '칸 직접 이동', transfer: '이송', return: '원래 자리 복귀' };

export function renderConfirmCard(state) {
  const sim = state.simDemo || {};
  const pending = sim.confirmation;
  if (!pending) return '';
  const remaining = sim.remainingSec == null ? pending.remaining_sec : sim.remainingSec;
  const evidence = pending.evidence || {};
  const busy = Boolean(sim.busy);
  const goalSteps = pending.kind === 'goal'
    ? `<div class="confirm-evidence"><p class="label-caps">실행 순서</p>${(pending.plan || [])
      .map((step) => row(`STEP ${String(step.step).padStart(2, '0')}`,
        `<strong>${esc(step.material_label || step.material || '')}</strong> `
        + (STEP_ACTION_LABELS[step.action] ? `<span>[${esc(STEP_ACTION_LABELS[step.action])}]</span> ` : '')
        + `<span>${esc(step.from_label || step.from || '')} → ${esc(step.to_label || step.to || '')}</span>`
        + (step.reason ? `<br><span class="robot-pick-sub">${esc(step.reason)}</span>` : '')))
      .join('')}${((pending.reasoning || {}).notes || [])
      .map((note) => row('판단', `<span>${esc(note)}</span>`)).join('')}</div>` : '';
  const interpretation = ((pending.interpretation || {}).evidence || []);
  const interpretationRows = interpretation.length
    ? `<div class="confirm-evidence"><p class="label-caps">해석 근거</p>${interpretation
      .map((line) => row('근거', `<span>${esc(line)}</span>`)).join('')}</div>` : '';
  return `<div class="confirm">
      <div class="confirm-head">
        ${pill('decide', '◈', '확인 필요')}
        <span class="confirm-countdown${remaining != null && remaining <= 10 ? ' urgent' : ''}"
          >${esc(countdownText(remaining))}</span>
      </div>
      <p class="confirm-summary">${esc(pending.summary || '')}</p>
      <div class="confirm-actions">
        <button class="btn decide" type="button" data-action="sim-confirm" ${busy ? 'disabled' : ''}>
          ${busy ? '<span class="spinner"></span>' : '✓'} 확인 — 시뮬레이션에서 실행
        </button>
        <button class="btn outline" type="button" data-action="sim-cancel" ${busy ? 'disabled' : ''}>
          취소
        </button>
      </div>
      <div class="rows">
        ${row('인식한 명령', `<strong>${esc((sim.result || {}).raw_transcript || pending.utterance || '')}</strong>`)}
        ${row('정규화 결과', `<span>${esc((sim.result || {}).normalized_transcript || pending.utterance || '')}</span>`)}
        ${(sim.result || {}).stt_confidence != null
          ? row('STT confidence', esc((sim.result || {}).stt_confidence)) : ''}
        ${row(pending.kind === 'goal' ? '목표' : '동작', `<span class="mono">${esc(pending.action || '')}</span>`
          + ` <span class="robot-pick-sub">${esc(evidence.material_korean || pending.material || '')}</span>`)}
        ${evidence.slot_label
          ? row('대상 위치', `<strong>${esc(evidence.slot_label)}</strong>`
            + ' <span class="robot-pick-sub">서버가 배정</span>')
          : ''}
      </div>
      ${interpretationRows}
      ${goalSteps}
      ${renderIntentEvidence(evidence.classifier)}
      ${renderConfirmState(evidence.state)}
      <p class="hint"><span class="hint-mark">⬡</span>
        <strong>확인을 누르기 전에는 ${pending.kind === 'goal' ? '하위 작업이' : '작업이'} 만들어지지 않습니다.</strong>
        누르면 Gazebo 시뮬레이터 안에서 실행됩니다.</p>
    </div>`;
}

function renderSimCommandResult(state, result) {
  const sim = state.simDemo || {};
  const job = sim.job || result.job;
  const progress = (job && job.progress) || [];
  const last = progress[progress.length - 1];
  const report = (job && job.report) || null;
  const stop = result.stop;
  return `
    <div class="row"><span class="row-label">인식한 명령</span>
      <div class="row-value">${esc(result.raw_transcript || result.utterance || '')}</div></div>
    ${result.normalized_transcript ? row('정규화 결과', esc(result.normalized_transcript)) : ''}
    ${result.stt_confidence != null ? row('STT confidence', esc(result.stt_confidence)) : ''}
    <div class="row"><span class="row-label">판단</span>
      <div class="row-value">${simDecisionPill(result.decision)}${
        result.intent ? ` <span class="mono robot-pick-sub">${esc(result.intent)}</span>` : ''
      }${result.material ? ` <span class="mono robot-pick-sub">${esc(result.material)}</span>` : ''}</div></div>
    ${result.slot_label ? `<div class="row"><span class="row-label">대상 위치</span>
      <div class="row-value"><strong>${esc(result.slot_label)}</strong></div></div>` : ''}
    ${result.reason ? `<p class="hint"><span class="hint-mark">!</span>${esc(result.reason)}</p>` : ''}
    ${job ? `<div class="row"><span class="row-label">작업</span>
      <div class="row-value mono truncate">${esc(job.job_id)} · ${esc(job.status || '')}</div></div>` : ''}
    ${last ? `<div class="row"><span class="row-label">진행</span>
      <div class="row-value mono">${esc(`${last.no}/${last.of} ${last.label}`)}</div></div>` : ''}
    ${report && report.status ? `<div class="row"><span class="row-label">결과</span>
      <div class="row-value mono">${esc(report.status)}</div></div>` : ''}
    ${stop ? `<div class="row"><span class="row-label">정지</span>
      <div class="row-value">${stop.requested ? '요청됨 — 시뮬레이터가 정지를 확인하면 체크포인트가 남습니다'
        : esc(stop.detail || '실행 중인 시연 작업이 없습니다')}</div></div>` : ''}
    ${result.intent_result && ['ASK', 'BLOCK'].includes(result.decision)
      ? renderIntentEvidence(result.intent_result) : ''}`;
}

/** 목업의 "시뮬레이션 작업 명령" 칸.
 *
 * 텍스트 + 마이크 on/off · 명령 보내기 / 지우기 · TTS 음성 on/off.
 * TTS는 **기본 꺼짐**이고, 켜면 신뢰된 결과만 짧게 읽는다(`tts.js`).
 * 브라우저가 SpeechSynthesis를 갖고 있지 않으면 토글이 잠기고 그 사실을 적는다.
 */
export function renderCommandCard(state, backendKind) {
  const busy = Boolean(state.simDemo && state.simDemo.busy);
  const pendingConfirm = Boolean(state.simDemo && state.simDemo.confirmation);
  const disabled = !state.command.trim() || busy || state.planLoading || isActive(state);
  const simulated = backendKind === 'server'
    && Boolean(state.serverWorkcell && state.serverWorkcell.registered);
  const g1 = state.robotId === 'unitree_g1';
  const title = g1 ? 'G1 이동 명령 (시뮬레이션)' : simulated ? '시뮬레이션 작업 명령' : '작업 명령';
  const { partial, final } = state.stt;
  const traceHtml = renderVoiceTrace(state.stt.trace);
  const transcript = partial || final || traceHtml
    ? `<div class="transcript">${
        partial ? `<p class="transcript-partial">${esc(partial)}</p>` : ''
      }${traceHtml}</div>`
    : '';
  const simHint =
    backendKind === 'simulation'
      ? `<p class="hint"><span class="hint-mark">⬡</span>
           시뮬레이션 모드 예시 — PASS: "1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘" ·
           BLOCK: "1번 팔레트에서 사각형 자재를 집어서 컨베이어에 올려줘" ·
           ASK: "그거 저기로 옮겨줘"</p>`
      : '';
  return `
    ${cardHeader(title)}
    <div class="card-body">
      <textarea class="textarea" id="command-input" rows="3" data-action="command-input"
        placeholder="작업 명령을 입력하거나 마이크로 말하세요. (Enter 전송 · Shift+Enter 줄바꿈)">${esc(state.command)}</textarea>
      <div class="btn-row" id="command-actions">${renderCommandActions(state)}</div>
      <div id="command-transcript">${transcript}</div>
      <div id="command-note">${g1 ? `<p class="hint"><span class="hint-mark">⬢</span>
        G1 명령: "컨베이어 앞에 가" · "컨베이어 한번 찍고 와" · "출발 위치로 돌아와" · "멈춰".
        이동은 <strong>확인 카드</strong>에서 승인한 뒤 실행하고, "멈춰"는 확인 없이 바로 전달합니다.
        확인 카드와 진행은 위 G1 카드에 표시됩니다. FR3 명령은 로봇 선택에서 FR3를 고르세요.</p>`
        : pendingConfirm ? '' : `<p class="hint"><span class="hint-mark">⬡</span>
        예: "원형 자재를 컨베이어로 옮겨줘" · "삼각형 자재를 원래 자리로 돌려놔" ·
        "돌려놔"(컨베이어에 하나만 있을 때) · "이어서 해줘" · "멈춰".
        모호한 자재 작업 발화는 해석 뒤 <strong>확인 카드</strong>가 먼저 뜹니다.
        그 밖의 명령은 기존 계획 생성으로 갑니다(집기·놓기 차단은 그대로입니다).</p>`}</div>
      <div class="result-area" id="sim-command-area">${renderSimCommandArea(state)}</div>
      <div id="command-foot">
        <p class="hint"><span class="hint-mark">ℹ</span>
          계획 생성은 로봇을 실행하지 않습니다. 안전 판단이 PASS일 때
          <strong>실행 시작</strong>을 눌러야 실행됩니다.
          TTS 음성은 판단·확인·작업 시작·완료·정지 결과만 짧게 읽습니다.</p>
        ${renderTtsStatus(state.tts)}
        ${simHint}
      </div>
    </div>`;
}

/** 명령 칸의 버튼 줄. **textarea와 따로 갱신한다.**
 *
 * 카드 전체를 다시 그리면 타이핑 중인 textarea가 새 요소로 바뀌어 포커스·
 * 커서·한글 조합이 끊긴다. 그래서 카드는 한 번만 그리고 이 줄만 갈아 끼운다.
 * (여기를 따로 갱신하지 않았더니 STT가 붙은 뒤에도 마이크 버튼이 첫 렌더의
 *  `disabled` 상태로 남아 눌리지 않았다 — 실측.)
 */
export function renderCommandActions(state) {
  const busy = Boolean(state.simDemo && state.simDemo.busy);
  const disabled = !state.command.trim() || busy || state.planLoading || isActive(state);
  const { recording, available } = state.stt;
  return `
    <button class="btn ${recording ? 'danger' : ''}" type="button"
      data-action="toggle-voice" ${available ? '' : 'disabled'}
      title="${esc(available ? '마이크 입력' : '서버 STT를 쓸 수 없습니다')}">
      ${recording ? '● 마이크 끄기' : '◎ 마이크 켜기'}
    </button>
    <button class="btn primary" type="button" data-action="generate" ${disabled ? 'disabled' : ''}>
      ${busy || state.planLoading ? '<span class="spinner"></span>' : '◈'} 명령 보내기
    </button>
    <button class="btn ghost" type="button" data-action="clear-command">지우기</button>
    <div class="spacer"></div>
    ${renderTtsToggle(state)}`;
}

/** 음성 명령 한 건의 단계 추적. **어디서 틀렸는지** 사용자가 보게 한다:
 *  음성 구간을 못 잡았는지(VAD) · 잘못 들었는지(STT 원문) · 들은 뒤 잘못
 *  해석했는지(해석). 값은 서버 응답에서만 온다. */
export function renderVoiceTrace(trace) {
  if (!trace) return '';
  const row = (label, value, cls = '') => `<div class="row"><span class="row-label">${esc(label)}</span>`
    + `<span class="row-value ${cls}">${value}</span></div>`;
  const conf = trace.confidence == null ? '' : ` · 신뢰도 ${Number(trace.confidence).toFixed(2)}`;
  const rows = [];
  if (trace.audioFormat) rows.push(row('⓪ 브라우저 오디오', esc(trace.audioFormat)));
  rows.push(row('① 음성 구간', esc(trace.vad || '—'), trace.vadOk === false ? 'danger' : ''));
  if (trace.audioLevel) rows.push(row('  마이크 음량', esc(trace.audioLevel)));
  if (trace.audioOnset) rows.push(row('  음성 시작', esc(trace.audioOnset)));
  (trace.audioSuspect || []).forEach((w) => rows.push(row('  ⚠ 의심', esc(w), 'warning')));
  if (trace.raw != null) rows.push(row('② STT 원문(들은 말)', `"${esc(trace.raw)}"${esc(conf)}`));
  if (trace.normalized != null && trace.normalized !== trace.raw) {
    rows.push(row('③ 표기 정리', `"${esc(trace.normalized)}"`));
  }
  (trace.corrections || []).forEach((c) => rows.push(row('⚠ 고쳐 들음',
    `"${esc(c.heard)}" → "${esc(c.fixed)}" — 원문과 다르니 확인 카드에서 꼭 확인하세요`, 'warning')));
  if (trace.interpretation) rows.push(row('④ 해석', esc(trace.interpretation)));
  if (trace.outcome) rows.push(row('⑤ 결과', esc(trace.outcome),
    trace.outcomeLevel === 'warning' ? 'warning' : trace.outcomeLevel === 'error' ? 'danger' : ''));
  return `<div class="voice-trace"><p class="label-caps">음성 명령 단계</p>${rows.join('')}</div>`;
}

const TTS_PHASES = {
  idle: '대기', off: '꺼짐 — 소리를 내지 않았습니다', queued: '재생 준비',
  requested: '재생 요청됨 — 브라우저 시작 알림 대기', speaking: '브라우저가 재생 시작을 알림(끝 알림 대기)',
  ended: '브라우저가 재생 끝을 알림(소리가 났다는 증거 아님 — 귀로 확인)', error: '소리를 내지 못했습니다',
  unsupported: '이 브라우저는 음성 합성을 지원하지 않습니다',
};

/** TTS 재생 상태. 안내 문장은 **소리와 관계없이** 화면에 남긴다. */
export function renderTtsStatus(tts) {
  const st = tts && tts.status;
  if (!st) return '';
  const cls = st.phase === 'error' ? 'danger' : st.phase === 'off' ? 'warning' : '';
  const engine = st.voice ? `음성 ${esc(st.voice)}` : `한국어 음성 없음(목록 ${st.voices || 0}개) — 브라우저 기본 음성`;
  return `<div class="tts-status"><p class="hint"><span class="hint-mark">♪</span>
      TTS: <span class="${cls}">${esc(TTS_PHASES[st.phase] || st.phase)}</span> · ${engine}
      ${st.elapsed_ms != null ? `<br>브라우저 보고 재생 시간 ${st.elapsed_ms} ms(문장 길이로 어림 ${st.expected_ms} ms)` : ''}
      ${st.error ? `<br><span class="danger">${esc(st.error)}</span>` : ''}
      ${st.text ? `<br>안내 문장: "${esc(st.text)}"` : ''}
      <br>실제 청취: ${st.heard === true ? '<strong>사용자 확인 — 들림</strong>'
        : st.heard === false ? '<span class="danger">사용자 확인 — 안 들림</span>' : '확인 안 됨(귀로 듣고 눌러 주세요)'}
      ${['speaking', 'ended', 'error'].includes(st.phase) ? `
      <button class="btn ghost sm" type="button" data-action="tts-heard">들렸음</button>
      <button class="btn ghost sm" type="button" data-action="tts-not-heard">안 들렸음</button>` : ''}</p></div>`;
}

/** TTS 켬/끔 단추. 상태는 `state.tts`에서만 온다 — 화면이 값을 만들지 않는다. */
export function renderTtsToggle(state) {
  const tts = state.tts || {};
  if (!tts.supported) {
    return `<button class="btn off sm" type="button" data-action="toggle-tts" disabled
      title="이 브라우저는 음성 합성(SpeechSynthesis)을 지원하지 않습니다.">
      ○ TTS 음성 없음</button>`;
  }
  return `<button class="btn ${tts.enabled ? 'on' : 'off'} sm" type="button"
      data-action="toggle-tts" aria-pressed="${tts.enabled ? 'true' : 'false'}"
      title="${esc(tts.enabled
        ? '작업 결과를 짧게 읽습니다. 누르면 끕니다.'
        : '켜면 작업 결과를 짧게 읽습니다. 진행 로그와 모델 원문은 읽지 않습니다.')}">
      ${tts.enabled ? '♪ TTS 음성 on' : '○ TTS 음성 off'}</button>
    <button class="btn ghost sm" type="button" data-action="tts-test"
      title="시험 문장을 읽습니다. 들리는지 귀로 확인하세요.">음성 시험</button>`;
}

/** 머리말의 로봇 선택 — 목업 위 줄의 "로봇 선택". */
export function renderRobotPickHeader(state) {
  const robot = withWorkcell(robotById(state.robotId), state.serverWorkcell);
  return `<button class="robot-pick" data-action="open-robot" type="button"
      title="로봇 구성 보기 · 변경">
      <span aria-hidden="true">${esc(robot.icon)}</span>
      <span class="robot-pick-name">${esc(robot.name)}</span>
      <span class="robot-pick-more" aria-hidden="true">›</span>
    </button>`;
}

/** 머리말의 명령 감지 — 지금 무엇을 받고 있는지. 추측하지 않는다. */
export function renderDetect(state) {
  const { recording, partial, final } = state.stt;
  const sim = state.simDemo || {};
  const live = recording || Boolean(partial) || sim.busy || state.planLoading;
  const label = recording
    ? (partial ? `듣는 중: ${partial}` : '음성 듣는 중')
    : sim.busy
      ? '명령 해석 중'
      : state.planLoading
        ? '계획 생성 중'
        : sim.confirmation
          ? '확인 대기'
          : final
            ? '전사 완료'
            : state.command.trim()
              ? '입력 중'
              : '명령 감지 대기';
  const tone = sim.confirmation ? 'warning' : live ? 'success' : 'muted';
  return `<span class="dot ${tone}${live ? ' pulse' : ''}"></span>`
    + `<span class="detect-label truncate">${esc(label)}</span>`;
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

/** "안전 판단 및 실행" 칸 안의 **시뮬레이션 작업** 구역.
 *
 * 목업에는 칸이 셋뿐이므로 기존 시연 카드의 진행·결과·체크포인트를 여기로
 * 흡수한다. 시연 정지 버튼도 여기 둔다 — 헤더의 전체 정지와 별개 동작이다.
 */
export function renderSimExecution(state) {
  const sim = state.simDemo || {};
  const status = sim.status;
  if (!status || !status.enabled) return '';
  const running = status.running_job;
  const demo = status.state || {};
  const job = sim.job && (!running || sim.job.job_id === running.job_id) ? sim.job : running;
  const progress = (job && job.progress) || [];
  const last = progress[progress.length - 1];
  const percent = last && last.of ? Math.round((last.no / last.of) * 100) : 0;
  const report = (job && job.report) || null;
  const result = report && RESULT_LABELS[report.status];
  const checkpoint = demo.checkpoint;

  const runningBlock = running
    ? `<div class="rows">
        ${row('작업', `${esc(running.action_label || '')} ·`
          + ` <span class="mono">${esc(running.material || '')}</span>`)}
        ${row('진행', `<span class="mono">${esc(last ? `${last.no}/${last.of} ${last.label}`
          : '시작 준비 중')}</span>`)}
        ${running.stop_requested ? row('정지', '요청됨 — 확인 대기') : ''}
      </div>
      <div class="progress-bar"><div class="progress-fill" style="width:${percent}%"></div></div>
      <button class="btn danger block" type="button" data-action="sim-stop">■ 시연 정지</button>`
    : '';

  const resultBlock = !running && report && report.status
    ? `<div class="rows">
        ${row('마지막 작업', result ? pill(result[0], result[1], result[2])
          : `<span class="mono">${esc(report.status)}</span>`)}
       </div>`
    : '';

  const checkpointBlock = checkpoint
    ? `<div class="confirm-evidence">
        <p class="label-caps">STOP 체크포인트</p>
        ${row('자재 · 상태', `<span class="mono">${esc(checkpoint.model)}</span> ·`
          + ` ${esc(checkpoint.object_state || '')}`)}
        ${row('정지 단계', `<span class="mono">${esc(checkpoint.stopped_stage || '')}</span>`)}
        <p class="hint"><span class="hint-mark">ℹ</span>
          "이어서 해줘"라고 말하면 확인 뒤 체크포인트에서 이어서 실행합니다.</p>
      </div>`
    : '';

  if (!runningBlock && !resultBlock && !checkpointBlock) return '';
  return `<div class="rows" style="gap:12px">
      <p class="label-caps">시뮬레이션 작업</p>
      ${runningBlock}${resultBlock}${checkpointBlock}
    </div>`;
}

/** "안전 판단 및 실행" 칸에 올라가는 **시뮬레이션 실행 준비** 구역.
 *
 * 확인 대기는 계획(`/v1/plan`)이 아니라 검증된 시연 작업이다. 그래서 여기서
 * 보여 주는 것은 안전 판단 PASS/BLOCK/ASK가 아니라 **무엇을 어디로 옮기는지와
 * 무엇이 확인됐는지**다. 확인 버튼을 누르기 전에는 작업이 만들어지지 않는다.
 *
 * 검사 표시는 서버가 준 `readiness.checks`를 그대로 읽는다 — 화면이 "통과"를
 * 만들어 내지 않는다. `ok:null`은 아직 보지 않은 것이고, `recheck`는 실행
 * 직전에 다시 본다는 뜻이다.
 */
export function renderSimReadiness(state) {
  const pending = (state.simDemo || {}).confirmation;
  const ready = pending && pending.evidence && pending.evidence.readiness;
  if (!ready) return '';
  const mark = (c) => (c.ok === true ? ['success', '✓'] : c.ok === false
    ? ['danger', '✕'] : ['muted', '◌']);
  const rows = (ready.checks || []).map((c) => {
    const [tone, icon] = mark(c);
    return `<div class="row">
        <span class="row-label">${esc(c.label)}</span>
        <div class="row-value">${pill(tone, icon, c.ok === true ? '통과'
          : c.ok === false ? '차단' : '실행 직전 검사')}
          ${c.recheck ? '<span class="robot-pick-sub">· 실행 직전 재검증</span>' : ''}
        </div>
      </div>
      <p class="hint" style="margin:-2px 0 4px">${esc(c.detail || '')}</p>`;
  }).join('');
  const resource = (id) => id
    ? ` <span class="mono robot-pick-sub">${esc(id)}</span>` : '';
  // 상태 배지는 카드 머리말이 이미 갖고 있다. 여기서 되풀이하지 않는다.
  return `<div class="rows" style="gap:10px">
      <div class="rows">
        ${row('자재', `<strong>${esc(ready.material_korean || ready.material || '')}</strong>`
          + ` <span class="mono robot-pick-sub">${esc(ready.material || '')}</span>`)}
        ${row('출발 → 도착', `${esc(ready.origin || '—')}${resource(ready.source_resource)}`
          + ` <span class="robot-pick-sub">→</span> <strong>${esc(ready.target || '—')}</strong>`
          + resource(ready.destination_resource))}
        ${ready.slot_label
          ? row('배정 슬롯', `<strong>${esc(ready.slot_label)}</strong>`
            + ' <span class="robot-pick-sub">서버가 배정</span>') : ''}
      </div>
      <div class="confirm-evidence">
        <p class="label-caps">검증 상태</p>
        ${rows}
      </div>
      <p class="hint"><span class="hint-mark">⬡</span>
        <strong>확인을 눌러야 시작됩니다.</strong>
        확인 카드는 <strong>시뮬레이션 작업 명령</strong> 칸에 있습니다.</p>
    </div>`;
}

/** 시뮬레이션 명령이 ASK/BLOCK으로 끝났을 때, 같은 카드에서 사유를 보인다.
 *
 * 확인 대기가 있으면 그리지 않는다 — 그때는 실행 준비 구역이 자리를 갖는다. */
export function renderSimDecision(state) {
  const sim = state.simDemo || {};
  if (sim.confirmation) return '';
  const result = sim.result;
  if (!result || !['ASK', 'BLOCK'].includes(result.decision)) return '';
  const blocked = result.decision === 'BLOCK';
  const full = ((sim.status || {}).conveyor || {}).full;
  // 서버 사유가 이미 같은 안내를 담고 있으면 되풀이하지 않는다.
  const hintAlready = String(result.reason || '').includes('대상 위치를 지정하세요');
  return `<div class="notice ${blocked ? 'danger' : 'warning'}">
      <span class="notice-mark">${blocked ? '✕' : '?'}</span>
      <p><strong>시뮬레이션 작업 ${blocked ? 'BLOCK' : 'ASK'}</strong> —
      ${esc(result.reason || (blocked ? '지금 상태에서는 할 수 없습니다.'
        : '명령을 더 구체적으로 말해 주세요.'))}</p>
    </div>
    ${blocked && full ? `<p class="hint"><span class="hint-mark">⬡</span>
      컨베이어가 모두 찼습니다 — 하나를 원래 자리로 돌려놓으면 다시 이송할 수 있습니다.</p>`
      : hintAlready ? '' : `<p class="hint"><span class="hint-mark">⬡</span>${esc(SIM_PICK_PLACE_HINT)}</p>`}`;
}

export function renderVerdictCard(state) {
  const { validation } = state;
  const verdict = validation ? validation.verdict : null;
  // 확인 대기가 있으면 **그것이 지금의 실행 후보**다. 계획 판정보다 앞선다.
  // 근거(readiness)가 없으면 준비됐다고 말하지 않는다 — 서버가 그 값을 주지
  // 않는 구성에서 머리말만 바뀌고 본문이 비는 일이 없도록 한다.
  const pendingReady = Boolean(
    ((state.simDemo || {}).confirmation || {}).evidence
    && state.simDemo.confirmation.evidence.readiness);
  // 시뮬레이션 쪽 판단(ASK/BLOCK)이 지금의 답이면, 계획 생성 안내를 겹쳐
  // 띄우지 않는다 — 어느 것이 지금 판단인지 흐려진다.
  const simAnswered = Boolean(
    !state.plan && !pendingReady
    && ['ASK', 'BLOCK'].includes(((state.simDemo || {}).result || {}).decision));
  const headerPill = pendingReady
    ? pill('decide', '◈', '실행 준비됨')
    : verdict === VERDICT.PASS
      ? pill('success', '✓', 'PASS')
      : verdict === VERDICT.BLOCK
        ? pill('danger', '✕', 'BLOCK')
        : verdict === VERDICT.ASK
          ? pill('warning', '?', 'ASK')
          : pill('muted', '◉', '판단 없음');

  let body;
  if (simAnswered && !validation) {
    // 시뮬레이션 판단 구역이 답을 갖고 있다.
    body = '';
  } else if (pendingReady && !validation) {
    // 실행 후보가 이미 있다 — 계획 생성 안내를 겹쳐 띄우지 않는다.
    body = '';
  } else if (!validation) {
    body = `<div class="notice">
        <span class="notice-mark">⬡</span>
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
    // 자유 pick/place는 계속 막는다. 다만 **할 수 있는 길**을 알려 준다 —
    // A/B/C 팔레트↔컨베이어는 검증된 시뮬레이션 작업으로 실행할 수 있다.
    const pickPlaceBlocked = (validation.reasonCodes || []).some(
      (code) => String(code).startsWith('capability.'))
      || /집|놓|pick|place/i.test(String(validation.detail || ''));
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
      </div>
      ${pickPlaceBlocked ? `<div class="notice">
        <span class="notice-mark">⬡</span>
        <p><strong>${esc(SIM_PICK_PLACE_HINT)}</strong><br />
        A·B·C 자재를 팔레트와 컨베이어 사이로 옮기는 작업은 검증된 시뮬레이션
        작업으로 실행할 수 있습니다. 자유 집기·놓기 계획은 그대로 차단됩니다.</p>
      </div>` : ''}`;
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

  // 정지 래치는 **사람이 푼다.** 예전에는 "새 계획을 생성하면 풀립니다"라고만
  // 적었는데, 이 화면은 계획을 만들지 않아서 풀 길이 없었다(실측 2026-09-22).
  // 해제 단추를 여기 둔다 — 누르면 서버가 판단하고, 진행 중인 동작이 있으면
  // 거부한다. 화면이 스스로 래치를 걷지 않는다.
  const latchNotice = state.stopLatched
    ? `<div class="notice danger"><span class="notice-mark">■</span>
         <p><strong>전체 정지가 걸려 있습니다.</strong><br />
         멈춘 이유가 해결됐으면 정지를 해제하세요. 진행 중인 동작이 있으면
         해제되지 않습니다.</p></div>
       <button class="btn danger block" type="button" data-action="release-stop">
         ◇ 정지 해제</button>`
    : '';

  // 계획이 없고 시뮬레이션 실행 후보만 있으면, 계획용 실행 버튼을 띄우지
  // 않는다. 누를 수 없는 버튼과 "계획이 없습니다"가 준비 상태와 겹쳐 보인다.
  const planOnly = !((pendingReady || simAnswered) && !state.plan);
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
      ${renderSimReadiness(state)}
      ${renderSimDecision(state)}
      ${renderSimExecution(state)}
      ${body}
      ${latchNotice}
      ${planOnly ? `<button class="btn primary block" type="button"
        data-action="execute" ${executeDisabled}>▶ 실행 시작</button>` : ''}
      ${planOnly && reason ? `<p class="hint"><span class="hint-mark">ℹ</span>${esc(reason)}</p>` : ''}
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
               Gazebo 시뮬레이션 관측값입니다.</p>`
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
                   Gazebo 시뮬레이션 실행 결과입니다.</p>
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
  // 목업 위 줄의 로봇 선택과 명령 감지.
  set('robot-pick-slot', renderRobotPickHeader(state));
  const detectNode = document.getElementById('detect');
  if (detectNode) {
    const detectHtml = renderDetect(state);
    if (detectNode.innerHTML !== detectHtml) detectNode.innerHTML = detectHtml;
    if (typeof detectNode.classList !== 'undefined') {
      detectNode.classList.toggle('live', detectHtml.includes('pulse'));
    }
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
  // 영상 위 배지(실행 중 작업·전체 정지)와 자재 상태 띠는 canvas와 따로
  // 갱신한다 — 카드 전체를 다시 그리면 그려 둔 프레임이 사라진다.
  set('scene-badges', renderSceneBadges(state));
  set('scene-empty-slot', renderSceneEmpty(state));
  set('scene-strip', renderSceneStrip(state));
  set('card-workcell', renderWorkcellCard(state));
  set('card-voice', renderVoiceCard(state));
  set('card-events', renderEventsCard(state));
  // **타이핑 중인 textarea를 부수지 않는다.** 입력 이벤트마다 상태가 바뀌고
  // 다시 그리는데, 카드 전체의 innerHTML을 갈아 끼우면 textarea가 새 요소로
  // 바뀌어 포커스·커서·한글 조합이 끊긴다(실측: 글자가 안 쳐진다). textarea가
  // 이미 있으면 버튼 상태만 맞추고, 안내문 구성이 바뀔 때만 전체를 다시 그린다.
  // **타이핑 중인 textarea를 부수지 않는다.** 입력 이벤트마다 상태가 바뀌고
  // 다시 그리는데, 카드 전체의 innerHTML을 갈아 끼우면 textarea가 새 요소로
  // 바뀌어 포커스·커서·한글 조합이 끊긴다(실측: 글자가 안 쳐진다).
  //
  // 그래서 카드는 textarea가 없을 때 **한 번만** 그리고, 그 뒤로는 textarea를
  // 뺀 조각들(머리말·버튼 줄·전사·안내·명령 결과)만 각자 갈아 끼운다. 예전에는
  // "안내문 개수가 달라졌을 때만 전체를 다시 그린다"는 어림짐작을 썼는데, 그
  // 개수가 그대로면 조각이 낡은 채로 남았다 — 서버 연결 뒤에도 제목이 "작업
  // 명령"이고 STT가 붙어도 마이크 버튼이 잠겨 있었다(둘 다 실측).
  const commandNode = document.getElementById('card-command');
  const canQueryCommand = commandNode && typeof commandNode.querySelector === 'function';
  const liveInput = canQueryCommand ? commandNode.querySelector('#command-input') : null;
  if (!liveInput) {
    set('card-command', renderCommandCard(state, backendKind));
  } else if (typeof document.createElement === 'function') {
    const fresh = document.createElement('div');
    fresh.innerHTML = renderCommandCard(state, backendKind);
    ['.card-header', '#command-actions', '#command-transcript', '#command-note',
     '#command-foot'].forEach((selector) => {
      const live = commandNode.querySelector(selector);
      const next = fresh.querySelector ? fresh.querySelector(selector) : null;
      if (live && next && live.innerHTML !== next.innerHTML) live.innerHTML = next.innerHTML;
    });
  }
  set('sim-command-area', renderSimCommandArea(state));
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
