import Spinner from '../components/Spinner.jsx';
import { Fragment, useEffect, useState } from 'react';
import { DetailRow, ExpandButton } from '../components/ExpandRow.jsx';
import { useExpand } from '../components/useExpand.js';
import { RESULT_LABELS } from '../simCommand.js';
import './records.css';

// 기록 = 서버 이력(GET /v1/history: 요청·판정·실행 + 시연 작업 파일) + /v1/sim-demo recent_jobs + 이 탭에서 보낸 명령(log).
// 서버는 시연 명령 발화와 그 발화가 만든 작업을 연결해 기록하지 않는다 — 화면도 억지로 짝짓지 않는다.
const HISTORY_POLL_MS = 10000; // 이력 조회 주기(화면 갱신 속도일 뿐 판단 기준 아님)
const HISTORY_LIMIT = 50;
// 서버 검증 판정(validation/safety_validator.py SafetyDecision: allow·block·ask) → 화면 용어표
const VERIFY_LABELS = { allow: '안전 확인됨', block: '실행 차단', ask: '추가 확인 필요' };
// 실행 최종 상태(core/execution_state.py) → [표시, 필터 분류]. unknown은 성공으로 바꾸지 않는다.
const STATE_LABELS = {
  completed: ['완료', '성공'], failed: ['실패', '실패'], stopped: ['정지 확인됨', 'STOP'], stopping: ['정지 요청됨 · 확인 전', null],
  executing: ['실행 중', null], accepted: ['실행 대기', null], idle: ['실행 전', null], unknown: ['확인 안 됨', null],
};

function useHistory() {
  const [state, setState] = useState({ data: null, failing: false });
  useEffect(() => {
    let cancelled = false;
    let timer;
    const loop = async () => {
      try {
        const res = await fetch(`/v1/history?limit=${HISTORY_LIMIT}`, { cache: 'no-store' });
        if (!res.ok) throw new Error(String(res.status));
        const data = await res.json();
        if (!cancelled) setState({ data, failing: false });
      } catch {
        if (!cancelled) setState((s) => ({ ...s, failing: true })); // 마지막 값 유지
      }
      if (!cancelled) timer = setTimeout(loop, HISTORY_POLL_MS);
    };
    loop();
    return () => { cancelled = true; clearTimeout(timer); };
  }, []);
  return state;
}
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

// 서버 작업(recent_jobs 한 줄)의 실행 결과 — 결과 코드가 없으면 상태·종료 코드로 정한다.
function execFromJob(j) {
  const hit = execFromCode(j.result_status);
  if (hit) return hit;
  if (j.status === 'running') return { label: '실행 중', kind: null };
  // 결과 파일의 상태 코드가 표에 없으면 성공·실패를 지어내지 않는다(내부 코드도 1차 화면에 쓰지 않는다).
  if (j.fromFile) return { label: '결과 확인 안 됨', kind: null };
  return j.exit_code === 0 ? { label: '완료', kind: '성공' } : { label: '실패', kind: '실패' };
}

// jobsById: 로그의 'job' 이벤트(detail = job_id)와 이어지는 서버 작업. 있으면 row.job으로 붙여 한 줄로 보인다.
function fromLog(e, robotFallback, jobsById, me) {
  const ev = (k) => e.events.filter((x) => x.kind === k);
  const decision = ev('decision')[0];
  const result = ev('result')[0];
  const job = ev('job').map((x) => jobsById[x.detail]).find(Boolean);
  let exec = result ? (execFromCode(result.detail) || { label: result.label, kind: null }) : null;
  if (!exec && job && job.status !== 'running') exec = execFromJob(job); // 로그에 결과가 아직 없으면 서버 작업 결과로
  if (!exec && ev('stop').length) exec = { label: '정지 요청', kind: 'STOP' };
  if (!exec && job) exec = execFromJob(job);
  if (!exec && ev('stop_none').length) exec = { label: ev('stop_none')[0].label, kind: null };
  if (!exec && ev('cancel').length) exec = { label: '실행 안 함(취소)', kind: null };
  if (!exec && ev('error').length) exec = { label: ev('error')[0].label, kind: '실패' };
  return {
    key: `L${e.id}`, at: e.sentAt, robot: e.robot || robotFallback, text: e.text, requester: me ? me.name || me.email : '이 화면',
    verify: decision ? decision.label : '확인 안 됨',
    // 차단·되묻기·계획 실패로 끝난 명령은 실행 자체가 없다.
    exec: exec || { label: !decision ? '확인 안 됨' : ['실행 차단', '추가 확인 필요', '계획 실패', '시연 명령 아님'].includes(decision.label) ? '실행 없음' : '진행 중 또는 실행 전', kind: null },
    search: `${e.text} L${e.id}${job ? ` ${job.job_id}` : ''}`, entry: e, job,
  };
}

