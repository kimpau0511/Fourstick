import { useEffect, useMemo, useState } from 'react';
import { NavLink, Route, Routes, useNavigate, useSearchParams } from 'react-router-dom';
import { alertsSeed, historySeed, robots as robotSeed, routinesSeed, services, usersSeed } from './data.js';

const STATUS = {
  normal: { label: '정상', icon: '●', tone: 'ok' },
  notice: { label: '알림', icon: '◆', tone: 'info' },
  warning: { label: '경고', icon: '▲', tone: 'warn' },
  critical: { label: '위험', icon: '■', tone: 'danger' },
  noData: { label: '데이터 없음', icon: '?', tone: 'muted' },
};

const VALIDATION = {
  safe: ['안전 확인됨', 'ok'],
  blocked: ['실행 차단', 'danger'],
  question: ['추가 확인 필요', 'warn'],
};

const EXECUTION = {
  success: ['성공', 'ok'],
  failed: ['실패', 'danger'],
  stop: ['STOP', 'danger'],
  none: ['해당 없음', 'muted'],
};

const SCENARIOS = [
  ['normal', '정상 대기'],
  ['robot-notice', '로봇 알림'],
  ['robot-warning', '로봇 경고'],
  ['robot-critical', '로봇 위험'],
  ['no-data', '데이터 없음'],
  ['processing', '명령 처리 중'],
  ['question', '질문 발생'],
  ['blocked', '차단 발생'],
  ['review', '실행 전 최종점검'],
  ['expired', '검증 결과 만료'],
  ['video-lost', '영상 끊김'],
  ['executing', '실행 중'],
  ['stopped', 'STOP 발생'],
  ['completed', '실행 완료'],
  ['emergency', '긴급 다중 발생'],
];

