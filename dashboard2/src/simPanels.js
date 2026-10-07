// 시뮬레이션 보기의 '관절 상태'·'오류 안내' 계산(2026-10-07). 화면 컴포넌트와 분리한 순수 함수 — 서버 값만 쓴다.

// ── 관절 상태 ─────────────────────────────────────────────────────────────
const DEG = 180 / Math.PI;
// 소수 한 자리로 0이 되는 아주 작은 음수(-1e-17 rad 등)는 '-0.0°'가 아니라 '0.0°'.
export const fmtDeg = (rad) => { const t = (rad * DEG).toFixed(1); return `${t === '-0.0' ? '0.0' : t}°`; };
const valid = (v) => typeof v === 'number' && Number.isFinite(v);

export function jointRows(data) {
  const limits = (data && data.limits && data.limits.available && data.limits.joints) || {};
  const names = (data && data.armJoints && data.armJoints.length) ? data.armJoints : Object.keys(limits);
  const fresh = !!(data && data.joints && !data.stale && data.connected !== false);
  return names.slice(0, 6).map((name, i) => {
    const raw = fresh ? data.joints[name] : undefined;
    const lim = limits[name];
    const range = lim && valid(lim.lower) && valid(lim.upper) && lim.unit === 'rad' ? lim : null;
    const value = valid(raw) ? raw : null;
    return {
      key: name, label: `J${i + 1}`, value,
      text: value == null ? '—' : fmtDeg(value),
      range: range ? `${fmtDeg(range.lower)} ~ ${fmtDeg(range.upper)}` : '—',
      out: value != null && range != null && (value < range.lower || value > range.upper),
    };
  });
}

// ── 오류 안내 ─────────────────────────────────────────────────────────────
// 오류·안전 차단·확인 필요일 때만 안내를 만든다(정상이면 null). 원인은 **서버가 준 사유 그대로**, 없으면 '원인 확인 필요'.
// 조치는 서버 사유 코드로만 고른다 — 코드를 모르면 추측하지 않고 '원인 확인 필요'. 사용자 일시정지·취소·즉시 정지는 오류가 아니다.
export const UNKNOWN = '원인 확인 필요';
const ACTIONS = [
  [/^plan\.llm_/, '계획 모델 서버 상태를 확인해 주세요(관리자). 서버가 다시 붙어야 계획을 만들 수 있습니다'],
  [/^geometry\.(environment_unavailable|snapshot_expired)/, '잠시 뒤 다시 보내 주세요 — 계속되면 작업 셀(MoveIt) 상태 확인이 필요합니다'],
  [/^geometry\./, '목적지·자세를 바꾸거나 작업 셀 상태를 확인한 뒤 다시 보내 주세요'],
  [/^safety\./, '안전 규칙에 맞게 명령을 수정해 주세요'],
  [/^capability\./, '로봇이 할 수 있는 범위를 넘습니다 — 명령을 수정해 주세요'],
  [/^exec\.environment_changed/, '명령을 다시 보내 새로 검증·승인받아 주세요'],
  [/^exec\.unverifiable/, '자재 위치를 확인하고, 필요하면 복구한 뒤 다시 시도해 주세요'],
  [/^plan\.(clarification_required|slot_)/, '질문에 답하거나 자재·목적지를 분명히 말해 주세요'],
  [/^plan\.resource_mismatch/, '요청한 자재·목적지를 다시 확인해 주세요'],
];
export const actionFor = (code) => (code && (ACTIONS.find(([re]) => re.test(code)) || [])[1]) || UNKNOWN;
// 사용자 동작의 결과(오류 아님): 즉시 정지·일시정지·취소.
const USER_ENDED = /^exec\.(stopped|canceled|cancelled)$/;

/** cmd(명령 흐름)·server(주기 조회) → 안내 하나 또는 null. key가 같으면 같은 안내다(닫기 기억용). */
export function simAlertOf({ cmd, server }) {
  const seq = cmd && cmd.seq;
  const make = (source, tone, title, cause, code, detail) => ({
    source, tone, title, cause: cause || UNKNOWN, action: actionFor(code), code: code || null,
    detail: detail || cause || null, key: `${source}:${seq}:${code || ''}:${detail || cause || ''}`,
  });
  if (cmd) {
    if (cmd.error) return make('request', 'danger', '요청 실패', cmd.error, null, cmd.error);
    const r = cmd.result;
    if (r && r.decision === 'BLOCK') {
      const code = r.code || r.reason_code || null;
      return make('decision', 'danger', r.rejected ? '실행 거부' : '실행 차단', r.reason, code, r.reason);
    }
    if (r && r.decision === 'ASK') return make('decision', 'warn', '확인 필요', r.reason, r.code || r.reason_code || 'plan.clarification_required', r.reason);
    const job = cmd.job;
    const stopAsked = !!(cmd.stopNote && cmd.stopNote.tone === 'ok');
    if (job && job.status !== 'running' && !job.paused && !stopAsked) {
      const g = job.general;
      if (g && g.tone === 'danger' && !USER_ENDED.test(g.code || '') && g.state !== 'stopped') {
        return make('job', 'danger', g.label || '작업 실패', g.detail, g.code, g.detail);
      }
      if (!g && job.report && /failed|incomplete|unstable/.test(job.report.status || '')) {
        return make('job', 'danger', '작업 실패', job.report.detail || job.report.status, job.report.reason_code || null, job.report.status);
      }
    }
    if (cmd.pauseNote && cmd.pauseNote.tone === 'danger') return make('control', 'danger', '요청을 처리하지 못했습니다', cmd.pauseNote.text, null, cmd.pauseNote.text);
  }
  // 서버 상태(주기 조회) — 조회가 성공했다는 것만으로 지우지 않는다. 서버 값이 바뀌어야 사라진다.
  const sd = server && server.simDemo;
  if (sd && sd.recovery_required) {
    const rr = sd.recovery_required;
    return { ...make('recovery', 'danger', '작업 셀 복구 필요', rr.reason, 'recovery_required', rr.reason),
      action: '작업 상태를 확인하고 복구해 주세요(관리자)' };
  }
  const run = server && server.repeat && server.repeat.run;
  if (run && (run.state === 'failed' || (run.state === 'interrupted' && run.lock_held))) {
    return { ...make('repeat', 'danger', run.state === 'failed' ? '반복 작업 실패' : '반복 작업 중단', run.reason, null, run.reason),
      key: `repeat:${run.run_id}:${run.state}:${run.lock_held ? 1 : 0}`,
      action: run.lock_held ? '반복 작업의 ‘상태 확인 후 잠금 해제’로 상태를 확인해 주세요' : UNKNOWN };
  }
  return null;
}
