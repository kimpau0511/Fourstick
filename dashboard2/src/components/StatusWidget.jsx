import { useWidgetPrefs } from '../widgetPrefs.js';
// System status 위젯(가-4·§15). 서버 값만 쓰고, 값이 없으면 "수신 없음" — 정상으로 세지 않는다(설계원칙 4).
const STATE = {
  ok: { icon: '●', text: '정상', cls: 'ok' },
  warn: { icon: '▲', text: '경고', cls: 'warn' },
  fail: { icon: '■', text: '장애', cls: 'danger' },
  none: { icon: '?', text: '수신 없음', cls: 'muted' },
};

const ROWS = [['ros2', 'ROS 2'], ['planner', 'PLANNER'], ['safety-plc', 'SAFETY PLC'], ['latency', 'LATENCY']];

// 반환: [{ id, label, state }] — 사이드바 위젯과 헤더 "N/4 정상"이 같은 계산을 쓴다.
function systemRows(server) {
  const { health, robots, robotsFailing, conn, config } = server;
  // 연결이 정상이 아니면 보관된 마지막 값으로 '정상'이라 하지 않는다 — 전부 '수신 없음'(설계원칙 4).
  if (conn?.status !== 'ok') return ROWS.map(([id, label]) => ({ id, label, state: 'none' }));
  const ros = robots?.stop_diagnostics && health?.robot && !robotsFailing ? (robots.stop_diagnostics.available && health.robot.configured ? 'ok' : 'fail') : 'none';
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

// 숨긴 행을 뺀 보이는 행 계산 — 사이드바 위젯과 헤더가 같이 쓴다. 숨겨도 각 행의 판정(state)은 그대로다.
function visibleRows(server, hidden) {
  const all = systemRows(server);
  const rows = all.filter((r) => !hidden.has(r.id));
  return { rows, hiddenCount: all.length - rows.length };
}

export default function StatusWidget({ server }) {
  const { hidden } = useWidgetPrefs();
  const { rows, hiddenCount } = visibleRows(server, hidden);
  return <div className="status-widget" role="group" aria-label="System status">
    <div className="status-head"><b>SYSTEM STATUS</b>{rows.length > 0 && <small>{okCount(rows)}/{rows.length} 정상{hiddenCount > 0 && <em style={{ fontStyle: "normal", fontWeight: 500, fontSize: 10 }}> · 숨김 {hiddenCount}</em>}</small>}</div>
    {rows.length === 0 && <a className="status-row" href="#/diagnostics"><span className="status-name rail-hide">표시할 항목 없음 — 실시간 진단에서 켜기</span></a>}
    {rows.map((r) => {
      const s = STATE[r.state];
      return <a key={r.id} className="status-row" href={`#/diagnostics?service=${r.id}`} title={`${r.label}: ${s.text}`}>
        <span className="status-name">{r.label}</span>
        <span className={`status-val ${s.cls}`}><i aria-hidden="true">{s.icon}</i><span className="rail-hide">{r.extra && r.state !== 'none' ? `${s.text} · ${r.extra}` : s.text}</span></span>
      </a>;
    })}
  </div>;
}

// 헤더용 "N/M 정상" — 사이드바 위젯과 같은 계산(M = 보이는 행 수).
export function StatusCount({ server }) {
  const { hidden } = useWidgetPrefs();
  const { rows } = visibleRows(server, hidden);
  // 전부 숨기면 '0/0 정상'처럼 정상으로 읽히지 않게 숨김이라고만 적는다.
  if (!rows.length) return <span className="muted header-meta">상태 위젯 숨김</span>;
  return <span className="muted header-meta">{okCount(rows)}/{rows.length} 정상</span>;
}
