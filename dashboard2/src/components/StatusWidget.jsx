// System status 위젯(가-4·§15). 서버 값만 쓰고, 값이 없으면 "수신 없음" — 정상으로 세지 않는다(설계원칙 4).
const STATE = {
  ok: { icon: '●', text: '정상', cls: 'ok' },
  warn: { icon: '▲', text: '경고', cls: 'warn' },
  fail: { icon: '■', text: '장애', cls: 'danger' },
  none: { icon: '?', text: '수신 없음', cls: 'muted' },
};

// 반환: [{ id, label, state }] — 사이드바 위젯과 헤더 "N/4 정상"이 같은 계산을 쓴다.
function systemRows(server) {
  const { health, robots, conn, config } = server;
  const ros = robots?.stop_diagnostics && health?.robot ? (robots.stop_diagnostics.available && health.robot.configured ? 'ok' : 'fail') : 'none';
  const planning = health?.features?.planning;
  const planner = planning ? (planning.available ? 'ok' : 'fail') : 'none';
  // 정상 기준 = 정지 취소 응답 제한(stop.cancel_ack_timeout_sec). 서버 정책이 없으면 판단 불가.
  const limitSec = config?.policies?.stop?.cancel_ack_timeout_sec;
  const latency = typeof conn?.latencyMs !== 'number' || typeof limitSec !== 'number' ? 'none' : (conn.latencyMs < limitSec * 1000 ? 'ok' : 'warn');
  return [
    { id: 'ros2', label: 'ROS 2', state: ros },
    { id: 'planner', label: 'PLANNER', state: planner },
    { id: 'safety-plc', label: 'SAFETY PLC', state: 'none' }, // 서버에 값 없음
    { id: 'latency', label: 'LATENCY', state: latency, extra: typeof conn?.latencyMs === 'number' ? `${Math.round(conn.latencyMs)}ms` : null },
  ];
}

const okCount = (rows) => rows.filter((r) => r.state === 'ok').length;

export default function StatusWidget({ server }) {
  const rows = systemRows(server);
  return <div className="status-widget" role="group" aria-label="System status">
    <div className="status-head"><b>SYSTEM STATUS</b><small>{okCount(rows)}/4 정상</small></div>
    {rows.map((r) => {
      const s = STATE[r.state];
      return <a key={r.id} className="status-row" href={`#/diagnostics?service=${r.id}`} title={`${r.label}: ${s.text}`}>
        <span className="status-name">{r.label}</span>
        <span className={`status-val ${s.cls}`}><i aria-hidden="true">{s.icon}</i><span className="rail-hide">{r.extra && r.state !== 'none' ? `${s.text} · ${r.extra}` : s.text}</span></span>
      </a>;
    })}
  </div>;
}

// 헤더용 "N/4 정상" — 사이드바 위젯과 같은 계산.
export function StatusCount({ server }) {
  return <span className="muted header-meta">{okCount(systemRows(server))}/4 정상</span>;
}
