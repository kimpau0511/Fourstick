import { useEffect, useRef, useState } from 'react';
import Alerts from './components/Alerts.jsx';
import CommandPanel from './components/CommandPanel.jsx';
import EmergencyBanner from './components/EmergencyBanner.jsx';
import Header from './components/Header.jsx';
import Sidebar from './components/Sidebar.jsx';
import SimOverlay from './components/SimOverlay.jsx';
import { NAV } from './nav.js';
import { useServer } from './server.js';
import { overlayOf } from './simOverlayState.js';
import { useSimCommand } from './simCommand.js';
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

export default function App() {
  const [closedKey, setClosedKey] = useState(null); // 닫은 시뮬레이션 창의 상태 key — 같은 상태 동안은 다시 열지 않는다
  const server = useServer();
  const cmd = useSimCommand();
  const now = useNow();
  const page = usePage();
  const [panelW, setPanelW] = useState(PANEL.def);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const nav = NAV.find((item) => item.id === page);

  const overlay = overlayOf(cmd); // 시뮬레이션 창 상태 = 명령 흐름의 서버 값(null이면 창 없음)

  // 헤더 즉시 정지는 오버레이 밖이라 언제든 누를 수 있다(피그마 메모). 실행 중인 시연 작업에
  // 정지를 요청한다(/v1/sim-demo/stop). 결과(요청됨·실패·정지할 작업 없음)는 명령 패널에 서버 응답대로 뜬다.
  // 오버레이의 '정지 확인' 화면은 서버 값과 이어지지 않은 목업이라, 실제 정지 버튼으로는 열지 않는다
  // (응답 없이 "정지 확인"을 보이면 확인 안 된 정지를 확인된 것처럼 보인다 — 설계원칙 4).
  function globalStop() {
    cmd.stop();
  }

  return <div className="app" style={{ '--cmd-w': `${panelW}px` }}>
    <Sidebar page={page} server={server} />
    <div className="main">
      <Header title={nav.title} onStop={globalStop} server={server} drawerOpen={drawerOpen} onToggleDrawer={() => setDrawerOpen((v) => !v)} />
      <EmergencyBanner alerts={server.alerts} />
      <div className="body">
        <div className="col-main">
          {page === 'home' && <Home server={server} cmdJob={cmd.job} />}
          {page === 'robots' && <Robots server={server} />}
          {page === 'history' && <History server={server} log={cmd.log} />}
          {page === 'diagnostics' && <Diagnostics server={server} />}
          {page === 'settings' && <Settings server={server} />}
        </div>
        <PanelSeparator width={panelW} onChange={setPanelW} />
        <div className={drawerOpen ? 'col-side open' : 'col-side'} id="command-drawer">
          <CommandPanel now={now} sim={cmd} server={server} />
          {page === 'home' && <Alerts server={server} />}
        </div>
      </div>
    </div>
    {overlay && overlay.key !== closedKey && <SimOverlay overlay={overlay} cmd={cmd} server={server} onClose={() => setClosedKey(overlay.key)} />}
  </div>;
}
