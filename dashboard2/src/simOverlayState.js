import { RESULT_LABELS } from './simCommand.js';

// 시뮬레이션 창 상태는 명령 흐름(useSimCommand)의 서버 값에서만 계산한다 — 화면이 지어내지 않는다.
//   danger   : /v1/sim-demo/command가 BLOCK으로 판정(확인 거부로 만든 BLOCK은 제외)
//   running  : 작업·목표가 실행 중
//   stopping : 실행 중 정지 요청이 서버에 접수됨(confirmed:false), 또는 그 뒤 작업이 정지 결과로 끝남(confirmed:true)
//   paused   : 창의 '일시정지'로 이 작업만 멈춤(정지 지점 남김) — '재개'는 서버가 재개 가능하다고 할 때만
// key는 "같은 상태인가"를 가리는 값이다 — 창을 닫으면 같은 key 동안은 다시 열지 않는다.
export function overlayOf(cmd) {
  const { job, goal, result, stopNote, pending, seq } = cmd;
  if (cmd.statusUnknown) return null;
  const stopAsked = !!stopNote && stopNote.tone === 'ok';
  const running = (job && job.status === 'running') || (goal && ['running', 'stopping'].includes(goal.status));
  if (running) return stopAsked ? { state: 'stopping', confirmed: false, key: `stopping:${seq}` } : { state: 'running', key: `running:${seq}` };
  // 일시정지(이 작업만 취소 → 실행기가 정지 지점을 남김). 재개 여부는 창이 서버 상태로 정한다.
  if (!stopAsked && job && job.paused) return { state: 'paused', key: `paused:${seq}` };
  // 일반 경로는 실행 최종 상태(job.general.state)가 서버 근거다 — 'stopped'만 정지 확인.
  if (stopAsked && job && job.general && job.general.state === 'stopped') return { state: 'stopping', confirmed: true, key: `stopped:${seq}`, resultLabel: job.general.label };
  const hit = job && !job.general && job.report && RESULT_LABELS[job.report.status];
  if (stopAsked && hit && /정지/.test(hit[1])) return { state: 'stopping', confirmed: true, key: `stopped:${seq}`, resultLabel: hit[1] };
  // 목표(여러 단계)는 서버가 최종 status 'stopped'로 끝낸다(server/sim_demo_goals.py FINAL).
  if (stopAsked && !job && goal && goal.status === 'stopped') return { state: 'stopping', confirmed: true, key: `stopped:${seq}`, resultLabel: '목표 정지' };
  if (result && result.decision === 'BLOCK' && !result.rejected && !job && !goal && !pending) return { state: 'danger', key: `danger:${seq}` };
  return null;
}
