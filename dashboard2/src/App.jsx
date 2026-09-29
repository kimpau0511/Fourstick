import { useCallback, useEffect, useState } from 'react';
import {
  alerts, command, commandLog, commandLogNote, engineMetrics, history, issues, metrics,
  robotDetails, robots, tool, toolHistory,
} from './data.js';
import SceneView, { sceneLabel } from './SceneView.jsx';
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
import headerDivider from './assets/header-divider.svg';
import house from './assets/house.svg';
import loaderCircle from './assets/loader-circle.svg';
import logs from './assets/logs.svg';
import mic from './assets/mic.svg';
import playCircle from './assets/play-circle.svg';
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

function clockText(date) {
  const day = date.toLocaleDateString('ko-KR', { year: 'numeric', month: 'long', day: 'numeric' });
  const weekday = date.toLocaleDateString('ko-KR', { weekday: 'short' });
  return `${day} (${weekday}) ${timeText(date)}`;
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

function useDashboard() {
  const [state, setState] = useState({ payload: null, error: '', updatedAt: null });
  useEffect(() => {
    let disposed = false;
    let timer;
    async function refresh() {
      try {
        const response = await fetch('/v1/dashboard', { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        if (!disposed) setState({ payload, error: '', updatedAt: new Date() });
      } catch (error) {
        if (!disposed) setState((prev) => ({ ...prev, error: error.message }));
      }
      if (!disposed) timer = setTimeout(refresh, 3000);
    }
    refresh();
    return () => { disposed = true; if (timer) clearTimeout(timer); };
  }, []);
  return state;
}

function dashboardModel(payload) {
  if (!payload) return {
    metrics: [
      { label: '누적 실행 기록', value: '—', badge: '연결 중', tone: 'warn' },
      { label: '활성 작업 세션', value: '—', badge: '연결 중', tone: 'warn' },
      { label: '안전 검증 통과 지수', value: '—', badge: '연결 중', tone: 'warn' },
    ],
    robots: [], history: [], alerts: [],
    command: {
      utterance: '백엔드에서 최근 명령을 불러오는 중입니다.', target: '', plan: [],
      checks: ['• PostgreSQL 연결 확인 중', '• Gazebo 상태 확인 중'],
      holdReason: '저장된 검증 결과를 확인하기 전에는 실행할 수 없습니다.',
    },
  };
  const m = payload.metrics || {};
  const total = Number(m.validation_total || 0);
  const allow = Number(m.validation_allow || 0);
  const rate = total ? `${Math.round((allow / total) * 100)}%` : '—';
  const metrics = [
    { label: '누적 실행 기록', value: String(m.executions || 0), badge: payload.backend === 'postgres' ? 'PostgreSQL' : 'SQLite', tone: 'ok' },
    { label: '활성 작업 세션', value: String(m.active_sessions || 0), badge: '실시간', tone: 'ok' },
    { label: '안전 검증 통과 지수', value: rate, badge: `차단 ${m.validation_block || 0}건`, tone: Number(m.validation_block || 0) ? 'warn' : 'ok' },
  ];
  const catalog = payload.catalog_robots || [];
  const robots = catalog.length ? catalog.map((r) => {
    const simulator = r.robot_code === 'FR3-SIM-01';
    const registered = simulator && payload.workcell?.registered;
    const online = r.status_code === 'ONLINE';
    return ({
    name: r.model_name || r.robot_code,
    meta: `${r.robot_code}${simulator ? ' · Gazebo' : ''}`,
    state: registered ? '시뮬레이터 등록' : online ? '연결됨' : '오프라인',
    tone: registered || online ? 'info' : 'idle',
    task: registered ? (payload.workcell?.detail || 'FR3 작업셀 명령 대기')
      : r.last_seen_at ? `최근 신호 ${new Date(r.last_seen_at).toLocaleString('ko-KR')}` : '최근 연결 기록 없음',
    progress: registered || online ? 1 : 0,
    load: 'DB 등록',
    temp: r.is_enabled ? '활성' : '비활성',
  }); }) : [];
  const history = (payload.history || []).map((h) => ({
    id: h.execution_id,
    task: h.utterance,
    robot: h.robot_id,
    check: String(h.validation_decision || '미검증').toUpperCase(),
    checkTone: h.validation_decision === 'allow' ? 'ok' : 'warn',
    status: h.result_state || '실행 중',
    statusTone: h.task_succeeded === 1 ? 'ok' : h.task_succeeded === 0 ? 'danger' : 'warn',
  }));
  const alerts = (payload.alerts || []).map((a) => ({
    level: a.collision_decision === 'block' ? '위험' : '경고',
    tone: a.collision_decision === 'block' ? 'danger' : 'warn',
    robot: payload.robot?.robot_id || '시뮬레이터',
    ago: new Date(Number(a.at) * 1000).toLocaleTimeString('ko-KR'),
    text: `${a.step}: ${a.detail || a.reason_code || a.state || '검증 실패'}`,
  }));
  const latest = payload.latest || {};
  const steps = latest.plan?.plan?.steps || [];
  const command = latest.request ? {
    utterance: `“${latest.request.utterance}”`,
    target: `요청 ID: ${latest.request.request_id}`,
    plan: steps.slice(0, 5).map((step) => ({
      done: true,
      text: step.description || `${step.skill || step.skill_id || '작업'} ${step.resource_id || ''}`.trim(),
    })),
    checks: [
      `• 최근 검증: ${(latest.validation?.decision || '미실행').toUpperCase()}`,
      `• 저장소: ${payload.backend}${payload.schema ? ` / ${payload.schema}` : ''}`,
    ],
    holdReason: latest.validation?.detail || '실행 전 승인과 최신 상태 확인이 필요합니다.',
  } : {
    utterance: '저장된 명령이 없습니다.', target: '', plan: [],
    checks: ['• 최근 검증 없음', `• 저장소: ${payload.backend}`],
    holdReason: '새 명령과 검증 결과가 PostgreSQL에 기록되면 여기에 표시됩니다.',
  };
  return {
    metrics, robots, history: history.length ? history : [],
    alerts: alerts.length ? alerts : [], command,
  };
}

export default function App() {
  // null(평소) | 'running'(안전 검사 중) | 'stopping'(검사 중 비상 정지) | 'danger'(위험 판정)
  const [sim, setSim] = useState(null);
  const [simAt, setSimAt] = useState(null);
  const now = useNow();
  const page = usePage();
  const nav = NAV.find((item) => item.id === page);
  const dashboard = useDashboard();
  const model = dashboardModel(dashboard.payload);
  const [commandSubmit, setCommandSubmit] = useState({ pending: false, tone: '', message: '' });

  const open = (state) => { setSim(state); setSimAt(new Date()); };

  // 헤더 비상 정지는 오버레이 밖이라 언제든 누를 수 있다(피그마 메모). 목업이라 실제 로봇에는
  // 아무것도 보내지 않고, 검사 중이면 '정지 확인' 상태를 보여준다.
  async function globalStop() {
    try {
      const response = await fetch('/v1/stop', {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}',
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      open('stopping');
    } catch {
      open('danger');
    }
  }

  async function submitCommand(utterance) {
    const text = utterance.trim();
    if (!text || commandSubmit.pending) return false;
    setCommandSubmit({ pending: true, tone: 'info', message: '세션을 확인하고 계획을 생성하는 중입니다.' });
    try {
      let sessionId = window.sessionStorage.getItem('forstick2.session_id');
      if (!sessionId) {
        const sessionResponse = await fetch('/v1/sessions', {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ origin: 'dashboard2' }),
        });
        const session = await sessionResponse.json();
        if (!sessionResponse.ok) throw new Error(session.detail || `세션 생성 실패 (HTTP ${sessionResponse.status})`);
        sessionId = session.session_id;
        window.sessionStorage.setItem('forstick2.session_id', sessionId);
      }

      const response = await fetch('/v1/plan', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, utterance: text }),
      });
      const result = await response.json();
      if (!response.ok || result.ok === false) {
        const reasonMessage = {
          'plan.llm_unavailable': '계획 모델 서버가 연결되지 않았습니다. 모델 서버를 실행한 뒤 다시 시도하세요.',
          'robot.not_registered': '명령을 수행할 로봇이 아직 연결되지 않았습니다.',
          'plan.slot_incomplete': '명령에 대상이나 위치가 부족합니다. 작업 대상을 더 구체적으로 입력하세요.',
        }[result.reason_code];
        throw new Error(result.detail || result.clarification || reasonMessage || result.reason_code || `계획 생성 실패 (HTTP ${response.status})`);
      }
      setCommandSubmit({ pending: false, tone: 'ok', message: '명령을 저장하고 안전 계획을 생성했습니다.' });
      return true;
    } catch (error) {
      setCommandSubmit({ pending: false, tone: 'danger', message: error.message || '명령을 전송하지 못했습니다.' });
      return false;
    }
  }

  return <div className="app">
    <Sidebar page={page} />
    <div className="main">
      <Header title={nav.title} now={now} onStop={globalStop} live={Boolean(dashboard.payload)} />
      <div className="body">
        <div className="col-main">
          {page === 'home' && <Home model={model} />}
          {page === 'robots' && <Robots />}
          {page === 'history' && <History />}
          {page === 'diagnostics' && <Diagnostics />}
          {page === 'settings' && <Settings />}
        </div>
        <div className="col-side">
          <CommandPanel command={model.command} submission={commandSubmit} onSubmit={submitCommand} />
          {page === 'home' && <Alerts alerts={model.alerts} />}
        </div>
      </div>
    </div>
    {sim && <SimOverlay state={sim} at={simAt} onClose={() => setSim(null)} onRerun={() => open('running')} />}
    <DemoBar state={sim} backend={dashboard.payload?.backend} error={dashboard.error} onChange={(state) => (state ? open(state) : setSim(null))} />
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

