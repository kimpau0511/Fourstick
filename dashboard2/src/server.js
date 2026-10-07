import { useCallback, useEffect, useRef, useState } from 'react';

// 서버 값 수집 훅. 화면은 이 값을 그대로 보여 준다(설계원칙 2) — 서버에 없는 값은 만들지 않는다.
// 조회 주기는 화면 갱신 속도일 뿐 판단 기준이 아니다. 오래됨(stale) 기준은 서버 정책값만 쓴다.
const FAST_MS = 2000; // /health · /v1/sim-demo · /v1/sim-view/state
const SLOW_MS = 10000; // /v1/robots · /v1/config(자주 안 바뀜)
const TICK_MS = 1000; // 마지막 수신 후 경과 시간 갱신

const SOURCES = [
  ['health', '/health', FAST_MS],
  ['simDemo', '/v1/sim-demo', FAST_MS],
  ['simState', '/v1/sim-view/state', FAST_MS],
  ['robots', '/v1/robots', SLOW_MS],
  ['config', '/v1/config', SLOW_MS],
];

const INITIAL = {
  health: null, config: null, robots: null, simDemo: null, simState: null,
  healthOkAt: null, healthFailing: false, robotsFailing: false, simDemoFailing: false, latencyMs: null, refreshedAt: null, nowMs: Date.now(),
  conn: { status: 'connecting', lastReceivedAt: null, latencyMs: null, ageSec: null },
  alerts: [], robotStatus: { level: 'NO_DATA', label: '', reasons: [] },
};

// 새 명령·승인을 잠그는 사유(없으면 null). 즉시 정지는 이 잠금을 타지 않는다.
// 명령 패널과 시뮬레이션 창의 '다시 보내기'가 같은 기준을 쓴다.
export function commandLock(server) {
  if (!server || server.conn.status !== 'ok') return '서버와 연결이 정상이 아니어서 명령을 보낼 수 없습니다';
  const { level, reasons } = server.robotStatus;
  if (level === 'CRITICAL') return '긴급 상태(정지 래치)라 새 명령을 보낼 수 없습니다';
  if (level === 'NO_DATA') return `로봇 상태를 확인할 수 없어 명령을 보낼 수 없습니다${reasons.length ? ` — ${reasons.join(', ')}` : ''}`;
  return null;
}

export const LEVEL_LABELS = {
  NORMAL: '정상', NOTICE: '작업 중', WARNING: '주의', CRITICAL: '긴급', NO_DATA: '데이터 없음',
};

function deriveConn(s) {
  const maxAge = s.config?.policies?.freshness?.environment_max_age_sec;
  const ageSec = s.healthOkAt == null ? null : (s.nowMs - s.healthOkAt) / 1000;
  const base = { lastReceivedAt: s.healthOkAt, latencyMs: s.latencyMs, ageSec };
  // 한 번도 받은 적 없이 요청이 실패하고 있으면 끊김이다 — 정책을 못 받았다고 무한히 '연결 중'으로 두지 않는다.
  if (s.healthOkAt == null && s.healthFailing) return { ...base, status: 'down' };
  // 기준(서버 정책)을 아직 못 받았으면 판단 불가 — 기본값을 지어내지 않고 연결 중으로 둔다.
  if (typeof maxAge !== 'number' || ageSec == null) return { ...base, status: 'connecting' };
  if (ageSec <= maxAge) return { ...base, status: 'ok' };
  return { ...base, status: s.healthFailing ? 'down' : 'stale' };
}

function deriveRobotStatus(s, conn) {
  const reasons = [];
  const simState = s.simState;
  const noData = [];
  if (conn.status !== 'ok') noData.push('서버 연결이 정상이 아닙니다');
  if (!simState) noData.push('3D 관측 값이 없습니다');
  else if (simState.stale) noData.push('3D 관측이 오래되었습니다');
  // 정지 래치는 /v1/robots에서만 온다. 못 받았거나 마지막 조회가 실패했으면 '래치 없음'으로 치지 않는다.
  if (!s.robots?.stop_diagnostics || s.robotsFailing) noData.push('정지 진단 값을 받지 못했습니다');
  else if (s.robots.stop_diagnostics.available === false) noData.push('정지 진단을 사용할 수 없습니다');
  // 복구 필요·실행 중 작업은 /v1/sim-demo에서만 온다 — 못 받았으면 '정상'이라 할 근거가 없다.
  if (!s.simDemo || s.simDemoFailing) noData.push('작업 상태 값을 받지 못했습니다');
  // enabled:false면 서버가 작업 상태(복구 필요·실행 중)를 주지 않는다 — 정상 판정 근거가 없다.
  else if (s.simDemo.enabled !== true) noData.push('시연 명령 서비스가 꺼져 있습니다');
  if (noData.length) return { level: 'NO_DATA', label: LEVEL_LABELS.NO_DATA, reasons: noData };
  if (s.robots?.stop_diagnostics?.stop_latch_active) {
    return { level: 'CRITICAL', label: LEVEL_LABELS.CRITICAL, reasons: ['정지 래치가 활성입니다'] };
  }
  if (s.simDemo?.recovery_required) reasons.push('복구가 필요합니다');
  // robots.declared는 실기(실물) 준비용 프로필이다 — 지금 시뮬레이션 로봇의 상태가 아니고,
  // 실기 준비도는 화면에 보이지 않는다(공개 필터와 같은 원칙). 상태 판정에 쓰지 않는다.
  if (reasons.length) return { level: 'WARNING', label: LEVEL_LABELS.WARNING, reasons };
  const job = s.simDemo?.running_job;
  if (job) return { level: 'NOTICE', label: LEVEL_LABELS.NOTICE, reasons: [job.action_label || job.action || '작업 실행 중'] };
  if (s.health?.running_execution_id) return { level: 'NOTICE', label: LEVEL_LABELS.NOTICE, reasons: ['일반 작업 실행 중'] };
  return { level: 'NORMAL', label: LEVEL_LABELS.NORMAL, reasons: [] };
}

