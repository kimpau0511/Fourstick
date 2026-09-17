/** 화면에 쓰는 표시용 데이터.
 *
 * **로봇 수치를 이 파일에서 만들지 않는다.** FR3-WMS 값은 저장소의 Profile
 * (`config/profiles/*.json`)과 8단계 검증 결과에서 확인된 것만 적고, 확인되지
 * 않은 항목은 "미확보"로 남긴다. 나머지 로봇은 **미구현**으로 표시하고 선택할
 * 수 없게 둔다 — 검증되지 않은 값을 화면에 만들어 넣지 않기 위해서다.
 */

export const ROBOTS = [
  {
    id: 'fairino_fr3',
    name: 'FAIRINO FR3-WMS + 2F-85',
    model: 'FR3-WMS + GRP-CPL-062 + 2F-85',
    dof: 6,
    icon: '🦾',
    implemented: true,
    summary: 'FAIRINO FR3-WMS + 2F-85 · Gazebo workcell simulation',
    payload: '미확보',
    reach: '0.630 m (flange 수평 반경 · 미검증)',
    adapter: 'FR3 GAZEBO',
    profile: 'fr3wms_2f85_workcell · 0.1.0-gazebo-workcell',
    environment: 'Simulator / Gazebo workcell',
    skills: ['home', 'move', 'stop'],
    disabledSkills: ['pick', 'place'],
    disabledReason:
      '2F-85 장착 근거, 그리퍼 close 안정성, 파지 관측, pick/place 재검증 미완료',
    badges: [
      { color: 'success', icon: '✓', label: 'MoveIt2 검증 완료' },
      { color: 'warning', icon: '⬡', label: '시뮬레이션 전용' },
      { color: 'danger', icon: '!', label: '실하드웨어 미검증' },
    ],
    notes:
      '작업 셀(받침대·작업대·팔레트 3개·자재 3개·컨베이어)에서 home/move/stop을'
      + ' 검증했다. pick/place는 비활성이다.',
    verification: [
      { label: 'MoveIt2 설정', value: 'config/moveit · KDL · OMPL RRTConnect' },
      { label: '안전 home', value: '바닥·환경 충돌 검증 완료 (MoveIt + Gazebo)' },
      { label: '접근 자세', value: '팔레트 3개 · 컨베이어 — 충돌 없음 확인' },
      { label: '그리퍼 제어', value: 'open / 30 mm / close 관측 개구 일치' },
      { label: 'arm·gripper STOP', value: '관측 정지 확인' },
      { label: '실기 하드웨어', value: '미검증' },
      { label: 'pick/place', value: '비활성 — 관문 9조건 미충족' },
    ],
  },
  {
    id: 'doosan_m1013',
    name: 'Doosan M1013',
    model: 'M1013',
    dof: 6,
    icon: '🏭',
    implemented: false,
    summary: '미구현 — Adapter·Profile 없음',
    payload: '미확보',
    reach: '미확보',
    adapter: '미구현',
    profile: '미확보',
    environment: '미정',
    skills: [],
    disabledSkills: [],
    disabledReason: '',
    badges: [{ color: 'muted', icon: '◉', label: '미구현' }],
    notes: '9단계 확장 후보. 공식 자료를 확보한 뒤 Profile을 만든다.',
    verification: [],
  },
  {
    id: 'kinova_gen3',
    name: 'Kinova Gen3',
    model: 'Gen3',
    dof: 7,
    icon: '✨',
    implemented: false,
    summary: '미구현 — Adapter·Profile 없음',
    payload: '미확보',
    reach: '미확보',
    adapter: '미구현',
    profile: '미확보',
    environment: '미정',
    skills: [],
    disabledSkills: [],
    disabledReason: '',
    badges: [{ color: 'muted', icon: '◉', label: '미구현' }],
    notes: '9단계 확장 후보. 공식 자료를 확보한 뒤 Profile을 만든다.',
    verification: [],
  },
  {
    id: 'ur5e',
    name: 'UR5e',
    model: 'e-Series UR5',
    dof: 6,
    icon: '⚙️',
    implemented: false,
    summary: '미구현 — 9단계 회귀 대상',
    payload: '미확보',
    reach: '미확보',
    adapter: '미구현',
    profile: '미확보',
    environment: '미정',
    skills: [],
    disabledSkills: [],
    disabledReason: '',
    badges: [{ color: 'muted', icon: '◉', label: '미구현' }],
    notes: '9단계 회귀 검증 대상. 같은 2F-85 Profile을 재사용할 예정이다.',
    verification: [],
  },
];

