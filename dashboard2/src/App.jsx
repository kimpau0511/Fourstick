import { useEffect, useRef, useState } from 'react';
import Alerts from './components/Alerts.jsx';
import CommandPanel from './components/CommandPanel.jsx';
import EmergencyBanner from './components/EmergencyBanner.jsx';
import Header from './components/Header.jsx';
import Sidebar from './components/Sidebar.jsx';
import SimOverlay from './components/SimOverlay.jsx';
import { LoginScreen, LogoutConfirm, SessionExpired } from './components/Login.jsx';
import { useAuth } from './auth.js';
import { NAV } from './nav.js';
import { previewServer, useServer } from './server.js';
import { overlayOf } from './simOverlayState.js';
import { useGeneralCommand } from './planCommand.js';
import { IDLE_COMMAND, useSimCommand } from './simCommand.js';

// 명령 경로. 기본은 시연 명령(결정 1). VITE_COMMAND_MODE=general(이 PC의 .env.local)이면 일반 경로(계획·안전 관문·승인·실행).
// 빌드마다 고정이라 훅 호출 순서는 바뀌지 않는다.
const useCommand = import.meta.env.VITE_COMMAND_MODE === 'general' ? useGeneralCommand : useSimCommand;
import Diagnostics from './pages/Diagnostics.jsx';
import History from './pages/History.jsx';
import Home from './pages/Home.jsx';
import Robots from './pages/Robots.jsx';
import Settings from './pages/Settings.jsx';

// 주소(#/robots 등)로 화면을 고른다 — 새로고침해도 같은 화면에 머문다.
function pageFromHash() {
  const id = window.location.hash.replace('#/', '').split('?')[0]; // #/diagnostics?service=… 처럼 뒤에 값이 붙을 수 있다
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

function useNow() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

// 명령 패널 폭(가-3): 기본 400은 피그마 값, 최소 320·최대 560은 입력칸·보내기가 보이는 범위.
const PANEL = { min: 320, max: 560, def: 400, step: 16 };
const clampW = (w) => Math.min(PANEL.max, Math.max(PANEL.min, w));

function PanelSeparator({ width, onChange }) {
  const drag = useRef(null);
  return <div className="col-sep" role="separator" aria-label="명령 패널 폭 조절" aria-orientation="vertical" tabIndex={0}
    aria-valuemin={PANEL.min} aria-valuemax={PANEL.max} aria-valuenow={width}
    onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); drag.current = { x: e.clientX, w: width }; }}
    onPointerMove={(e) => { if (drag.current) onChange(clampW(drag.current.w + drag.current.x - e.clientX)); }}
    onPointerUp={() => { drag.current = null; }}
    onPointerCancel={() => { drag.current = null; }}
    onKeyDown={(e) => {
      if (e.key === 'ArrowLeft') { e.preventDefault(); onChange(clampW(width + PANEL.step)); }
      if (e.key === 'ArrowRight') { e.preventDefault(); onChange(clampW(width - PANEL.step)); }
    }} />;
}

function Dashboard({ auth }) {
  return <DashboardView auth={auth} server={useServer()} cmd={useCommand()} now={useNow()} page={usePage()} />;
}

// 로그인 화면 뒤 배경. 그림 대신 실제 대시보드를 원래 크기로 그린다 — 화면 폭이 달라도 확대·흐림이 없다.
// 서버 값은 부르지 않는다(결정 Q2): 받은 것 없는 상태("연결 확인 중")와 명령 대기 상태만 넣는다.
// 페이지는 늘 홈 — 다른 페이지(기록·진단)는 스스로 서버를 부른다.
const NO_USER = { user: null, error: null, expired: false };
function DashboardPreview() {
  const [server] = useState(previewServer);
  const [now] = useState(() => new Date());
  return <div className="login-backdrop" inert="" aria-hidden="true">
    <DashboardView auth={NO_USER} server={server} cmd={IDLE_COMMAND} now={now} page="home" preview />
  </div>;
}

