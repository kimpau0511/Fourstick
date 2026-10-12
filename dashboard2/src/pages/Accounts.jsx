import { useEffect, useState } from 'react';
import { Notice } from '../components/Login.jsx';
import Spinner from '../components/Spinner.jsx';

// 계정 관리(최고 관리자만) — 피그마 Light · Screens "계정 관리 /" 프레임(y=24600), 결정 2026-10-12.
//
// 지키는 것:
// - 관리자 판정은 서버가 한다. 화면이 이 칸을 숨기는 건 표시일 뿐이고, 서버가 매 요청 다시 확인한다.
// - "삭제"는 사용 중지다(명령 기록의 보낸 사람이 남아야 한다). 다시 추가하면 다시 켜진다.
// - 최고 관리자는 화면에서 지울 수 없다 — 체크 칸을 막고, 서버도 거절한다.
// - 결과는 서버가 항목별로 돌려준 것만 보여 준다. 일부만 성공해도 성공한 것과 실패한 것을 나눠 보인다.
//
// 서버 약속(server/routes/auth.py):
//   GET  /v1/auth/accounts          200 {accounts:[{email,name,picture,is_admin,created_at,first_login_at,last_login_at}]}
//   POST /v1/auth/accounts          {emails} → 200 {results:[{email,ok,result|reason_code}], accounts}
//   POST /v1/auth/accounts/delete   {emails} → 200 {results, accounts}
//   401 로그인 안 됨 · 403 session.admin_required

const EMAIL = /^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$/;
const REASONS = {
  'session.account_email_invalid': '이메일 형식이 아닙니다.',
  'session.account_already_registered': '이미 등록된 계정입니다.',
  'session.account_protected': '최고 관리자 계정은 삭제할 수 없습니다.',
  'session.account_not_registered': '이미 삭제됐거나 등록되지 않은 계정입니다.',
};