function Header({ title, now, onStop, live }) {
  return <header className="header">
    <div className="header-title"><h1>{title}</h1><span className="chip-site">스마트 팩토리 B동</span></div>
    <div className="header-right">
      <button className="estop" onClick={onStop} title="백엔드 전체 정지 API로 전달됩니다">비상 정지</button>
      <span className="grade">DB·시뮬레이터: <b>{live ? '연결됨' : '연결 중'}</b></span>
      <span className="risk-badge">{live ? 'PostgreSQL 실시간' : '연결 확인 중'}</span>
      <img src={headerDivider} alt="" className="header-divider" />
      <time>{clockText(now)}</time>
    </div>
  </header>;
}

function Home({ model }) {
  const { metrics, robots, history } = model;
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
          {!robots.length && <div className="card robot"><span className="muted">등록된 로봇 정보를 불러오는 중입니다.</span></div>}
        </div>
      </section>
      <section>
        <h2>최근 지시 작업 진행 내역 (감사 이력)</h2>
        <div className="card table">
          <div className="tr th"><span>ID</span><span>수행 작업 내용</span><span>담당 로봇</span><span>안전 검증</span><span>상태</span></div>
          {history.map((h) => <div key={h.id} className="tr">
            <b>{h.id}</b><span className="muted">{h.task}</span><span>{h.robot}</span><b className={h.checkTone}>{h.check}</b><b className={h.statusTone}>{h.status}</b>
          </div>)}
          {!history.length && <div className="tr"><span>—</span><span className="muted">저장된 실행 이력이 없습니다.</span><span>—</span><span>—</span><span>—</span></div>}
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

function Alerts({ alerts }) {
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
      {!alerts.length && <div className="card alert"><div><p className="muted">현재 저장된 안전 경고가 없습니다.</p></div></div>}
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
        <p>안전 검증을 통과한 계획만 실행한다는 원칙에 따라 충돌 회피 검증 생략과 안전 구역 감속 제한 해제는 설정으로 바꿀 수 없습니다. (안)</p>
      </div>
    </section>
  </>;
}

function CommandPanel({ command, submission, onSubmit }) {
  const [utterance, setUtterance] = useState('');
  async function submit(event) {
    event.preventDefault();
    const sent = await onSubmit(utterance);
    if (sent) setUtterance('');
  }
  return <section className="command">
    <div className="command-head"><span><img src={dotPanel} alt="" width="8" height="8" />통합 작업 명령 패널</span><small>정지 상태 확인 필요</small></div>
    <div className="step">
      <div className="step-title info">STEP 1. 자연어 명령 입력 (음성/텍스트)<img src={mic} alt="" width="14" height="14" /></div>
      <form className="command-form" onSubmit={submit}>
        <label htmlFor="command-utterance">새 작업 명령</label>
        <textarea
          id="command-utterance"
          value={utterance}
          onChange={(event) => setUtterance(event.target.value)}
          placeholder="예: 1번 팔레트의 자재를 컨베이어로 옮겨줘"
          rows="3"
          disabled={submission.pending}
        />
        <button type="submit" disabled={!utterance.trim() || submission.pending}>
          {submission.pending ? '계획 생성 중…' : '명령 전송 · 계획 생성'}
        </button>
        {submission.message && <p className={`command-feedback ${submission.tone}`}>{submission.message}</p>}
      </form>
      <span className="command-latest">최근 저장 명령</span>
      <p className="utterance">{command.utterance}</p>
      <small>{command.target}</small>
    </div>
    <div className="step">
      <div className="step-title warn">STEP 2. 자연어 기반 기계 작업 계획 구조화</div>
      <ul className="plan">
        {command.plan.map((p) => <li key={p.text} className={p.done ? 'done' : ''}><img src={p.done ? checkSquare : loaderCircle} alt="" width="14" height="14" />{p.text}</li>)}
      </ul>
    </div>
    <div className="step step-warn">
      <div className="step-title warn">STEP 3. 계획 검증 · 전체 안전 미확인<img src={shieldCheck} alt="" width="14" height="14" /></div>
      <div className="checks"><b>{command.checks[0]}</b><span>{command.checks[1]}</span></div>
    </div>
    <div className="decide">
      <button className="hold" disabled><img src={playCircle} alt="" width="18" height="18" />실행 승인 보류 · 위험 확인</button>
      <p>{command.holdReason}</p>
      <button className="panel-estop" disabled title="비상 정지는 헤더 버튼으로만 실행합니다"><img src={alertOctagon} alt="" width="22" height="22" />비상 정지 · 별도 조작</button>
    </div>
  </section>;
}

const STAGES = ['명령·범위 확인', '로봇 상태 확인', '안전 검증'];

function SimOverlay({ state, at, onClose, onRerun }) {
  const [scene, setScene] = useState({ status: 'connecting', fps: 0, snapshotAt: null });
  const onView = useCallback((view) => setScene(view), []);
  const time = at ? timeText(at) : '';
  const card = {
    running: {
      dot: dotSimRunning, title: 'STEP 3. 안전 검사 실행 중', titleTone: '', sub: sceneLabel(scene),
      section: 'STEP 3 세부 진행 (검증 하위 단계)',
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
      dot: dotSimDanger, title: 'STEP 3. 안전 검사 결과 — 실행 불가', titleTone: 'danger', sub: '검증 완료 · 위험 감지',
      section: 'STEP 3 완료 (위험 판정)',
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
        {state === 'running' && <SceneView onView={onView} />}
        {state === 'stopping' && <p className="sim-message">로봇이 현재 위치에서 정지했습니다<br />(정지 확인됨 · 이 명령의 이전 검증 결과는 폐기되었습니다)</p>}
        {state === 'danger' && <div className="sim-message left">
          <b>• 충돌 위협 반경 분석: 위험 감지 (BLOCKED)</b>
          <span><b>• 공동 안전 구역 센서 </b>피드백: Beta 펜스 2 경고 미해결</span>
          <span className="gap">안전 검증을 통과하지 못해 이 계획은 로봇에 전달되지 않았습니다.</span>
        </div>}
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

/** 화면 상태 점검 도구. 데이터와 STOP은 백엔드에 연결되며, 이 버튼은 오버레이 표현만 바꾼다. */
function DemoBar({ state, backend, error, onChange }) {
  const options = [[null, '기본'], ['running', '안전 검사 중'], ['stopping', '정지 확인'], ['danger', '위험 판정']];
  return <div className="demo-bar">
    <span>{backend ? `LIVE · ${backend.toUpperCase()} + Gazebo` : `연결 대기 · ${error || '백엔드 확인 중'}`}</span>
    {options.map(([value, label]) => <button key={label} className={state === value ? 'on' : ''} onClick={() => onChange(value)}>{label}</button>)}
  </div>;
}