// 알림은 서버 상태에서만 만든다. 상태가 사라지면 알림도 사라지고, 같은 id가 이어지면 since를 유지한다.
function deriveAlerts(s, conn, prev) {
  const robot = s.health?.robot?.robot_id || '';
  const found = [];
  const add = (id, level, text) => found.push({ id, level, robot, text });
  if (s.robots?.stop_diagnostics?.stop_latch_active) add('stop-latch', 'EMERGENCY', '정지 래치가 활성입니다');
  if (s.health?.db && !s.health.db.available) add('db', 'WARNING', `DB를 사용할 수 없습니다${s.health.db.detail ? ` — ${s.health.db.detail}` : ''}`);
  const features = s.health?.features;
  if (features?.planning && !features.planning.available) add('planning', 'WARNING', '계획 모델을 사용할 수 없습니다');
  if (features?.stt && !features.stt.available) add('stt', 'WARNING', '음성 인식을 사용할 수 없습니다');
  if (s.simDemoFailing) add('sim-demo', 'WARNING', '작업 상태 조회가 실패하고 있습니다(마지막 값 표시 중)');
  if (s.simState?.stale) add('sim-stale', 'WARNING', '3D 관측이 오래되었습니다');
  if (s.simDemo?.recovery_required) add('recovery', 'WARNING', '복구가 필요합니다');
  if (conn.status === 'down' || conn.status === 'stale') add('conn', 'WARNING', '서버와 연결이 끊겼습니다');
  const job = s.simDemo?.running_job;
  if (job) add('job', 'TASK', `${job.action_label || job.action || '작업'} 실행 중`);
  const since = new Map(prev.map((a) => [a.id, a.since]));
  const sameIds = found.length === prev.length && found.every((a, i) => a.id === prev[i].id && a.text === prev[i].text);
  if (sameIds) return prev; // 바뀐 게 없으면 같은 배열 — 불필요한 렌더 방지
  return found.map((a) => ({ ...a, since: since.get(a.id) ?? s.nowMs }));
}

function derive(s) {
  const conn = deriveConn(s);
  return { ...s, conn, robotStatus: deriveRobotStatus(s, conn), alerts: deriveAlerts(s, conn, s.alerts) };
}

// 로그인 화면 뒤에 그리는 대시보드용 — 아직 아무것도 받지 않은 상태("연결 확인 중"). 서버를 부르지 않는다(결정 Q2).
export const previewServer = () => derive({ ...INITIAL, nowMs: Date.now() });

export function useServer() {
  const [state, setState] = useState(INITIAL);
  const fetchers = useRef({});

  useEffect(() => {
    let cancelled = false;
    const timers = [];
    const update = (fn) => { if (!cancelled) setState((s) => derive(fn({ ...s, nowMs: Date.now() }))); };

    SOURCES.forEach(([key, path, every]) => {
      const fetchOnce = async () => {
        const startedAt = Date.now();
        try {
          const res = await fetch(path, { cache: 'no-store' });
          if (!res.ok) throw new Error(String(res.status));
          const data = await res.json();
          const at = Date.now();
          update((s) => ({
            ...s, [key]: data, refreshedAt: at,
            ...(key === 'health' ? { healthOkAt: at, healthFailing: false, latencyMs: at - startedAt } : {}),
            ...(key === 'robots' ? { robotsFailing: false } : {}),
            ...(key === 'simDemo' ? { simDemoFailing: false } : {}),
          }));
        } catch {
          // 실패한 값은 지우지 않는다(마지막 값 유지) — 얼마나 오래됐는지는 conn이 말한다.
          // 3D 관측은 서버가 못 줄 수도 있으므로(503) 값 없음으로 둔다.
          update((s) => ({ ...s, ...(key === 'health' ? { healthFailing: true } : {}), ...(key === 'robots' ? { robotsFailing: true } : {}), ...(key === 'simDemo' ? { simDemoFailing: true } : {}), ...(key === 'simState' ? { simState: null } : {}) }));
        }
      };
      fetchers.current[key] = fetchOnce;
      const loop = async () => {
        await fetchOnce();
        if (!cancelled) timers.push(setTimeout(loop, every));
      };
      loop();
    });
    const tick = setInterval(() => update((s) => s), TICK_MS);
    return () => { cancelled = true; fetchers.current = {}; timers.forEach(clearTimeout); clearInterval(tick); };
  }, []);

  // 작업이 끝나면(완료·실패·취소) 주기를 기다리지 않고 자재 상태를 다시 조회한다 — 버튼이 서버 기록을 따라가게.
  const refresh = useCallback(() => Promise.all(['simDemo', 'simState'].map((key) => fetchers.current[key]?.())), []);

  const { health, config, robots, robotsFailing, simDemo, simDemoFailing, simState, conn, alerts, robotStatus, refreshedAt } = state;
  return { health, config, robots, robotsFailing, simDemo, simDemoFailing, simState, conn, alerts, robotStatus, refreshedAt, refresh };
}
