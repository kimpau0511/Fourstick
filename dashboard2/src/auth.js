import { useCallback, useEffect, useState } from 'react';

// 로그인(구글만, 등록된 계정만) — 피그마 Light · Screens "로그인 /" 프레임, 결정은 2026-10-06 Q1~Q10.
//
// 지키는 것:
// - 로그인 확인은 서버가 한다. 화면은 서버 답(/v1/auth/me)이 200일 때만 대시보드를 연다.
//   답이 없거나 이상하면 로그인 화면이다 — 확인 안 됨은 통과가 아니다(설계 원칙 4).
// - 세션 만료(로그인 API 밖의 서버 요청이 401)에도 대시보드는 그대로 두고 다시 로그인 창만 띄운다.
//   헤더 즉시 정지는 덮지 않는다(정지 요청의 인증 예외는 서버 몫).
// - 한 탭에서 로그아웃하면 다른 탭도 로그인 화면으로 간다.
//
// 서버 약속(백엔드에 전달, 아직 서버에 없음 — 2026-10-07 확인):
//   GET  /v1/auth/me      200 {user:{email,name,picture}} · 401 로그인 안 됨
//   POST /v1/auth/google  {code} + 헤더 X-Requested-With — 구글 팝업이 준 인가 코드. 서버가 교환·검증하고 등록된 계정이면 HttpOnly 세션 쿠키 + 200 {user}.
//                         팝업 방식의 교환 redirect_uri는 로그인을 부른 페이지의 origin(예: https://foursticks.xos.kr) —
//                         구글 문서 identity/oauth2/web/guides/use-code-model(2026-10-07 확인). 화면은 교환 뒤 /v1/auth/me로 쿠키를 다시 확인한다.
//                         403 {reason_code:'session.account_not_registered', email} · 403 {reason_code:'session.account_disabled'}
//   POST /v1/auth/logout  세션 삭제
//   정지(/v1/stop, /v1/sim-demo/stop)는 세션이 없어도 받는다.

export const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID || '';
const GSI_SRC = 'https://accounts.google.com/gsi/client';
const channel = typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel('forstick-auth');

// 세션 만료는 서버 요청(/health·/v1/*)이 흩어진 여러 파일(server.js·simCommand.js·History.jsx 등)에서 드러난다.
// 호출 지점마다 고치지 않고 fetch 한 곳에서 401을 잡는다.
let onUnauthorized = null;
const rawFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
  const res = await rawFetch(input, init);
  const url = String(input instanceof Request ? input.url : input);
  if (res.status === 401 && onUnauthorized && !url.includes('/v1/auth/')) onUnauthorized();
  return res;
};

let gsiPromise = null;
function loadGsi() {
  if (!gsiPromise) {
    gsiPromise = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = GSI_SRC; s.async = true;
      s.onload = () => { if (window.google?.accounts?.oauth2) resolve(window.google.accounts.oauth2); else { gsiPromise = null; reject(new Error('gsi')); } };
      s.onerror = () => { gsiPromise = null; reject(new Error('gsi')); };
      document.head.appendChild(s);
    });
  }
  return gsiPromise;
}

async function readJson(res) {
  try { return await res.json(); } catch { return {}; }
}

// 로그인 상태 확인 — 200 + user일 때만 로그인. 401은 로그인 안 됨, 그 밖(꺼짐·404·오류)은 '서버 확인 안 됨'.
async function sessionState() {
  try {
    const res = await rawFetch('/v1/auth/me', { cache: 'no-store', credentials: 'same-origin' });
    if (res.status === 200) {
      const body = await readJson(res);
      if (body.user) return { status: 'signed_in', user: body.user, expired: false, error: null };
    }
    return { status: 'signed_out', user: null, error: res.status === 401 ? null : { kind: 'server' } };
  } catch {
    return { status: 'signed_out', user: null, error: { kind: 'server' } };
  }
}

