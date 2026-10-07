import { Fragment, useState } from 'react';
import { DetailRow, ExpandButton } from '../components/ExpandRow.jsx';
import { useExpand } from '../components/useExpand.js';
import Spinner from '../components/Spinner.jsx';
import { LEVEL_LABELS } from '../server.js';
import RobotConfig from './RobotConfig.jsx';
import './robots.css';

// 화면 탭. 고른 탭은 주소(#/robots?tab=config)에 둔다 — 새로고침해도 같은 탭에 머문다.
const VIEWS = [['list', '로봇 목록'], ['config', '구성 · 작업 셀']];
const viewFromHash = () => (new URLSearchParams(window.location.hash.split('?')[1] || '').get('tab') === 'config' ? 'config' : 'list');

const TABS = ['개요', '도구 장착 이력', '프로파일 버전'];
const NONE = '데이터 없음';
const roleName = (skills) => (skills?.includes('pick') || skills?.includes('place') ? '이송 로봇' : '로봇');

// 운용 가능 여부는 서버 값에서 계산한 결과값이다(켜고 끄는 조작 아님).
function operability(server) {
  const { health, robots, conn, simState } = server;
  const why = [];
  if (!health?.robot?.configured) why.push('로봇 미설정');
  if (robots?.stop_diagnostics?.stop_latch_active) why.push('정지 래치 활성');
  if (server.robotsFailing) why.push('로봇 정보 조회 실패');
  if (robots?.stop_diagnostics?.available !== true) why.push('정지 진단 확인 안 됨');
  if (conn.status !== 'ok') why.push('서버 연결 확인 안 됨');
  if (!simState || simState.stale) why.push('3D 관측 신선하지 않음');
  return why.length ? `운용 불가 — ${why.join(', ')}` : '운용 가능';
}

export default function Robots({ server }) {
  const [view, setView] = useState(viewFromHash);
  function pick(v) {
    setView(v);
    window.history.replaceState(null, '', v === 'config' ? '#/robots?tab=config' : '#/robots');
  }
  return <section>
    <h2>로봇 관리</h2>
    <div className="page-tabs" role="tablist" aria-label="로봇 관리 화면">
      {VIEWS.map(([v, label]) => <button key={v} type="button" role="tab" id={`robots-tab-${v}`} aria-controls={`robots-view-${v}`}
        aria-selected={view === v} onClick={() => pick(v)}>{label}</button>)}
    </div>
    <div role="tabpanel" id={`robots-view-${view}`} aria-labelledby={`robots-tab-${view}`}>
      {view === 'config' ? <RobotConfig server={server} /> : <RobotList server={server} />}
    </div>
  </section>;
}

function RobotList({ server }) {
  const { health, config, robots, robotStatus } = server;
  const [selected, setSelected] = useState(null);
  const [tab, setTab] = useState(TABS[0]);
  const expand = useExpand();
  const [open, setOpen] = useState(false); // 상세(개요·도구 장착 이력·프로파일 버전)는 버튼을 눌러야 펼친다
  const ids = Object.keys(robots?.robots || {});
  const id = ids.includes(selected) ? selected : ids[0];
  const profile = id ? robots.robots[id] : null;
  const robot = id && config?.robot?.robot_id === id ? config.robot : null; // config는 서버 로봇 1대 분
  const skills = profile?.supported_skills || robot?.supported_skills;
  const isServerRobot = !!id && health?.robot?.robot_id === id;
  const op = isServerRobot ? operability(server) : NONE;

  return <>
    {!ids.length ? <p className="muted">{!robots && server.conn.status === 'connecting' ? <><Spinner size={14} label="불러오는 중" /> 불러오는 중</> : NONE}</p> : <div className="robots-split">
      <div className="card table robots-list">
        <table className="data-table" aria-label="로봇 목록">
          <thead><tr><th>이름</th><th className="col-extra">모델</th><th className="col-extra">셀</th><th className="col-extra">현재 도구</th><th>운용 여부</th></tr></thead>
          <tbody>{ids.map((rid) => {
            const r = config?.robot?.robot_id === rid ? config.robot : null;
            const model = robots.robots[rid].profile_id || rid;
            const cell = r?.workcell?.workcell_id || NONE;
            const tool = r ? (r.has_gripper ? '그리퍼 장착' : '장착 도구 없음') : NONE;
            return <Fragment key={rid}>
              <tr aria-selected={rid === id} onClick={() => setSelected(rid)}>
                <td><ExpandButton open={expand.isOpen(rid)} onToggle={() => expand.toggle(rid)} /><button type="button" onClick={() => setSelected(rid)}>{roleName(robots.robots[rid].supported_skills)}</button></td>
                <td className="col-extra">{model}</td>
                <td className="col-extra">{cell}</td>
                <td className="col-extra">{tool}</td>
                <td>{health?.robot?.robot_id === rid ? (operability(server) === '운용 가능' ? '운용 가능' : '운용 불가') : NONE}</td>
              </tr>
              <DetailRow open={expand.isOpen(rid)} span={2} items={[['모델', model], ['셀', cell], ['현재 도구', tool]]} />
            </Fragment>;
          })}</tbody>
        </table>
      </div>
      <div className="card robots-detail" aria-label="로봇 상세">
        {/* 왼쪽 목록에 있는 이름·모델·셀·도구·운용 여부는 되풀이하지 않는다. 목록에 없는 것만 둔다. */}
        <div className="detail-fixed">
          <div><span>현재 상태</span><b>{isServerRobot ? robotStatus.label || LEVEL_LABELS.NO_DATA : NONE}</b></div>
          <div><span>지원 스킬</span><b>{skills?.join(', ') || NONE}</b></div>
          {op !== '운용 가능' && op !== NONE && <div className="wide"><span>운용 불가 사유</span><b className="warn">{op.replace('운용 불가 — ', '')}</b></div>}
        </div>
        <button type="button" className="detail-toggle" aria-expanded={open} aria-controls="robot-detail" onClick={() => setOpen((v) => !v)}>{open ? '상세 닫기' : '상세'}</button>
        {open && <div id="robot-detail" className="detail-more">
        <div className="detail-tabs" role="tablist">
          {TABS.map((t) => <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>{t}</button>)}
        </div>
        <div className="detail-body" role="tabpanel">
          {tab === '개요' && <dl>
            <dt>프로파일</dt><dd>{profile.profile_id || NONE}</dd>
            <dt>자유도</dt><dd>{profile.dof ?? NONE}</dd>
            <dt>가반 하중</dt><dd>{profile.payload_kg != null ? `${profile.payload_kg} kg` : NONE}</dd>
            <dt>작업 반경</dt><dd>{profile.work_radius_m != null ? `${profile.work_radius_m} m` : NONE}</dd>
          </dl>}
          {tab === '도구 장착 이력' && <p className="muted">기록 없음</p>}
          {tab === '프로파일 버전' && <dl>
            <dt>시뮬레이션 프로파일</dt><dd>{profile.profile_version || NONE}</dd>
            <dt>설정 프로파일</dt><dd>{robot?.profile_id ? `${robot.profile_id} ${robot.profile_version || ''}` : NONE}</dd>
          </dl>}
        </div>
        </div>}
      </div>
    </div>}
  </>;
}