const pad = (n) => String(n).padStart(2, '0');
const when = (sec) => { const d = new Date(sec * 1000); return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const subline = (a) => [
  a.name || '이름 없음(아직 로그인 전)',
  a.is_admin ? '최고 관리자' : '일반',
  a.last_login_at ? `마지막 로그인 ${when(a.last_login_at)}` : `등록 ${when(a.created_at)}`,
].join(' · ');

async function api(path, emails) {
  const init = emails
    // X-Requested-With: 로그인과 같은 출처 표시 — 서버가 다른 사이트에서 보낸 변경 요청을 거절한다.
    ? { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' }, body: JSON.stringify({ emails }) }
    : { credentials: 'same-origin', cache: 'no-store' };
  const res = await fetch(path, init);
  let body = {};
  try { body = await res.json(); } catch { /* 본문 없음 */ }
  if (!res.ok) throw Object.assign(new Error(body.error || `HTTP ${res.status}`), { status: res.status });
  return body;
}

// 붙여 넣은 글에서 쉼표·공백·줄바꿈으로 나눈 이메일들.
const splitEmails = (text) => text.split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean);

function AddDialog({ accounts, onCancel, onDone }) {
  const [rows, setRows] = useState([{ value: '', error: null }]);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(null);
  const registered = new Set(accounts.map((a) => a.email));

  const setRow = (i, value) => setRows(rows.map((r, j) => (j === i ? { value, error: null } : r)));
  const paste = (i, e) => {
    const parts = splitEmails(e.clipboardData.getData('text'));
    if (parts.length < 2) return;
    e.preventDefault();
    setRows([...rows.slice(0, i), ...parts.map((value) => ({ value, error: null })), ...rows.slice(i + 1)]);
  };
  const remove = (i) => setRows(rows.length > 1 ? rows.filter((_, j) => j !== i) : [{ value: '', error: null }]);
  const filled = rows.filter((r) => r.value.trim());

  const submit = async () => {
    // 보내기 전에 화면에서 거를 수 있는 것(형식·이미 등록·중복)은 먼저 알려 준다. 최종 판정은 서버.
    const seen = new Set();
    const checked = rows.map((r) => {
      const v = r.value.trim().toLowerCase();
      if (!v) return r;
      const error = !EMAIL.test(v) ? REASONS['session.account_email_invalid']
        : registered.has(v) ? REASONS['session.account_already_registered']
          : seen.has(v) ? '위 칸과 같은 이메일입니다.' : null;
      seen.add(v);
      return { ...r, error };
    });
    setRows(checked);
    if (checked.some((r) => r.error)) return;
    setBusy(true); setFailed(null);
    try {
      const body = await api('/v1/auth/accounts', filled.map((r) => r.value.trim()));
      const bad = new Map(body.results.filter((r) => !r.ok).map((r) => [r.email, REASONS[r.reason_code] || r.reason_code]));
      const left = checked.filter((r) => bad.has(r.value.trim().toLowerCase())).map((r) => ({ ...r, error: bad.get(r.value.trim().toLowerCase()) }));
      onDone(body.accounts, body.results.filter((r) => r.ok).length, left.length === 0);
      if (left.length) setRows(left);
    } catch (e) {
      setFailed(e.status === 403 ? '최고 관리자만 계정을 추가할 수 있습니다.' : '서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.');
    } finally { setBusy(false); }
  };

  return <div className="scrim" onKeyDown={(e) => { if (e.key === 'Escape' && !busy) onCancel(); }}>
    <section className="auth-dialog" role="dialog" aria-modal="true" aria-labelledby="acct-add-title">
      <h2 id="acct-add-title">계정 추가</h2>
      <div className="acct-field">
        <b id="acct-add-label">구글 이메일 (여러 개 한 번에)</b>
        {rows.map((r, i) => <div key={i} className="acct-email">
          <div className="acct-email-row">
            <input className={r.error ? 'acct-input invalid' : 'acct-input'} type="email" inputMode="email" autoComplete="off" placeholder="name@gmail.com"
              aria-labelledby="acct-add-label" aria-invalid={!!r.error} aria-describedby={r.error ? `acct-err-${i}` : undefined}
              value={r.value} autoFocus={i === rows.length - 1} disabled={busy}
              onChange={(e) => setRow(i, e.target.value)} onPaste={(e) => paste(i, e)}
              onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } }} />
            <button type="button" className="acct-x" aria-label={`${i + 1}번째 칸 지우기`} onClick={() => remove(i)} disabled={busy}>×</button>
          </div>
          {r.error && <small id={`acct-err-${i}`} className="danger">{r.error}</small>}
        </div>)}
        <button type="button" className="acct-plus" onClick={() => setRows([...rows, { value: '', error: null }])} disabled={busy}>+ 이메일 칸 추가</button>
        <small className="muted">등록한 이메일의 구글 계정으로만 로그인할 수 있습니다. 쉼표나 줄바꿈으로 여러 개를 붙여 넣어도 됩니다.</small>
      </div>
      {failed && <Notice tone="danger">{failed}</Notice>}
      <div className="auth-actions">
        <button type="button" className="btn-secondary" onClick={onCancel} disabled={busy}>취소</button>
        <button type="button" className="btn-primary" onClick={submit} disabled={busy || !filled.length}>
          {busy ? <><Spinner size={14} label="추가 중" /> 추가 중</> : `${filled.length}개 추가`}
        </button>
      </div>
    </section>
  </div>;
}

function DeleteDialog({ emails, onCancel, onDone }) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(null);
  const submit = async () => {
    setBusy(true); setFailed(null);
    try {
      const body = await api('/v1/auth/accounts/delete', emails);
      onDone(body.accounts, body.results);
    } catch (e) {
      setFailed(e.status === 403 ? '최고 관리자만 계정을 삭제할 수 있습니다.' : '서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.');
      setBusy(false);
    }
  };
  return <div className="scrim" onKeyDown={(e) => { if (e.key === 'Escape' && !busy) onCancel(); }}>
    <section className="auth-dialog" role="alertdialog" aria-modal="true" aria-labelledby="acct-del-title">
      <h2 id="acct-del-title">선택한 계정 {emails.length}개를 삭제할까요?</h2>
      <ul className="acct-del-list">{emails.map((e) => <li key={e}>{e}</li>)}</ul>
      <Notice tone="warn" title="삭제하면 바로 로그아웃되고 다시 들어올 수 없습니다">이 계정이 남긴 명령·승인 기록은 지워지지 않습니다. 다시 추가하면 다시 쓸 수 있습니다.</Notice>
      {failed && <Notice tone="danger">{failed}</Notice>}
      <div className="auth-actions">
        <button type="button" className="btn-secondary" onClick={onCancel} disabled={busy} autoFocus>취소</button>
        <button type="button" className="btn-danger" onClick={submit} disabled={busy}>{busy ? <><Spinner size={14} label="삭제 중" /> 삭제 중</> : '삭제'}</button>
      </div>
    </section>
  </div>;
}