function Icon({ name, size = 20 }) {
  const paths = {
    overview: <><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></>,
    robot: <><rect x="4" y="10" width="16" height="10" rx="3"/><path d="M8 10V7a4 4 0 0 1 8 0v3M8 15h.01M16 15h.01M9 20v2M15 20v2"/></>,
    history: <><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/></>,
    diagnostics: <><path d="M4 14h3l2-7 4 12 2-8 2 3h3"/><path d="M4 4h16v16H4z"/></>,
    settings: <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></>,
    mic: <><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8"/></>,
    stop: <rect x="5" y="5" width="14" height="14" rx="2"/>,
    chevron: <path d="m9 18 6-6-6-6"/>,
    shield: <path d="M12 3 5 6v5c0 5 3.2 8.3 7 10 3.8-1.7 7-5 7-10V6l-7-3Z"/>,
    download: <><path d="M12 3v12m0 0 4-4m-4 4-4-4"/><path d="M4 19h16"/></>,
    search: <><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></>,
    alert: <><path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v4m0 3h.01"/></>,
  };
  return <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

function Badge({ tone = 'neutral', icon, children, title }) {
  return <span className={`badge badge-${tone}`} title={title}>{icon && <span className="badge-icon">{icon}</span>}{children}</span>;
}

function PageHeader({ eyebrow, title, description, actions }) {
  return <header className="page-header">
    <div><p className="eyebrow">{eyebrow}</p><h1>{title}</h1><p>{description}</p></div>
    {actions && <div className="page-actions">{actions}</div>}
  </header>;
}

function Empty({ children }) {
  return <div className="empty-state">{children}</div>;
}

function Dialog({ title, children, onClose, footer }) {
  return <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
    <section className="dialog" role="dialog" aria-modal="true" aria-label={title} onMouseDown={(e) => e.stopPropagation()}>
      <header><h2>{title}</h2><button className="icon-button" onClick={onClose} aria-label="닫기">×</button></header>
      <div className="dialog-body">{children}</div>
      {footer && <footer>{footer}</footer>}
    </section>
  </div>;
}

function App() {
  const [role, setRole] = useState('admin');
  const [online, setOnline] = useState(true);
  const [scenario, setScenario] = useState('normal');
  const [emergencyAcknowledged, setEmergencyAcknowledged] = useState(false);
  const [alerts, setAlerts] = useState(alertsSeed);
  const [adminStops, setAdminStops] = useState({});
  const [panelWidth, setPanelWidth] = useState(390);
  const scenarioEmergency = scenario === 'emergency';

  useEffect(() => {
    if (scenarioEmergency) setEmergencyAcknowledged(false);
  }, [scenarioEmergency]);

  const robotScenario = scenarioEmergency ? 'emergency' : ['robot-notice', 'robot-warning', 'robot-critical', 'no-data'].includes(scenario) ? scenario : null;
  const robots = useMemo(() => robotSeed.map((robot, index) => {
    if (!robotScenario || (index !== 0 && robotScenario !== 'emergency')) return { ...robot, adminStopped: Boolean(adminStops[robot.id]) };
    const status = { 'robot-notice': 'notice', 'robot-warning': 'warning', 'robot-critical': 'critical', 'no-data': 'noData', emergency: 'critical' }[robotScenario];
    return {
      ...robot,
      status,
      available: !['robot-critical', 'no-data', 'emergency'].includes(robotScenario),
      lastReceived: robotScenario === 'no-data' ? '18초 전' : robot.lastReceived,
      safety: robotScenario === 'robot-critical' || robotScenario === 'emergency' ? (index === 0 ? { eStop: true, protectiveStop: false, guard: true } : { eStop: false, protectiveStop: true, guard: true }) : robot.safety,
      adminStopped: Boolean(adminStops[robot.id]),
    };
  }), [robotScenario, adminStops]);

  function startResize(event) {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = panelWidth;
    const move = (e) => setPanelWidth(Math.min(540, Math.max(330, startWidth + startX - e.clientX)));
    const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  return <Routes>
    <Route path="*" element={<div className="app-shell" style={{ '--command-width': `${panelWidth}px` }}>
    <Sidebar robots={robots} />
    <div className="workspace">
      <Topbar role={role} setRole={setRole} online={online} setOnline={setOnline} scenario={scenario} setScenario={setScenario} />
      {!online && <div className="connection-banner"><Icon name="alert" /> <strong>연결 끊김</strong><span>18초 전 데이터입니다. 동기화 전까지 새 명령과 실행이 잠겼습니다.</span></div>}
      {scenarioEmergency && <EmergencyBanner collapsed={emergencyAcknowledged} onAcknowledge={() => setEmergencyAcknowledged(true)} />}
      <main className="main-content">
        <Routes>
          <Route path="/" element={<Overview robots={robots} alerts={alerts} setAlerts={setAlerts} />} />
          <Route path="/robots" element={<RobotManagement robots={robots} role={role} adminStops={adminStops} setAdminStops={setAdminStops} />} />
          <Route path="/history" element={<History />} />
          <Route path="/diagnostics" element={<Diagnostics />} />
          <Route path="/settings" element={<Settings role={role} />} />
          <Route path="*" element={<Overview robots={robots} alerts={alerts} setAlerts={setAlerts} />} />
        </Routes>
      </main>
    </div>
    <div className="panel-resizer" role="separator" aria-label="명령 패널 너비 조절" onPointerDown={startResize} />
    <CommandPanel robots={robots} scenario={scenario} setScenario={setScenario} online={online} emergency={scenarioEmergency} />
    </div>} />
  </Routes>;
}

function Sidebar({ robots }) {
  const navigate = useNavigate();
  const nav = [
    ['/', 'overview', '현황'],
    ['/robots', 'robot', '로봇 관리'],
    ['/history', 'history', '기록'],
    ['/diagnostics', 'diagnostics', '진단'],
    ['/settings', 'settings', '설정'],
  ];
  const available = robots.filter((robot) => robot.available && !robot.adminStopped).length;
  return <aside className="sidebar">
    <div className="brand"><div className="brand-mark">F</div><div><strong>FORSTICK</strong><span>CONTROL BOARD</span></div></div>
    <nav aria-label="주 메뉴">
      {nav.map(([to, icon, label]) => <NavLink key={to} to={to} end={to === '/'} className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>
        <Icon name={icon} /><span>{label}</span>{label === '로봇 관리' && <em>가동 {available}/{robots.length}</em>}
      </NavLink>)}
    </nav>
    <section className="system-widget" aria-label="시스템 상태">
      <div className="widget-title"><span>System status</span><Badge tone="warn" icon="▲">3/4 정상</Badge></div>
      {[
        ['ROS 2', '정상', 'ok', 'ros2'],
        ['PLANNER', '지연', 'warn', 'planner'],
        ['SAFETY PLC', '정상', 'ok', 'safety'],
        ['LATENCY', '42 ms', 'ok', 'latency'],
      ].map(([name, value, tone, id]) => <button key={name} onClick={() => navigate(`/diagnostics?service=${id}`)}>
        <span><i className={`dot ${tone}`} />{name}</span><strong>{value}</strong><Icon name="chevron" size={15} />
      </button>)}
    </section>
  </aside>;
}

function Topbar({ role, setRole, online, setOnline, scenario, setScenario }) {
  const navigate = useNavigate();
  const roleLabel = { operator: '김현우 작업자', admin: '이지민 관리자', safety: '박서준 안전관리자' }[role];
  return <header className="topbar">
    <div className="freshness"><i className={`dot ${online ? 'ok' : 'danger'}`} /><span>{online ? '실시간 연결 · 방금 동기화' : '오프라인 · 18초 전 데이터'}</span></div>
    <div className="demo-controls" aria-label="발표용 시나리오 도구">
      <span className="demo-label">DEMO</span>
      <label>역할<select value={role} onChange={(e) => setRole(e.target.value)}><option value="operator">작업자</option><option value="admin">관리자</option><option value="safety">안전관리자</option></select></label>
      <label>시나리오<select value={scenario} onChange={(e) => setScenario(e.target.value)}>{SCENARIOS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <button className="connection-toggle" onClick={() => setOnline((v) => !v)}>{online ? '연결 끊기' : '재연결'}</button>
    </div>
    {role !== 'operator' && <button className="approval-notice" onClick={() => navigate('/settings?section=safety')}><span>◆</span> 승인 필요 1</button>}
    <div className="user-block"><span>안산 1공장</span><strong>{roleLabel}</strong><div className="avatar">{roleLabel.slice(0, 2)}</div></div>
  </header>;
}

function EmergencyBanner({ collapsed, onAcknowledge }) {
  return <section className={`emergency-banner ${collapsed ? 'collapsed' : ''}`} role="alert">
    <div className="emergency-count"><span className="emergency-symbol">!</span><strong>긴급 2건</strong></div>
    {!collapsed && <><div><strong>포장 셀 · 비상정지 작동</strong><span>안전회로가 복구되기 전까지 명령을 실행할 수 없습니다.</span></div><button onClick={onAcknowledge}>인지 확인 후 축소</button></>}
    {collapsed && <><span>포장 셀 비상정지 외 1건 · 미해결</span><button onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}>상세 보기</button></>}
  </section>;
}

function Overview({ robots, alerts, setAlerts }) {
  const updateAlert = (id) => setAlerts((items) => items.map((item) => {
    if (item.id !== id) return item;
    const next = { 발생: '확인', 확인: '조치중', 조치중: '해결', 해결: '해결' }[item.state];
    return { ...item, state: next, assignee: next === '발생' ? item.assignee : '이지민' };
  }));
  return <div className="page overview-page">
    <PageHeader eyebrow="Operations overview" title="현황" description="로봇의 현재 작업과 안전 상태를 한눈에 확인합니다." actions={<div className="header-stat"><span>가동 현황</span><strong>{robots.filter((r) => r.available && !r.adminStopped).length} / {robots.length}</strong></div>} />
    <section className="robot-grid" aria-label="로봇 상태">
      {robots.map((robot) => <RobotCard key={robot.id} robot={robot} />)}
    </section>
    <section className="section-block alerts-section">
      <div className="section-heading"><div><h2>알림 및 조치</h2><p>경고 이상 항목은 해결될 때까지 상태와 담당자를 추적합니다.</p></div><Badge tone="warn" icon="▲">미해결 {alerts.filter((a) => a.level !== 'task' && a.state !== '해결').length}건</Badge></div>
      <div className="alert-list">
        {alerts.filter((alert) => alert.level !== 'task').map((alert) => <article className="alert-row" key={alert.id}>
          <div className="alert-level warn"><span>▲</span></div>
          <div className="alert-copy"><strong>{alert.title}</strong><span>{alert.detail}</span><small>권고 조치: {alert.action}</small></div>
          <div className="alert-meta"><span>{alert.elapsed} 경과</span><strong>{alert.assignee}</strong></div>
          <div className="lifecycle" aria-label={`현재 상태 ${alert.state}`}>
            {['발생', '확인', '조치중', '해결'].map((state) => <span key={state} className={state === alert.state ? 'current' : ''}>{state}</span>)}
          </div>
          {alert.state !== '해결' && <button className="button secondary" onClick={() => updateAlert(alert.id)}>{alert.state === '발생' ? '확인했습니다' : alert.state === '확인' ? '조치 시작' : '해결 처리'}</button>}
        </article>)}
        {alerts.filter((a) => a.level !== 'task').length === 0 && <Empty>현재 확인할 경고가 없습니다.</Empty>}
      </div>
    </section>
  </div>;
}

function RobotCard({ robot }) {
  const state = STATUS[robot.status] || STATUS.normal;
  const activeSafety = [robot.safety.eStop && '비상정지', robot.safety.protectiveStop && '보호정지', robot.safety.guard && '가드 작동중'].filter(Boolean);
  const summary = robot.safety.eStop ? '정지됨 · 비상정지' : robot.safety.protectiveStop ? '정지됨 · 보호정지' : activeSafety.length ? '가드 작동중' : null;
  return <article className={`robot-card status-${state.tone}`}>
    <header>
      <div><p>{robot.location}</p><h2>{robot.alias}</h2><span>{robot.duty}</span></div>
      <Badge tone={state.tone} icon={state.icon}>{state.label}</Badge>
    </header>
    {summary && <div className={`safety-summary ${robot.safety.eStop ? 'danger' : 'warn'}`}><Icon name="shield" /><strong>{summary}</strong>{activeSafety.length > 1 && <span>외 {activeSafety.length - 1}건</span>}</div>}
    {robot.status === 'noData' && <div className="stale-notice"><strong>마지막 수신: {robot.lastReceived}</strong><span>새 명령과 실행이 잠겼습니다.</span></div>}
    <div className="robot-work">
      <div><span>현재 작업</span><strong>{robot.skill}</strong></div>
      {robot.progress > 0 && <div className="progress-wrap"><div className="progress-label"><span>{robot.currentTask}</span><strong>{robot.progress}%</strong></div><div className="progress"><i style={{ width: `${robot.progress}%` }} /></div></div>}
    </div>
    <dl className="robot-facts">
      <div><dt>운전 모드</dt><dd>{robot.mode}</dd></div><div><dt>제어권</dt><dd>{robot.owner}</dd></div><div><dt>그리퍼</dt><dd>{robot.gripper}</dd></div>
    </dl>
    <div className="safety-grid" aria-label="안전 상태">
      <SafetyCell label="비상정지" active={robot.safety.eStop} severity="danger" />
      <SafetyCell label="보호정지" active={robot.safety.protectiveStop} severity="warn" />
      <SafetyCell label="가드" active={robot.safety.guard} severity="info" activeText="작동중" />
    </div>
    <footer><div className="temperature-status"><span>관절 온도</span><Badge tone={robot.temperature.level === 'warning' ? 'warn' : 'ok'} icon={robot.temperature.level === 'warning' ? '▲' : '●'}>{robot.temperature.label}{robot.temperature.level !== 'normal' && ` · ${robot.temperature.value} / ${robot.temperature.limit}`}</Badge>{robot.temperature.level !== 'normal' && <i className="temperature-meter" style={{ '--temperature': `${Math.min(100, parseFloat(robot.temperature.value) / parseFloat(robot.temperature.limit) * 100)}%` }} aria-hidden="true" />}</div><div><span>부하</span><Badge tone="ok" icon="●">정상</Badge></div></footer>
  </article>;
}

function SafetyCell({ label, active, severity, activeText = '작동' }) {
  return <div className={active ? `active ${severity}` : ''}><span>{active ? (severity === 'danger' ? '■' : severity === 'warn' ? '▲' : '◆') : '○'}</span><small>{label}</small><strong>{active ? activeText : '해제'}</strong></div>;
}

function RobotManagement({ robots, role, adminStops, setAdminStops }) {
  const [selectedId, setSelectedId] = useState(robots[0].id);
  const [tab, setTab] = useState('overview');
  const [dialog, setDialog] = useState(null);
  const [reason, setReason] = useState('');
  const selected = robots.find((robot) => robot.id === selectedId) || robots[0];
  const isAdmin = role === 'admin' || role === 'safety';
  const isStopped = Boolean(adminStops[selected.id]);
  const checks = [
    ['안전 상태', !selected.safety.eStop && !selected.safety.protectiveStop, selected.safety.eStop ? '비상정지 작동' : selected.safety.protectiveStop ? '보호정지 작동' : '이상 없음'],
    ['연결 상태', selected.status !== 'noData', selected.status === 'noData' ? '데이터 수신 없음' : '정상 연결'],
    ['도구 적합성', true, `${selected.tool} 확인됨`],
  ];
  const canResume = checks.every(([, pass]) => pass);
  function confirmAction() {
    if (dialog === 'stop' && !reason.trim()) return;
    setAdminStops((current) => ({ ...current, [selected.id]: dialog === 'stop' }));
    setDialog(null);
    setReason('');
  }
  return <div className="page">
    <PageHeader eyebrow="Robot registry" title="로봇 관리" description="현재 구성과 운용 가능 여부, 변경 이력을 확인합니다." />
    <div className="master-detail">
      <section className="master-list" aria-label="로봇 목록">
        <div className="list-caption"><strong>등록 로봇</strong><span>{robots.length}대</span></div>
        {robots.map((robot) => <button key={robot.id} className={`robot-list-row ${robot.id === selected.id ? 'selected' : ''}`} onClick={() => setSelectedId(robot.id)}>
          <div><strong>{robot.alias}</strong><span>{robot.location}</span></div>
          <div><small>{robot.model}</small><Badge tone={robot.available && !robot.adminStopped ? 'ok' : 'danger'} icon={robot.available && !robot.adminStopped ? '●' : '■'}>{robot.adminStopped ? '새 작업 차단' : robot.available ? '운용 가능' : '운용 불가'}</Badge></div>
        </button>)}
      </section>
      <section className="detail-panel">
        <header className="robot-detail-head">
          <div><p>{selected.location}</p><h2>{selected.alias}</h2><span>{selected.model} · {selected.code}</span></div>
          <div className="admin-action-zone">
            <span>관리 액션 · 실행 중 동작을 즉시 멈추지 않음</span>
            {isAdmin && <button className={`button ${isStopped ? 'primary' : 'administrative'}`} onClick={() => setDialog(isStopped ? 'resume' : 'stop')}>{isStopped ? '재개 승인' : '새 작업 차단'}</button>}
            {!isAdmin && <Badge tone="neutral">조회 전용</Badge>}
          </div>
        </header>
        <div className="robot-summary-grid">
          <div><span>운용 판정</span><strong><Badge tone={selected.available && !isStopped ? 'ok' : 'danger'} icon={selected.available && !isStopped ? '●' : '■'}>{isStopped ? '관리자에 의해 새 작업 차단' : selected.available ? '운용 가능' : '운용 불가'}</Badge></strong></div>
          <div><span>현재 도구</span><strong>{selected.tool}</strong></div>
          <div><span>현재 셀</span><strong>{selected.location}</strong></div>
          <div><span>가능한 작업</span><strong>{selected.skills.join(' · ')}</strong></div>
        </div>
        <div className="tabs" role="tablist">
          {[['overview', '개요'], ['tools', '도구 장착 이력'], ['profiles', '프로파일 버전']].map(([id, label]) => <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>{label}</button>)}
        </div>
        {tab === 'overview' && <div className="tab-content split-info">
          <section><h3>기본 정보</h3><dl className="definition-list"><div><dt>담당 작업</dt><dd>{selected.duty}</dd></div><div><dt>운전 모드</dt><dd>{selected.mode}</dd></div><div><dt>제어권</dt><dd>{selected.owner}</dd></div><div><dt>마지막 수신</dt><dd>{selected.lastReceived}</dd></div></dl></section>
          <section><h3>운용 가능 조건</h3>{checks.map(([name, pass, detail]) => <div className="check-row" key={name}><span className={pass ? 'check-pass' : 'check-fail'}>{pass ? '✓' : '!'}</span><div><strong>{name}</strong><small>{detail}</small></div></div>)}</section>
        </div>}
        {tab === 'tools' && <HistoryTable columns={['도구', '장착 시각', '해제 시각', '작업자', '변경 사유']} rows={selected.id === 'robot-ur5e' ? [['진공 그리퍼 VG10', '2026.09.10 08:42', '현재 사용 중', '이지민', 'A형 상자 공정 전환'], ['2지 평행 그리퍼', '2026.08.21 13:10', '2026.09.10 08:40', '박서준', '정기 작업 변경']] : [['2지 평행 그리퍼', '2026.09.02 09:15', '현재 사용 중', '이지민', '조립 부품 공급 공정']]}/>} 
        {tab === 'profiles' && (isAdmin ? <HistoryTable columns={['버전', '적용일', '변경자', '상태', '변경 요약']} rows={[['현재 설정 v4', '2026.09.10', '박서준', '사용 중', '가반하중 검증 범위 보완'], ['이전 설정 v3', '2026.08.21', '이지민', '보관됨', '그리퍼 좌표 보정']] } expandable /> : <Empty>프로파일 버전 이력은 관리자만 조회할 수 있습니다.</Empty>)}
      </section>
    </div>
    {dialog && <Dialog title={dialog === 'stop' ? '새 작업을 차단하시겠습니까?' : '로봇 운용을 재개하시겠습니까?'} onClose={() => setDialog(null)} footer={<><button className="button ghost" onClick={() => setDialog(null)}>취소</button><button className={`button ${dialog === 'stop' ? 'administrative' : 'primary'}`} disabled={dialog === 'stop' ? !reason.trim() : !canResume} onClick={confirmAction}>{dialog === 'stop' ? '새 작업 차단' : '재개 승인'}</button></>}>
      <div className="impact-card"><dl><div><dt>로봇</dt><dd>{selected.alias}</dd></div><div><dt>위치</dt><dd>{selected.location}</dd></div><div><dt>현재 작업</dt><dd>{selected.currentTask}</dd></div><div><dt>제어권자</dt><dd>{selected.owner}</dd></div></dl><p>{dialog === 'stop' ? '새로운 명령만 차단됩니다. 실행 중인 로봇을 즉시 멈추려면 명령패널의 즉시 정지를 사용하세요.' : '재개 승인 후에도 작업은 자동으로 시작되지 않습니다.'}</p></div>
      {dialog === 'stop' ? <label className="field"><span>차단 사유 <b>필수</b></span><textarea value={reason} onChange={(e) => setReason(e.target.value)} placeholder="예: 그리퍼 정기점검 예정" /></label> : <div className="resume-checks"><p>방금 다시 확인했습니다 · 14:36:02</p>{checks.map(([name, pass, detail]) => <div key={name}><span className={pass ? 'check-pass' : 'check-fail'}>{pass ? '✓' : '!'}</span><strong>{name}</strong><small>{detail}</small></div>)}</div>}
    </Dialog>}
  </div>;
}

function HistoryTable({ columns, rows, expandable = false }) {
  const [expanded, setExpanded] = useState(null);
  return <div className="table-wrap tab-content"><table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => [<tr key={`row-${rowIndex}`} className={expandable ? 'clickable' : ''} onClick={() => expandable && setExpanded(expanded === rowIndex ? null : rowIndex)}>{row.map((cell, index) => <td key={index}>{cell}</td>)}</tr>, expandable && expanded === rowIndex ? <tr key={`diff-${rowIndex}`} className="diff-row"><td colSpan={columns.length}><strong>이전 버전과 달라진 내용</strong><div><span>허용 가반하중</span><del>4.5 kg</del><ins>4.0 kg</ins></div><div><span>충돌검사 여유거리</span><del>90 mm</del><ins>110 mm</ins></div></td></tr> : null])}</tbody></table></div>;
}

