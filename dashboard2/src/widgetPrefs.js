import { useCallback, useMemo, useSyncExternalStore } from 'react';

// 사이드바 상태 위젯에서 숨길 행 id 목록. localStorage가 막혀도 이 탭 안에서는 mem으로 동작한다.
const KEY = 'forstick.statusWidget.hidden';
const EVENT = 'forstick:widget-prefs';
let mem = '[]';

function read() {
  try { const v = window.localStorage.getItem(KEY); if (v !== null) mem = v; } catch { /* 막힘 → mem */ }
  return mem;
}
function write(ids) {
  mem = JSON.stringify(ids);
  try { window.localStorage.setItem(KEY, mem); } catch { /* 저장 불가 — 이 탭에서만 유지 */ }
  window.dispatchEvent(new Event(EVENT)); // 같은 탭의 다른 컴포넌트(사이드바)에 알림
}
function subscribe(cb) {
  const onStorage = (e) => { if (e.key === KEY || e.key === null) cb(); }; // 다른 탭
  window.addEventListener(EVENT, cb);
  window.addEventListener('storage', onStorage);
  return () => { window.removeEventListener(EVENT, cb); window.removeEventListener('storage', onStorage); };
}
const parse = (raw) => { try { const a = JSON.parse(raw); return Array.isArray(a) ? a.filter((x) => typeof x === 'string') : []; } catch { return []; } };

export function useWidgetPrefs() {
  const raw = useSyncExternalStore(subscribe, read, () => '[]');
  const hidden = useMemo(() => new Set(parse(raw)), [raw]);
  const toggle = useCallback((id) => {
    const next = new Set(parse(read()));
    if (!next.delete(id)) next.add(id);
    write([...next]);
  }, []);
  const showAll = useCallback(() => write([]), []);
  return { hidden, toggle, showAll };
}
