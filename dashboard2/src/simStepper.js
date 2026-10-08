// 시뮬레이션 작업 진행 — 가로 스테퍼 상태(2026-10-08). **서버 값만** 쓴다(표시 전용).
//
// 근거
//   stage_plan : 실행기가 단계를 시작하기 전에 남긴 단계 목록({of, stages:[{no,label}]}) — 이름표일 뿐이다.
//   progress   : 단계가 끝날 때마다 실행기가 남긴 줄([{no,of,label,reached}]) — 단계 **완료**의 유일한 근거.
//   general    : 실행 최종 상태(planCommand) — '전체 완료'는 최종 상태가 completed일 때만.
// 지키는 것
//   - 진행 중인 단계를 완료로 치지 않는다. 연결 바는 양쪽 단계가 모두 완료일 때만 채운다(중간 진행률을 만들지 않는다).
//   - 마지막 단계 줄이 와도 최종 성공 확인 전에는 '완료'라고 하지 않는다.
//   - 단계 목록이 없으면 진행 줄의 단계 수(of)만큼 번호로 두고, 이름은 그 단계가 끝나 줄이 오면 채운다(추측하지 않는다).
//   - 둘 다 없으면 null('진행 단계 정보 없음').
//   - 통신과 단계 진행을 구분한다(2026-10-08 실기 시험 지시). 진행 조회가 **연속으로 실패**해야 '통신 확인 안 됨'(펄스 멈춤).
//     조회는 성공하는데 단계 줄이 오래 안 늘어나면 정상 동작일 수 있다 — 경고가 아니라 '이 단계 N초째'로만 알린다.
//     성공한 조회가 한동안 없지만 실패도 확인되지 않으면 '갱신 지연'(끊김으로 단정하지 않는다). 어느 경우도 완료로 바꾸지 않는다.

export const STALE_MS = 6000; // 조회 주기(1.5초)의 4배
const FAILS_FOR_LOST = 2;    // 연속 조회 실패 횟수

const PHASE_LABELS = {
  running: '진행 중', stopping: '즉시 정지 요청됨 · 확인 대기', stopped: '즉시 정지됨', paused: '일시정지됨',
  completed: '완료', failed: '실패', cancelled: '취소됨', unknown: '결과 확인 안 됨',
};

function phaseOf(job, stopAsked) {
  if (!job) return null;
  if (job.status === 'running') return stopAsked ? 'stopping' : 'running';
  if (job.paused) return 'paused';
  const state = job.general ? job.general.state : null;
  // 사용자 취소(이 실행만 중단)는 실패가 아니라 취소로 보인다 — 서버 중단 코드 그대로.
  if (job.general && /cancel/.test(String(job.general.code || ''))) return 'cancelled';
  if (state === 'completed') return 'completed';
  if (state === 'stopped') return 'stopped';
  if (state === 'canceled' || state === 'cancelled') return 'cancelled';
  if (state === 'failed' || state === 'aborted' || state === 'rejected') return 'failed';
  return 'unknown';
}

// 한 실행(작업 하나)의 단계 목록. done/failed는 진행 줄로만 정한다.
function stepsOfRun(run) {
  const plan = run && run.stage_plan && Array.isArray(run.stage_plan.stages) && run.stage_plan.stages.length ? run.stage_plan.stages : null;
  const rows = (run && Array.isArray(run.progress) ? run.progress : []).filter((r) => r && typeof r.no === 'number');
  const byNo = new Map(rows.map((r) => [r.no, r]));
  let base;
  if (plan) base = plan.map((p) => ({ no: p.no, label: p.label, named: true }));
  else if (rows.length && typeof rows[0].of === 'number' && rows[0].of > 0) {
    base = Array.from({ length: rows[0].of }, (_, i) => {
      const row = byNo.get(i + 1);
      return row ? { no: i + 1, label: row.label, named: true } : { no: i + 1, label: `단계 ${i + 1}`, named: false };
    });
  } else return null;
  return base.map((s) => {
    const row = byNo.get(s.no);
    return { ...s, status: row ? (row.reached ? 'done' : 'failed') : 'wait' };
  });
}

/**
 * cmd: 명령 흐름 값(job·stopNote·updatedAt·statusUnknown). 반복 작업처럼 cmd.job이 없으면 `run`(서버 조회한 작업)을 쓴다.
 * 돌려주는 값: { key, steps:[{no,label,named,status}], total, done, phase, phaseLabel, current, stale, resumed } | null
 *   status: done | current(펄스) | paused | halted(정지·실패·취소 지점) | failed(그 단계에서 도달 실패) | wait
 */
// 통신 상태: ok | failing(연속 조회 실패) | delayed(성공한 조회가 오래 없음, 실패는 미확인) | waiting(아직 조회 결과 없음)
export function commOf(comm, now) {
  if (!comm || (!comm.okAt && !comm.failAt)) return 'waiting';
  if ((comm.fails || 0) >= FAILS_FOR_LOST) return 'failing';
  if (comm.okAt && now - comm.okAt > STALE_MS) return 'delayed';
  return 'ok';
}