function History() {
  const [query, setQuery] = useState('');
  const [validation, setValidation] = useState('all');
  const [execution, setExecution] = useState('all');
  const [robot, setRobot] = useState('all');
  const [selected, setSelected] = useState(historySeed[0]);
  const executionDisabled = validation === 'blocked';
  useEffect(() => { if (executionDisabled) setExecution('all'); }, [executionDisabled]);
  const filtered = historySeed.filter((item) => {
    const q = query.toLowerCase();
    return (!q || `${item.command} ${item.id}`.toLowerCase().includes(q)) && (validation === 'all' || item.validation === validation) && (execution === 'all' || item.execution === execution) && (robot === 'all' || item.robotId === robot);
  });
  return <div className="page">
    <PageHeader eyebrow="Command history" title="기록" description="명령이 계획·검증·실행으로 이어진 전 과정을 추적합니다." />
    <section className="filter-panel">
      <div className="search-field"><Icon name="search" size={18}/><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="명령 내용 또는 명령 ID 검색" /></div>
      <FilterGroup title="검증 판정" value={validation} onChange={setValidation} options={[['all', '전체'], ['safe', '안전 확인됨'], ['blocked', '실행 차단'], ['question', '추가 확인 필요']]} />
      <FilterGroup title="실행 결과" value={execution} onChange={setExecution} disabled={executionDisabled} options={[['all', '전체'], ['success', '성공'], ['failed', '실패'], ['stop', 'STOP']]} />
      <label className="select-field"><span>조회 로봇</span><select value={robot} onChange={(e) => setRobot(e.target.value)}><option value="all">전체 로봇</option><option value="robot-ur5e">팔레타이징 로봇</option><option value="robot-fr3">부품 공급 로봇</option></select></label>
    </section>
    {(validation !== 'all' || execution !== 'all' || robot !== 'all') && <div className="filter-summary">적용 조건 · {validation === 'all' ? '모든 검증' : VALIDATION[validation][0]} + {execution === 'all' ? '모든 실행결과' : EXECUTION[execution][0]}</div>}
    <div className="history-layout">
      <section className="table-wrap history-list"><table><thead><tr><th>요청시각</th><th>로봇</th><th>명령요약</th><th>요청자</th><th>검증결과</th><th>실행결과</th></tr></thead><tbody>{filtered.map((item) => <tr key={item.id} className={selected?.id === item.id ? 'selected-row' : ''} onClick={() => setSelected(item)}><td>{item.time}</td><td>{item.robot}</td><td className="command-cell">{item.command}</td><td>{item.requester}</td><td><Badge tone={VALIDATION[item.validation][1]} icon={item.validation === 'safe' ? '●' : item.validation === 'blocked' ? '■' : '◆'}>{VALIDATION[item.validation][0]}</Badge></td><td><Badge tone={EXECUTION[item.execution][1]} icon={item.execution === 'stop' ? '■' : item.execution === 'success' ? '●' : undefined}>{EXECUTION[item.execution][0]}</Badge></td></tr>)}</tbody></table>{filtered.length === 0 && <Empty>조건에 맞는 명령 기록이 없습니다.</Empty>}</section>
      {selected && <HistoryDetail item={selected} />}
    </div>
  </div>;
}

