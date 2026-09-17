/** 배선. 백엔드(데이터) ↔ 상태 ↔ 화면을 잇는다.
 *
 * 흐름: 명령 입력 → 계획 생성 → 안전 판단(PASS/BLOCK/ASK) → PASS에서 사용자가
 * 실행 시작 → 실행 상태 표시 → 개별 실행 취소 또는 전체 정지.
 *
 * **자동 실행 경로가 없다.** 실행은 사용자가 버튼을 누른 뒤에만 시작된다.
 */

import { setResourceLabels } from './catalog.js';
import { HttpBackend } from './backend-http.js';
import { SimulationBackend } from './backend-sim.js';
import { render } from './render.js';
import { EXECUTION, createStore, makeEvent } from './state.js';

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

async function handleFinalTranscript(text, confidence) {
  const key = `${text}|${confidence ?? ''}`;
  if (lastHandledFinal === key) {
    log('warning', '같은 최종 전사가 다시 도착했습니다 — 계획 생성을 다시 하지 않습니다.');
    return;
  }
  lastHandledFinal = key;
  // 정지 발화는 **계획 생성을 거치지 않는다.** 즉시 전체 정지로 보낸다.
  if (isStopUtterance(text)) {
    log('warning', `음성 정지 발화: "${text}" → 전체 정지`);
    await stopAll();
    return;
  }
  await generatePlan();
}

async function generatePlan() {
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

async function stopAll() {
  store.dispatch({ type: 'stop-requested' });
  log('error', '■ 전체 정지 요청 전송됨 — scope: global');
  try {
    await backend.stopAll();
  } catch (error) {
    log('error', `전체 정지 요청 중 오류: ${error.message}`);
  }
}

async function toggleVoice() {
  const state = store.get();
  if (state.stt.recording) {
    await backend.stopVoice();
    store.dispatch({ type: 'stt', stt: { recording: false, partial: '' } });
    return;
  }
  store.dispatch({ type: 'stt', stt: { recording: true, partial: '', final: '', confidence: null } });
  const result = await backend.startVoice((event) => {
    if (event.kind === 'partial') {
      store.dispatch({ type: 'stt', stt: { partial: event.text || '' } });
    } else if (event.kind === 'final') {
      store.dispatch({
        type: 'stt',
        stt: {
          recording: false,
          partial: '',
          final: event.text || '',
          confidence: event.confidence ?? null,
        },
      });
      store.dispatch({ type: 'command', command: event.text || '' });
      log('info', `음성 전사 완료: "${event.text || ''}"`);
      // **final만 계획 생성으로 보낸다.** partial은 표시만 한다.
      handleFinalTranscript(event.text || '', event.confidence ?? null);
    } else if (event.kind === 'clarify') {
      store.dispatch({ type: 'stt', stt: { recording: false, partial: '' } });
      log('warning', `되묻기: ${event.detail || '신뢰도가 낮습니다.'}`);
    } else if (event.kind === 'error') {
      store.dispatch({ type: 'stt', stt: { recording: false } });
      log('error', `STT 오류: ${event.reason_code || ''} ${event.detail || ''}`);
    }
  });
  if (!result.ok) {
    store.dispatch({ type: 'stt', stt: { recording: false, detail: result.detail } });
    log('warning', `음성 입력을 쓸 수 없습니다: ${result.detail || ''}`);
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
  'dismiss-stop': () => store.dispatch({ type: 'stop-dismissed' }),
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
  // Ctrl+Enter로 계획 생성(목업의 버튼과 같은 동작이다).
  if (keyEvent.key === 'Enter' && (keyEvent.ctrlKey || keyEvent.metaKey)) {
    generatePlan();
  }
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
    store.dispatch({ type: 'stt', stt: { available: sttAvailable } });
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
window.forstick = { store, get backend() { return backend; }, EXECUTION };