// 요청자 = 서버가 남긴 보낸 사람(command_actors, 결정 Q10). 기록 기능 전 명령·로그인 없이 보낸 정지는 "기록 없음".
const actorLabel = (a) => (a ? a.name || a.email : '기록 없음');

// 서버 요청 기록(/v1/history commands 한 줄). created_at·execution_started_at은 초 단위.
function fromCommand(c, robot) {
  const v = c.decision ? VERIFY_LABELS[c.decision] || '확인 안 됨' : '검증 기록 없음';
  const st = c.final_state ? STATE_LABELS[c.final_state] : null;
  const exec = st ? { label: st[0], kind: st[1] } : c.execution_id ? { label: '확인 안 됨', kind: null } : { label: '실행 없음', kind: null };
  return {
    key: `R${c.request_id}`, at: c.created_at * 1000, robot, text: c.utterance, requester: actorLabel(c.actor), verify: v, exec,
    search: `${c.utterance} ${c.request_id}${c.execution_id ? ` ${c.execution_id}` : ''}`, command: c,
  };
}

// /v1/history sim_jobs(작업 결과 파일) → recent_jobs와 같은 모양. written_at은 결과를 쓴 시각(ISO)이라 시작 시각이 아니다.
function jobFromFile(f) {
  const at = Date.parse(f.written_at);
  return { job_id: f.job_id, action: f.action, action_label: f.action_label, material: f.material, status: 'finished',
    result_status: f.status, started_at: Number.isFinite(at) ? at / 1000 : null, fromFile: true, actor: f.actor };
}

function fromJob(j, robot, names, actionLabels = {}) {
  // 자재는 서버가 준 한국어 이름(simDemo.materials[].korean)으로 — 내부 id를 1차로 보이지 않는다.
  // 동작 이름: 이력 API가 한국어 이름을 못 주면(action_label이 내부 이름 그대로) /v1/sim-demo action_labels로.
  const label = j.action_label && j.action_label !== j.action ? j.action_label : actionLabels[j.action] || '작업';
  const summary = [label, names[j.material] || j.material].filter(Boolean).join(' · ');
  return {
    key: j.job_id, at: j.started_at * 1000, robot, text: summary, requester: actorLabel(j.actor), verify: '서버 기록 없음', exec: execFromJob(j),
    search: `${summary} ${j.job_id}`, job: j,
  };
}

function Timeline({ row }) {
  const { entry, job, command } = row;
  if (command) {
    return <ol className="timeline">
      <li data-state="on"><b>계획</b><small>명령 원문: {command.utterance}</small></li>
      <li data-state={command.decision ? 'on' : 'none'}><b>시뮬레이션(안전 확인)</b><small>{row.verify}</small></li>
      <li data-state="none"><b>허가(승인/취소)</b><small>서버 기록 없음</small></li>
      <li data-state={command.execution_id ? 'on' : 'none'}><b>실행</b><small>{row.exec.label}{command.execution_started_at ? ` · ${time(command.execution_started_at * 1000)} 시작` : ''}</small></li>
      {command.final_state === 'stopped'
        ? <li data-state="stop"><b>정지</b><small>정지 확인됨</small></li>
        : <li data-state="none"><b>정지</b><small>{command.final_state === 'stopping' ? '정지 요청됨 · 확인 전' : '정지 기록 없음'}</small></li>}
    </ol>;
  }
  const ev = (k) => (entry ? entry.events.filter((x) => x.kind === k) : []);
  const none = '서버 기록 없음';
  const decision = ev('decision')[0];
  const stops = ev('stop');
  const approved = ev('confirm').length ? '승인' : ev('cancel').length ? '취소' : null;
  const steps = [
    ['계획', entry ? `명령 원문: ${entry.text}` : '명령 원문: 서버 기록 없음', 'on'],
    ['시뮬레이션(안전 확인)', !entry ? none : decision ? `${decision.label}${decision.detail ? ` — ${decision.detail}` : ''}` : '판정 없음', decision ? 'on' : 'none'],
    ['허가(승인/취소)', !entry ? none : approved || '승인 기록 없음', approved ? 'on' : 'none'],
    ['실행', row.exec.label + (job ? `${job.started_at ? ` · ${time(job.started_at * 1000)} ${job.fromFile ? '결과 기록' : '시작'}` : ''} · 작업 ID ${job.job_id}` : ''), row.exec.kind || job ? 'on' : 'none'],
  ];
  const stopNone = ev('stop_none')[0]; // 서버가 정지를 접수하지 않은 요청 — 붉은 정지 노드가 아니다
  const stopped = stops.length > 0 || (job && row.exec.kind === 'STOP');
  return <ol className="timeline">
    {steps.map(([name, text, state]) => <li key={name} data-state={state}><b>{name}</b><small>{text}</small></li>)}
    {stopped
      ? <li data-state="stop"><b>정지</b><small>{stops.length ? stops.map((s) => `${time(s.at)} ${s.label}`).join(', ') : row.exec.label}</small></li>
      : <li data-state="none"><b>정지</b><small>{stopNone ? '정지 요청 — 정지할 작업 없음' : '정지 요청 없음'}</small></li>}
  </ol>;
}

