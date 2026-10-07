import Spinner from '../components/Spinner.jsx';

// 로봇 관리 > 구성 · 작업 셀 탭. 옛 웹(html/static/js/render.js)의 「로봇 및 정책」·「작업 셀 자원」 카드를 옮겼다.
// 서버 값(/v1/config)만 보여 준다 — 옛 웹 catalog.js의 ROBOTS(배지·검증 상태·페이로드 '미확보'·비활성 pick/place)는
// 화면에 적어 둔 값이고 서버 값과 이미 어긋나서 옮기지 않았다. 서버에 없으면 '데이터 없음'.
const NONE = '데이터 없음';

function policyRows(config) {
  const { policies = {}, catalogs = {}, session = {} } = config;
  const ttl = session.plan_ttl_sec;
  const count = (list) => (Array.isArray(list) ? `${list.length}개` : NONE);
  return [
    ['계획 유효 시간', ttl != null ? `${Math.round(ttl / 60)}분 (${ttl}s)` : NONE],
    ['안전 정책', policies.safety?.policy_version || NONE],
    ['정지 정책', policies.stop ? `${policies.stop.policy_version} · hold ${policies.stop.hold_sec}s` : NONE],
    ['위치 카탈로그', count(catalogs.locations)],
    ['자재 카탈로그', count(catalogs.objects)],
    ['스킬 카탈로그', count(catalogs.skills)],
  ];
}

// 한국어 이름 → 자원 id → Gazebo 모델 → 프레임. 위치 먼저, 그다음 자재(mat_).
function resourceRows(workcell) {
  const moves = workcell.move_poses || {};
  return Object.entries(workcell.resources || {})
    .map(([id, m]) => ({
      id, korean: m.korean || id, model: m.gazebo_model || NONE, frame: m.frame || NONE,
      kind: id.startsWith('mat_') ? '자재' : '위치', movePose: moves[id] || '—',
    }))
    .sort((a, b) => (a.kind === b.kind ? a.id.localeCompare(b.id) : a.kind === '위치' ? -1 : 1));
}

function KV({ rows }) {
  return <dl className="cfg-kv">{rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>;
}

export default function RobotConfig({ server }) {
  const { config } = server;
  if (!config) {
    return <p className="muted">{server.conn.status === 'connecting' ? <><Spinner size={14} label="불러오는 중" /> 불러오는 중</> : NONE}</p>;
  }
  const robot = config.robot || {};
  const workcell = robot.workcell || null;
  const registered = !!workcell?.registered;
  const resources = registered ? resourceRows(workcell) : [];

  const robotRows = [
    ['실행 어댑터', robot.robot_id ? `${robot.robot_id}${robot.kind ? ` (${robot.kind})` : ''}` : NONE],
    ['작업 셀', registered
      ? `${workcell.workcell_id || NONE} ${workcell.workcell_version || ''}`.trim()
      : <span className="warn">연결되지 않았습니다 — {workcell?.detail || '이유 미기록'}</span>],
  ];
  if (registered) {
    robotRows.push(['Gazebo world', workcell.world || NONE]);
    robotRows.push(['격리', `${workcell.gz_partition || NONE} · domain ${workcell.ros_domain_id ?? NONE}`]);
  }

  return <div className="robot-config">
    <div className="row3">
      <div className="card cfg-card" aria-label="로봇 구성">
        <h3>로봇 구성</h3>
        <KV rows={robotRows} />
        <div>
          <p className="cfg-label">지원 스킬</p>
          <div className="cfg-tags">{robot.supported_skills?.length
            ? robot.supported_skills.map((s) => <span key={s} className="tag info">{s}</span>)
            : <span className="muted">{NONE}</span>}</div>
        </div>
        {robot.is_simulated && <p className="cfg-notice warn">실행 결과는 모두 Gazebo 시뮬레이션 결과입니다.</p>}
        {robot.kind === 'fake' && !registered && <p className="cfg-notice danger">
          개발용 Fake 실행 — 실행은 개발용 Fake Adapter({robot.robot_id || 'fake'})로 수행됩니다. Gazebo 작업 셀에 연결되지 않았습니다.</p>}
      </div>
      <div className="card cfg-card" aria-label="정책">
        <h3>정책</h3>
        <KV rows={policyRows(config)} />
      </div>
    </div>
    <div className="card table">
      <table className="data-table" aria-label="작업 셀 자원">
        <thead><tr><th>구분</th><th>이름</th><th>자원 id</th><th className="col-extra">Gazebo 모델</th><th className="col-extra">프레임</th><th className="col-extra">접근 자세</th></tr></thead>
        <tbody>{resources.length ? resources.map((r) => <tr key={r.id}>
          <td>{r.kind}</td><td>{r.korean}</td><td className="mono">{r.id}</td>
          <td className="col-extra mono">{r.model}</td><td className="col-extra mono">{r.frame}</td><td className="col-extra mono">{r.movePose}</td>
        </tr>) : <tr><td colSpan={6} className="muted">{registered ? NONE : '작업 셀에 연결되지 않았습니다'}</td></tr>}</tbody>
      </table>
    </div>
    <p className="muted cfg-hint">좌표는 화면에 두지 않습니다. 자세는 config/workcell의 검증된 값에서만 옵니다.</p>
  </div>;
}