export function robotById(id) {
  return ROBOTS.find((r) => r.id === id) || ROBOTS[0];
}

/** 화면 정책 항목. **승인 관련 항목을 두지 않는다.**
 *
 * 수치는 서버 설정(`/v1/config`)에서 온다 — 화면에 숫자를 적어 두지 않는다.
 * 서버가 없으면 "미연결"로 표시한다(추측하지 않는다).
 */
export function policyItems(config) {
  if (!config) {
    return [
      { label: '실행 전 사용자 확인', value: '필수' },
      { label: '서버 설정', value: '미연결 (시뮬레이션 모드)' },
    ];
  }
  const policies = config.policies || {};
  const catalogs = config.catalogs || {};
  const session = config.session || {};
  const ttl = session.plan_ttl_sec;
  return [
    { label: '실행 전 사용자 확인', value: '필수' },
    {
      label: '계획 유효 시간',
      value: ttl ? `${Math.round(ttl / 60)}분 (${ttl}s)` : '미설정',
    },
    {
      label: '안전 정책',
      value: (policies.safety && policies.safety.policy_version) || '미설정',
    },
    {
      label: '정지 정책',
      value: policies.stop
        ? `${policies.stop.policy_version} · hold ${policies.stop.hold_sec}s`
        : '미설정',
    },
    { label: '위치 카탈로그', value: `${(catalogs.locations || []).length}개` },
    { label: '자재 카탈로그', value: `${(catalogs.objects || []).length}개` },
    { label: '스킬 카탈로그', value: `${(catalogs.skills || []).length}개` },
  ];
}

/** 스킬 설명. 서버가 계획 스텝을 줄 때 사람이 읽을 문구로 바꾼다. */
const SKILL_TEXT = {
  home: '안전 위치 이동',
  move: '이동',
  pick: '집기',
  place: '배치',
  stop: '정지',
};

/** 자원 표시 이름. **화면에 목록을 만들어 두지 않는다** — 서버 카탈로그
 *  (`/v1/config`의 catalogs)에서 받아 채우고, 없으면 id를 그대로 보여준다. */
const RESOURCE_LABELS = new Map([
  // home 자세는 서버 카탈로그의 자원이 아니다. 화면에서만 쓰는 이름이다.
  ['loc_home', '안전 위치'],
]);

export function setResourceLabels(entries) {
  (entries || []).forEach((entry) => {
    if (entry && entry.resource_id && entry.display_name) {
      RESOURCE_LABELS.set(entry.resource_id, entry.display_name);
    }
  });
}

export function resourceLabel(id) {
  return RESOURCE_LABELS.get(id) || id;
}

/** 스텝 하나를 사람이 읽는 문구로. 인자 이름을 파싱해 동작을 정하지 않는다. */
export function describeStep(skill, args = {}) {
  const base = SKILL_TEXT[skill] || skill;
  const target = args.to || args.from || args.object;
  if (skill === 'home' || skill === 'stop' || !target) return base;
  if (skill === 'pick') return `${resourceLabel(args.object || target)} 집기`;
  if (skill === 'place') return `${resourceLabel(args.to || target)}에 배치`;
  return `${resourceLabel(target)} 이동`;
}

