// 반복 작업 서버 호출(2026-10-07) — 명령 패널(RepeatPanel)과 시뮬레이션 보기가 같이 쓴다.
// 세션은 명령 패널과 같은 탭 세션(sessionStorage)을 쓴다 — 시뮬레이션 보기의 일시정지가 같은 세션을 멈춘다.
const SESSION_KEY = 'forstick2.dashboard.session_id';

export async function call(method, path, body) {
  const res = await fetch(path, { method, headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined, cache: 'no-store' });
  let payload = {};
  try { payload = await res.json(); } catch { /* JSON 아님 */ }
  return { ok: res.ok, status: res.status, payload };
}
export const reasonOf = (r) => r.payload.detail || r.payload.error || `서버 응답 ${r.status}`;

export async function repeatSession() {
  let stored = null;
  try { stored = sessionStorage.getItem(SESSION_KEY); } catch { /* 저장소 없음 */ }
  if (stored) {
    const claimed = await call('POST', `/v1/sessions/${encodeURIComponent(stored)}/clients`, {});
    if (claimed.ok) return stored;
  }
  const created = await call('POST', '/v1/sessions', { origin: 'dashboard2' });
  if (!created.ok || !created.payload.session_id) throw new Error(`세션을 만들 수 없습니다 — ${reasonOf(created)}`);
  try { sessionStorage.setItem(SESSION_KEY, created.payload.session_id); } catch { /* 이 탭만 */ }
  return created.payload.session_id;
}

/** 반복 작업 제어(일시정지·재개·취소·회차 후 종료) — 시뮬레이션 보기도 같은 함수를 쓴다. */
export async function repeatAction(runId, action) {
  // 반복을 시작한 세션만 제어할 수 있다(2026-10-08) — 같은 탭 세션을 보낸다.
  let sessionId;
  try { sessionId = await repeatSession(); } catch (e) { return { ok: false, reason: e.message }; }
  const res = await call('POST', `/v1/sim-demo/repeat/${encodeURIComponent(runId)}/${action}`, { session_id: sessionId });
  return res.ok ? { ok: true } : { ok: false, reason: reasonOf(res) };
}