export default function Accounts({ user }) {
  const [accounts, setAccounts] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [selected, setSelected] = useState(() => new Set());
  const [dialog, setDialog] = useState(null); // 'add' | 'delete'
  const [message, setMessage] = useState(null);

  const [reload, setReload] = useState(0);
  useEffect(() => {
    if (!user?.is_admin) return undefined;
    let cancelled = false;
    api('/v1/auth/accounts').then(
      (body) => { if (!cancelled) { setAccounts(body.accounts); setLoadError(null); } },
      (e) => { if (!cancelled) setLoadError(e.status === 403 ? 'forbidden' : 'server'); },
    );
    return () => { cancelled = true; };
  }, [user?.is_admin, reload]);

  if (!user?.is_admin || loadError === 'forbidden') {
    return <div className="card rec-pad"><h2>계정 관리</h2><p className="rec-empty">계정 관리는 최고 관리자만 할 수 있습니다. 계정이 필요하면 최고 관리자에게 요청하세요.</p></div>;
  }
  if (!accounts) {
    return <div className="card rec-pad"><h2>계정 관리</h2>{loadError
      ? <p className="rec-empty">계정 목록을 불러오지 못했습니다(확인 안 됨). <button type="button" className="rec-btn" onClick={() => { setLoadError(null); setReload((n) => n + 1); }}>다시 시도</button></p>
      : <p className="rec-empty"><Spinner size={14} label="불러오는 중" /> 불러오는 중</p>}</div>;
  }

  const deletable = accounts.filter((a) => !a.is_admin).map((a) => a.email);
  const picked = deletable.filter((e) => selected.has(e));
  const allPicked = deletable.length > 0 && picked.length === deletable.length;
  const toggle = (email) => setSelected((s) => { const n = new Set(s); if (n.has(email)) n.delete(email); else n.add(email); return n; });

  return <section className="card rec-pad acct" aria-labelledby="acct-title">
    <h2 id="acct-title">계정 관리 <small className="muted">최고 관리자 전용</small></h2>
    <div className="acct-head">
      <input type="checkbox" className="acct-check" aria-label="삭제할 수 있는 계정 모두 선택" checked={allPicked} disabled={!deletable.length}
        ref={(el) => { if (el) el.indeterminate = picked.length > 0 && !allPicked; }}
        onChange={() => setSelected(allPicked ? new Set() : new Set(deletable))} />
      <div className="acct-text">
        <b>로그인 허용 계정 {accounts.length}개{picked.length ? ` · ${picked.length}개 선택됨` : ''}</b>
        <small>여기 등록된 구글 계정만 로그인할 수 있습니다. 삭제하면 바로 로그아웃됩니다.</small>
      </div>
      <button type="button" className="btn-danger" disabled={!picked.length} onClick={() => setDialog('delete')}>선택 삭제{picked.length ? ` (${picked.length})` : ''}</button>
      <button type="button" className="rec-btn" onClick={() => setDialog('add')}>계정 추가</button>
    </div>
    {message && <Notice tone={message.tone}>{message.text}</Notice>}
    <ul className="acct-list" aria-label="로그인 허용 계정">
      {accounts.map((a) => <li key={a.email} aria-selected={selected.has(a.email)}>
        <input type="checkbox" className="acct-check" checked={selected.has(a.email)} disabled={a.is_admin}
          aria-label={a.is_admin ? `${a.email} — 최고 관리자는 삭제할 수 없음` : `${a.email} 선택`} onChange={() => toggle(a.email)} />
        <div className="acct-text"><b>{a.email}{a.email === user.email ? <span className="muted"> (나)</span> : null}</b><small>{subline(a)}</small></div>
      </li>)}
    </ul>
    {dialog === 'add' && <AddDialog accounts={accounts} onCancel={() => setDialog(null)} onDone={(next, added, allDone) => {
      setAccounts(next);
      if (added) setMessage({ tone: 'info', text: `계정 ${added}개를 추가했습니다.` });
      if (allDone) setDialog(null);
    }} />}
    {dialog === 'delete' && <DeleteDialog emails={picked} onCancel={() => setDialog(null)} onDone={(next, results) => {
      setAccounts(next); setSelected(new Set()); setDialog(null);
      const bad = results.filter((r) => !r.ok);
      const done = results.length - bad.length;
      setMessage(bad.length
        ? { tone: 'warn', text: `${done}개 삭제, ${bad.length}개는 삭제하지 못했습니다: ${bad.map((r) => `${r.email}(${REASONS[r.reason_code] || r.reason_code})`).join(', ')}` }
        : { tone: 'info', text: `계정 ${done}개를 삭제했습니다.` });
    }} />}
  </section>;
}
