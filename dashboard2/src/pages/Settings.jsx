import { useState } from 'react';
import './records.css';

// 설정 3구역. 개인 = 이 브라우저(localStorage)에만 저장하는 루틴, 운영 = 인증 도입 전 빈 상태,
// 안전·권한 = 서버 정책 조회 전용(편집 UI 없음).
const SECTIONS = ['개인', '운영', '안전·권한'];
const STORE_KEY = 'forstick.routines';
const POLICY_LABELS = { safety: '안전', freshness: '데이터 최신성', planning: '계획', stop: '정지', stt: '음성 인식' };
const STATES = ['작성 중', '검사 중', '승인 필요', '적용 예약', '사용 중'];
const CURRENT_STATE = '사용 중'; // 지금 보이는 값은 서버가 적용 중인 값이다

function loadRoutines() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORE_KEY) || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch { return []; }
}
function saveRoutines(list) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(list)); } catch { /* 저장 불가(사생활 보호 창 등) — 이번 화면에서만 유지 */ }
}

function Personal() {
  const [routines, setRoutinesState] = useState(loadRoutines);
  const [selected, setSelected] = useState(null);
  const [favOnly, setFavOnly] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const setRoutines = (list) => { setRoutinesState(list); saveRoutines(list); };
  const current = routines.find((r) => r.id === selected);
  const patch = (change) => setRoutines(routines.map((r) => (r.id === selected ? { ...r, ...change } : r)));
  const shown = routines.filter((r) => !favOnly || r.fav);

  const add = () => {
    const id = `r${Date.now()}`;
    setRoutines([...routines, { id, name: '새 루틴', text: '', fav: false }]);
    setSelected(id); setConfirming(false);
  };
  const remove = () => {
    setRoutines(routines.filter((r) => r.id !== selected));
    setSelected(null); setConfirming(false);
  };

  return <>
    <div className="rec-head"><h2>저장 루틴 <small className="muted">이 브라우저에만 저장됩니다</small></h2>
      <div className="rec-group">
        <button type="button" className="rec-chip" aria-pressed={favOnly} onClick={() => setFavOnly(!favOnly)}>즐겨찾기만</button>
        <button type="button" className="rec-btn" onClick={add}>새 루틴</button>
      </div>
    </div>
    <div className="rec-split">
      <div className="card" style={{ flex: 1, minWidth: 0 }}>
        {shown.length === 0 ? <p className="rec-empty">{routines.length ? '즐겨찾기한 루틴이 없습니다' : '저장한 루틴이 없습니다'}</p> : <table className="rec-table" aria-label="저장 루틴 목록">
          <thead><tr><th>이름</th><th>명령</th><th>즐겨찾기</th></tr></thead>
          <tbody>{shown.map((r) => <tr key={r.id} tabIndex={0} aria-selected={selected === r.id}
            onClick={() => { setSelected(r.id); setConfirming(false); }}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelected(r.id); setConfirming(false); } }}>
            <td>{r.name}</td><td>{r.text || '—'}</td><td>{r.fav ? '★' : '☆'}</td>
          </tr>)}</tbody>
        </table>}
      </div>
      {current && <div className="card rec-pad" style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 12 }} aria-label="루틴 상세" role="group">
        <label className="rec-field">이름<input className="rec-input" value={current.name} onChange={(e) => patch({ name: e.target.value })} /></label>
        <label className="rec-field">명령 문장<input className="rec-input" value={current.text} onChange={(e) => patch({ text: e.target.value })} /></label>
        <div className="rec-group">
          <button type="button" className="rec-chip" aria-pressed={!!current.fav} onClick={() => patch({ fav: !current.fav })}>즐겨찾기</button>
          {confirming
            ? <><span className="danger">이 루틴을 삭제할까요?</span>
              <button type="button" className="rec-btn" onClick={remove}>삭제 확인</button>
              <button type="button" className="rec-btn" onClick={() => setConfirming(false)}>취소</button></>
            : <button type="button" className="rec-btn" onClick={() => setConfirming(true)}>삭제</button>}
        </div>
        <p className="note muted">루틴은 문장만 저장합니다. 실행은 명령 패널에서 직접 보냅니다.</p>
      </div>}
    </div>
  </>;
}

function Operations() {
  return <div className="card rec-pad">
    <h2>운영</h2>
    <p className="rec-empty">사용자 계정은 인증 도입 뒤 제공합니다.</p>
  </div>;
}

function Policies({ server }) {
  const policies = server?.config?.policies;
  return <>
    <div className="card rec-pad">
      <b>조회 전용 · 변경은 안전관리자 권한 필요</b>
      <p className="note muted">아래는 서버가 지금 적용 중인 정책입니다. 이 화면에서는 바꿀 수 없습니다.</p>
    </div>
    {!policies ? <p className="rec-empty">정책 값을 받지 못했습니다(확인 안 됨)</p> : Object.entries(policies).map(([group, values]) => <section key={group} className="card rec-pad">
      <h2>{POLICY_LABELS[group] || group} <small className="muted">정책 버전 {values.policy_version || '확인 안 됨'}</small></h2>
      <dl className="rec-kv">
        {Object.entries(values).filter(([k]) => k !== 'policy_version').map(([k, v]) => <div key={k} style={{ display: 'contents' }}>
          <dt>{k}</dt><dd>{Array.isArray(v) ? v.join(', ') : String(v)}</dd>
        </div>)}
      </dl>
    </section>)}
    <section className="card rec-pad">
      <h2>정책 변경 단계</h2>
      <ol className="rec-steps" aria-label="변경 상태 흐름">
        {STATES.map((s, i) => <li key={s} aria-current={s === CURRENT_STATE ? 'step' : undefined}>{s}{i < STATES.length - 1 ? ' →' : ''}</li>)}
      </ol>
      <p className="note muted">현재 보이는 값은 “{CURRENT_STATE}” 단계입니다.</p>
    </section>
  </>;
}

export default function Settings({ server }) {
  const [section, setSection] = useState('개인');
  return <div className="rec-split">
    <nav className="card rec-nav" aria-label="설정 구역">
      {SECTIONS.map((s) => <button key={s} type="button" aria-current={section === s ? 'page' : undefined} onClick={() => setSection(s)}>{s}</button>)}
    </nav>
    <div className="rec-body">
      {section === '개인' && <Personal />}
      {section === '운영' && <Operations />}
      {section === '안전·권한' && <Policies server={server} />}
    </div>
  </div>;
}
