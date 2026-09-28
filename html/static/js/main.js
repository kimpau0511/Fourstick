/** 배선. 백엔드(데이터) ↔ 상태 ↔ 화면을 잇는다.
 *
 * 흐름: 명령 입력 → 계획 생성 → 안전 판단(PASS/BLOCK/ASK) → PASS에서 사용자가
 * 실행 시작 → 실행 상태 표시 → 개별 실행 취소 또는 전체 정지.
 *
 * 시뮬레이션 자재 명령은 다른 길이다. 규칙이 명확하면 기존대로 바로 작업이 되고,
 * 모호한 자재 작업 발화는 서버의 Qwen 분류기를 지나 **확인 카드**로 온다.
 * 확인 카드의 버튼을 누른 뒤에만 `/v1/sim-demo/confirm`이 작업을 만든다.
 *
 * **자동 실행 경로가 없다.** 실행은 사용자가 버튼을 누른 뒤에만 시작된다.
 */

import { setResourceLabels } from './catalog.js';
import { HttpBackend } from './backend-http.js';
import { SimulationBackend } from './backend-sim.js';
import { render } from './render.js';
import { createHumanoidPanel } from './humanoid.js';
import { createSimDemoCard } from './sim-demo.js';
import { EXECUTION, createStore, makeEvent } from './state.js';
import { createSpeaker } from './tts.js';

const params = new URLSearchParams(location.search);