function DashboardView({ auth, server, cmd, now, page, preview = false }) {
  const [closedKey, setClosedKey] = useState(null); // 닫은 시뮬레이션 창의 상태 key — 같은 상태 동안은 다시 열지 않는다
  const [viewOpen, setViewOpen] = useState(false);
  const [panelW, setPanelW] = useState(PANEL.def);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [askLogout, setAskLogout] = useState(false);
  const nav = NAV.find((item) => item.id === page);

  const overlay = preview ? null : overlayOf(cmd); // 시뮬레이션 창 상태 = 명령 흐름의 서버 값(null이면 창 없음)
  // 수동 관측은 명령을 보내지 않는다. 작업 상태가 있으면 해당 상태를 그대로 보여 준다.
  const visibleOverlay = overlay && (viewOpen || overlay.key !== closedKey)
    ? overlay : viewOpen ? { state: 'viewing', key: 'viewing' } : null;
  // 로그인·로그아웃 창이 열리면 헤더(즉시 정지)만 남기고 뒤쪽은 키보드로도 닿지 않게 한다.
  const modal = askLogout || auth.expired ? '' : undefined;

  // 헤더 즉시 정지는 오버레이 밖이라 언제든 누를 수 있다(피그마 메모). 실행 중인 시연 작업에
  // 정지를 요청한다(/v1/sim-demo/stop). 결과(요청됨·실패·정지할 작업 없음)는 명령 패널에 서버 응답대로 뜬다.
  // 오버레이의 '정지 확인' 화면은 서버 값과 이어지지 않은 목업이라, 실제 정지 버튼으로는 열지 않는다
  // (응답 없이 "정지 확인"을 보이면 확인 안 된 정지를 확인된 것처럼 보인다 — 설계원칙 4).
  function globalStop() {
    cmd.stop();
  }

  // 로봇 작업 중이면 확인을 받는다 — 로그아웃은 작업을 멈추지 않는다(결정 Q6).
  function requestLogout() {
    // 이 탭의 명령 흐름 + 서버가 보고한 진행 작업(다른 탭·다른 사람이 시작한 작업도) 둘 다 본다.
    const busy = (overlay && ['running', 'stopping'].includes(overlay.state) && !overlay.confirmed) || !!server.simDemo?.running_job;
    if (busy) setAskLogout(true); else auth.logout();
  }

  return <div className={preview ? 'app preview' : 'app'} style={{ '--cmd-w': `${panelW}px` }}>
    <Sidebar page={page} server={server} user={auth.user} onLogout={requestLogout} logoutFailed={auth.error?.kind === 'logout_failed'} inert={modal} />
    <div className="main">
      <Header title={nav.title} onStop={globalStop} server={server} drawerOpen={drawerOpen} onToggleDrawer={() => setDrawerOpen((v) => !v)} />
      <EmergencyBanner alerts={server.alerts} />
      <div className="body" inert={modal}>
        <div className="col-main">
          {page === 'home' && <Home server={server} cmdJob={cmd.job} />}
          {page === 'robots' && <Robots server={server} />}
          {page === 'history' && <History server={server} log={cmd.log} />}
          {page === 'diagnostics' && <Diagnostics server={server} />}
          {page === 'settings' && <Settings server={server} />}
        </div>
        <PanelSeparator width={panelW} onChange={setPanelW} />
        <div className={drawerOpen ? 'col-side open' : 'col-side'} id="command-drawer">
          <CommandPanel now={now} sim={cmd} server={server} onOpenSimulation={() => setViewOpen(true)} />
          {page === 'home' && <Alerts server={server} />}
        </div>
      </div>
    </div>
    {visibleOverlay && <SimOverlay overlay={visibleOverlay} cmd={cmd} server={server} onClose={() => {
      setViewOpen(false);
      if (overlay) setClosedKey(overlay.key);
    }} />}
    {askLogout && <LogoutConfirm onCancel={() => setAskLogout(false)} onConfirm={() => { setAskLogout(false); auth.logout(); }} />}
    {auth.expired && <SessionExpired auth={auth} />}
  </div>;
}

// 로그인한 뒤에만 대시보드를 만든다 — 로그인 전에는 서버 값을 부르지 않는다(결정 Q2).
// 주소(#/history 등)는 그대로라 로그인하면 원래 가려던 화면이 열린다(결정 Q7).
export default function App() {
  const auth = useAuth();
  if (auth.status === 'signed_in') return <Dashboard auth={auth} />;
  if (auth.status === 'checking') return <LoginScreen auth={auth} />;
  return <><DashboardPreview /><LoginScreen auth={auth} /></>;
}
