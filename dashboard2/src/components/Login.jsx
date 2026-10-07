import googleG from '../assets/google-g.svg';
import { GOOGLE_CLIENT_ID } from '../auth.js';
import Spinner from './Spinner.jsx';
import './login.css';

// 로그인 화면·세션 만료 창·로그아웃 확인 창. 피그마 Light · Screens "로그인 /" 프레임(B안 — 투명 유리 + 진한 글자).
// 문구는 피그마 LoginCard 상태별 문구 그대로.

const PRIVACY_URL = import.meta.env.VITE_PRIVACY_URL || '';

const ERRORS = {
  not_registered: (e) => ({ tone: 'danger', text: `이 계정${e.email ? `(${e.email})` : ''}은 사용 권한이 없습니다. 관리자에게 등록을 요청하세요.`, button: '다른 계정으로 로그인' }),
  disabled: () => ({ tone: 'danger', text: '사용이 중지된 계정입니다. 관리자에게 문의하세요.', button: '다른 계정으로 로그인' }),
  cancelled: () => ({ tone: 'info', text: '로그인이 취소되었습니다.' }),
  popup_blocked: () => ({ tone: 'warn', text: '브라우저가 로그인 창을 막았습니다. 팝업을 허용한 뒤 다시 시도하세요.', button: '다시 시도' }),
  server: () => ({ tone: 'danger', text: '로그인 서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.', button: '다시 시도' }),
  config: () => ({ tone: 'danger', text: '구글 로그인 설정(클라이언트 ID)이 없습니다. 관리자에게 문의하세요.' }),
  logout_failed: () => ({ tone: 'danger', text: '로그아웃을 서버에서 확인하지 못했습니다. 다시 시도하세요.' }),
};

function AlertIcon() {
  return <svg className="notice-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M7.86 2h8.28L22 7.86v8.28L16.14 22H7.86L2 16.14V7.86z" /><path d="M12 8v4M12 16h.01" />
  </svg>;
}

export function Notice({ tone, title, children }) {
  return <div className={`auth-notice ${tone}`} role={tone === 'info' ? 'status' : 'alert'}>
    <AlertIcon />
    <div>{title && <strong>{title}</strong>}<span>{children}</span></div>
  </div>;
}

export function GoogleButton({ auth, label = 'Google로 로그인', autoFocus = false }) {
  const noConfig = !GOOGLE_CLIENT_ID;
  return <button type="button" className="google-btn" onClick={auth.login} disabled={auth.pending || noConfig} aria-busy={auth.pending || undefined} autoFocus={autoFocus}>
    {auth.pending ? <Spinner size={16} decorative /> : <img src={googleG} alt="" width="18" height="18" />}
    {auth.pending ? 'Google 인증 대기 중…' : label}
  </button>;
}

function errorOf(auth) {
  if (!GOOGLE_CLIENT_ID) return ERRORS.config();
  return auth.error ? ERRORS[auth.error.kind](auth.error) : null;
}

export function LoginScreen({ auth }) {
  if (auth.status === 'checking') {
    return <div className="login-page checking" role="status"><Spinner size={28} decorative /><span>로그인 상태 확인 중…</span></div>;
  }
  const err = errorOf(auth);
  return <div className="login-page">
    <main className="login-card" aria-labelledby="login-title">
      <div className="login-head">
        <h1 id="login-title">로그인</h1>
        <p>{auth.pending ? '열린 Google 창에서 로그인을 완료하세요' : '등록된 Google 계정으로 로그인하세요'}</p>
      </div>
      {err && !auth.pending && <Notice tone={err.tone}>{err.text}</Notice>}
      <GoogleButton auth={auth} label={err?.button} />
      <p className="login-help">계정이 없나요? 관리자에게 등록을 요청하세요.</p>
      {PRIVACY_URL && <a className="login-link" href={PRIVACY_URL} target="_blank" rel="noreferrer">개인정보 처리방침</a>}
    </main>
  </div>;
}

// 세션 만료 — 대시보드 위에 띄우고 헤더(즉시 정지)는 덮지 않는다(.scrim은 헤더 아래부터).
export function SessionExpired({ auth }) {
  const err = auth.error && ERRORS[auth.error.kind](auth.error);
  return <div className="scrim">
    <section className="auth-dialog" role="alertdialog" aria-labelledby="expired-title" aria-describedby="expired-desc">
      <h2 id="expired-title">로그인 시간이 지났습니다</h2>
      <p id="expired-desc">다시 로그인하면 지금 보던 화면으로 그대로 돌아옵니다.</p>
      <Notice tone="info" title="즉시 정지는 다시 로그인하지 않아도 누를 수 있습니다">로봇 작업은 계속 진행됩니다. 위쪽 즉시 정지 버튼을 쓰세요.</Notice>
      {err && !auth.pending && <Notice tone={err.tone}>{err.text}</Notice>}
      <GoogleButton auth={auth} label={err?.button} autoFocus />
    </section>
  </div>;
}

export function LogoutConfirm({ onCancel, onConfirm }) {
  return <div className="scrim" onKeyDown={(e) => { if (e.key === 'Escape') onCancel(); }}>
    <section className="auth-dialog" role="alertdialog" aria-labelledby="logout-title">
      <h2 id="logout-title">로그아웃할까요?</h2>
      <Notice tone="warn" title="로그아웃해도 로봇 작업은 멈추지 않습니다">로봇 작업이 진행 중입니다. 멈추려면 즉시 정지를 누르세요.</Notice>
      <div className="auth-actions">
        <button type="button" className="btn-secondary" onClick={onCancel} autoFocus>취소</button>
        <button type="button" className="btn-primary" onClick={onConfirm}>로그아웃</button>
      </div>
    </section>
  </div>;
}
