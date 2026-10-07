import { useState } from 'react';
import { robotNameOf } from '../server.js';

// 로봇 이름·호출어(2026-10-07). 저장은 서버(/v1/settings/robot-name) — 새로고침·다른 브라우저에서도 같은 이름이다.
// 호출어는 늘 '이름 + 야'. 검증 규칙은 서버(server/robot_names.py)와 같다: 한글 2~4글자.
const NAME_RULE = /^[가-힣]{2,4}$/;
function nameProblem(value) {
  const v = (value || '').trim();
  if (!v) return '로봇 이름을 입력하세요';
  if (v.length > 4) return '로봇 이름은 4글자까지입니다';
  if (!NAME_RULE.test(v)) return '로봇 이름은 한글 2~4글자로 입력하세요(공백·영문·숫자 제외)';
  return null;
}

export default function RobotNameCard({ server, robotId }) {
  const saved = robotNameOf(server);
  // 고치기 전에는 서버 값을 그대로 보인다(draft가 null). 저장에 성공하면 다시 서버 값을 따른다.
  const [draft, setDraft] = useState(null);
  const [note, setNote] = useState(null);
  const [busy, setBusy] = useState(false);
  const value = draft ?? saved;
  const touched = draft !== null;
  const problem = nameProblem(value);

  async function save(event) {
    event.preventDefault();
    if (problem) { setNote({ tone: 'danger', text: problem }); return; }
    setBusy(true); setNote(null);
    try {
      const res = await fetch('/v1/settings/robot-name', { method: 'POST', headers: { 'Content-Type': 'application/json' }, cache: 'no-store',
        body: JSON.stringify({ robot_id: robotId || undefined, name: value.trim() }) });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) setNote({ tone: 'danger', text: body.detail || body.error || `저장 실패 (${res.status})` });
      else { await server.refresh?.(); setDraft(null); setNote({ tone: 'ok', text: '로봇 이름이 저장되었습니다.' }); }
    } catch (error) { setNote({ tone: 'danger', text: `저장 요청 실패: ${error.message}` }); }
    setBusy(false);
  }

  return <form className="card robot-name" aria-label="로봇 이름" onSubmit={save}>
    <h3>로봇 이름 · 호출어</h3>
    <div className="robot-name-row">
      <label htmlFor="robot-name-input">로봇 이름</label>
      <input id="robot-name-input" value={value} maxLength={10} placeholder="지니" aria-invalid={touched && !!problem}
        onChange={(e) => { setDraft(e.target.value); setNote(null); }} />
      <button type="submit" className="btn-primary" disabled={busy}>{busy ? '저장 중…' : '저장'}</button>
    </div>
    <small className="muted">한글 2~4글자. 호출어는 이름 뒤에 ‘야’를 붙여 만듭니다(예: 지니 → 지니야).</small>
    <p className="robot-name-current">현재 호출어: <b>{saved}야</b></p>
    {touched && problem && !note && <small className="danger">{problem}</small>}
    {note && <small className={note.tone} role="status">{note.text}</small>}
  </form>;
}
