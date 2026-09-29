import { useCallback, useEffect, useState } from 'react';
import { alerts, command, history, metrics, robots } from './data.js';
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
import headerDivider from './assets/header-divider.svg';
import house from './assets/house.svg';
import loaderCircle from './assets/loader-circle.svg';
import logs from './assets/logs.svg';
import mic from './assets/mic.svg';
import playCircle from './assets/play-circle.svg';
import settings from './assets/settings.svg';
import shieldCheck from './assets/shield-check.svg';

const NAV = [
  { label: '홈', icon: house, active: true },
  { label: '로봇 관리', icon: circleX },
  { label: '기록 및 로그', icon: logs },
  { label: '실시간 진단', icon: chartLine },
  { label: '관제 설정', icon: settings },
];

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

export default function App() {
  // null(평소) | 'running'(안전 검사 중) | 'stopping'(검사 중 비상 정지) | 'danger'(위험 판정)
  const [sim, setSim] = useState(null);
  const [simAt, setSimAt] = useState(null);
  const now = useNow();

  const open = (state) => { setSim(state); setSimAt(new Date()); };

  // 헤더 비상 정지는 오버레이 밖이라 언제든 누를 수 있다(피그마 메모). 목업이라 실제 로봇에는
  // 아무것도 보내지 않고, 검사 중이면 '정지 확인' 상태를 보여준다.
  function globalStop() {
    if (sim === 'running') open('stopping');
  }

  return <div className="app">
    <Sidebar />
    <div className="main">
      <Header now={now} onStop={globalStop} />
      <Home />
    </div>
    {sim && <SimOverlay state={sim} at={simAt} onClose={() => setSim(null)} onRerun={() => open('running')} />}
    <DemoBar state={sim} onChange={(state) => (state ? open(state) : setSim(null))} />
  </div>;
}

function Sidebar() {
  return <aside className="sidebar">
    <div>
      <div className="brand">
        <span className="brand-mark"><img src={bot} alt="" width="18" height="18" /></span>
        <div><strong>FORSTICK</strong><small>CONTROL PANEL</small></div>
      </div>
      <nav className="nav">
        {NAV.map((item) => <button key={item.label} className={item.active ? 'nav-item active' : 'nav-item'} disabled={!item.active} title={item.active ? undefined : '다음 단계에서 구현 예정'}>
          <span className="nav-icon"><img src={item.icon} alt="" /></span>{item.label}
        </button>)}
      </nav>
    </div>
    <div className="profile">
      <div className="profile-user"><img src={avatar} alt="" /><div><strong>김민우 조장</strong><small>제2공정 주간 근무</small></div></div>
      <span className="network"><img src={dotNetwork} alt="" width="6" height="6" />미들웨어 네트워크 정상</span>
    </div>
  </aside>;
}

function Header({ now, onStop }) {
  return <header className="header">
    <div className="header-title"><h1>종합 관제 대시보드</h1><span className="chip-site">스마트 팩토리 B동</span></div>
    <div className="header-right">
      <button className="estop" onClick={onStop} title="목업: 실제 로봇에는 전달되지 않습니다">비상 정지</button>
      <span className="grade">실시간 공장 안전 등급: <b>안전</b></span>
      <span className="risk-badge">주의 · Beta</span>
      <img src={headerDivider} alt="" className="header-divider" />
      <time>{clockText(now)}</time>
    </div>
  </header>;
}

function Home() {
  return <div className="body">
    <div className="col-main">
      <section>
        <h2>오늘의 핵심 운영 지표</h2>
        <div className="row3">
          {metrics.map((m) => <div key={m.label} className="card metric">
            <span className="metric-label">{m.label}</span>
            <div className="metric-value"><strong>{m.value}{m.unit && <small>{m.unit}</small>}</strong><span className={`tag ${m.tone}`}>{m.badge}</span></div>
          </div>)}
        </div>
      </section>
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
    </div>
    <div className="col-side">
      <CommandPanel />
      <section>
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
      </section>
    </div>
  </div>;
}

function CommandPanel() {
  return <section className="command">
    <div className="command-head"><span><img src={dotPanel} alt="" width="8" height="8" />통합 작업 명령 패널</span><small>정지 상태 확인 필요</small></div>
    <div className="step">
      <div className="step-title info">STEP 1. 자연어 명령 입력 (음성/텍스트)<img src={mic} alt="" width="14" height="14" /></div>
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

/** 목업 상태 전환 도구. 실제 흐름(명령 → 계획 → 검사)이 백엔드와 연결되기 전까지 오버레이
 *  상태를 확인하기 위한 것이다. */
function DemoBar({ state, onChange }) {
  const options = [[null, '기본'], ['running', '안전 검사 중'], ['stopping', '정지 확인'], ['danger', '위험 판정']];
  return <div className="demo-bar">
    <span>DEMO · 목업 데이터 (영상만 실제 Gazebo)</span>
    {options.map(([value, label]) => <button key={label} className={state === value ? 'on' : ''} onClick={() => onChange(value)}>{label}</button>)}
  </div>;
}