function FilterGroup({ title, value, onChange, options, disabled }) {
  return <fieldset className="filter-group" disabled={disabled}><legend>{title}</legend><div>{options.map(([id, label]) => <button type="button" key={id} className={value === id ? 'active' : ''} onClick={() => onChange(id)}>{label}</button>)}</div>{disabled && <small>차단된 명령은 실행되지 않습니다.</small>}</fieldset>;
}

function HistoryDetail({ item }) {
  const blocked = item.validation === 'blocked';
  return <aside className="history-detail">
    <header><div><span>{item.time}</span><h2>{item.command}</h2><p>{item.robot} · {item.requester} · {item.input}</p></div><Badge tone={VALIDATION[item.validation][1]}>{VALIDATION[item.validation][0]}</Badge></header>
    <div className="vertical-timeline">
      <TimelineNode title="동작 준비" time="14:32:19" tone="ok"><p>상자 집기 → 출하 팔레트 이동 → 지정 위치 놓기</p></TimelineNode>
      <TimelineNode title="가상 동작 확인" time="14:32:27" tone={blocked ? 'danger' : 'ok'}><p>{blocked ? '로봇 경로가 허용 안전거리보다 가까워 실행이 차단되었습니다.' : '충돌 없이 이동할 수 있습니다. 최소 이격거리 124 mm'}</p></TimelineNode>
      <TimelineNode title="실행 허가" time="14:32:30" tone={blocked ? 'muted' : 'ok'}><p>{blocked ? '안전 확인을 통과하지 못해 실행 허가를 요청하지 않았습니다.' : `${item.requester}님이 실행 전 최종점검을 확인했습니다.`}</p></TimelineNode>
      {!blocked && <TimelineNode title="로봇 실행" time="14:32:32" tone={item.stopped ? 'danger' : 'ok'}><p>{item.stopped ? '2단계 이동 중 사용자가 즉시 정지를 요청했습니다.' : `전체 작업을 ${item.duration} 만에 완료했습니다.`}</p>{item.stopped && <div className="stop-event"><span>◆ STOP</span><dl><div><dt>요청수단</dt><dd>화면 버튼</dd></div><div><dt>요청자</dt><dd>{item.requester}</dd></div><div><dt>ACK 응답</dt><dd>180 ms</dd></div><div><dt>중단 단계</dt><dd>작업대 이동</dd></div></dl></div>}</TimelineNode>}
    </div>
    {item.stopped && <div className="recovery-flow"><strong>복구 진행</strong>{['원인 확인 완료', '현장 리셋 대기', '재실행 승인 대기'].map((step, i) => <span key={step} className={i === 0 ? 'done' : ''}>{i === 0 ? '✓' : i + 1} {step}</span>)}</div>}
    <details className="technical"><summary>기술 정보</summary><dl><div><dt>명령 ID</dt><dd>{item.id}</dd></div><div><dt>계획 ID</dt><dd>plan_8f3a21</dd></div><div><dt>판정 결과</dt><dd>{blocked ? '실행 차단' : '안전 확인됨'}</dd></div></dl><pre>{JSON.stringify({ command_id: item.id, validation: item.validation, execution: item.execution }, null, 2)}</pre></details>
  </aside>;
}

function TimelineNode({ title, time, tone, children }) {
  return <div className={`timeline-node ${tone}`}><i>{tone === 'ok' ? '✓' : tone === 'danger' ? '!' : '–'}</i><div><header><strong>{title}</strong><time>{time}</time></header>{children}</div></div>;
}

