import { jointRows } from '../simPanels.js';
import './joints.css';

// 관절 상태(2026-10-07). 시뮬레이션 보기 왼쪽(좁으면 아래). 값은 SimView3D의 관측 스트림 원본(rad)이고
// 허용 범위는 서버가 준 현재 로봇 Profile 한계(/v1/sim-view/model joint_limits) — 화면에 숫자를 두지 않는다.
// 관측이 없거나 낡았거나 숫자가 아니면 '—'. 범위 밖 판정은 반올림 전 원본(rad)으로 한다. 표시만 한다(제어와 무관).
export default function JointPanel({ data }) {
  const rows = jointRows(data);
  return <aside className="joint-panel" aria-label="관절 상태">
    <h3>관절 상태</h3>
    {rows.length === 0 ? <p className="joint-empty">—</p> : <ol className="joint-list">
      {rows.map((r) => <li key={r.key} className={r.out ? 'out' : ''} data-joint={r.label}>
        <span className="joint-name">{r.label}</span>
        <b className="joint-value" aria-label={`${r.label} 현재 각도`}>{r.text}{r.out && <span className="sr-only"> 허용 범위 밖</span>}</b>
        <small className="joint-range" aria-label={`${r.label} 허용 범위`}>{r.range}</small>
      </li>)}
    </ol>}
  </aside>;
}
