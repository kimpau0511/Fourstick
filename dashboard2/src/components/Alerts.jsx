import { useState } from 'react';

const TONE = { EMERGENCY: 'danger', WARNING: 'warn' };
const LEVEL = { EMERGENCY: '긴급', WARNING: '주의' };
const hhmmss = (ms) => new Date(ms).toTimeString().slice(0, 8);

// 서버 상태에서 만든 알림(server.alerts)만 보여 준다. 생명주기(§8): 발생 → "확인" 클릭 → 확인됨(이 화면 상태, 서버 기록 아님).
export default function Alerts({ server }) {
  const [acked, setAcked] = useState(() => new Set());
  const list = (server?.alerts || []).filter((a) => a.level === 'EMERGENCY' || a.level === 'WARNING');
  return <section>
    <div className="alerts-head"><h2>공정 실시간 안전 알림</h2></div>
    <div className="alerts">
      {list.length === 0 && <p className="muted">현재 알림 없음</p>}
      {list.map((a) => <div key={a.id} className={`alert ${TONE[a.level]}`}>
        <i />
        <div>
          <div className="alert-head"><span><em className={`tag ${TONE[a.level]}`}>{LEVEL[a.level]}</em>{a.robot && <b>{a.robot}</b>}</span><small>{hhmmss(a.since)} 발생</small></div>
          <p title={a.text}>{a.text}</p>
          {acked.has(a.id)
            ? <small className="muted">확인됨</small>
            : <button type="button" className="alert-ack" onClick={() => setAcked((s) => new Set(s).add(a.id))}>확인</button>}
        </div>
      </div>)}
    </div>
  </section>;
}
