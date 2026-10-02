import { useCallback, useEffect, useState } from 'react';
import {
  alerts, commandLog, commandLogNote, engineMetrics, history, issues, metrics,
  robotDetails, robots, tool, toolHistory,
} from './data.js';
import SimView3D from './SimView3D.jsx';
import { simLabel } from './simLabel.js';
import { RESULT_LABELS, useSimCommand } from './simCommand.js';
import alertOctagon from './assets/alert-octagon.svg';
import avatar from './assets/avatar.jpg';
import bot from './assets/bot.svg';
import chartLine from './assets/chart-line.svg';
import checkSquare from './assets/check-square-2.svg';
import circleX from './assets/circle-x.svg';
import dotNetwork from './assets/dot-network.svg';
import dotPanel from './assets/dot-panel.svg';
import dotSimDanger from './assets/dot-sim-danger.svg';
import dotSimRunning from './assets/dot-sim-running.svg';
import dotSimStopping from './assets/dot-sim-stopping.svg';
import dividerDiagnostics from './assets/divider-diagnostics.svg';
import dividerSettings from './assets/divider-settings.svg';
import house from './assets/house.svg';
import loaderCircle from './assets/loader-circle.svg';
import logs from './assets/logs.svg';
import mic from './assets/mic.svg';
import settings from './assets/settings.svg';
import shieldCheck from './assets/shield-check.svg';

const NAV = [
  { id: 'home', label: '홈', icon: house, title: '종합 관제 대시보드' },
  { id: 'robots', label: '로봇 관리', icon: circleX, title: '로봇 세부 관리 및 기구 정보' },
  { id: 'history', label: '기록 및 로그', icon: logs, title: '명령 수행 이력 및 가상 안전 감사 로그' },
  { id: 'diagnostics', label: '실시간 진단', icon: chartLine, title: '미들웨어 및 서비스 자가 점검 시스템' },
  { id: 'settings', label: '관제 설정', icon: settings, title: '통합 관제 시스템 환경 설정' },
];

// 주소(#/robots 등)로 화면을 고른다 — 새로고침해도 같은 화면에 머문다.
function pageFromHash() {
  const id = window.location.hash.replace('#/', '');
  return NAV.some((item) => item.id === id) ? id : 'home';
}

