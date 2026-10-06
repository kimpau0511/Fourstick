import { useState } from 'react';

// 표 행 펼침 상태(ExpandRow.jsx의 버튼·상세 행과 같이 쓴다). 훅은 컴포넌트 파일과 분리 — fast refresh 규칙.
export function useExpand() {
  const [open, setOpen] = useState(() => new Set());
  const toggle = (key) => setOpen((s) => { const n = new Set(s); if (!n.delete(key)) n.add(key); return n; });
  return { isOpen: (key) => open.has(key), toggle };
}
