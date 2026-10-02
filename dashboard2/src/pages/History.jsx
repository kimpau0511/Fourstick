import { useState } from 'react';
import { RESULT_LABELS } from '../simCommand.js';
import './records.css';

// 기록 = 서버 recent_jobs + 이 탭에서 보낸 명령(log). 서버 기록에는 명령 원문·판정이 없다 — 지어내지 않는다.
const VERIFY = ['전체', '안전 확인됨', '실행 차단', '추가 확인 필요'];
const EXEC = ['전체', '성공', '실패', 'STOP'];
const time = (ms) => new Date(ms).toLocaleString('ko-KR', { hour12: false });
const dayKey = (ms) => {
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
};

// 작업 결과 코드 → { label, kind('성공'|'실패'|'STOP') }
function execFromCode(code) {
  const hit = RESULT_LABELS[code];
  if (!hit) return null;
  if (hit[0] === 'ok') return { label: hit[1], kind: '성공' };
  return { label: hit[1], kind: /정지/.test(hit[1]) ? 'STOP' : '실패' };
}

function fromLog(e, robotFallback) {
  const ev = (k) => e.events.filter((x) => x.kind === k);
  const decision = ev('decision')[0];
  const result = ev('result')[0];
  let exec = result ? (execFromCode(result.detail) || { label: result.label, kind: null }) : null;
  if (!exec && ev('stop').length) exec = { label: '정지 요청', kind: 'STOP' };
  if (!exec && ev('cancel').length) exec = { label: '실행 안 함(취소)', kind: null };
  if (!exec && ev('error').length) exec = { label: ev('error')[0].label, kind: '실패' };
  return {
    key: `L${e.id}`, at: e.sentAt, robot: e.robot || robotFallback, text: e.text, requester: '이 화면',
    verify: decision ? decision.label : '확인 안 됨',
    exec: exec || { label: decision ? '진행 중 또는 실행 전' : '확인 안 됨', kind: null },
    search: `${e.text} L${e.id}`, entry: e,
  };
}

function fromJob(j, robot, names) {
  let exec = execFromCode(j.result_status);
  if (!exec) {
    if (j.status === 'running') exec = { label: '실행 중', kind: null };
    else exec = j.exit_code === 0 ? { label: '완료', kind: '성공' } : { label: '실패', kind: '실패' };
  }
  // 자재는 서버가 준 한국어 이름(simDemo.materials[].korean)으로 — 내부 id를 1차로 보이지 않는다.
  const summary = [j.action_label || j.action, names[j.material] || j.material].filter(Boolean).join(' · ');
  return {
    key: j.job_id, at: j.started_at * 1000, robot, text: summary, requester: '서버 기록', verify: '서버 기록 없음', exec,
    search: `${summary} ${j.job_id}`, job: j,
  };
}

function Timeline({ row }) {
  const { entry, job } = row;
  const ev = (k) => (entry ? entry.events.filter((x) => x.kind === k) : []);
  const none = '서버 기록 없음';
  const decision = ev('decision')[0];
  const stops = ev('stop');
  const approved = ev('confirm').length ? '승인' : ev('cancel').length ? '취소' : null;
  const steps = [
    ['계획', entry ? `명령 원문: ${entry.text}` : '명령 원문: 서버 기록 없음', 'on'],
    ['시뮬레이션(안전 확인)', !entry ? none : decision ? `${decision.label}${decision.detail ? ` — ${decision.detail}` : ''}` : '판정 없음', decision ? 'on' : 'none'],
    ['허가(승인/취소)', !entry ? none : approved || '승인 기록 없음', approved ? 'on' : 'none'],
    ['실행', row.exec.label + (job ? ` · ${time(row.at)} 시작` : ''), row.exec.kind || job ? 'on' : 'none'],
  ];
  const stopped = stops.length > 0 || (job && row.exec.kind === 'STOP');
  return <ol className="timeline">
    {steps.map(([name, text, state]) => <li key={name} data-state={state}><b>{name}</b><small>{text}</small></li>)}
    {stopped
      ? <li data-state="stop"><b>정지</b><small>{stops.length ? stops.map((s) => `${time(s.at)} ${s.label}`).join(', ') : row.exec.label}</small></li>
      : <li data-state="none"><b>정지</b><small>정지 요청 없음</small></li>}
  </ol>;
}

