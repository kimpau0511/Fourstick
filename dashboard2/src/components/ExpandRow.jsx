// 좁은 폭(768~1023)에서 표의 핵심이 아닌 열(col-extra)을 감추고, 행을 펼쳐 "라벨: 값"으로 보이는 공용 조각(상태는 useExpand).
// 넓은 폭에서는 버튼·상세 행 모두 CSS가 숨긴다(styles.css .row-expand · .row-detail).
// 행 클릭·Enter/Space(선택)와 겹치지 않게 이벤트를 여기서 끊는다.
export function ExpandButton({ open, onToggle }) {
  return <button type="button" className="row-expand" aria-expanded={open} aria-label="자세히"
    onClick={(e) => { e.stopPropagation(); onToggle(); }} onKeyDown={(e) => e.stopPropagation()}>{open ? '▾' : '▸'}</button>;
}

// span = 좁은 폭에서 보이는 핵심 열 수. items = [[라벨, 값], …]
export function DetailRow({ open, span, items }) {
  if (!open) return null;
  return <tr className="row-detail"><td colSpan={span}>{items.map(([label, value]) => <div key={label}>{label}: {value}</div>)}</td></tr>;
}