export function stepperOf({ job, stopNote, updatedAt, statusUnknown, comm, stageAt } = {}, { now = Date.now(), run = null, names = {} } = {}) {
  const src = job || run;
  if (!src) return null;
  const stopAsked = !!stopNote && stopNote.tone === 'ok';
  const phase = statusUnknown ? 'unknown' : phaseOf(src, stopAsked);
  // 재개: 같은 자재의 앞 실행에서 **확인된** 완료 단계 뒤에 재개 실행의 단계를 잇는다.
  const prior = src.resumedFrom ? (stepsOfRun(src.resumedFrom) || []).filter((s) => s.status === 'done') : [];
  const own = stepsOfRun(src);
  if (!own && !prior.length) return { key: src.execution_id || src.job_id || null, steps: null, phase, phaseLabel: PHASE_LABELS[phase] || '', current: currentText(src, phase, null, names), ...liveness(phase, comm, stageAt, updatedAt, now), resumed: !!src.resumed };
  // 재개 실행이 아직 단계 목록을 남기기 전(실행기 사전 검증 중, 2026-10-08 실기 시험에서 약 15초): 앞 단계만 보이면 '7/7 완료'처럼
  // 끝난 것으로 읽힌다 — 남은 단계는 추측하지 않고 '남은 단계 확인 중' 자리 하나를 둔다(전체 단계 수는 보이지 않는다).
  const waitingRest = prior.length > 0 && !own;
  const rest = waitingRest ? [{ no: 0, label: '남은 단계 확인 중', named: false, status: 'wait', placeholder: true }] : (own || []);
  const steps = [...prior.map((s) => ({ ...s, prior: true })), ...rest.map((s) => ({ ...s, resumePart: prior.length > 0 }))]
    .map((s, i) => ({ ...s, index: i + 1 }));
  const firstOpen = steps.findIndex((s) => s.status === 'wait');
  const anyFailed = steps.some((s) => s.status === 'failed');
  if (firstOpen >= 0 && !anyFailed) {
    const mark = { running: 'current', paused: 'paused', stopping: 'halted', stopped: 'halted', failed: 'halted', cancelled: 'halted', unknown: 'halted' }[phase];
    if (mark) steps[firstOpen] = { ...steps[firstOpen], status: mark };
  }
  const done = steps.filter((s) => s.status === 'done').length;
  // '현재' 문구는 멈춘·실패한 단계가 있으면 그 단계, 아니면 첫 미완료 단계를 가리킨다.
  const open = steps.find((s) => s.status === 'failed') || (firstOpen >= 0 ? steps[firstOpen] : null);
  return {
    key: src.execution_id || src.job_id || null, steps, total: waitingRest ? null : steps.length, done, phase, waitingRest,
    phaseLabel: PHASE_LABELS[phase] || '', current: currentText(src, phase, open, names), ...liveness(phase, comm, stageAt, updatedAt, now), resumed: !!src.resumed,
    allConfirmed: !waitingRest && done === steps.length,
  };
}

// 실행 중일 때만: 통신(link)과 단계 경과(stageSec). stale = 통신 실패 확인(펄스 멈춤). comm이 없으면(예전 호출) updatedAt만으로 '지연'.
function liveness(phase, comm, stageAt, updatedAt, now) {
  if (phase !== 'running') return { link: null, stale: false, stageSec: null };
  let link = comm !== undefined ? commOf(comm, now) : 'ok';
  if (comm === undefined && typeof updatedAt === 'number' && now - updatedAt > STALE_MS) link = 'delayed';
  const stageSec = typeof stageAt === 'number' ? Math.max(0, Math.floor((now - stageAt) / 1000)) : null;
  return { link, stale: link === 'failing', stageSec };
}

// '현재: A자재 이송 중 · 4/12 pick 접근' — 자재·동작 이름은 서버 작업 값(material·action_label), 단계는 위 목록.
function currentText(src, phase, open, names) {
  // 재개 실행의 첫 응답 전에는 자재·동작이 비어 있다 — 같은 자재를 멈춘 앞 실행 값을 쓴다(재개임을 밝힌다).
  const from = src.resumedFrom || {};
  const material = src.material || from.material;
  const action = src.action_label || (from.action_label ? `${from.action_label} 재개` : null);
  const what = [names[material] || material, action].filter(Boolean).join(' ');
  const verb = { running: '중', stopping: '정지 확인 대기', paused: '일시정지됨', stopped: '정지됨', failed: '실패', cancelled: '취소됨', completed: '완료', unknown: '결과 확인 안 됨' }[phase] || '';
  const head = what ? `${what} ${verb}` : PHASE_LABELS[phase] || '';
  if (!open || phase === 'completed') return head;
  if (open.placeholder) return `${head} · 재개 단계 목록을 기다리는 중(실행기 사전 확인)`;
  return `${head} · ${open.index}단계 ${open.named ? open.label : '(이름은 단계가 끝나면 표시)'}`;
}