function Diagnostics() {
  const [params, setParams] = useSearchParams();
  const requested = params.get('service');
  const [selectedId, setSelectedId] = useState(requested || 'planner');
  const [rawOpen, setRawOpen] = useState(false);
  const [exportDialog, setExportDialog] = useState(false);
  const [exportState, setExportState] = useState(null);
  const selected = services.find((service) => service.id === selectedId) || services[0];
  const ordered = [...services].sort((a, b) => ({ critical: 0, warning: 1, unknown: 2, healthy: 3 }[a.status] - ({ critical: 0, warning: 1, unknown: 2, healthy: 3 }[b.status])));
  useEffect(() => { if (requested && services.some((service) => service.id === requested)) setSelectedId(requested); }, [requested]);
  function selectService(id) { setSelectedId(id); setParams({ service: id }); setRawOpen(false); }
  function exportDiagnostics() {
    setExportDialog(false);
    setExportState('queued');
    window.setTimeout(() => setExportState('working'), 700);
    window.setTimeout(() => setExportState('complete'), 2400);
  }
  return <div className="page">
    <PageHeader eyebrow="System diagnostics" title="진단" description="문제가 작업에 미치는 영향과 안전한 조치 순서부터 확인합니다." actions={<button className="button secondary" onClick={() => setExportDialog(true)}><Icon name="download" size={18}/> 진단자료 내보내기</button>} />
    {exportState && <div className={`export-job ${exportState}`}><div><span className="job-spinner">{exportState === 'complete' ? '✓' : '◌'}</span><div><strong>{exportState === 'queued' ? '내보내기 대기 중' : exportState === 'working' ? '진단자료 생성 중' : '진단자료 준비 완료'}</strong><span>최근 24시간 · {selected.name} · 시스템 이벤트 포함</span></div></div>{exportState === 'complete' && <button className="button primary" onClick={() => setExportState(null)}>파일 받기</button>}</div>}
    <div className="diagnostic-grid">
      <section className="service-list">
        <div className="list-caption"><strong>서비스 상태</strong><span>문제 항목 우선</span></div>
        {ordered.map((service) => <button key={service.id} className={selected.id === service.id ? 'selected' : ''} onClick={() => selectService(service.id)}>
          <div><Badge tone={service.status === 'healthy' ? 'ok' : 'warn'} icon={service.status === 'healthy' ? '●' : '▲'}>{service.status === 'healthy' ? '정상' : '경고'}</Badge><strong>{service.name}</strong><small>{service.technical}</small></div>
          <dl><div><dt>최근 점검</dt><dd>{service.checked}</dd></div><div><dt>마지막 정상</dt><dd>{service.lastHealthy}</dd></div></dl>
          <p>{service.issue}</p>
        </button>)}
      </section>
      <section className="diagnostic-detail">
        <header><div><Badge tone={selected.status === 'healthy' ? 'ok' : 'warn'} icon={selected.status === 'healthy' ? '●' : '▲'}>{selected.status === 'healthy' ? '정상' : '경고'}</Badge><h2>{selected.name}</h2><span>상태 지속 {selected.duration}</span></div><details className="technical-inline"><summary>기술 이름</summary>{selected.technical}</details></header>
        <div className="impact-triad"><div><span>문제</span><strong>{selected.issue}</strong></div><div><span>작업 영향</span><strong>{selected.impact}</strong></div><div><span>권장 조치</span><strong>{selected.action}</strong></div></div>
        <section className="health-section"><div className="section-heading compact"><div><h3>최근 24시간 상태</h3><p>언제부터 문제가 반복됐는지 확인합니다.</p></div></div><div className="health-strip" aria-label="최근 24시간 서비스 상태"><i className="healthy" style={{ flex: 7 }}/><i className="warning" style={{ flex: 1 }}/><i className="healthy" style={{ flex: 4 }}/><i className="warning" style={{ flex: 1 }}/><i className="healthy" style={{ flex: 11 }}/></div><div className="strip-legend"><span>24시간 전</span><span><i className="dot ok"/>정상 <i className="dot warn"/>경고</span><span>현재</span></div></section>
        <section className="health-section"><h3>최근 실패 헬스체크</h3>{selected.status === 'healthy' ? <Empty>최근 24시간 동안 실패한 점검이 없습니다.</Empty> : <div className="event-list">
          <article><time>14:31:02</time><div><strong>동작계획 응답시간 초과</strong><p>응답시간 2.4초 · 기준 2.0초</p></div><Badge tone="warn">4분 26초 지속</Badge></article>
          <article><time>11:08:44</time><div><strong>작업 큐 응답 지연</strong><p>3회 연속 기준 초과 후 정상 복구</p></div><Badge tone="ok">11:10 복구</Badge></article>
        </div>}</section>
        <section className="health-section"><h3>관련 시스템 이벤트</h3><div className="event-list"><article><time>14:31:06</time><div><strong>새 명령 처리 지연 안내</strong><p>사용자 화면에 지연 상태를 표시했습니다.</p></div><Badge tone="info">안내</Badge></article></div></section>
        {selected.status !== 'healthy' && <section className="raw-samples"><div><h3>문제 발생 전후 로봇 상태</h3><p>문제와 관련된 시점의 원시 샘플만 제한적으로 확인합니다.</p></div><button className="button secondary" onClick={() => setRawOpen((v) => !v)}>{rawOpen ? '원시 샘플 닫기' : '문제 발생 전후 원시 샘플 보기'}</button>{rawOpen && <div className="table-wrap"><table><thead><tr><th>시각</th><th>관절각 J2</th><th>속도</th><th>안전상태</th></tr></thead><tbody><tr><td>14:30:58.420</td><td>−42.8°</td><td>0.18 m/s</td><td>정상</td></tr><tr><td>14:31:01.106</td><td>−43.1°</td><td>0.02 m/s</td><td>감속</td></tr><tr><td>14:31:02.014</td><td>−43.1°</td><td>0.00 m/s</td><td>대기</td></tr></tbody></table></div>}</section>}
      </section>
    </div>
    {exportDialog && <Dialog title="진단자료 내보내기" onClose={() => setExportDialog(false)} footer={<><button className="button ghost" onClick={() => setExportDialog(false)}>취소</button><button className="button primary" onClick={exportDiagnostics}>백그라운드 생성</button></>}><div className="two-fields"><label className="field"><span>조회 시작</span><input type="datetime-local" defaultValue="2026-09-15T14:36" /></label><label className="field"><span>조회 종료</span><input type="datetime-local" defaultValue="2026-09-16T14:36" /></label></div><label className="field"><span>대상 서비스</span><input value={selected.name} readOnly /></label><div className="export-options"><label><input type="checkbox" defaultChecked/> 서비스 헬스체크</label><label><input type="checkbox" defaultChecked/> 관련 시스템 이벤트</label><label><input type="checkbox" defaultChecked={selected.status !== 'healthy'}/> 문제 발생 전후 로봇 샘플</label></div><div className="notice-box">파일은 백그라운드에서 생성되며, 완료되면 이 화면에 알림이 표시됩니다.</div></Dialog>}
  </div>;
}

function Settings({ role }) {
  const [params] = useSearchParams();
  const allowed = role === 'operator' ? ['personal'] : ['personal', 'operations', 'safety'];
  const requestedSection = params.get('section');
  const [section, setSection] = useState(allowed.includes(requestedSection) ? requestedSection : 'personal');
  useEffect(() => { if (allowed.includes(requestedSection)) setSection(requestedSection); }, [requestedSection, role]);
  useEffect(() => { if (!allowed.includes(section)) setSection('personal'); }, [role]);
  return <div className="page settings-page">
    <PageHeader eyebrow="Workspace settings" title="설정" description="개인 편의, 운영 관리, 안전 변경을 권한별로 분리합니다." />
    <div className="settings-layout">
      <nav className="settings-nav" aria-label="설정 구역">{allowed.includes('personal') && <button className={section === 'personal' ? 'active' : ''} onClick={() => setSection('personal')}><span>개인</span><small>저장 루틴 · 내 설정</small></button>}{allowed.includes('operations') && <button className={section === 'operations' ? 'active' : ''} onClick={() => setSection('operations')}><span>운영</span><small>사용자 · 로봇 배정</small></button>}{allowed.includes('safety') && <button className={section === 'safety' ? 'active' : ''} onClick={() => setSection('safety')}><span>안전 · 권한</span><small>임계값 · 승인 이력</small></button>}</nav>
      <section className="settings-content">{section === 'personal' && <PersonalSettings />}{section === 'operations' && <OperationsSettings />}{section === 'safety' && <SafetySettings editable={role === 'safety'} />}</section>
    </div>
  </div>;
}

