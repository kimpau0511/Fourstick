import { useState } from 'react';

// 긴급 배너(⑤·§5·6): EMERGENCY만. 닫기 없음 — "인지 확인"은 축소일 뿐 제거가 아니다.
// 인지 상태는 화면 상태(§8: 발생 → 확인 클릭 → 확인됨). 인지 키는 `${id}@${since}` — 사라졌다 다시 생긴 긴급은 새 since를 받아 다시 펼쳐진다.
// 접힌 뒤에도 "다시 펼치기"로 되돌릴 수 있다.
export default function EmergencyBanner({ alerts }) {
  const list = alerts.filter((a) => a.level === 'EMERGENCY');
  const key = list.map((a) => `${a.id}@${a.since}`).join('|');
  const [acked, setAcked] = useState('');
  if (!list.length) return null;
  const folded = acked === key;
  return <div className={folded ? 'emergency folded' : 'emergency'} role="alert">
    <b>긴급 {list.length}건</b>
    {!folded && <span className="emergency-text">대표: {list[0].text}</span>}
    {folded ? <><span className="emergency-text">인지 확인됨 · {list[0].text}</span>
      <button type="button" className="emergency-ack" onClick={() => setAcked('')}>다시 펼치기</button></>
      : <button type="button" className="emergency-ack" onClick={() => setAcked(key)}>인지 확인</button>}
  </div>;
}
