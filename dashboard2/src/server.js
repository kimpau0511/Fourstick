import { useEffect, useState } from 'react';

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
  healthOkAt: null, healthFailing: false, latencyMs: null, refreshedAt: null, nowMs: Date.now(),
  conn: { status: 'connecting', lastReceivedAt: null, latencyMs: null, ageSec: null },
  alerts: [], robotStatus: { level: 'NO_DATA', label: '', reasons: [] },
};

export const LEVEL_LABELS = {
  NORMAL: '정상', NOTICE: '작업 중', WARNING: '주의', CRITICAL: '긴급', NO_DATA: '데이터 없음',
};

function deriveConn(s) {
  const maxAge = s.config?.policies?.freshness?.environment_max_age_sec;
  const ageSec = s.healthOkAt == null ? null : (s.nowMs - s.healthOkAt) / 1000;
  const base = { lastReceivedAt: s.healthOkAt, latencyMs: s.latencyMs, ageSec };
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

export function useServer() {
  const [state, setState] = useState(INITIAL);

  useEffect(() => {
    let cancelled = false;
    const timers = [];
    const update = (fn) => { if (!cancelled) setState((s) => derive(fn({ ...s, nowMs: Date.now() }))); };

    SOURCES.forEach(([key, path, every]) => {
      const loop = async () => {
        const startedAt = Date.now();
        try {
          const res = await fetch(path, { cache: 'no-store' });
          if (!res.ok) throw new Error(String(res.status));
          const data = await res.json();
          const at = Date.now();
          update((s) => ({
            ...s, [key]: data, refreshedAt: at,
            ...(key === 'health' ? { healthOkAt: at, healthFailing: false, latencyMs: at - startedAt } : {}),
          }));
        } catch {
          // 실패한 값은 지우지 않는다(마지막 값 유지) — 얼마나 오래됐는지는 conn이 말한다.
          // 3D 관측은 서버가 못 줄 수도 있으므로(503) 값 없음으로 둔다.
          update((s) => ({ ...s, ...(key === 'health' ? { healthFailing: true } : {}), ...(key === 'simState' ? { simState: null } : {}) }));
        }
        if (!cancelled) timers.push(setTimeout(loop, every));
      };
      loop();
    });
    const tick = setInterval(() => update((s) => s), TICK_MS);
    return () => { cancelled = true; timers.forEach(clearTimeout); clearInterval(tick); };
  }, []);

  const { health, config, robots, simDemo, simState, conn, alerts, robotStatus, refreshedAt } = state;
  return { health, config, robots, simDemo, simState, conn, alerts, robotStatus, refreshedAt };
}
