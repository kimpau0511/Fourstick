import './spinner.css';

// 로딩 아이콘 — uiverse.io/mrhyddenn/black-bullfrog-16(막대 12개가 30°씩 돌며 차례로 흐려짐)을
// 라이트 테마 색(--muted)으로 옮겼다. size는 px(글자 크기 = 지름).
// decorative: 목록 안에 여러 개가 있을 때처럼 상태 문구가 따로 있으면 스크린리더가 반복해 읽지 않게 숨긴다.
export default function Spinner({ size = 16, label = '불러오는 중', decorative = false }) {
  const a11y = decorative ? { 'aria-hidden': true } : { role: 'status', 'aria-label': label };
  return <span className="spinner" {...a11y} style={{ fontSize: size }}>
    {Array.from({ length: 12 }, (_, i) => <span key={i} className="spinner-blade" style={{ transform: `rotate(${i * 30}deg)`, animationDelay: `${(i * 0.083).toFixed(3)}s` }} />)}
  </span>;
}
