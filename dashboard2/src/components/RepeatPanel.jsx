import { useRef, useState } from 'react';
import { call, reasonOf, repeatAction, repeatSession } from '../repeatApi.js';
import './repeat.css';

// 반복 작업(2026-10-07). 반복은 서버가 관리한다(/v1/sim-demo/repeat) — 화면은 미리보기 → 사용자 승인 → 시작을
// 보내고, 상태는 server.repeat(주기 조회)로만 그린다. 새로고침해도 같은 실행을 다시 본다.
export default function RepeatPanel({ server, lockReason }) {
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState([]);
  const [count, setCount] = useState('1');
  const [preview, setPreview] = useState(null);
  const [note, setNote] = useState(null);
  const [busy, setBusy] = useState(false);
  const session = useRef(null);
  const materials = (server && server.simDemo && server.simDemo.materials) || [];
  const run = server && server.repeat ? server.repeat.run : null;
  const active = !!(run && run.active);
  const n = Number(count);
  const countOk = /^\d+$/.test(count.trim()) && n >= 1 && n <= 100;
  const canStart = !active && !busy && picked.length > 0 && countOk && !lockReason && !preview;

  async function ask() {
    setBusy(true); setNote(null);
    try {
      session.current = await repeatSession();
      const res = await call('POST', '/v1/sim-demo/repeat/preview', { session_id: session.current, materials: picked, count: count.trim() });
      if (!res.ok) setNote({ tone: 'danger', text: reasonOf(res) });
      else setPreview(res.payload);
    } catch (error) { setNote({ tone: 'danger', text: error.message }); }
    setBusy(false);
  }
  async function approve() {
    setBusy(true); setNote(null);
    const res = await call('POST', '/v1/sim-demo/repeat/start', { session_id: session.current, token: preview.token })
      .catch((e) => ({ ok: false, status: 0, payload: { detail: e.message } }));
    setPreview(null);
    if (!res.ok) setNote({ tone: 'danger', text: `시작하지 않았습니다 — ${reasonOf(res)}` });
    await server.refresh?.();
    setBusy(false);
  }
  async function finishAfterRound() {
    setBusy(true);
    const out = await repeatAction(run.run_id, 'finish-after-round');
    if (!out.ok) setNote({ tone: 'danger', text: out.reason });
    await server.refresh?.();
    setBusy(false);
  }
  async function verify() {
    setBusy(true); setNote(null);
    const res = await call('POST', `/v1/sim-demo/repeat/${encodeURIComponent(run.run_id)}/verify`, {})
      .catch((e) => ({ ok: false, status: 0, payload: { detail: e.message } }));
    if (!res.ok) setNote({ tone: 'danger', text: reasonOf(res) });
    else if (res.payload.run && res.payload.run.lock_held) setNote({ tone: 'danger', text: '확인되지 않은 항목이 있어 잠금을 유지했습니다' });
    await server.refresh?.();
    setBusy(false);
  }
  const toggle = (id) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  const statusTone = !run ? 'muted' : run.state === 'completed' ? 'ok' : ['failed', 'stopped', 'interrupted'].includes(run.state) ? 'danger' : 'info';

  return <section className="repeat" aria-label="반복 작업">
    <button type="button" className="repeat-toggle" aria-expanded={open} aria-controls="repeat-body" onClick={() => setOpen((v) => !v)}>
      <span>{open ? '▾' : '▸'} 반복 작업</span>
      {run && <small className={`repeat-status ${statusTone}`} role="status">{run.label}{run.finish_after_round && active ? ' · 이번 회차 후 종료' : ''}</small>}
    </button>
    {run && run.lock_held && <div className="repeat-confirm blocked" role="alert" aria-label="중단된 반복 작업 확인">
      <b>중단된 반복 작업 — 작업 셀 잠금 유지 중</b>
      <small>{run.reason}</small>
      {run.check && <ul className="repeat-checks">{run.check.checks.map((c) => <li key={c.name} className={c.ok ? 'ok' : 'danger'}>{c.ok ? '✓' : '✗'} {c.detail}</li>)}
        {(run.check.notes || []).map((n) => <li key={n} className="muted">{n}</li>)}</ul>}
      <div className="repeat-actions"><button type="button" className="btn-primary" disabled={busy} onClick={verify}>{busy ? '확인 중…' : '상태 확인 후 잠금 해제'}</button></div>
      {note && <small className={note.tone} role="status">{note.text}</small>}
    </div>}
    {open && <div id="repeat-body" className="repeat-body">
      <fieldset disabled={active || busy || !!preview}>
        <legend>자재</legend>
        <div className="repeat-materials">
          {materials.map((m) => <label key={m.resource_id || m.model}>
            <input type="checkbox" checked={picked.includes(m.resource_id)} onChange={() => toggle(m.resource_id)} />{m.korean}
          </label>)}
        </div>
        <label className="repeat-count">반복 횟수
          <input type="number" inputMode="numeric" min="1" max="100" step="1" value={count} onChange={(e) => setCount(e.target.value)} aria-invalid={!countOk} />
          <span>회</span>
        </label>
        {!countOk && <small className="danger">1~100 사이의 정수를 입력하세요</small>}
      </fieldset>
      {preview && <div className={`repeat-confirm ${preview.ok ? '' : 'blocked'}`} role={preview.ok ? 'group' : 'alert'} aria-label="반복 작업 확인">
        {preview.ok ? <>
          <b>이대로 시작할까요?</b>
          <small>{preview.count}회 × [{preview.sequence.join(' → ')}] · 총 {preview.total_steps}동작</small>
          <small className="muted">각 동작 직전에 위치·목적지·로봇 상태를 다시 확인하고, 이상이 있으면 멈춥니다(자동 재시도 없음).</small>
          <div className="repeat-actions">
            <button type="button" className="btn-primary" disabled={busy || !!lockReason} onClick={approve}>승인하고 시작</button>
            <button type="button" className="btn-secondary" disabled={busy} onClick={() => setPreview(null)}>취소</button>
          </div>
        </> : <>
          <b>지금은 시작할 수 없습니다</b>
          <ul>{preview.problems.map((p) => <li key={p}>{p}</li>)}</ul>
          <button type="button" className="btn-secondary" onClick={() => setPreview(null)}>닫기</button>
        </>}
      </div>}
      {!preview && <div className="repeat-actions">
        <button type="button" className="btn-primary" disabled={!canStart} title={active ? '반복 작업이 진행 중입니다' : lockReason || undefined} onClick={ask}>반복 시작</button>
        <button type="button" className="btn-secondary" disabled={!active || busy || run.finish_after_round || !['running', 'pausing', 'resuming'].includes(run.state)} onClick={finishAfterRound}>현재 회차 후 종료</button>
      </div>}
      {run && run.reason && !active && !run.lock_held && <small className={statusTone}>{run.reason}</small>}
      {run && run.check && !run.lock_held && <ul className="repeat-checks">{run.check.checks.map((c) => <li key={c.name} className={c.ok ? 'ok' : 'danger'}>{c.ok ? '✓' : '✗'} {c.detail}</li>)}</ul>}
      {note && !(run && run.lock_held) && <small className={note.tone} role="status">{note.text}</small>}
    </div>}
  </section>;
}