function PersonalSettings() {
  const [routines, setRoutines] = useState(routinesSeed);
  const [selectedId, setSelectedId] = useState(routines[0].id);
  const [query, setQuery] = useState('');
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  const [editing, setEditing] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [draftName, setDraftName] = useState('');
  const [draftCommand, setDraftCommand] = useState('');
  const filtered = routines.filter((r) => (!favoritesOnly || r.favorite) && (!query || `${r.name} ${r.command}`.includes(query)));
  const selected = routines.find((r) => r.id === selectedId) || routines[0];
  function toggleFavorite(id) { setRoutines((items) => items.map((item) => item.id === id ? { ...item, favorite: !item.favorite } : item)); }
  function openRoutineEditor(routine = null) { setEditing(routine || { id: null }); setDraftName(routine?.name || ''); setDraftCommand(routine?.command || ''); }
  function saveRoutine() { if (!draftName.trim() || !draftCommand.trim()) return; if (editing.id) setRoutines((items) => items.map((item) => item.id === editing.id ? { ...item, name: draftName, command: draftCommand } : item)); else { const item = { id: Date.now(), name: draftName, command: draftCommand, skills: '실행 시 확인', robots: '실행 시 선택', favorite: false }; setRoutines((items) => [item, ...items]); setSelectedId(item.id); } setEditing(null); }
  function removeRoutine() { setRoutines((items) => items.filter((item) => item.id !== selected.id)); setSelectedId(routines.find((item) => item.id !== selected.id)?.id); setDeleting(false); }
  return <><div className="settings-heading"><div><h2>저장 루틴</h2><p>자주 쓰는 명령을 저장하고 빠르게 불러옵니다.</p></div><button className="button primary" onClick={() => openRoutineEditor()}>새 루틴</button></div>
    <div className="routine-toolbar"><div className="search-field"><Icon name="search" size={18}/><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="루틴 검색" /></div><label className="check-control"><input type="checkbox" checked={favoritesOnly} onChange={(e) => setFavoritesOnly(e.target.checked)}/> 즐겨찾기만</label></div>
    <div className="routine-layout"><div className="routine-list">{filtered.map((routine) => <button key={routine.id} className={selected?.id === routine.id ? 'selected' : ''} onClick={() => setSelectedId(routine.id)}><span className="favorite-button" role="button" tabIndex="0" onClick={(e) => { e.stopPropagation(); toggleFavorite(routine.id); }} aria-label="즐겨찾기 전환">{routine.favorite ? '★' : '☆'}</span><div><strong>{routine.name}</strong><span>{routine.command}</span></div><Badge tone="neutral">{routine.robots}</Badge></button>)}</div>{selected && <aside className="routine-detail"><p>저장된 명령</p><h3>{selected.name}</h3><blockquote>{selected.command}</blockquote><dl className="definition-list"><div><dt>사용 작업</dt><dd>{selected.skills}</dd></div><div><dt>기본 대상</dt><dd>{selected.robots}</dd></div></dl><div className="notice-box">실행할 때 현재 로봇·도구·안전조건을 다시 확인합니다.</div><div className="button-row"><button className="button secondary" onClick={() => openRoutineEditor(selected)}>수정</button><button className="button destructive-text" onClick={() => setDeleting(true)}>삭제</button></div></aside>}</div>
    {editing && <Dialog title={editing.id ? '저장 루틴 수정' : '새 루틴 만들기'} onClose={() => setEditing(null)} footer={<><button className="button ghost" onClick={() => setEditing(null)}>취소</button><button className="button primary" disabled={!draftName.trim() || !draftCommand.trim()} onClick={saveRoutine}>{editing.id ? '변경 저장' : '루틴 만들기'}</button></>}><label className="field"><span>루틴 이름</span><input value={draftName} onChange={(e) => setDraftName(e.target.value)} placeholder="예: 출하 팔레트 적재" /></label><label className="field"><span>명령 내용</span><textarea value={draftCommand} onChange={(e) => setDraftCommand(e.target.value)} placeholder="자연어 명령을 입력하세요" /></label></Dialog>}
    {deleting && selected && <Dialog title="저장 루틴을 삭제하시겠습니까?" onClose={() => setDeleting(false)} footer={<><button className="button ghost" onClick={() => setDeleting(false)}>취소</button><button className="button destructive-text" onClick={removeRoutine}>삭제</button></>}><p><strong>{selected.name}</strong> 루틴이 목록에서 삭제됩니다. 실제 로봇 작업 기록에는 영향을 주지 않습니다.</p></Dialog>}
  </>;
}

function OperationsSettings() {
  const [users, setUsers] = useState(usersSeed);
  const [invite, setInvite] = useState(false);
  const [reasonFor, setReasonFor] = useState(null);
  const [reason, setReason] = useState('');
  const [nextRole, setNextRole] = useState('작업자');
  function openUserChange(user) { setReasonFor(user); setNextRole(user.role); setReason(''); }
  function applyUserChange() { setUsers((items) => items.map((user) => user.id === reasonFor.id ? { ...user, role: nextRole, status: reasonFor.action === 'status' ? (user.status === '활성' ? '비활성' : '활성') : user.status } : user)); setReasonFor(null); setReason(''); }
  return <><div className="settings-heading"><div><h2>사용자 관리</h2><p>사용자의 역할과 작업 범위를 관리합니다.</p></div><button className="button primary" onClick={() => setInvite(true)}>사용자 추가</button></div>
    <div className="table-wrap"><table><thead><tr><th>사용자</th><th>역할</th><th>배정 셀·로봇</th><th>상태</th><th>최근 로그인</th><th>관리</th></tr></thead><tbody>{users.map((user) => <tr key={user.id}><td><strong>{user.name}</strong><small>{user.identity}</small></td><td><Badge tone={user.role === '안전관리자' ? 'brand' : 'neutral'}>{user.role}</Badge></td><td>{user.assignment}</td><td><Badge tone={user.status === '활성' ? 'ok' : 'muted'} icon={user.status === '활성' ? '●' : '○'}>{user.status}</Badge></td><td>{user.lastLogin}</td><td><div className="table-actions"><button className="table-action" onClick={() => { openUserChange(user); setReasonFor({ ...user, action: 'role' }); }}>역할 변경</button><button className="table-action" onClick={() => { openUserChange(user); setReasonFor({ ...user, action: 'status' }); }}>{user.status === '활성' ? '비활성화' : '활성화'}</button></div></td></tr>)}</tbody></table></div>
    {invite && <Dialog title="사용자 추가" onClose={() => setInvite(false)} footer={<><button className="button ghost" onClick={() => setInvite(false)}>취소</button><button className="button primary" onClick={() => setInvite(false)}>초대 보내기</button></>}><label className="field"><span>이메일 또는 사번</span><input placeholder="name@company.com" /></label><div className="two-fields"><label className="field"><span>역할</span><select><option>작업자</option><option>관리자</option><option>안전관리자</option></select></label><label className="field"><span>배정 셀</span><select><option>포장 셀</option><option>조립 셀</option><option>전체 셀</option></select></label></div><div className="notice-box">대상자가 회사 로그인으로 접속하면 신원이 자동 연결됩니다.</div></Dialog>}
    {reasonFor && <Dialog title={reasonFor.action === 'role' ? `${reasonFor.name}님의 역할 변경` : `${reasonFor.name}님을 ${reasonFor.status === '활성' ? '비활성화' : '활성화'}하시겠습니까?`} onClose={() => setReasonFor(null)} footer={<><button className="button ghost" onClick={() => setReasonFor(null)}>취소</button><button className="button primary" disabled={!reason.trim() || (reasonFor.action === 'role' && nextRole === reasonFor.role)} onClick={applyUserChange}>변경 적용</button></>}>{reasonFor.action === 'role' && <label className="field"><span>새 역할</span><select value={nextRole} onChange={(e) => setNextRole(e.target.value)}><option>작업자</option><option>관리자</option><option>안전관리자</option></select></label>}<label className="field"><span>변경 사유 <b>필수</b></span><textarea value={reason} onChange={(e) => setReason(e.target.value)} placeholder="감사기록에 남을 사유를 입력하세요" /></label></Dialog>}
  </>;
}