// 로그아웃 뒤에 늦게 도착한 로그인 확인 답이 다시 로그인시키지 않게 세대를 센다.
let generation = 0;

// error.kind: not_registered · disabled · cancelled · popup_blocked · server · config · logout_failed
export function useAuth() {
  const [state, setState] = useState({ status: 'checking', user: null, expired: false, pending: false, error: null });
  const patch = useCallback((p) => setState((s) => ({ ...s, ...p })), []);
  const signedOut = useCallback(() => { generation += 1; patch({ status: 'signed_out', user: null, expired: false, pending: false, error: null }); }, [patch]);

  useEffect(() => {
    const check = async () => { const g = generation; const next = await sessionState(); if (g === generation) patch(next); };
    check();
    onUnauthorized = () => setState((s) => (s.status === 'signed_in' ? { ...s, expired: true } : s));
    if (channel) channel.onmessage = (e) => {
      if (e.data === 'logout') signedOut();
      if (e.data === 'login') check();
    };
    return () => { onUnauthorized = null; if (channel) channel.onmessage = null; };
  }, [patch, signedOut]);

  const exchange = useCallback(async (code) => {
    try {
      const res = await rawFetch('/v1/auth/google', {
        // X-Requested-With: 서버가 로그인 CSRF를 막는 표시(구글 팝업 코드 흐름 지침). 다른 사이트의 폼은 이 헤더를 못 붙인다.
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' }, body: JSON.stringify({ code }),
      });
      const body = await readJson(res);
      if (res.ok && body.user) {
        // 쿠키가 실제로 붙었는지 서버에 다시 묻는다 — 교환 응답만 믿으면 쿠키가 막힌 브라우저에서 만료 창이 반복된다.
        const g = generation;
        const next = await sessionState();
        if (g !== generation) return;
        if (next.status !== 'signed_in') return patch({ pending: false, error: { kind: 'server' } });
        patch({ ...next, pending: false });
        channel?.postMessage('login');
        return;
      }
      if (res.status === 403) {
        const kind = body.reason_code === 'session.account_disabled' ? 'disabled' : 'not_registered';
        return patch({ pending: false, error: { kind, email: body.email || null } });
      }
      patch({ pending: false, error: { kind: 'server' } });
    } catch {
      patch({ pending: false, error: { kind: 'server' } });
    }
  }, [patch]);

  const login = useCallback(async () => {
    if (!GOOGLE_CLIENT_ID) return patch({ error: { kind: 'config' } });
    patch({ pending: true, error: null });
    let oauth2;
    try { oauth2 = await loadGsi(); } catch { return patch({ pending: false, error: { kind: 'server' } }); }
    try {
      oauth2.initCodeClient({
        client_id: GOOGLE_CLIENT_ID,
        scope: 'openid email profile',
        ux_mode: 'popup',
        select_account: true, // "다른 계정으로 로그인"이 되려면 매번 계정을 고르게 한다
        callback: (r) => (r.code ? exchange(r.code) : patch({ pending: false, error: { kind: 'cancelled' } })),
        error_callback: (e) => patch({
          pending: false,
          error: { kind: e?.type === 'popup_failed_to_open' ? 'popup_blocked' : e?.type === 'popup_closed' ? 'cancelled' : 'server' },
        }),
      }).requestCode();
    } catch {
      patch({ pending: false, error: { kind: 'server' } });
    }
  }, [exchange, patch]);

  const logout = useCallback(async () => {
    // 서버가 세션을 지웠다고 답할 때만 로그아웃으로 보인다 — 실패를 성공처럼 보이면 공용 PC에 세션이 남는다.
    let ok = false;
    try { ok = (await rawFetch('/v1/auth/logout', { method: 'POST', credentials: 'same-origin' })).ok; } catch { ok = false; }
    if (!ok) return patch({ error: { kind: 'logout_failed' } });
    signedOut();
    channel?.postMessage('logout');
  }, [patch, signedOut]);

  return { ...state, login, logout };
}