export default function History({ server, log = [], user = null }) {
  const [verify, setVerify] = useState('전체');
  const [exec, setExec] = useState('전체');
  const [robot, setRobot] = useState('전체');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [q, setQ] = useState('');
  const [selected, setSelected] = useState(null);

  const robotId = server?.health?.robot?.robot_id || '';
  const expand = useExpand();
  const history = useHistory();
  // 시연 작업 = 메모리 recent_jobs(시작 시각 있음) + 결과 파일(/v1/history, 서버 재시작 뒤에도 남음). 같은 id는 recent_jobs 우선.
  // 보낸 사람은 이력 API에만 있다(sim_jobs의 actor, recent_jobs용 job_actors) — recent_jobs 줄에도 붙인다.
  const fileActor = { ...Object.fromEntries((history.data?.sim_jobs || []).map((f) => [f.job_id, f.actor])), ...(history.data?.job_actors || {}) };
  const memJobs = (server?.simDemo?.recent_jobs || []).map((j) => ({ ...j, actor: fileActor[j.job_id] }));
  const memIds = new Set(memJobs.map((j) => j.job_id));
  const serverJobs = [...memJobs, ...(history.data?.sim_jobs || []).filter((f) => !memIds.has(f.job_id)).map(jobFromFile)];
  const jobsById = Object.fromEntries(serverJobs.map((j) => [j.job_id, j]));
  const names = Object.fromEntries((server?.simDemo?.materials || []).map((m) => [m.model, m.korean]));
  // 이 탭에서 보낸 명령(log)과 이어진 서버 작업은 로그 행에 붙이고, recent_jobs 쪽 줄은 빼서 같은 명령이 두 줄로 나오지 않게 한다.
  const logRows = log.map((e) => fromLog(e, robotId, jobsById, user));
  const linked = new Set(logRows.filter((r) => r.job).map((r) => r.job.job_id));
  // 목표(여러 단계) 명령의 단계 작업도 그 명령 줄에 속한다.
  log.forEach((e) => e.events.forEach((x) => { if (x.kind === 'goal_job') linked.add(x.detail); }));
  // 이 탭에서 보낸 명령이 서버 요청 기록에도 있으면(음성 확정 등) 한 줄만 — 같은 문장이 10초 안에 들어온 것만 같은 명령으로 본다(표시용 중복 제거).
  const SAME_CMD_MS = 10000;
  const commands = (history.data?.commands || []).filter((c) => !log.some((e) => e.text === c.utterance && Math.abs(e.sentAt - c.created_at * 1000) < SAME_CMD_MS));
  const rows = [
    ...logRows,
    ...commands.map((c) => fromCommand(c, robotId)),
    ...serverJobs.filter((j) => !linked.has(j.job_id)).map((j) => fromJob(j, robotId, names, server?.simDemo?.action_labels || {})),
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
    {server?.simDemoFailing && <p className="note muted">작업 상태 조회 실패 — 마지막으로 받은 값입니다</p>}
    {history.failing && <p className="note muted">서버 이력 조회 실패{history.data ? ' — 마지막으로 받은 값입니다' : ' — 이 화면에서 보낸 명령만 보입니다'}</p>}
    <div className="card">
      <table className="data-table" aria-label="명령 기록">
        <thead><tr><th>요청 시각</th><th className="col-extra">로봇</th><th>명령 요약</th><th className="col-extra">요청자</th><th className="col-extra">검증 결과</th><th>실행 결과</th></tr></thead>
        <tbody>
          {shown.map((r) => <Fragment key={r.key}>
            <tr tabIndex={0} aria-selected={selected === r.key}
              onClick={() => setSelected(r.key)}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelected(r.key); } }}>
              <td><ExpandButton open={expand.isOpen(r.key)} onToggle={() => expand.toggle(r.key)} />{time(r.at)}</td>
              <td className="col-extra">{r.robot || '확인 안 됨'}</td><td>{r.text}</td><td className="col-extra">{r.requester}</td><td className="col-extra">{r.verify}</td><td>{r.exec.label}</td>
            </tr>
            <DetailRow open={expand.isOpen(r.key)} span={3} items={[['로봇', r.robot || '확인 안 됨'], ['요청자', r.requester], ['검증 결과', r.verify]]} />
          </Fragment>)}
        </tbody>
      </table>
      {!shown.length && <p className="rec-empty">{rows.length ? '조건에 맞는 기록이 없습니다' : !server?.simDemo && server?.conn?.status === 'connecting' ? <><Spinner size={14} label="불러오는 중" /> 불러오는 중</> : '기록이 없습니다'}</p>}
    </div>
    {current && <section className="card rec-detail" aria-label="명령 상세">
      <h2>명령 상세 · {current.text}</h2>
      <Timeline row={current} />
    </section>}
  </>;
}