function SafetySettings({ editable }) {
  const [workflow, setWorkflow] = useState(2);
  const steps = ['작성 중', '검사 중', '승인 필요', '적용 예약', '사용 중'];
  return <><div className="settings-heading"><div><div className="inline-title"><h2>안전 임계값</h2>{!editable && <Badge tone="neutral">조회 전용</Badge>}</div><p>{editable ? '변경안과 검증·승인 근거를 한 화면에서 확인합니다.' : '변경은 안전관리자 권한이 필요합니다.'}</p></div>{editable && <button className="button primary">새 변경안 작성</button>}</div>
    <section className="safety-workflow"><header><div><span>변경안 v8</span><h3>공유구역 최소 안전거리 조정</h3><p>요청자 박서준 · 2026.09.16 11:20</p></div><Badge tone={workflow === 2 ? 'warn' : workflow === 4 ? 'ok' : 'info'}>{steps[workflow]}</Badge></header>
      <div className="workflow-timeline">{steps.map((step, index) => <div key={step} className={index < workflow ? 'done' : index === workflow ? 'current' : ''}><i>{index < workflow ? '✓' : index + 1}</i><span>{step}</span></div>)}</div>
      <div className="change-grid"><div><span>변경 전</span><strong>최소 안전거리 100 mm</strong></div><div><span>변경 후</span><strong>최소 안전거리 120 mm</strong></div><div><span>변경 사유</span><strong>신규 대형 상자 공정의 회전 반경 반영</strong></div><div><span>자동 검사</span><strong><Badge tone="ok" icon="●">기존 봉인세트 210건 통과</Badge></strong></div></div>
      <section className="approval-log"><h4>검토 및 승인 기록</h4><article><i>✓</i><div><strong>자동 검사가 완료되었습니다</strong><span>규칙엔진 · 오늘 11:28</span></div></article><article><i>2</i><div><strong>2차 승인자의 검토를 기다리고 있습니다</strong><span>승인자 이지민 · 알림 발송됨</span></div></article></section>
      {editable && workflow === 2 && <div className="approval-actions"><button className="button secondary" onClick={() => setWorkflow(0)}>반려</button><button className="button primary" onClick={() => setWorkflow(3)}>승인하고 적용 예약</button></div>}
    </section>
    <section className="section-block"><div className="section-heading compact"><div><h3>현재 사용 중인 기준</h3><p>v7 · 2026.08.21 적용 · 승인자 김안전</p></div><Badge tone="ok">사용 중</Badge></div><dl className="threshold-list"><div><dt>최소 안전거리</dt><dd>100 mm</dd></div><div><dt>관절 온도 주의 기준</dt><dd>80°C</dd></div><div><dt>통신 지연 제한</dt><dd>200 ms</dd></div></dl></section>
  </>;
}

function CommandPanel({ robots, scenario, setScenario, online, emergency }) {
  const [target, setTarget] = useState(robots[0].id);
  const [command, setCommand] = useState('A형 상자 4개를 출하 팔레트로 옮겨줘');
  const [questionAnswer, setQuestionAnswer] = useState('');
  const [processStep, setProcessStep] = useState(0);
  const [videoConnected, setVideoConnected] = useState(true);
  const targetRobot = robots.find((robot) => robot.id === target) || robots[0];
  const stage = commandStage(scenario);
  useEffect(() => {
    setVideoConnected(scenario !== 'video-lost');
    if (scenario === 'processing') setProcessStep(1);
  }, [scenario]);

  function begin() {
    if (!command.trim() || !online || emergency || !targetRobot.available || targetRobot.adminStopped) return;
    setScenario('processing');
    setProcessStep(0);
    window.setTimeout(() => setProcessStep(1), 650);
    window.setTimeout(() => setProcessStep(2), 1300);
    window.setTimeout(() => setScenario('review'), 2100);
  }
  function answerQuestion() {
    if (!questionAnswer) return;
    setScenario('processing');
    setProcessStep(0);
    window.setTimeout(() => setProcessStep(1), 600);
    window.setTimeout(() => setProcessStep(2), 1200);
    window.setTimeout(() => setScenario('review'), 1900);
  }
  function stopNow() { if (stage === 'executing') setScenario('stopped'); }
  const cannotCommand = !online || emergency || !targetRobot.available || targetRobot.adminStopped;
  return <aside className="command-panel" aria-label="명령 패널">
    <header className="command-header"><div><span>자연어 명령</span><h2>로봇에게 작업 지시</h2></div><Badge tone={online ? 'ok' : 'danger'} icon={online ? '●' : '■'}>{online ? '명령 채널 연결됨' : '연결 끊김'}</Badge></header>
    <button className="stop-button" onClick={stopNow} disabled={stage !== 'executing'}><span><Icon name="stop" size={22}/><strong>즉시 정지</strong></span><small>현재 실행 중인 동작 취소 요청 · 물리 비상정지 아님</small></button>
    <div className="control-strip"><span><b>운전 모드</b> 원격 운전</span><span><b>제어권</b> {stage === 'executing' ? '김작업자 (본인)' : '제어 가능'}</span></div>
    <div className="command-scroll">
      {stage === 'idle' && <>
        <section className="target-section"><label>대상 로봇</label><div className="target-chips">{robots.map((robot) => <button key={robot.id} disabled={!robot.available || robot.adminStopped} className={target === robot.id ? 'selected' : ''} onClick={() => setTarget(robot.id)}><span>{robot.location.split(' · ')[0]}</span><strong>{robot.alias}</strong>{(!robot.available || robot.adminStopped) && <small>{robot.adminStopped ? '새 작업 차단됨' : '운용 불가'}</small>}</button>)}</div></section>
        <section className="command-input"><label htmlFor="command-text">무엇을 할까요?</label><textarea id="command-text" value={command} onChange={(e) => setCommand(e.target.value)} placeholder="예: A형 상자를 출하 팔레트로 옮겨줘"/><div><button className="voice-button" aria-label="음성으로 명령"><Icon name="mic" size={22}/> 음성 입력</button><button className="button primary large" disabled={cannotCommand || !command.trim()} onClick={begin}>명령 확인</button></div></section>
        {scenario === 'robot-notice' && <div className="task-notice"><Badge tone="info" icon="◆">작업 알림</Badge><div><strong>도구 점검이 30분 뒤 예정되어 있습니다</strong><span>현재 작업은 계속할 수 있습니다.</span></div></div>}
        {cannotCommand && <div className="panel-warning"><Icon name="alert"/><div><strong>새 명령을 시작할 수 없습니다</strong><span>{!online ? '연결이 복구되고 동기화될 때까지 기다려주세요.' : emergency ? '긴급 상황이 해결될 때까지 기다려주세요.' : '선택한 로봇의 운용 상태를 확인하세요.'}</span></div></div>}
      </>}
      {stage === 'processing' && <ProcessingSteps current={processStep} />}
      {stage === 'question' && <QuestionCard answer={questionAnswer} setAnswer={setQuestionAnswer} onSubmit={answerQuestion} />}
      {stage === 'blocked' && <BlockedCard onEdit={() => setScenario('normal')} />}
      {(stage === 'review' || stage === 'expired') && <ReviewCard robot={targetRobot} command={command} expired={stage === 'expired'} videoConnected={videoConnected} online={online} onExecute={() => setScenario('executing')} onRevalidate={begin} onChangeRobot={() => setScenario('normal')} />}
      {stage === 'executing' && <ExecutingCard robot={targetRobot} command={command} />}
      {stage === 'stopped' && <StoppedCard onReset={() => setScenario('normal')} />}
      {stage === 'completed' && <CompletedCard onNew={() => setScenario('normal')} />}
    </div>
    <footer className="command-footer"><span>마지막 수신 {online ? '0.2초 전' : '18초 전'}</span><span>안전 제어장치 {online ? '정상' : '확인 불가'}</span></footer>
  </aside>;
}

function commandStage(scenario) {
  if (['processing', 'question', 'blocked', 'review', 'expired', 'video-lost', 'executing', 'stopped', 'completed'].includes(scenario)) return scenario === 'video-lost' ? 'review' : scenario;
  return 'idle';
}