function usePage() {
  const [page, setPage] = useState(pageFromHash);
  useEffect(() => {
    const onHash = () => setPage(pageFromHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  return page;
}

function timeText(date) {
  return [date.getHours(), date.getMinutes(), date.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
}

function useNow() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

export default function App() {
  // null(평소) | 'running'(안전 검사 중) | 'stopping'(검사 중 비상 정지) | 'danger'(위험 판정)
  const [sim, setSim] = useState(null);
  const [simAt, setSimAt] = useState(null);
  const cmd = useSimCommand();
  const now = useNow();
  const page = usePage();
  const nav = NAV.find((item) => item.id === page);

  const open = (state) => { setSim(state); setSimAt(new Date()); };

  // 헤더 비상 정지는 오버레이 밖이라 언제든 누를 수 있다(피그마 메모). 실행 중인 시연 작업에
  // 정지를 요청한다(/v1/sim-demo/stop). 오버레이의 '정지 확인' 화면은 아직 목업이다.
  function globalStop() {
    cmd.stop();
    if (sim === 'running') open('stopping');
  }

  return <div className="app">
    <Sidebar page={page} />
    <div className="main">
      <Header title={nav.title} onStop={globalStop} />
      <div className="body">
        <div className="col-main">
          {page === 'home' && <Home />}
          {page === 'robots' && <Robots />}
          {page === 'history' && <History />}
          {page === 'diagnostics' && <Diagnostics />}
          {page === 'settings' && <Settings />}
        </div>
        <div className="col-side">
          <CommandPanel now={now} sim={cmd} />
          {page === 'home' && <Alerts />}
        </div>
      </div>
    </div>
    {sim && <SimOverlay state={sim} at={simAt} onClose={() => setSim(null)} onRerun={() => open('running')} />}
    <DemoBar state={sim} onChange={(state) => (state ? open(state) : setSim(null))} />
  </div>;
}

function Sidebar({ page }) {
  return <aside className="sidebar">
    <div>
      <div className="brand">
        <span className="brand-mark"><img src={bot} alt="" width="18" height="18" /></span>
        <div><strong>FORSTICK</strong><small>CONTROL PANEL</small></div>
      </div>
      <nav className="nav">
        {NAV.map((item) => <a key={item.id} href={`#/${item.id}`} className={item.id === page ? 'nav-item active' : 'nav-item'} aria-current={item.id === page ? 'page' : undefined}>
          <span className={item.id === 'home' ? 'nav-icon inset' : 'nav-icon'}><img src={item.icon} alt="" /></span>{item.label}
        </a>)}
      </nav>
    </div>
    <div className="profile">
      <div className="profile-user"><img src={avatar} alt="" /><div><strong>김민우 조장</strong><small>제2공정 주간 근무</small></div></div>
      <span className="network"><img src={dotNetwork} alt="" width="6" height="6" />미들웨어 네트워크 정상</span>
    </div>
  </aside>;
}

function Header({ title, onStop }) {
  return <header className="header">
    <div className="header-title"><h1>{title}</h1><span className="chip-site">스마트 팩토리 B동</span></div>
    <div className="header-right">
      <span className="grade">실시간 공장 안전 등급: <b>안전</b></span>
      <span className="risk-badge">주의 · Beta</span>
      <button className="estop" onClick={onStop} title="실행 중인 시연 작업에 정지를 요청합니다"><img src={alertOctagon} alt="" width="20" height="20" />비상 정지</button>
    </div>
  </header>;
}

function Home() {
  return <>
      <MetricRow title="오늘의 핵심 운영 지표" items={metrics} />
      <section>
        <h2>실시간 로봇 작동 상태</h2>
        <div className="row3">
          {robots.map((r) => <div key={r.name} className="card robot">
            <div className="robot-head"><div><strong>{r.name}</strong><small>{r.meta}</small></div><span className={`state ${r.tone}`}>{r.state}</span></div>
            <div className="robot-task">
              <div><span>현재 작업</span><b>{r.task}</b></div>
              <div className="bar"><i className={r.progress ? '' : 'idle'} style={{ width: `${r.progress * 100}%` }} /></div>
            </div>
            <div className="robot-stats">
              <div><span>적재 중량</span><b>{r.load}</b></div>
              <div><span>관절 온도</span><b className={r.tempTone}>{r.temp}</b></div>
            </div>
          </div>)}
        </div>
      </section>
      <section>
        <h2>최근 지시 작업 진행 내역 (감사 이력)</h2>
        <div className="card table">
          <div className="tr th"><span>ID</span><span>수행 작업 내용</span><span>담당 로봇</span><span>안전 검증</span><span>상태</span></div>
          {history.map((h) => <div key={h.id} className="tr">
            <b>{h.id}</b><span className="muted">{h.task}</span><span>{h.robot}</span><b className={h.checkTone}>{h.check}</b><b className={h.statusTone}>{h.status}</b>
          </div>)}
        </div>
      </section>
  </>;
}

function MetricRow({ title, items, badgeSize }) {
  return <section>
    <h2>{title}</h2>
    <div className="row3">
      {items.map((m) => <div key={m.label} className="card metric">
        <span className="metric-label">{m.label}</span>
        <div className="metric-value"><strong>{m.value}{m.unit && <small>{m.unit}</small>}</strong><span className={`tag ${m.tone} ${badgeSize || ''}`}>{m.badge}</span></div>
      </div>)}
    </div>
  </section>;
}

function Alerts() {
  return <section>
    <div className="alerts-head"><h2>공정 실시간 안전 알림</h2><span>전체보기</span></div>
    <div className="alerts">
      {alerts.map((a) => <div key={a.text} className={`alert ${a.tone}`}>
        <i />
        <div>
          <div className="alert-head"><span><em className={`tag ${a.tone}`}>{a.level}</em><b>{a.robot}</b></span><small>{a.ago}</small></div>
          <p>{a.text}</p>
        </div>
      </div>)}
    </div>
  </section>;
}

function Robots() {
  return <>
    <section>
      <h2>실시간 운용 로봇 상태 비교</h2>
      <div className="row3">
        {robotDetails.map((r) => <div key={r.name} className={r.selected ? 'card robot selected' : 'card robot'}>
          <div className="robot-head"><div><strong>{r.name}</strong><small>{r.meta}</small></div><span className={`state ${r.tone}`}>{r.state}</span></div>
          <div className="robot-task stacked">
            <div><span>현재 공정 단계</span><b>{r.step}</b></div>
            <div className="bar"><i className={r.barTone} style={{ width: `${r.progress * 100}%` }} /></div>
          </div>
          <div className="robot-stats">
            <div><span>배터리 잔량</span><b className={r.batteryTone}>{r.battery}</b></div>
            <div><span>통신 감도</span><b className={r.signalTone}>{r.signal[0]}<br />{r.signal[1]}</b></div>
          </div>
        </div>)}
      </div>
    </section>
    <section>
      <h2>{tool.title}</h2>
      <div className="tool-row">
        <div className="card tool">
          <p className="info tool-kicker">현재 장착 도구 정보</p>
          <div><strong>{tool.name}</strong><small>{tool.model}</small></div>
          <div className="tool-pressure"><span className="muted">{tool.range}</span><span>{tool.measured}</span></div>
          <span className="tool-status">{tool.status}</span>
        </div>
        <div className="card table tool-table">
          <div className="tr th"><span>교체/점검 일시</span><span>엔드 이펙터 툴 종류</span><span>점검 내용 및 조치 내역</span><span>담당 작업자</span></div>
          {toolHistory.map((t) => <div key={t.at} className="tr"><span>{t.at}</span><span>{t.tool}</span><span className="muted">{t.note}</span><span>{t.by}</span></div>)}
        </div>
      </div>
    </section>
  </>;
}

function History() {
  return <>
    {/* 필터는 목업이다 — 실제 이력 조회 API가 연결되면 동작한다. */}
    <div className="card filter-bar">
      <span className="filter-label">날짜 검색</span><span className="filter-field">2024.10.20 ~ 2024.10.24</span>
      <span className="filter-label">담당 로봇</span><span className="filter-field">전체 로봇 ▾</span>
      <span className="filter-label">안전 검증 상태</span><span className="filter-field ok">PASS (안전) ▾</span>
      <button className="filter-run" disabled>필터 검색 실행</button>
    </div>
    <div className="card table log-table">
      <div className="tr th"><span>명령ID</span><span>입력된 자연어 지시 사항</span><span>최종 승인자</span><span>안전 시뮬레이션</span><span>가동률 보정</span><span>실행 상태</span></div>
      {commandLog.map((c) => <div key={c.id} className="tr">
        <b>{c.id}</b><span className="muted">{c.text}</span><span>{c.approver}</span><b className={c.simTone}>{c.sim}</b><span className={c.adjustTone}>{c.adjust}</span><b className={c.statusTone}>{c.status}</b>
      </div>)}
    </div>
    <p className="note warn">{commandLogNote}</p>
  </>;
}

function Diagnostics() {
  return <>
    <MetricRow title="관제 핵심 엔진 실시간 지수" items={engineMetrics} badgeSize="small" />
    <section>
      <h2>최근 시스템 이슈 진단 및 대응 매뉴얼</h2>
      <div className="card issues">
        {issues.map((issue, i) => <div key={issue.title} className="issue-wrap">
          {i > 0 && <img src={dividerDiagnostics} alt="" className="divider" />}
          <div className="issue">
            <div className="issue-head"><span><em className={`tag ${issue.tone}`}>{issue.tag}</em><b>{issue.title}</b></span><small>{issue.ago}</small></div>
            <p className="muted">{issue.body}</p>
            <div className="issue-action"><b className="info">현장 작업자 대응 권장 사항:</b><span>{issue.action}</span></div>
          </div>
        </div>)}
      </div>
    </section>
  </>;
}

function Settings() {
  // 설정 버튼은 목업이다. 3번은 피그마 v3 결정: 위험 옵션은 UI에서 제거하고 사유로 대체한다.
  return <>
    <section>
      <h2>1. 개인 프로필 및 알림 제어</h2>
      <div className="card setting">
        <div className="setting-row"><div><b>관제 근무자 권한 이메일 연동</b><small>minwoo.kim@forstick-factory.co.kr (제2공정 관리용 라이센스)</small></div><button className="setting-btn" disabled>계정 연동 변경</button></div>
        <img src={dividerSettings} alt="" className="divider" />
        <div className="setting-row"><div><b>실시간 비상 SMS 및 사이렌 브로드캐스트</b><small>레벨 3 안전 위협 발생 시 개인 무전기 및 스마트워치로 강제 사이렌 전송</small></div><span className="setting-on">SMS 상시 활성화</span></div>
      </div>
    </section>
    <section>
      <h2>2. 공정 효율 및 속도 가중치 정책</h2>
      <div className="card setting">
        <div className="setting-row"><div><b>금일 가동률 가속 드라이브 조정 지수</b><small>설정된 가동률(현재 94.2%)에 비례한 관절 구동 가속 한계 임계치 1.2배 상향 적용</small></div><button className="setting-btn" disabled>지수 설정 수정</button></div>
      </div>
    </section>
    <section>
      <h2 className="soft">3. 안전 검증 정책 (변경 불가)</h2>
      <div className="policy">
        <b>검증 무시·감속 해제 설정은 이 화면에서 제공하지 않습니다</b>
        <p>안전 검증을 통과한 계획만 실행한다는 원칙에 따라 충돌 회피 검증 생략과 안전 구역 감속 제한 해제는 설정으로 바꿀 수 없습니다.</p>
      </div>
    </section>
  </>;
}

// 서버 판정 → [톤, 표시]. 판정 코드는 서버 계약(server/routes/sim_demo.py) 그대로다.
const DECISIONS = {
  CONFIRM: ['info', '확인 필요'],
  CONFIRM_GOAL: ['info', '확인 필요 · 여러 단계'],
  RUN: ['ok', '실행'],
  STOP: ['danger', '정지 요청'],
  ASK: ['warn', '되묻기 · 실행 안 함'],
  BLOCK: ['danger', '차단 · 실행 안 함'],
  PASS_THROUGH: ['warn', '시연 명령 아님'],
  ENVIRONMENT: ['info', '환경 변경'],
  NOOP: ['info', '할 일 없음'],
  CANCELLED: ['warn', '취소됨'],
};

const stepText = (p) => `${p.material_label || p.material} · ${p.from_label || ''} → ${p.to_label || ''}`;

function CommandPanel({ now, sim }) {
  const [text, setText] = useState('');
  const { result, pending, job, goal } = sim;
  const [tone, label] = (result && DECISIONS[result.decision]) || ['info', result ? result.decision : ''];
  const remaining = sim.deadline ? Math.max(0, Math.ceil((sim.deadline - now.getTime()) / 1000)) : null;
  const expired = remaining === 0;
  const running = (job && job.status === 'running') || (goal && ['running', 'stopping'].includes(goal.status));
  const report = job && job.report;
  const [reportTone, resultLabel] = (report && RESULT_LABELS[report.status]) || ['warn', report ? report.status : ''];
  const resultTone = report ? reportTone : goal ? (goal.status === 'completed' ? 'ok' : goal.status === 'failed' ? 'danger' : 'warn') : 'warn';

  // 피그마 Light CommandPanel(23:2517)의 상태: 입력 → 해석·승인/되묻기/차단… → 실행 중 → 완료.
  // 입력 칸은 대기·되묻기에서만 보이고, 나머지는 결과 카드 하나가 그 자리를 쓴다.
  // 판정·문구는 서버 값이고, 여기서는 상태별 톤만 고른다.
  const phase = pending ? 'confirm' : running ? 'running' : (job || goal) ? 'done'
    : result && result.decision === 'ASK' ? 'ask' : (result || sim.error) ? 'other' : 'idle';
  const [statusTone, statusLabel] = {
    idle: ['muted', '명령 대기'],
    ask: ['warn', '추가 확인 필요'],
    confirm: ['warn', expired ? '확인 시간 만료' : `승인 대기 · ${remaining}초`],
    running: ['info', '실행 중'],
    done: [resultTone, '작업 종료'],
    other: [sim.error ? 'danger' : tone, sim.error ? '오류' : label || '오류'],
  }[phase];
  const steps = job ? (job.progress || []).map((p) => p.reached) : goal ? (goal.plan || []).map((p) => p.status === 'completed') : [];
  const percent = steps.length ? Math.round((steps.filter(Boolean).length / steps.length) * 100) : 0;
  const cardTone = { confirm: expired ? 'warn' : 'ok', running: 'info', done: resultTone, ask: 'warn', other: statusTone }[phase];

  function submit(event) {
    event.preventDefault();
    if (!sim.busy && text.trim()) { sim.send(text); setText(''); }
  }

  // 해석 줄: 서버가 준 요약·사유만 보인다.
  let interpretation = [];
  if (sim.error) interpretation = [sim.error];
  else if (pending) interpretation = [pending.summary, pending.evidence && pending.evidence.slot_label].filter(Boolean);
  else if (result && result.decision === 'PASS_THROUGH') interpretation = ['자재 이송·복귀·정지·이어서 명령이 아닙니다. 일반 계획 경로는 이 화면에 아직 연결되지 않았습니다.'];
  else if (result && result.decision === 'STOP') interpretation = ['시연 작업에 정지를 요청했습니다.'];
  else if (result) interpretation = [result.summary || (job && `${job.action_label || job.action} · ${job.slot_label || ''}`), result.reason].filter(Boolean);
  const [headline, ...details] = interpretation;

  const input = <form className="cmd-form" onSubmit={submit}>
    <div className="cmd-field">
      <textarea id="command-input" className="command-input" rows={phase === 'idle' ? 3 : 1} value={text}
        aria-label="자연어 명령" placeholder={phase === 'ask' ? '답을 입력해 다시 보내기' : '예: A 자재를 컨베이어로 옮겨줘'}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) submit(e); }} />
      <button type="button" className="mic-btn" disabled title="음성 입력은 아직 연결되지 않았습니다" aria-label="음성 입력(연결 안 됨)">
        <img src={mic} alt="" width="16" height="16" />
      </button>
    </div>
    {phase === 'idle' && <button type="submit" className="btn-primary send" disabled={sim.busy || !text.trim()}>{sim.busy ? '보내는 중…' : '보내기'}</button>}
  </form>;

  const progress = job ? (job.progress || []).map((p) => ({ key: p.no, done: p.reached, text: `${p.no}/${p.of} ${p.label}` }))
    : goal ? (goal.plan || []).map((p) => ({ key: p.step, done: p.status === 'completed', text: `${stepText(p)} · ${p.status}` })) : [];

  return <section className="command">
    <div className="command-head"><span><img src={dotPanel} alt="" width="8" height="8" />통합 작업 명령 패널</span><small className={`state-label ${statusTone}`}>{statusLabel}</small></div>
    {phase === 'idle' ? <div className="step step-input">{input}</div>
      : <div className={`step cmd-card ${cardTone}`}>
        {phase === 'running' || phase === 'done'
          ? <img src={shieldCheck} alt="" width="16" height="16" />
          : <span className={`tag ${cardTone}`}>{phase === 'confirm' && expired ? '확인 시간 만료' : sim.error ? '오류' : label}</span>}
        {headline && <b className="cmd-title">{headline}</b>}
        {details.map((line) => <small key={line} className="cmd-reason">{line}</small>)}
        {pending && pending.kind === 'goal' && <ul className="plan">
          {(pending.plan || []).map((p) => <li key={p.step}><img src={loaderCircle} alt="" width="14" height="14" />{stepText(p)}</li>)}
        </ul>}
        {progress.length > 0 && <ul className="plan">
          {progress.map((p) => <li key={p.key} className={p.done ? 'done' : ''}><img src={p.done ? checkSquare : loaderCircle} alt="" width="14" height="14" />{p.text}</li>)}
        </ul>}
        {running && <div className="run-bar" role="progressbar" aria-valuenow={percent} aria-valuemin="0" aria-valuemax="100"><i style={{ width: `${percent}%` }} /></div>}
        {running && <small className="info">Gazebo에서 실행 중…</small>}
        {phase === 'done' && job && <b className={resultTone}>{resultLabel || `종료 코드 ${job.exit_code}`}</b>}
        {phase === 'done' && goal && !job && <small>목표 상태: {goal.status}</small>}
        {phase === 'ask' && input}
        {phase === 'confirm' && <div className="approve-row">
          <button className="approve" disabled={sim.busy || expired} onClick={() => sim.answer('confirm')}><span className="icon-play" aria-hidden="true" />{pending.kind === 'goal' ? '전체 실행 승인' : '실행 승인'}</button>
          {!expired && <button className="approve-cancel" disabled={sim.busy} onClick={() => sim.answer('cancel')}>취소</button>}
        </div>}
        {(phase === 'done' || phase === 'other' || (phase === 'confirm' && expired)) &&
          <button className="btn-secondary" onClick={sim.reset}>{phase === 'other' && result && result.decision === 'BLOCK' ? '명령 수정' : '새 명령 입력'}</button>}
      </div>}
    <div className="decide">
      {phase === 'confirm' && !expired && <p className="muted">{remaining}초 안에 승인하지 않으면 취소됩니다</p>}
      {phase === 'confirm' && expired && <p>확인 시간이 지났습니다 — 명령을 다시 보내 주세요</p>}
      {phase === 'running' && <p className="muted">정지는 확인 없이 즉시 요청됩니다</p>}
      {phase === 'done' && <p className="muted">다음 명령을 입력해 주세요</p>}
      {sim.stopNote && <p className={sim.stopNote.tone} role="status">{sim.stopNote.text}</p>}
    </div>
  </section>;
}

const STAGES = ['명령·범위 확인', '로봇 상태 확인', '안전 검증'];

function SimOverlay({ state, at, onClose, onRerun }) {
  const [scene, setScene] = useState({ mode: '3d', status: 'connecting', fps: 0 });
  const onView = useCallback((view) => setScene(view), []);
  const time = at ? timeText(at) : '';
  const card = {
    running: {
      dot: dotSimRunning, title: '안전 검사 실행 중', titleTone: '', sub: simLabel(scene),
      section: '검사 세부 진행',
      stages: [{ mark: '1', tone: 'done' }, { mark: '2', tone: 'current' }, { mark: '3', tone: 'wait' }],
      lines: ['done', 'wait'],
      foot: `1/3 단계 완료 · 마지막 갱신 ${time}   ·   이 창이 열려 있어도 헤더의 비상 정지는 언제든 즉시 실행됩니다.`,
    },
    stopping: {
      dot: dotSimStopping, title: '비상 정지 처리됨', titleTone: 'danger', sub: '검사가 중단되었습니다 · 이전 검증 결과는 재사용되지 않습니다',
      section: '비상 정지 처리 흐름',
      stages: [{ mark: '✓', tone: 'requested', label: '비상 정지 요청됨' }, { mark: '✓', tone: 'confirmed', label: '정지 확인' }],
      lines: ['danger'],
      foot: `정지 확인 · ${time}   ·   재개하려면 명령을 다시 확인해야 합니다. 헤더의 비상 정지는 계속 즉시 실행됩니다.`,
    },
    danger: {
      dot: dotSimDanger, title: '안전 검사 결과 — 실행 불가', titleTone: 'danger', sub: '검증 완료 · 위험 감지',
      section: '안전 검사 완료 · 위험 판정',
      stages: [{ mark: '✓', tone: 'done' }, { mark: '✓', tone: 'current' }, { mark: '!', tone: 'danger', label: '안전 검증 (위험)' }],
      lines: ['done', 'danger'],
      foot: '3/3 완료 · 위험 판정으로 실행이 차단되었습니다   ·   헤더의 비상 정지는 언제든 즉시 실행됩니다.',
    },
  }[state];
  return <div className="scrim">
    <section className={`sim-card ${state}`} role="dialog" aria-label={card.title}>
      <div className="sim-head">
        <img src={card.dot} alt="" width="8" height="8" />
        <div><strong className={card.titleTone}>{card.title}</strong><small>{card.sub}</small></div>
        {state === 'running' && <button className="sim-cancel" onClick={onClose}><b>안전 검사 취소</b><small>검사만 중단 · 로봇 동작에는 영향 없음</small></button>}
      </div>
      <div className="sim-video">
        {state === 'running' && <SimView3D onView={onView} />}
        {state === 'stopping' && <p className="sim-message">로봇이 현재 위치에서 정지했습니다<br />(정지 확인됨 · 이 명령의 이전 검증 결과는 폐기되었습니다)</p>}
        {/* 위험 판정: 영상 칸은 시뮬레이션 화면 자리다 — 문구를 두지 않는다(피그마 결정 2026-10-02). */}
        {state === 'danger' && <SimView3D onView={onView} />}
      </div>
      <hr />
      <p className="sim-section">{card.section}</p>
      <div className="stages">
        {card.stages.map((s, i) => <div key={i} className="stage-wrap">
          {i > 0 && <i className={`stage-line ${card.lines[i - 1]}`} />}
          <div className="stage"><span className={`stage-mark ${s.tone}`}>{s.mark}</span><small className={s.tone === 'wait' ? 'muted' : s.tone === 'danger' ? 'danger' : ''}>{s.label || STAGES[i]}</small></div>
        </div>)}
      </div>
      <p className="sim-foot">{card.foot}</p>
      {/* 위험 판정에서는 판정을 덮어쓰는 승인 버튼을 두지 않는다(피그마 메모). */}
      {state === 'danger' && <div className="sim-actions"><button onClick={onClose}>명령 수정</button><button onClick={onRerun}>검사 재실행</button></div>}
    </section>
  </div>;
}

/** 목업 상태 전환 도구. 실제 흐름(명령 → 계획 → 검사)이 백엔드와 연결되기 전까지 오버레이
 *  상태를 확인하기 위한 것이다. */
function DemoBar({ state, onChange }) {
  const options = [[null, '기본'], ['running', '안전 검사 중'], ['stopping', '정지 확인'], ['danger', '위험 판정']];
  return <div className="demo-bar">
    <span>DEMO · 목업 데이터 (시뮬레이션 화면·명령 패널만 실제 Gazebo)</span>
    {options.map(([value, label]) => <button key={label} className={state === value ? 'on' : ''} onClick={() => onChange(value)}>{label}</button>)}
  </div>;
}
