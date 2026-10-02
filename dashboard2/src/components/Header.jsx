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

export default function Header({ title, onStop, server, drawerOpen, onToggleDrawer }) {
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
      <button className="estop" onClick={onStop} title="실행 중인 시연 작업에 정지를 요청합니다"><img src={alertOctagon} alt="" width="20" height="20" />즉시 정지</button>
    </div>
  </header>;
}