function ProcessingSteps({ current }) {
  const steps = ['동작 준비', '안전 규칙 확인', '가상 동작 확인'];
  return <section className="process-card"><div className="processing-orbit" aria-hidden="true"><i/><i/><span>확인 중</span></div><h3>실행 전에 안전하게 확인하고 있습니다</h3><p>각 단계는 자동으로 진행됩니다. 사용자가 확인할 내용이 생기면 알려드립니다.</p><div className="process-steps">{steps.map((step, index) => <div key={step} className={index < current ? 'done' : index === current ? 'current' : ''}><i>{index < current ? '✓' : index + 1}</i><span>{step}</span>{index === current && <small>진행 중</small>}</div>)}</div><details className="technical"><summary>처리가 오래 걸리나요?</summary><p>현재 단계가 10초 이상 걸리면 세부 처리상태와 경과시간을 표시합니다.</p></details></section>;
}

function QuestionCard({ answer, setAnswer, onSubmit }) {
  return <section className="decision-card question-card"><Badge tone="warn" icon="◆">정보가 더 필요합니다</Badge><h3>어떤 상자를 옮길까요?</h3><p>포장 셀에 작업 가능한 상자가 두 종류 있습니다.</p><div className="answer-options">{['A형 상자 · 4개', 'B형 상자 · 4개'].map((option) => <button key={option} className={answer === option ? 'selected' : ''} onClick={() => setAnswer(option)}>{option}</button>)}</div><label className="field"><span>직접 입력</span><input value={answer.startsWith('A형') || answer.startsWith('B형') ? '' : answer} onChange={(e) => setAnswer(e.target.value)} placeholder="상자 종류와 수량을 입력하세요" /></label><button className="button primary full" disabled={!answer} onClick={onSubmit}>답변 보내기</button><p className="card-footnote">이 답변은 실행 승인이 아닙니다. 답변 후 안전 확인을 계속합니다.</p></section>;
}

function BlockedCard({ onEdit }) {
  return <section className="decision-card blocked-card"><div className="blocked-symbol">■</div><Badge tone="danger">실행 차단</Badge><h3>이 명령은 실행할 수 없습니다</h3><p className="reason-sentence">로봇 경로가 허용 안전거리보다 가까워 실행이 차단되었습니다.</p><div className="blocked-facts"><div><span>영향받은 조건</span><strong>공유 통로 최소 이격거리</strong></div><div><span>확인된 거리</span><strong>82 mm · 기준 100 mm</strong></div><div><span>수정 방법</span><strong>통로 밖의 적재 위치를 선택하세요</strong></div></div><div className="button-stack"><button className="button secondary full" onClick={onEdit}>명령 수정</button><button className="button ghost full" onClick={onEdit}>새 명령 입력</button></div><details className="technical"><summary>기술 정보</summary><dl><div><dt>판정 코드</dt><dd>SAFE_DISTANCE_003</dd></div><div><dt>계획 ID</dt><dd>plan_8f3a21</dd></div></dl></details></section>;
}

function ReviewCard({ robot, command, expired, videoConnected, online, onExecute, onRevalidate, onChangeRobot }) {
  const valid = !expired && online;
  return <section className="decision-card review-card"><div className="review-kicker"><Badge tone={valid ? 'ok' : 'danger'} icon={valid ? '●' : '■'}>{expired ? '검증 만료' : '안전 확인됨'}</Badge><time>검증 시각 14:36:12</time></div><h3>실행 전 최종점검</h3><p>아래 내용대로 로봇에 명령을 전달합니다.</p><div className="action-summary"><div><span>대상 로봇</span><strong>{robot.location}<br/>{robot.alias}</strong><button onClick={onChangeRobot}>다른 로봇 선택</button></div><div><span>사용 도구</span><strong>{robot.tool}</strong></div><div><span>동작</span><strong>{command}</strong></div><div><span>출발지 → 목적지</span><strong>입고대 A → 출하 팔레트 1</strong></div><div><span>영향 구역</span><strong>포장 셀 + 공유 통로</strong></div></div>
    <div className="simulation-evidence"><div className="simulation-frame"><span>SIMULATION · 실시간 아님</span><div className="cell-map"><i className="cell-a">포장 셀</i><i className="route"/><i className="cell-b">공유 통로</i></div>{!videoConnected && <div className="video-lost">영상 끊김<br/><small>판정 결과는 유효합니다</small></div>}</div><div className="simulation-result"><strong>충돌 없이 이동 가능</strong><span>최소 이격거리 124 mm</span><span>판정 유효시간 {expired ? '만료됨' : '14:37:12까지'}</span></div></div>
    {expired && <div className="panel-warning"><Icon name="alert"/><div><strong>안전 확인 결과가 만료되었습니다</strong><span>현재 상태로 다시 확인해야 실행할 수 있습니다.</span></div></div>}
    {!videoConnected && <div className="inline-warning">▲ 검증 영상이 끊겼지만 구조화된 판정 결과는 아직 유효합니다.</div>}
    <button className="button primary full execute-button" disabled={!online} onClick={expired ? onRevalidate : onExecute}>{expired ? '다시 안전 확인' : '이대로 실행'}</button><p className="card-footnote">로봇·계획·안전상태가 바뀌면 이 승인은 즉시 무효화됩니다.</p><details className="technical"><summary>기술 정보</summary><dl><div><dt>계획 ID</dt><dd>plan_8f3a21</dd></div><div><dt>판정 상태</dt><dd>{expired ? '검증 만료' : '안전 확인됨'}</dd></div></dl></details></section>;
}

function ExecutingCard({ robot, command }) {
  return <section className="decision-card executing-card"><Badge tone="info" icon="◆">실행 중</Badge><h3>{command}</h3><p>{robot.location} · {robot.alias}</p><div className="execution-progress"><div><span>상자 집기</span><strong>완료</strong></div><div className="current"><span>출하 팔레트로 이동</span><strong>진행 중 · 42%</strong></div><div><span>지정 위치에 놓기</span><strong>대기</strong></div></div><div className="progress large"><i style={{ width: '52%' }}/></div><div className="execution-meta"><span>경과 00:38</span><span>예상 잔여 00:44</span></div><div className="notice-box">멈춰야 한다면 패널 상단의 <strong>즉시 정지</strong>를 누르세요.</div></section>;
}

function StoppedCard({ onReset }) {
  const [step, setStep] = useState(0);
  return <section className="decision-card stopped-card"><Badge tone="danger" icon="■">정지 요청됨</Badge><h3>로봇 동작이 중단되었습니다</h3><p>화면의 즉시 정지 버튼으로 실행 취소를 요청했습니다. 물리 비상정지는 작동하지 않았습니다.</p><div className="recovery-steps">{['원인 확인', '안전상태 리셋', '재실행 승인'].map((label, index) => <div key={label} className={index < step ? 'done' : index === step ? 'current' : ''}><i>{index < step ? '✓' : index + 1}</i><div><strong>{label}</strong><span>{index === 0 ? '작업 구역과 로봇 상태를 확인하세요.' : index === 1 ? '보호정지 원인을 해소한 뒤 리셋하세요.' : '작업은 자동 재개되지 않습니다.'}</span></div></div>)}</div>{step < 2 ? <button className="button primary full" onClick={() => setStep((value) => value + 1)}>{step === 0 ? '원인 확인 완료' : '안전상태 리셋'}</button> : <button className="button secondary full" onClick={onReset}>새 명령으로 돌아가기</button>}</section>;
}

function CompletedCard({ onNew }) {
  return <section className="decision-card completed-card"><div className="completion-mark">✓</div><Badge tone="ok">완료</Badge><h3>요청한 작업을 완료했습니다</h3><p>A형 상자 4개를 출하 팔레트로 옮겼습니다.</p><dl><div><dt>완료 시각</dt><dd>14:38:04</dd></div><div><dt>소요 시간</dt><dd>1분 42초</dd></div><div><dt>작업 결과</dt><dd>4 / 4 완료</dd></div></dl><button className="button primary full" onClick={onNew}>새 명령 입력</button></section>;
}

export default App;
