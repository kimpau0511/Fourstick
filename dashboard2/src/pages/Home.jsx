import Spinner from '../components/Spinner.jsx';
import MetricRow from '../components/MetricRow.jsx';
import { RESULT_LABELS } from '../simCommand.js';
import './home.css';

const SHAPE = { NORMAL: '●', NOTICE: '◆', WARNING: '▲', CRITICAL: '■', NO_DATA: '?' };
const hhmmss = (sec) => new Date(sec * 1000).toTimeString().slice(0, 8);
const roleName = (skills) => (skills?.includes('pick') || skills?.includes('place') ? '이송 로봇' : '로봇');

// 3D 관측의 그리퍼 관절 값. 정상 범위 기준이 서버에 없으므로 "관측됨 = 정상"만 말하고 원시값은 숨긴다.
function gripperText(simState) {
  const g = simState && !simState.stale && Object.entries(simState.joints || {}).find(([k]) => /knuckle|gripper/i.test(k));
  // 정상 범위 기준이 서버에 없다 — 값이 관측된다는 것만 말하고 '정상'이라 하지 않는다(설계원칙 4).
  return g && Number.isFinite(g[1]) ? '관측 중' : '확인 안 됨';
}

function resultText(j) {
  if (j.status === 'running') return '실행 중';
  return RESULT_LABELS[j.result_status]?.[1] || (j.exit_code === 0 ? '완료' : j.exit_code == null ? '기록 없음' : '확인 필요');
}

export default function Home({ server }) {
  const { health, config, simDemo, simState, robotStatus } = server;
  const loading = server.conn.status === 'connecting'; // 첫 응답 전 — 연결이 실패·끊김이면 기존 문구
  const wait = <><Spinner size={14} label="불러오는 중" /> 불러오는 중</>;
  const none = loading ? wait : '데이터 없음';
  const exec = health?.executions;
  const planning = health?.features?.planning;
  const metrics = [
    { label: '시뮬레이션 실행 누적', value: exec ? exec.simulated : none, unit: exec ? '건' : '' },
    { label: '확인 안 된 실행', value: exec ? exec.unknown : none, unit: exec ? '건' : '', ...(exec?.unknown > 0 ? { badge: '확인 필요', tone: 'warn' } : {}) },
    { label: '계획 모델 상태', value: planning ? (planning.available ? '사용 가능' : '사용 불가') : none,
      ...(planning ? { badge: planning.detail || '', tone: planning.available ? 'ok' : 'warn' } : {}) },
  ];
  const robot = config?.robot;
  const job = simDemo?.running_job;
  const jobs = simDemo?.recent_jobs;
  const matName = Object.fromEntries((simDemo?.materials || []).map((m) => [m.model, m.korean]));
  return <>
    <MetricRow title="오늘의 핵심 운영 지표" items={metrics} />
    <section>
      <h2>실시간 로봇 작동 상태</h2>
      <div className="row3">
        {health?.robot?.configured !== false ? <div className="card robot">
          <div className="robot-head">
            <div><strong>{roleName(robot?.supported_skills)}</strong><small>{[robot?.workcell?.workcell_id, robot?.profile_id].filter(Boolean).join(' · ')}</small></div>
            <span className="state state-shape" data-shape={SHAPE[robotStatus.level]} data-level={robotStatus.level}>{SHAPE[robotStatus.level]} {robotStatus.label}</span>
          </div>
          <div className="robot-meta">
            <div><span>현재 작업</span><b>{job ? (job.action_label || job.action) : '대기 중'}</b></div>
            {typeof job?.progress === 'number' && <div className="bar"><i style={{ width: `${job.progress * 100}%` }} /></div>}
            <div><span>그리퍼</span><b>{gripperText(simState)}</b></div>
            {robotStatus.reasons.length > 0 && <div><span>사유</span><b>{robotStatus.reasons.join(' · ')}</b></div>}
          </div>
          <div className="safety-line">안전 신호(비상정지·보호정지·가드): 확인 안 됨</div>
        </div> : <div className="card robot muted">로봇 미설정</div>}
      </div>
    </section>
    <section>
      <h2>최근 작업 내역</h2>
      <div className="card table">
        {jobs?.length ? <table className="data-table" aria-label="최근 작업">
          <thead><tr><th>시각</th><th>자재</th><th>작업</th><th>결과</th></tr></thead>
          <tbody>{jobs.map((j) => <tr key={j.job_id}>
            <td>{j.started_at ? hhmmss(j.started_at) : '기록 없음'}</td>
            <td>{matName[j.material] || j.material || '기록 없음'}{j.slot_label ? ` · ${j.slot_label}` : ''}</td>
            <td>{j.action_label || j.action}</td>
            <td><b>{resultText(j)}</b></td>
          </tr>)}</tbody>
        </table> : <p className="muted" style={{ padding: 14 }}>{!jobs && loading ? wait : '기록 없음'}</p>}
      </div>
    </section>
  </>;
}
