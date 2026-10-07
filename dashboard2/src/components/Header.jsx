import { useState } from 'react';
import alertOctagon from '../assets/alert-octagon.svg';
import Spinner from './Spinner.jsx';
import { StatusCount } from './StatusWidget.jsx';

const pad = (n) => String(n).padStart(2, '0');
const clock = (ms) => { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };

// 연결 상태(㉔·§7): ok면 수신 시각을 옅게, 끊기면 경고. 연결 중은 판단 전이라 "연결 확인 중".
function connText(conn) {
  if (conn.status === 'ok') return { cls: 'conn muted', text: `수신 ${clock(conn.lastReceivedAt)}` };
  if (conn.status === 'connecting') return { cls: 'conn muted', text: conn.lastReceivedAt ? `연결 확인 중 · 수신 ${clock(conn.lastReceivedAt)}` : '연결 확인 중' };
  return { cls: 'conn warn', text: `연결 끊김${conn.ageSec == null ? '' : ` · ${Math.round(conn.ageSec)}초 전 데이터`}` };
}

// 즉시 정지 ↔ 정지 해제(2026-10-07): 서버 정지 래치(stop_diagnostics.stop_latch_active)가 걸려 있으면 같은 자리 버튼이
// '정지 해제'가 된다. 해제는 서버(/v1/stop/release)가 판단한다 — 움직이는 작업이 남아 있으면 거부하고 사유를 준다.
// 해제는 로봇을 움직이지 않는다. 멈춘 작업을 이어서 할지(재개)·되돌릴지(복구)는 시뮬레이션 보기에서 따로 고른다.
export default function Header({ title, onStop, server, drawerOpen, onToggleDrawer }) {
  const [releasing, setReleasing] = useState(false);
  const [releaseNote, setReleaseNote] = useState(null);
  const latched = server.robots?.stop_diagnostics?.stop_latch_active === true;
  async function stop() {
    setReleaseNote(null);
    await onStop();
    // onStop은 정지 요청을 기다리지 않을 수 있다 — 래치 상태를 잠시 뒤 두 번 다시 읽는다.
    setTimeout(() => server.refresh?.(), 500);
    setTimeout(() => server.refresh?.(), 1500);
  }
  async function release() {
    setReleasing(true);
    try {
      const res = await fetch('/v1/stop/release', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}', cache: 'no-store' });
      const body = await res.json().catch(() => ({}));
      setReleaseNote(body.released ? null : body.detail || body.error || `정지 해제 실패 (${res.status})`);
    } catch (error) {
      setReleaseNote(`정지 해제 요청 실패: ${error.message}`);
    } finally {
      await server.refresh?.();
      setReleasing(false);
    }
  }
  const conn = connText(server.conn);
  const robot = server.health?.robot;
  const mode = robot ? (robot.is_simulated ? '시뮬레이션' : '실기') : '정보 없음';
  return <header className="header">
    <div className="header-title"><h1>{title}</h1></div>
    <div className="header-right">
      <span className={conn.cls} role="status">{server.conn.status === 'connecting' && <><Spinner size={12} label="연결 확인 중" />{' '}</>}{conn.text}</span>
      <span className="grade">로봇 상태: <b>{server.robotStatus.label || '확인 중'}</b></span>
      <span className="muted header-meta">운전 모드: {mode}</span>
      <span className="muted header-meta">제어권: 정보 없음</span>
      <StatusCount server={server} />
      <button type="button" className="drawer-btn" aria-expanded={drawerOpen} aria-controls="command-drawer" onClick={onToggleDrawer}>명령 패널</button>
      {releaseNote && <span className="warn header-meta" role="status">{releaseNote}</span>}
      {latched
        ? <button className="estop estop-release" onClick={release} disabled={releasing} title="정지 래치를 풉니다 — 로봇은 움직이지 않습니다. 멈춘 작업은 시뮬레이션 보기에서 재개·복구합니다">▶ {releasing ? '해제 중…' : '정지 해제'}</button>
        : <button className="estop" onClick={stop} title="실행 중인 작업 전체에 정지를 요청합니다"><img src={alertOctagon} alt="" width="20" height="20" />즉시 정지</button>}
    </div>
  </header>;
}