/** 시뮬레이션 노드. 계획의 위치 자원 순서에서 만든다(고정 좌표를 쓰지 않는다). */
export function simNodesFromPlan(plan) {
  if (!plan || !plan.steps || !plan.steps.length) return [];
  const sequence = [];
  plan.steps.forEach((step) => {
    const resource =
      step.skill === 'home' ? 'loc_home' : step.args && (step.args.to || step.args.from);
    if (!resource) return;
    const last = sequence[sequence.length - 1];
    if (last && last.resource === resource) {
      last.steps.push(step.no);
      return;
    }
    sequence.push({ resource, steps: [step.no] });
  });
  // 목업과 같은 배치 리듬(위·아래로 번갈아)을 유지한다.
  const rhythm = [50, 25, 65, 35, 60, 30];
  const span = sequence.length > 1 ? 80 / (sequence.length - 1) : 0;
  return sequence.map((node, index) => ({
    id: `${node.resource}_${index}`,
    label: node.resource === 'loc_home' ? 'HOME' : resourceLabel(node.resource).toUpperCase(),
    sub: node.resource,
    x: 10 + span * index,
    y: rhythm[index % rhythm.length],
    steps: node.steps,
  }));
}

/** 어느 노드가 지금 단계인가. */
export function activeNodeIndex(nodes, currentStep) {
  if (!currentStep) return -1;
  return nodes.findIndex((node) => node.steps.includes(currentStep));
}

export const EVENT_LEVELS = {
  info: { label: '정보', dot: 'info' },
  warning: { label: '경고', dot: 'warning' },
  error: { label: '오류', dot: 'danger' },
  execution: { label: '실행', dot: 'success' },
};


/** 로봇 카드에 **서버가 알려 준 작업 셀 값**을 덮어씌운다.
 *
 * 화면에 값을 만들어 두지 않는다. 서버가 붙어 있지 않으면 표시용 기본값을
 * 그대로 쓰고, 붙어 있으면 실제 world·파티션·스킬을 보여준다.
 */
export function withWorkcell(robot, workcell) {
  if (!workcell || !workcell.registered) return robot;
  const skills = workcell.supported_skills || robot.skills;
  return {
    ...robot,
    skills,
    // 서버가 연 스킬만 지원으로 본다. 나머지는 비활성으로 남긴다.
    disabledSkills: ['pick', 'place'].filter((s) => !skills.includes(s)),
    adapter: 'FR3 GAZEBO',
    environment: `Gazebo workcell · ${workcell.world || '—'}`,
  };
}

/** 작업 셀 자원 목록: 한국어 이름 → 자원 id → Gazebo 모델 → 프레임.
 *
 * **화면에 목록을 만들어 두지 않는다.** 서버가 준 대조표만 보여주고, 없으면
 * 비어 있다고 표시한다.
 */
export function workcellResources(workcell) {
  if (!workcell || !workcell.resources) return [];
  const moves = workcell.move_poses || {};
  const picks = workcell.pick_poses || {};
  const places = workcell.place_poses || {};
  return Object.entries(workcell.resources)
    .map(([id, mapping]) => ({
      id,
      korean: mapping.korean || id,
      model: mapping.gazebo_model || '—',
      frame: mapping.frame || '—',
      kind: id.startsWith('mat_') ? 'object' : 'location',
      movePose: moves[id] || null,
      pickPose: picks[id] || null,
      placePose: places[id] || null,
    }))
    .sort((a, b) => (a.kind === b.kind ? a.id.localeCompare(b.id) : a.kind === 'location' ? -1 : 1));
}

/** 계획의 자원을 씬 id·프레임과 대조한 행. 대조표에 없으면 그 사실을 남긴다. */
export function planResourceMapping(plan, workcell) {
  if (!plan || !plan.steps) return [];
  const table = (workcell && workcell.resources) || {};
  const moves = (workcell && workcell.move_poses) || {};
  const seen = new Map();
  plan.steps.forEach((step) => {
    const args = step.args || {};
    [args.to, args.from, args.object].forEach((value) => {
      if (!value || seen.has(value)) return;
      const mapping = table[value];
      seen.set(value, {
        resource: value,
        label: resourceLabel(value),
        model: mapping ? mapping.gazebo_model : null,
        frame: mapping ? mapping.frame : null,
        pose: moves[value] || null,
        known: Boolean(mapping),
      });
    });
  });
  return [...seen.values()];
}