function nowTime() {
  return new Date().toLocaleTimeString('ko-KR', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

const store = createStore();
let backend = null;
let tickTimer = null;
/** 시연 카드(자재 수동 조작 + 상태 폴링). 서버에 붙었을 때만 만들어진다. */
let simDemoCard = null;
/** G1 휴머노이드 카드. 로봇 선택이 G1일 때만 보이고, 명령은 `/v1/humanoid/*`로만 간다. */
let humanoidPanel = null;
const G1_ID = 'unitree_g1';
const isG1 = () => store.get().robotId === G1_ID;
/** 음성 안내. **기본 꺼짐**이고, 못 쓰는 환경이면 모든 호출이 조용히 지나간다.
 *  읽는 문장은 `tts.js`가 열거값에서 만든다 — 여기서 문자열을 만들지 않는다. */
const speaker = createSpeaker({
  // 재생 단계(요청·시작·끝·오류)를 화면에 올린다. 소리가 안 났으면 오류로 보인다.
  onStatus: (status) => store.dispatch({ type: 'tts', tts: { status } }),
});

/** 음성 명령 단계 추적을 고친다(`state.stt.trace`). */
function traceVoice(fields) {
  const trace = store.get().stt.trace || {};
  store.dispatch({ type: 'stt', stt: { trace: { ...trace, ...fields } } });
}

/** STT 확정 이벤트의 오디오 진단(서버 계산, 오디오 없음) + 브라우저 형식 → 단계 추적 칸. */
function audioTrace(event) {
  const d = event.audio_diag;
  const c = event.client_info || {};
  const ts = c.track_settings || {};
  const fmt = c.context_sample_rate
    ? `${c.context_sample_rate} Hz 처리(마이크 ${ts.sampleRate ?? '?'} Hz · ${ts.channelCount ?? '?'}ch)`
      + ` · 에코제거 ${ts.echoCancellation} · 잡음억제 ${ts.noiseSuppression} · 자동이득 ${ts.autoGainControl}`
      + ` · 준비 ${c.ready_ms ?? '?'} ms · 열리기 전 버린 프레임 ${c.frames_dropped_before_open ?? '?'}`
    : null;
  if (!d || !d.frames) return { audioFormat: fmt };
  const level = `발화 ${d.rms_speech_median_dbfs ?? '—'} dBFS · 최대 ${d.peak_dbfs ?? '—'} dBFS`
    + ` · 잡음 ${d.noise_floor_dbfs ?? '—'} dBFS · SNR ${d.snr_db ?? '—'} dB · 잘림 ${d.clipped_samples}`;
  const onset = `음성 시작 ${d.vad_onset_s ?? '—'} s(녹음 ${d.audio_s} s)`
    + ` · 시작 0.2 s 음량 ${d.onset_0_2s_vs_median_db ?? '—'} dB(중앙값 대비)`
    + (d.speech_at_first_frame ? ' · 녹음 첫 프레임부터 음성' : '');
  return { audioFormat: fmt, audioLevel: level, audioOnset: onset, audioSuspect: d.suspect || [] };
}

/** 서버가 발화를 확정했다 — 마이크를 놓는다. 화면만 끄고 마이크를 열어 두면
 *  닫힌 세션에 오디오가 계속 흘러 stream_aborted가 난다(실측). */
function releaseMic() {
  if (backend && typeof backend.releaseVoice === 'function') {
    backend.releaseVoice().catch(() => {});
  }
}

function log(level, message) {
  store.dispatch({ type: 'event', event: makeEvent(level, message, nowTime()) });
}

function draw() {
  render(store.get(), backend ? backend.kind : 'simulation');
}

store.subscribe(draw);

// ── 백엔드 선택 ───────────────────────────────────────────────────────
async function pickBackend() {
  const wanted = params.get('backend');
  if (wanted === 'sim') return new SimulationBackend({ scenario: params.get('scenario') });
  try {
    const response = await fetch('/v1/config', { method: 'GET' });
    if (response.ok) return new HttpBackend();
  } catch {
    // 서버가 없다. 아래에서 시뮬레이션으로 내려간다.
  }
  if (wanted === 'http') throw new Error('서버에 연결할 수 없다');
  return new SimulationBackend({ scenario: params.get('scenario') });
}

// ── 백엔드 이벤트 ─────────────────────────────────────────────────────
function handleBackendEvent(event) {
  switch (event.kind) {
    case 'step-started':
      store.dispatch({ type: 'step-started', step: event.step });
      break;
    case 'step-result':
      store.dispatch({ type: 'step-result', result: event.result });
      break;
    case 'completed':
      store.dispatch({ type: 'execution-completed' });
      log('execution', '모든 단계가 완료되었습니다.');
      break;
    case 'failed':
      store.dispatch({ type: 'execution-failed' });
      log('error', `실행이 실패했습니다: ${event.reasonCode || ''}`);
      break;
    case 'cancel-result':
      store.dispatch({ type: 'cancel-result', record: event.record });
      log(
        'warning',
        `실행 취소 ${event.record.result === 'confirmed' ? '확인됨' : '미확인'}`
          + ` — scope: execution · ${event.record.reasonCode}`
          + ` · ${event.record.stoppedAtStep || '—'}단계`,
      );
      break;
    case 'stop-result':
      store.dispatch({ type: 'stop-result', record: event.record });
      speaker.speak({ kind: 'stop-result', result: event.record.result });
      log(
        'error',
        `전체 정지 ${event.record.result === 'confirmed' ? '확인됨' : '미확인'}`
          + ` — scope: global · ${event.record.reasonCode}`
          + ` · 영향 실행 ${event.record.affectedExecutionCount ?? 0}건`,
      );
      break;
    case 'connection':
      store.dispatch({ type: 'connection', state: event.state, detail: event.detail });
      break;
    case 'log':
      log(event.level, event.message);
      break;
    default:
      break;
  }
}

// ── 동작 ──────────────────────────────────────────────────────────────
/** 정지 키워드. **화면에 목록을 만들어 두지 않는다** — 서버 정책
 *  (`/v1/config`의 policies.stt.stop_keywords)에서 온다. 서버가 없으면 비어
 *  있고, 그때는 음성으로 정지를 걸지 않는다(추측하지 않는다). */
function stopKeywords() {
  const config = store.get().serverConfig;
  const stt = config && config.policies && config.policies.stt;
  return (stt && stt.stop_keywords) || [];
}

function isStopUtterance(text) {
  const normalized = (text || '').replace(/\s+/g, '');
  if (!normalized) return false;
  return stopKeywords().some((word) => normalized.includes(word.replace(/\s+/g, '')));
}

/** final transcript 하나당 계획 생성을 **정확히 한 번**만 요청한다. */
let lastHandledFinal = null;

async function handleFinalTranscript(text, confidence, rawText = text) {
  const key = `${text}|${confidence ?? ''}`;
  if (lastHandledFinal === key) {
    log('warning', '같은 최종 전사가 다시 도착했습니다 — 계획 생성을 다시 하지 않습니다.');
    return;
  }
  lastHandledFinal = key;
  // G1을 골랐으면 G1 경로로만 보낸다(FR3 시연·계획 생성으로 흘리지 않는다).
  if (isG1()) {
    if (humanoidPanel) await humanoidPanel.command(text, 'stt_final');
    else log('warning', 'G1 카드가 준비되지 않았습니다 — 명령을 보내지 않았습니다.');
    return;
  }
  // final만 시뮬레이션 명령으로 보낸다(partial은 표시만 한다).
  if (await sendSimDemoCommand(text, 'stt_final', { rawText, confidence })) return;
  traceVoice({ interpretation: '시연 작업 명령이 아님', outcome:
    '기존 계획 생성으로 보냄 — 안전 판단 뒤 실행 시작을 눌러야 실행됩니다', outcomeLevel: 'info' });
  // 정지 발화는 **계획 생성을 거치지 않는다.** 즉시 전체 정지로 보낸다.
  if (isStopUtterance(text)) {
    log('warning', `음성 정지 발화: "${text}" → 전체 정지`);
    await stopAll();
    return;
  }
  await generatePlan();
}

/** 시뮬레이션 명령. 텍스트와 STT final이 **같은 입구**를 쓴다.
 *
 * CONFIRM이면 입력 방식과 관계없이 확인 카드가 뜬다. 사용자가 누를 때만
 * 서버의 재검증·슬롯·기하 검사 뒤에 작업이 만들어진다.
 *
 * 돌려주는 값: 여기서 처리했으면 true. `PASS_THROUGH`(시뮬레이션 명령이 아님)나
 * 이 셀에서 쓸 수 없으면 false — 호출한 쪽이 기존 계획 생성으로 간다. */
async function sendSimDemoCommand(utterance, source, stt = {}) {
  const text = (utterance || '').trim();
  if (!text) return false;
  // 새 명령이다 — 읽던 안내를 즉시 끊는다.
  speaker.cancel();
  store.dispatch({ type: 'sim-demo-busy', busy: true });
  let result;
  try {
    result = await backend.simDemoCommand(text, source, stt);
  } catch (error) {
    store.dispatch({ type: 'sim-demo-busy', busy: false });
    if (source === 'stt_final') {
      // 음성 명령은 해석을 확인하지 못했으면 다른 경로로 넘기지 않는다.
      log('error', `음성 명령을 해석하지 못했습니다(${error.message}) — 실행하지 않았습니다. 다시 말해 주세요.`);
      traceVoice({ interpretation: `해석 요청 실패: ${error.message}`,
                   outcome: '실행하지 않음 — 다시 말해 주세요', outcomeLevel: 'error' });
      return true;
    }
    log('warning', `시뮬레이션 명령을 확인하지 못했습니다(${error.message}) — 계획 생성으로 갑니다.`);
    return false;
  }
  // 시뮬레이션 명령이 아니거나 이 셀에서 쓸 수 없다 → 기존 계획 생성이 맡는다.
  if (result.decision === 'PASS_THROUGH' || result.status === 403 || result.status === 404) {
    store.dispatch({ type: 'sim-demo-passed-through' });
    return false;
  }
  store.dispatch({ type: 'sim-demo-result', result });
  const level = result.decision === 'RUN' || result.decision === 'STOP'
    || result.decision === 'CONFIRM' || result.decision === 'CONFIRM_GOAL'
    || result.decision === 'ENVIRONMENT' || result.decision === 'NOOP' ? 'info' : 'warning';
  if (result.decision === 'ENVIRONMENT') refreshSimDemo();
  log(level, `시뮬레이션 명령(${source}) 판단 ${result.decision}`
    + `${result.reason ? ` — ${result.reason}` : ''}`);
  if (source === 'stt_final') {
    log('info',
      `명령 진단: STT="${result.raw_transcript || stt.rawText || text}"; 정규화="${result.normalized_transcript || text}"; 해석=${result.decision}/${result.intent || '없음'}/${result.material || '없음'}`);
    traceVoice(voiceOutcome(result));
  }
  if (result.decision === 'CONFIRM') {
    const pending = result.confirmation || {};
    // **여기서 작업이 만들어지지 않는다.** 확인 카드가 뜨고, 사용자가 누르면
    // 그때 /v1/sim-demo/confirm이 작업을 만든다.
    log('info', `해석 확인 대기: ${pending.summary || ''} — 확인을 눌러야 실행됩니다.`);
    // 읽는 말은 서버가 정한 **동작·자재**에서만 만든다(모델 원문이 아니다).
    speaker.speak({
      kind: 'confirm-pending',
      action: pending.action,
      material: (pending.evidence || {}).material_korean,
      slot: pending.slot || (pending.evidence || {}).slot,
    });
  } else if (result.decision === 'STOP') {
    speaker.speak({ kind: 'stop-requested', scope: 'sim' });
  } else if (result.decision === 'ASK' || result.decision === 'BLOCK') {
    speaker.speak({ kind: 'sim-decision', decision: result.decision,
                    heard: source === 'stt_final' ? result.raw_transcript : null });
  } else if (result.decision === 'RUN' && result.job) {
    // 규칙이 바로 알아들은 명령 — 확인 없이 작업이 시작된 경우다.
    speaker.speak({ kind: 'job-started', action: result.job.action,
                    material: materialKorean(result.job.material),
                    slot: result.job.slot || result.slot });
  }
  if (result.job) pollSimDemoJob(result.job.job_id);
  return true;
}

/** 서버 판단 → 단계 추적의 ④ 해석 · ⑤ 결과. 서버 값만 옮긴다. */
function voiceOutcome(result) {
  const pending = result.confirmation || {};
  const material = materialKorean(result.material) || result.material || '';
  const interpretation = pending.summary
    || [result.intent, material, result.slot_label].filter(Boolean).join(' · ')
    || '작업을 정하지 못함';
  const outcomes = {
    CONFIRM: ['확인 카드 표시 — 확인을 눌러야 실행됩니다', 'info'],
    CONFIRM_GOAL: ['확인 카드 표시 — 확인을 눌러야 실행됩니다', 'info'],
    STOP: ['정지 요청을 보냄(확인 없이 즉시)', 'info'],
    ASK: [`되묻기 — 실행하지 않음. ${result.reason || ''}`, 'warning'],
    BLOCK: [`차단 — 실행하지 않음. ${result.reason || ''}`, 'error'],
    RUN: ['실행 시작', 'info'],
  };
  const [outcome, outcomeLevel] = outcomes[result.decision]
    || [`${result.decision}${result.reason ? ` — ${result.reason}` : ''}`, 'info'];
  return { interpretation, outcome, outcomeLevel, corrections: result.stt_corrections || [] };
}

/** 자재 모델 id → 작업 셀이 선언한 한글 이름. 없으면 빈 문자열이다.
 *  **화면·소리가 이름을 만들어 내지 않는다** — 서버 상태에 있는 것만 쓴다. */
function materialKorean(model) {
  const status = store.get().simDemo.status;
  const row = ((status && status.materials) || []).find((m) => m.model === model);
  return (row && row.korean) || '';
}

/** 확인 카드의 버튼. `confirm`일 때만 작업 또는 고정 목표가 시작된다. */
async function answerSimDemoConfirm(action) {
  const pending = store.get().simDemo.confirmation;
  if (!pending) return;
  store.dispatch({ type: 'sim-demo-busy', busy: true });
  let result;
  try {
    result = pending.kind === 'goal'
      ? await backend.simDemoGoalConfirm(pending.goal_id, action)
      : await backend.simDemoConfirm(pending.token, action);
  } catch (error) {
    store.dispatch({ type: 'sim-demo-confirm-cleared' });
    log('error', `확인 요청 중 오류: ${error.message}`);
    return;
  }
  store.dispatch({ type: 'sim-demo-confirm-cleared' });
  store.dispatch({ type: 'sim-demo-result', result: { ...result, confirmation: null } });
  if (action === 'cancel') {
    log('warning', pending.kind === 'goal'
      ? `${pending.goal === 'arrange' ? '목표 배치' : '전체 복귀 목표'}를 취소했습니다 — 자재를 움직이지 않았습니다.`
      : '해석 확인을 취소했습니다 — 작업을 만들지 않았습니다.');
    speaker.speak({ kind: 'confirm-cancelled' });
    return;
  }
  if (pending.kind === 'goal' && result.status === 'running') {
    log('execution', `확인됨 — ${result.goal === 'arrange' ? '목표 배치' : '전체 복귀 목표'} 시작: ${result.goal_id}`);
    refreshSimDemo();
    return;
  }
  if (result.decision === 'RUN' && result.job) {
    log('execution', `확인됨 — 시뮬레이션 작업 생성: ${result.job.job_id}`);
    // 새 작업이다 — 읽던 안내를 끊고 시작만 알린다.
    speaker.speak({ kind: 'job-started', action: result.job.action,
                    material: materialKorean(result.job.material),
                    slot: result.job.slot || result.slot });
    pollSimDemoJob(result.job.job_id);
    refreshSimDemo();
    return;
  }
  log('error', `확인이 거부되었습니다 — ${result.reason || ''}`
    + ` (작업 없음${result.confirm_rejection ? `, ${result.confirm_rejection}` : ''})`);
  speaker.speak({ kind: 'sim-decision', decision: 'BLOCK' });
}

/** 시연 상태를 한 번 더 읽는다(자재 기록·체크포인트·실행 중 작업). */
function refreshSimDemo() {
  if (simDemoCard) simDemoCard.refresh();
}

/** 시연 정지. 헤더의 전체 정지와 다른 동작이다 — 시연 작업에만 보낸다. */
async function stopSimDemo() {
  if (!simDemoCard) return;
  // 정지다 — 읽던 안내를 먼저 끊는다.
  speaker.cancel();
  await simDemoCard.stop();
  log('warning', '시연 정지를 요청했습니다 — 시뮬레이터가 정지를 확인하면 체크포인트가 남습니다.');
  speaker.speak({ kind: 'stop-requested', scope: 'sim' });
}

/** 작업이 끝날 때까지 진행 단계를 읽는다. 로봇 명령을 보내지 않는다. */
async function pollSimDemoJob(jobId) {
  const job = await backend.simDemoJob(jobId);
  if (!job) return;
  store.dispatch({ type: 'sim-demo-job', job });
  if (job.status === 'running') {
    setTimeout(() => pollSimDemoJob(jobId), 1500);
    return;
  }
  const status = (job.report && job.report.status) || `종료 코드 ${job.exit_code}`;
  log('info', `시연 작업 완료: ${status}`);
  // 완료·실패·정지·resume 결과가 모두 여기로 온다. 읽는 말은 화면 배지와
  // 같은 표(`sim-demo.js`의 RESULT_LABELS)에서 나온다.
  speaker.speak({ kind: 'job-finished', status: (job.report || {}).status,
                  action: job.action, slot: job.slot });
}

async function generatePlan() {
  // 시뮬레이션 작업 셀에서는 자재 이송·복귀·정지·이어서가 **기본 동작**이다.
  // 시뮬레이션 명령이 아니면(PASS_THROUGH) 아래 기존 계획 생성으로 이어간다.
  if (isG1()) {
    const text = store.get().command.trim();
    if (!text) return;
    if (humanoidPanel) await humanoidPanel.command(text, 'text');
    else log('warning', 'G1 카드가 준비되지 않았습니다 — 명령을 보내지 않았습니다.');
    return;
  }
  if (await sendSimDemoCommand(store.get().command, 'text')) return;
  // **정지 발화는 계획 생성을 거치지 않는다.** 텍스트도 음성과 같은 규칙이다
  // (정지 계획은 안전 정책의 종료 스킬 요구와 충돌해 차단된다 — 실측).
  if (isStopUtterance(store.get().command)) {
    log('warning', `정지 발화: "${store.get().command}" → 전체 정지`);
    await stopAll();
    return;
  }
  const state = store.get();
  const utterance = state.command.trim();
  if (!utterance) return;
  store.dispatch({ type: 'plan-requested' });
  log('info', '계획 생성 요청을 전송했습니다.');
  try {
    const result = await backend.createPlan(utterance);
    if (!result.ok) {
      store.dispatch({
        type: 'plan-failed',
        block: {
          reasonCode: result.reasonCode || '',
          detail: result.detail || '',
          clarification: result.clarification || '',
          slots: result.slots || null,
          draftSteps: result.draftSteps || [],
          blocked: result.blocked || null,
          planValidation: result.planValidation || null,
          utterance,
        },
      });
      log('error', `계획을 만들지 못했습니다: ${result.reasonCode || ''} ${result.detail || ''}`);
      speaker.speak({ kind: 'plan-verdict', verdict: 'BLOCK' });
      return;
    }
    store.dispatch({
      type: 'plan-received',
      plan: result.plan,
      validation: result.validation,
      stopLatchCleared: result.stopLatchCleared,
    });
    const verdict = result.validation.verdict;
    log('info', `계획 생성 완료 — 안전 판단 ${verdict}`);
    if (verdict === 'PASS') {
      log('info', '실행 시작을 누르면 실행됩니다. 자동으로 실행되지 않습니다.');
    } else if (verdict === 'BLOCK') {
      log('error', `실행 차단 — ${(result.validation.reasonCodes || []).join(', ')}`);
    } else {
      log('warning', `정보 부족 — ${(result.validation.reasonCodes || []).join(', ')}`);
    }
    // PASS는 읽지 않는다 — 사용자가 실행 버튼을 누르는 자리다.
    speaker.speak({ kind: 'plan-verdict', verdict });
  } catch (error) {
    store.dispatch({
      type: 'plan-failed',
      block: { reasonCode: '', detail: error.message, utterance },
    });
    log('error', `계획 생성 중 오류: ${error.message}`);
  }
}

async function startExecution() {
  try {
    const result = await backend.startExecution();
    if (!result.ok) {
      log('error', `실행을 시작할 수 없습니다: ${result.reasonCode || ''} ${result.detail || ''}`);
      return;
    }
    store.dispatch({ type: 'execution-started', executionId: result.executionId, step: 1 });
    log('execution', `실행이 시작되었습니다. 실행 ID: ${result.executionId}`);
    // 서버 백엔드는 실행이 끝난 뒤 한 번에 응답한다. 결과를 반영한다.
    if (result.steps && result.steps.length) {
      result.steps.forEach((step) => {
        store.dispatch({
          type: 'step-result',
          result: {
            no: step.index,
            skill: step.skill,
            status: step.task_succeeded ? 'done' : 'failed',
            requestAccepted: step.request_accepted,
            motionDone: step.motion_completed,
            goalReached: step.target_reached,
            taskSuccess: step.task_succeeded,
            reasonCode: step.reason_code || '',
            retry: 0,
          },
        });
      });
      store.dispatch({ type: 'step-started', step: result.steps.length });
    }
    if (result.interrupted) {
      log('warning', `실행이 중단되었습니다: ${result.interrupted}`);
    } else if (result.final && result.final.task_succeeded) {
      store.dispatch({ type: 'execution-completed' });
    } else if (result.final && result.final.reason_code === 'exec.stopped'
               && result.final.verified) {
      // 계획이 정지로 끝났고 관측으로 확인됐다. **실패가 아니다.**
      store.dispatch({ type: 'execution-stop-confirmed' });
      log('warning', '정지가 관측으로 확인되었습니다.');
    } else if (result.final && result.final.terminal) {
      // 종료했지만 성공이 아니다. **계측으로 확인하지 못한 것을 성공으로
      // 바꾸지 않는다** — 이유를 그대로 보여준다.
      const reason = result.final.reason_code || '';
      if (result.final.task_succeeded === false && reason !== 'exec.unverifiable') {
        store.dispatch({ type: 'execution-failed' });
        log('error', `실행이 실패했습니다: ${reason}`);
      } else {
        store.dispatch({
          type: 'execution-unverified',
          reasonCode: reason,
          detail: result.final.detail || '결과를 계측으로 확인하지 못했습니다.',
        });
        log('warning', `실행 결과를 확인하지 못했습니다: ${reason}`);
      }
    }
  } catch (error) {
    log('error', `실행 요청 중 오류: ${error.message}`);
  }
}

async function cancelExecution() {
  const { execution } = store.get();
  if (!execution.id) return;
  store.dispatch({ type: 'cancel-requested' });
  log('warning', `실행 취소 요청 전송됨 (${execution.id}) — scope: execution`);
  try {
    const result = await backend.cancelExecution(execution.id);
    if (!result.ok && result.reasonCode) {
      log('error', `취소가 거절되었습니다: ${result.reasonCode} ${result.detail || ''}`);
    }
  } catch (error) {
    log('error', `취소 요청 중 오류: ${error.message}`);
  }
}

/** 로봇 선택에 맞춰 카드를 바꾼다. G1이면 G1 카드(3D 포함)를, FR3이면 FR3 작업 셀 화면을 보인다.
 *  두 로봇의 확인 카드·대화 맥락은 서로 다른 경로·저장소에 있다 — 여기서 옮기지 않는다. */
function applyRobotView(robotId) {
  const g1 = robotId === G1_ID;
  const sim3d = document.getElementById('card-sim3d');
  const scene = document.getElementById('card-scene');
  if (humanoidPanel) humanoidPanel.show(g1);
  if (sim3d) sim3d.hidden = g1;
  if (g1) {
    if (scene) scene.hidden = true;
  } else if (window.__simView) {
    window.__simView.setView(localStorageView());
  } else if (scene) {
    scene.hidden = false;
  }
}

function localStorageView() {
  try { return localStorage.getItem('forstick2.simView') || '3d'; } catch { return '3d'; }
}

async function stopAll() {
  // 정지가 가장 먼저다 — 읽던 안내를 즉시 끊는다.
  speaker.cancel();
  store.dispatch({ type: 'stop-requested' });
  log('error', '■ 전체 정지 요청 전송됨 — scope: global');
  speaker.speak({ kind: 'stop-requested', scope: 'global' });
  // 전체 정지는 G1에도 간다(확인 없이). G1은 목표만 취소하고 균형 제어는 유지한다.
  if (humanoidPanel) humanoidPanel.stop('전체 정지').catch(() => {});
  try {
    await backend.stopAll();
  } catch (error) {
    log('error', `전체 정지 요청 중 오류: ${error.message}`);
  }
}

/** 전체 정지 래치를 푼다.
 *
 * 래치는 지금까지 **새 계획이 수락될 때만** 풀렸다(`/v1/plan`). 시뮬레이션 작업
 * 화면은 계획을 만들지 않아서, 정지를 걸면 화면에서 풀 길이 없었다(실측).
 *
 * 화면이 스스로 풀지 않는다 — 서버가 `released: true`로 답했을 때만 상태를
 * 바꾼다. 진행 중인 실행이나 시뮬레이션 작업이 있으면 서버가 거부하고, 그
 * 사유를 그대로 적는다.
 */
async function releaseStop() {
  if (!backend || typeof backend.releaseStop !== 'function') {
    log('warning', '이 백엔드는 정지 해제를 제공하지 않습니다.');
    return;
  }
  let result;
  try {
    result = await backend.releaseStop();
  } catch (error) {
    log('error', `정지 해제 요청 중 오류: ${error.message}`);
    return;
  }
  if (!result.released) {
    log('warning', `정지 해제가 거부되었습니다 — ${result.detail || ''}`);
    return;
  }
  store.dispatch({ type: 'stop-released' });
  log('info', `정지 해제됨 — ${result.detail || ''}`);
}

async function toggleVoice() {
  const state = store.get();
  if (state.stt.recording) {
    await backend.stopVoice();
    store.dispatch({ type: 'stt', stt: { recording: false, partial: '' } });
    return;
  }
  store.dispatch({ type: 'stt', stt: { recording: true, partial: '', final: '', normalized: '', confidence: null,
    trace: { vad: '마이크 준비 중 — 아직 말하지 마세요' } } });
  const result = await backend.startVoice((event) => {
    if (event.kind === 'partial') {
      store.dispatch({ type: 'stt', stt: { partial: event.text || '' } });
    } else if (event.kind === 'final') {
      releaseMic();
      store.dispatch({ type: 'stt', stt: { trace: {
        vad: '말소리 감지 → 전사', vadOk: true, raw: event.raw_text || event.text || '',
        normalized: event.text || '', confidence: event.confidence ?? null,
        interpretation: '해석 중…', ...audioTrace(event) } } });
      store.dispatch({
        type: 'stt',
        stt: {
          recording: false,
          partial: '',
          final: event.raw_text || event.text || '',
          normalized: event.text || '',
          confidence: event.confidence ?? null,
        },
      });
      store.dispatch({ type: 'command', command: event.text || '' });
      log('info', `STT 원문: "${event.raw_text || event.text || ''}" → 정규화: "${event.text || ''}" (confidence ${event.confidence ?? '없음'})`);
      // **final만 계획 생성으로 보낸다.** partial은 표시만 한다.
      handleFinalTranscript(event.text || '', event.confidence ?? null,
        event.raw_text || event.text || '');
    } else if (event.kind === 'clarify') {
      releaseMic();
      store.dispatch({ type: 'stt', stt: { recording: false, partial: '', trace: {
        vad: '말소리 감지 → 전사', vadOk: true, raw: event.raw_text || event.text || '',
        normalized: event.text || '', confidence: event.confidence ?? null,
        interpretation: '해석하지 않음(전사 신뢰도가 기준 미만)',
        outcome: '되묻기 — 실행하지 않음. 다시 말해 주세요', outcomeLevel: 'warning', ...audioTrace(event) } } });
      log('warning', `되묻기: ${event.detail || '신뢰도가 낮습니다.'}`);
      speaker.speak({ kind: 'sim-decision', decision: 'ASK', heard: event.raw_text || event.text });
    } else if (event.kind === 'error') {
      releaseMic();
      const noSpeech = event.reason_code === 'stt.no_speech';
      store.dispatch({ type: 'stt', stt: { recording: false, trace: noSpeech
        ? { vad: `말소리를 감지하지 못했습니다 — ${event.detail || ''} 마이크 입력 장치·음량을 확인하세요`,
            vadOk: false, outcome: '실행하지 않음 — 다시 말해 주세요', outcomeLevel: 'warning', ...audioTrace(event) }
        : { vad: '—', outcome: `STT 오류 ${event.reason_code || ''} ${event.detail || ''} — 실행하지 않음`,
            outcomeLevel: 'error' } } });
      log(noSpeech ? 'warning' : 'error', `STT ${noSpeech ? '말소리 없음' : '오류'}: ${event.reason_code || ''} ${event.detail || ''}`);
      if (noSpeech) speaker.speak({ kind: 'sim-decision', decision: 'ASK' });
    }
  }, () => traceVoice({ vad: '듣는 중 — 지금 말하세요(말을 마치면 잠시 뒤 확정)' }));
  if (!result.ok) {
    store.dispatch({ type: 'stt', stt: { recording: false, detail: result.detail,
      trace: { vad: `마이크를 시작하지 못했습니다: ${result.detail || ''}`, vadOk: false } } });
    log('warning', `음성 입력을 쓸 수 없습니다: ${result.detail || ''}`);
  } else if (!store.get().stt.recording) {
    // 마이크가 켜지는 사이에 끄기를 눌렀다 — 막 열린 마이크를 바로 닫는다.
    await backend.stopVoice();
  }
}

// ── 입력 배선 ─────────────────────────────────────────────────────────
const ACTIONS = {
  'open-robot': () => store.dispatch({ type: 'modal', modal: 'robot' }),
  'close-modal': () => store.dispatch({ type: 'modal', modal: null }),
  'toggle-policy': () => store.dispatch({ type: 'policy-open', open: !store.get().policyOpen }),
  'toggle-voice': toggleVoice,
  generate: generatePlan,
  'clear-command': () => store.dispatch({ type: 'command', command: '' }),
  execute: () => store.dispatch({ type: 'modal', modal: 'execute' }),
  'execute-confirm': async () => {
    store.dispatch({ type: 'modal', modal: null });
    await startExecution();
  },
  cancel: () => store.dispatch({ type: 'modal', modal: 'cancel' }),
  'cancel-confirm': async () => {
    store.dispatch({ type: 'modal', modal: null });
    await cancelExecution();
  },
  'stop-all': stopAll,
  'sim-confirm': () => answerSimDemoConfirm('confirm'),
  'sim-cancel': () => answerSimDemoConfirm('cancel'),
  'sim-stop': stopSimDemo,
  'tts-heard': () => {
    speaker.confirmHeard(true);
    log('info', `TTS 실제 청취: 들림(사용자 확인) — "${speaker.status.text || ''}"`);
  },
  'tts-not-heard': () => {
    speaker.confirmHeard(false);
    const st = speaker.status;
    log('warning', `TTS 실제 청취: 안 들림(사용자 확인) — 브라우저 상태 ${st.phase}${st.error ? ` · ${st.error}` : ''}`
      + ` · 음성 ${st.voice || '없음'} · 재생 ${st.elapsed_ms ?? '?'} ms`);
  },
  'tts-test': () => {
    // 사용자가 누른 자리에서 재생한다(브라우저 재생 허가). 결과는 TTS 상태 줄에 뜬다.
    if (!speaker.test()) log('warning', `음성 시험을 재생하지 못했습니다: ${speaker.status.error || '지원하지 않는 브라우저'}`);
  },
  'toggle-tts': () => {
    const on = speaker.toggle();
    store.dispatch({ type: 'tts', tts: { enabled: on } });
    log('info', `음성 안내를 ${on ? '켰습니다' : '껐습니다'}.`);
    // 켜는 순간(사용자 조작 안)에 시험 문장을 읽어 재생 허가를 얻고, 들리는지 바로 확인한다.
    if (on) speaker.test();
  },
  'dismiss-stop': () => store.dispatch({ type: 'stop-dismissed' }),
  'release-stop': releaseStop,
  'goto-workspace': () => store.dispatch({ type: 'view', view: 'workspace' }),
  'back-to-plan': () => store.dispatch({ type: 'view', view: 'plan' }),
};

document.addEventListener('click', (clickEvent) => {
  const target = clickEvent.target.closest('[data-action]');
  if (!target) return;
  const action = target.dataset.action;
  if (action === 'modal-backdrop') {
    if (clickEvent.target === target) store.dispatch({ type: 'modal', modal: null });
    return;
  }
  if (action === 'event-filter') {
    store.dispatch({ type: 'event-filter', filter: target.dataset.filter });
    return;
  }
  if (action === 'select-robot') {
    const robotId = target.dataset.robot;
    store.dispatch({ type: 'robot', robotId });
    store.dispatch({ type: 'modal', modal: null });
    applyRobotView(robotId);
    log('info', `로봇 선택: ${robotId}`);
    return;
  }
  if (action === 'command-input') return;
  const handler = ACTIONS[action];
  if (handler) handler();
});

document.addEventListener('input', (inputEvent) => {
  const target = inputEvent.target;
  if (target && target.id === 'command-input') {
    store.dispatch({ type: 'command', command: target.value });
  }
});

document.addEventListener('keydown', (keyEvent) => {
  if (keyEvent.key === 'Escape' && store.get().modal) {
    store.dispatch({ type: 'modal', modal: null });
  }
  if (keyEvent.key !== 'Enter') return;
  // Ctrl+Enter는 어디서나 명령을 보낸다(목업의 버튼과 같은 동작이다).
  if (keyEvent.ctrlKey || keyEvent.metaKey) {
    generatePlan();
    return;
  }
  // 명령칸에서는 **그냥 Enter로도 보낸다.** 예전에는 Ctrl+Enter나 단추만
  // 받아서, 치고 Enter를 누르면 줄만 바뀌고 "입력 중"에 머물렀다(실측).
  // 줄바꿈은 Shift+Enter다.
  const target = keyEvent.target;
  if (!target || target.id !== 'command-input' || keyEvent.shiftKey) return;
  // **한글 조합 중의 Enter는 글자를 확정하는 입력이다.** 그것으로 보내면
  // 마지막 글자가 잘린 채 나간다. 조합이 끝난 Enter만 받는다.
  if (keyEvent.isComposing || keyEvent.keyCode === 229) return;
  keyEvent.preventDefault();
  generatePlan();
});

// ── 작업 셀 장면 스트림 ───────────────────────────────────────────────
/** 장면 프레임을 WebSocket으로 받아 canvas에 그린다.
 *
 * 폴링하지 않는다. 서버가 새 프레임마다 바이너리를 밀어 보낸다. 원본 RGB를
 * 그대로 받아 `putImageData`로 그리므로 이미지 인코딩·디코딩이 없다.
 * 끊기면 다시 붙되, **프레임을 만들어 그리지 않는다.**
 */
let sceneSocket = null;
let sceneGeometry = null;
let sceneFrameCount = 0;
let sceneFpsTimer = null;

function drawSceneFrame(buffer) {
  const canvas = document.getElementById('scene-canvas');
  if (!canvas || !sceneGeometry) return;
  const { width, height, channels } = sceneGeometry;
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const source = new Uint8Array(buffer);
  if (source.length !== width * height * channels) return;
  const context = canvas.getContext('2d');
  const image = context.createImageData(width, height);
  const target = image.data;
  if (channels === 4) {
    target.set(source);
  } else {
    for (let i = 0, j = 0; i < source.length; i += 3, j += 4) {
      target[j] = source[i];
      target[j + 1] = source[i + 1];
      target[j + 2] = source[i + 2];
      target[j + 3] = 255;
    }
  }
  context.putImageData(image, 0, 0);
  sceneFrameCount += 1;
}

function connectSceneStream() {
  if (sceneSocket) return;
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${scheme}://${window.location.host}/v1/scene/stream`);
  socket.binaryType = 'arraybuffer';
  sceneSocket = socket;

  socket.onmessage = (event) => {
    if (typeof event.data === 'string') {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      if (payload.type === 'scene_header') {
        sceneGeometry = {
          width: payload.width,
          height: payload.height,
          channels: payload.channels,
        };
        store.dispatch({
          type: 'scene-stream', state: 'open', detail: payload.detail || '',
        });
      } else if (payload.type === 'scene_stalled') {
        store.dispatch({
          type: 'scene-stream', state: 'stalled', detail: payload.detail || '',
        });
      } else if (payload.type === 'scene_unavailable') {
        store.dispatch({
          type: 'scene-available', available: false,
          detail: payload.detail || '장면을 쓸 수 없습니다.',
        });
      }
      return;
    }
    drawSceneFrame(event.data);
  };
  socket.onclose = () => {
    sceneSocket = null;
    store.dispatch({ type: 'scene-stream', state: 'closed' });
    // 서버가 다시 뜰 수 있다. 천천히 다시 붙는다.
    setTimeout(connectSceneStream, 3000);
  };
  socket.onerror = () => {
    store.dispatch({
      type: 'scene-stream', state: 'closed', detail: '스트림 연결 오류',
    });
  };
  if (sceneFpsTimer === null) {
    sceneFpsTimer = setInterval(() => {
      store.dispatch({ type: 'scene-fps', fps: sceneFrameCount });
      sceneFrameCount = 0;
    }, 1000);
  }
}

// ── 시작 ──────────────────────────────────────────────────────────────
async function boot() {
  // 음성 안내의 **저장된 켬/끔**을 화면에 올린다. 기본은 꺼짐이고, 브라우저가
  // SpeechSynthesis를 갖고 있지 않으면 토글이 잠긴 채로 그려진다.
  store.dispatch({
    type: 'tts',
    tts: { supported: speaker.supported, enabled: speaker.enabled },
  });
  draw();
  try {
    backend = await pickBackend();
  } catch (error) {
    store.dispatch({ type: 'connection', state: 'lost', detail: error.message });
    log('error', `서버에 연결할 수 없습니다: ${error.message}`);
    return;
  }
  backend.onEvent(handleBackendEvent);
  try {
    const { session, config, sttAvailable } = await backend.connect();
    store.dispatch({ type: 'session', session });
    store.dispatch({ type: 'server-robot', robot: (config && config.robot) || null });
    store.dispatch({
      type: 'server-config',
      config: backend.kind === 'server' ? config : null,
    });
    const catalogs = (config && config.catalogs) || {};
    setResourceLabels([...(catalogs.locations || []), ...(catalogs.objects || [])]);
    store.dispatch({ type: 'connection', state: 'ok', detail: backend.label });
    // 마이크 사용 여부는 **연결 결과가 정해지는 이 자리에서** 화면에 싣는다.
    // 아래 단계(시연 카드·장면 영상·장면 스트림) 중 하나가 실패해 catch로
    // 빠지면, 여기서 싣지 않은 값은 초기값 false로 남고 **새로고침 전까지
    // 마이크 단추가 잠긴 채 복구되지 않는다**(실측). 서버가 STT를 쓸 수
    // 있다고 답한 사실은 그 실패들과 무관하다.
    store.dispatch({ type: 'stt', stt: { available: sttAvailable } });
    // G1 카드. 서버에 붙었을 때만 만든다. 로봇 선택이 G1일 때만 보인다.
    const humanoidRoot = document.getElementById('card-humanoid');
    if (humanoidRoot && backend.kind === 'server') {
      humanoidPanel = createHumanoidPanel({
        root: humanoidRoot, getSessionId: () => backend.sessionId, log,
      });
      applyRobotView(store.get().robotId);
      window.__humanoid = humanoidPanel;             // 검증용(읽기)
    }
    // 시뮬레이션 시연 카드. 서버에 붙었을 때만 서버 상태를 읽는다 — 자기 카드만
    // 그리고, 다른 카드의 상태 저장소를 건드리지 않는다.
    const simDemoRoot = document.getElementById('card-sim-demo');
    if (simDemoRoot) {
      if (backend.kind === 'server') {
        simDemoCard = createSimDemoCard({
          root: simDemoRoot,
          // 읽은 상태를 목업 칸(가제보 화면 아래 자재 띠 · 안전 판단 및 실행)에도
          // 그대로 싣는다. 카드가 상태를 **고치지 않는다.**
          onStatus: ({ status, job }) => {
            store.dispatch({ type: 'sim-demo-status', status, job });
            const pending = status && status.pending_confirmation;
            const current = store.get().simDemo.confirmation;
            const pendingId = pending && (pending.token || pending.goal_id);
            const currentId = current && (current.token || current.goal_id);
            // 새로고침·다른 탭 복원: 서버가 들고 있는 확인 대기를 화면에 세운다.
            if (pending && (!current || currentId !== pendingId)) {
              store.dispatch({ type: 'sim-demo-confirmation', confirmation: pending });
            } else if (!pending && current) {
              store.dispatch({ type: 'sim-demo-confirm-cleared' });
            }
          },
        });
        simDemoCard.refresh();
      } else {
        simDemoRoot.hidden = true;
      }
    }
    // 작업 셀 장면 영상을 쓸 수 있는지 확인한다. 추측하지 않는다.
    try {
      const scene = await fetch('/v1/scene', { cache: 'no-store' });
      const info = await scene.json();
      store.dispatch({
        type: 'scene-available',
        available: Boolean(info.available),
        detail: info.detail || '',
      });
    } catch (error) {
      store.dispatch({
        type: 'scene-available', available: false, detail: error.message,
      });
    }
    // 장면 스트림을 붙인다(서버가 프레임을 밀어 보낸다).
    connectSceneStream();
    log('info', `시스템이 초기화되었습니다. (${backend.label})`);
    if (backend.kind === 'simulation') {
      log('warning', '시뮬레이션 모드입니다. 로봇은 움직이지 않습니다.');
    }
  } catch (error) {
    store.dispatch({ type: 'connection', state: 'lost', detail: error.message });
    log('error', `초기화 실패: ${error.message}`);
    return;
  }

  tickTimer = setInterval(() => store.dispatch({ type: 'tick' }), 1000);
  window.addEventListener('beforeunload', () => {
    if (tickTimer) clearInterval(tickTimer);
  });

  // 새로고침 복원: 자기 세션의 진행 상태만 읽는다.
  try {
    const restored = await backend.restore();
    if (restored && restored.executions && restored.executions.length) {
      const running = restored.running_execution_id;
      if (running) {
        log('info', `진행 중인 실행을 복원했습니다: ${running}`);
        store.dispatch({ type: 'execution-started', executionId: running, step: 1 });
      }
    }
  } catch {
    // 복원 실패는 흐름을 막지 않는다.
  }
}

// 헤더의 전체 정지 버튼은 상태와 무관하게 항상 동작한다.
const estop = document.getElementById('btn-estop');
if (estop) estop.addEventListener('click', stopAll);

boot();

// 테스트·디버깅용 접근 통로. 화면 코드가 여기 값을 읽지 않는다.
window.forstick = { store, get backend() { return backend; }, EXECUTION, speaker };

// 작업 셀 3D 화면(읽기 전용 관측). 따로 불러온다 — 실패해도 기존 화면은 그대로이고
// sim-view.js가 없으면 Gazebo 영상 카드만 보인다.
import('./sim-view.js').catch((error) => {
  console.warn('3D 작업 셀 화면을 불러오지 못했다:', error);
});