export default function History({ server, log = [] }) {
  const [verify, setVerify] = useState('전체');
  const [exec, setExec] = useState('전체');
  const [robot, setRobot] = useState('전체');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [q, setQ] = useState('');
  const [selected, setSelected] = useState(null);

  const robotId = server?.health?.robot?.robot_id || '';
  const rows = [
    ...log.map((e) => fromLog(e, robotId)),
    ...(server?.simDemo?.recent_jobs || []).map((j) => fromJob(j, robotId, Object.fromEntries((server?.simDemo?.materials || []).map((m) => [m.model, m.korean])))),
  ].sort((a, b) => b.at - a.at);

  const needle = q.trim().toLowerCase();
  const shown = rows.filter((r) => (verify === '전체' || r.verify === verify)
    && (exec === '전체' || r.exec.kind === exec)
    && (robot === '전체' || r.robot === robot)
    && (!from || dayKey(r.at) >= from) && (!to || dayKey(r.at) <= to)
    && (!needle || r.search.toLowerCase().includes(needle)));
  const current = rows.find((r) => r.key === selected);
  const blocked = verify === '실행 차단'; // 차단된 명령은 실행 자체가 없다 — 실행 결과 필터 비활성
  const pickVerify = (v) => () => { setVerify(v); if (v === '실행 차단') setExec('전체'); };
  const robotOptions = ['전체', ...new Set(rows.map((r) => r.robot).filter(Boolean))];

  return <>
    <div className="card rec-bar">
      <div role="group" aria-label="검증 결과" className="rec-group">
        <span className="rec-label">검증 결과</span>
        {VERIFY.map((v) => <button key={v} type="button" className="rec-chip" aria-pressed={verify === v} onClick={pickVerify(v)}>{v}</button>)}
      </div>
      <div role="group" aria-label="실행 결과" className="rec-group">
        <span className="rec-label">실행 결과</span>
        {EXEC.map((v) => <button key={v} type="button" className="rec-chip" aria-pressed={exec === v} disabled={blocked} onClick={() => setExec(v)}>{v}</button>)}
      </div>
      <div role="group" aria-label="조회 범위" className="rec-group">
        <span className="rec-label">조회 범위</span>
        <select className="rec-input" aria-label="로봇" value={robot} onChange={(e) => setRobot(e.target.value)}>
          {robotOptions.map((r) => <option key={r} value={r}>{r === '전체' ? '전체 로봇' : r}</option>)}
        </select>
        <input className="rec-input" type="date" aria-label="시작 날짜" value={from} onChange={(e) => setFrom(e.target.value)} />
        <span>~</span>
        <input className="rec-input" type="date" aria-label="끝 날짜" value={to} onChange={(e) => setTo(e.target.value)} />
      </div>
      <input className="rec-input" type="search" aria-label="명령 원문·ID 검색" placeholder="명령 원문·ID 검색" value={q} onChange={(e) => setQ(e.target.value)} />
    </div>
    <p className="note muted">{verify === '전체' ? '검증 전체' : verify} · {exec === 'STOP' ? 'STOP 조건 적용 중' : exec === '전체' ? '실행 전체' : `실행 ${exec}`} · {shown.length}건</p>
    <div className="card">
      <table className="rec-table" aria-label="명령 기록">
        <thead><tr><th>요청 시각</th><th>로봇</th><th>명령 요약</th><th>요청자</th><th>검증 결과</th><th>실행 결과</th></tr></thead>
        <tbody>
          {shown.map((r) => <tr key={r.key} tabIndex={0} aria-selected={selected === r.key}
            onClick={() => setSelected(r.key)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelected(r.key); } }}>
            <td>{time(r.at)}</td><td>{r.robot || '확인 안 됨'}</td><td>{r.text}</td><td>{r.requester}</td><td>{r.verify}</td><td>{r.exec.label}</td>
          </tr>)}
        </tbody>
      </table>
      {!shown.length && <p className="rec-empty">{rows.length ? '조건에 맞는 기록이 없습니다' : '기록이 없습니다'}</p>}
    </div>
    {current && <section className="card rec-detail" aria-label="명령 상세">
      <h2>명령 상세 · {current.text}</h2>
      <Timeline row={current} />
      {current.job && <p className="note muted">작업 ID {current.job.job_id}</p>}
    </section>}
  </>;
}
