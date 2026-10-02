import { useState } from 'react';

// 긴급 배너(⑤·§5·6): EMERGENCY만. 닫기 없음 — "인지 확인"은 축소일 뿐 제거가 아니다.
// 인지 상태는 화면 상태(§8: 발생 → 확인 클릭 → 확인됨). 새 긴급이 생기면 다시 펼쳐진다.
export default function EmergencyBanner({ alerts }) {
  const list = alerts.filter((a) => a.level === 'EMERGENCY');
  const key = list.map((a) => a.id).join('|');
  const [acked, setAcked] = useState('');
  if (!list.length) return null;
  const folded = acked === key;
  return <div className={folded ? 'emergency folded' : 'emergency'} role="alert">
    <b>긴급 {list.length}건</b>
    {!folded && <span className="emergency-text">대표: {list[0].text}</span>}
    {folded ? <span className="emergency-text">인지 확인됨 · {list[0].text}</span>
      : <button type="button" className="emergency-ack" onClick={() => setAcked(key)}>인지 확인</button>}
  </div>;
}
