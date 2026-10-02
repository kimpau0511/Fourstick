/** 목업 상태 전환 도구. 실제 흐름(명령 → 계획 → 검사)이 백엔드와 연결되기 전까지 오버레이
 *  상태를 확인하기 위한 것이다. */
export default function DemoBar({ state, onChange }) {
  const options = [[null, '기본'], ['running', '안전 검사 중'], ['stopping', '정지 확인'], ['danger', '위험 판정']];
  return <div className="demo-bar">
    <span>DEMO · 시뮬레이션 창 상태 미리보기(화면만 바뀜, 로봇 명령 없음)</span>
    {options.map(([value, label]) => <button key={label} className={state === value ? 'on' : ''} onClick={() => onChange(value)}>{label}</button>)}
  </div>;
}
