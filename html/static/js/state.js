/** 화면 상태 (순수 함수).
 *
 * **DOM도 네트워크도 모른다.** 그래서 브라우저 없이 node로 테스트한다
 * (`tests/web/state.test.mjs`).
 *
 * 화면 정책 (사용자 요구):
 *  - 작업자 승인 단계를 화면에 두지 않는다. 안전 판단은 PASS/BLOCK/ASK만이다.
 *  - **PASS일 때만** 사용자가 실행을 시작할 수 있다. 자동 실행은 없다.
 *  - 전체 정지(global)와 개별 실행 취소(execution)는 서로 다른 동작이고
 *    결과도 따로 남는다.
 */

/** 실행 상태 값. `cancelled`는 개별 취소, `global_stopped`는 전체 정지다. */
export const EXECUTION = {
  IDLE: 'idle',
  RUNNING: 'running',
  CANCELLING: 'cancelling',
  CANCELLED: 'cancelled',
  STOPPING: 'stopping',
  GLOBAL_STOPPED: 'global_stopped',
  COMPLETED: 'completed',
  FAILED: 'failed',
  // 모션은 끝났지만 결과를 계측으로 확인하지 못했다.
  // **성공으로 바꾸지 않는다** — 화면이 그 사실을 그대로 보여준다.
  UNVERIFIED: 'unverified',
  // 계획이 정지로 끝났고 **관측으로 정지가 확인됐다.**
  // 실패가 아니다 — 정지가 목표였다.
  STOP_CONFIRMED: 'stop_confirmed',
};

/** 안전 판단 값. 이 세 개만 화면에 나온다. */
export const VERDICT = { PASS: 'PASS', BLOCK: 'BLOCK', ASK: 'ASK' };

/** 서버 판정(allow/block/ask)을 화면 값으로 바꾼다. */
export function verdictFromDecision(decision) {
  if (decision === 'allow') return VERDICT.PASS;
  if (decision === 'block') return VERDICT.BLOCK;
  if (decision === 'ask') return VERDICT.ASK;
  return null;
}

export function initialState() {
  return {
    view: 'plan',
    command: '',
    planLoading: false,
    /** {requestId, planId, planHash, createdAt, ttlSec, steps:[{no,skill,description,args}], utterance, resources:[]} */
    plan: null,
    /** {verdict, reasonCodes:[], detail, rules:[], blocked:[], missing:[], fixes:[]} */
    validation: null,
    execution: {
      status: EXECUTION.IDLE,
      id: null,
      currentStep: 0,
      elapsedSec: 0,
      results: [],
    },
    /** 개별 실행 취소 결과. 전체 정지와 섞지 않는다. */
    cancelRecord: null,
    /** 전체 정지 결과. */
    stopRecord: null,
    /** 전체 정지 래치. 새 계획을 수락할 때까지 실행을 막는다. */
    stopLatched: false,
    events: [],
    robotId: 'fairino_fr3',
    /** 서버에 실제로 등록된 실행 어댑터. 선언된 구성과 다를 수 있다. */
    serverRobot: null,
    // 시뮬레이션 작업 셀 연결 상태(서버 /v1/config의 robot.workcell).
    // 붙지 않았으면 null이거나 registered=false다 — 추측하지 않는다.
    serverWorkcell: null,
    // 실기 전환 준비 상태(8-12). **읽기 전용이다.**
    hardwareReadiness: null,
    // Gazebo 이송 시연 요약(8-11). 실기 준비와 다른 축이다.
    simulationE2e: null,
    /** 작업 셀 장면 영상 갱신 카운터. tick마다 올라간다. */
    sceneSeq: 0,
    /** 장면 스트림 상태: 'idle' | 'open' | 'stalled' | 'closed'. */
    sceneStream: 'idle',
    sceneStreamDetail: '',
    /** 최근 1초 동안 받은 프레임 수. 관측값이다. */
    sceneFps: 0,
    /** 장면 영상을 쓸 수 있는가(서버 /v1/scene). null이면 모름. */
    sceneAvailable: null,
    /** 차단된 계획 요청의 근거(자원·초안·이유 코드·부족한 입력). */
    planBlock: null,
    /** 서버 설정(`/v1/config`). 정책·카탈로그 수치의 출처다. */
    serverConfig: null,
    session: null,
    connection: { state: 'connecting', detail: '' },
    stt: { recording: false, partial: '', final: '', confidence: null, available: false },
    modal: null, // 'execute' | 'cancel' | 'robot' | null
    eventFilter: 'all',
    policyOpen: false,
    lastError: null,
  };
}

let eventSeq = 0;

export function makeEvent(level, message, time) {
  eventSeq += 1;
  return { id: eventSeq, level, message, time };
}

/** 실행 시작 버튼을 누를 수 있는가. **PASS가 아니면 절대 참이 아니다.** */
export function canExecute(state) {
  if (!state.plan || !state.validation) return false;
  if (state.validation.verdict !== VERDICT.PASS) return false;
  if (state.execution.status !== EXECUTION.IDLE) return false;
  if (state.stopLatched) return false;
  return true;
}

/** 개별 취소 버튼을 누를 수 있는가. */
export function canCancel(state) {
  return state.execution.status === EXECUTION.RUNNING && Boolean(state.execution.id);
}

/** 실행이 진행 중인가(취소 요청 중 포함). */
export function isActive(state) {
  return (
    state.execution.status === EXECUTION.RUNNING ||
    state.execution.status === EXECUTION.CANCELLING ||
    state.execution.status === EXECUTION.STOPPING
  );
}

export function isStopped(state) {
  return (
    state.execution.status === EXECUTION.CANCELLED ||
    state.execution.status === EXECUTION.STOP_CONFIRMED ||
    state.execution.status === EXECUTION.GLOBAL_STOPPED
  );
}

export function progressPercent(state, totalSteps) {
  const { status, currentStep } = state.execution;
  if (status === EXECUTION.COMPLETED || status === EXECUTION.UNVERIFIED
      || status === EXECUTION.STOP_CONFIRMED) return 100;
  if (status === EXECUTION.IDLE || !totalSteps) return 0;
  return Math.round((Math.max(0, currentStep - 1) / totalSteps) * 100);
}

/** 상태 전이. 모든 변화는 여기를 지난다 — 화면 코드가 상태를 직접 고치지 않는다. */
export function reduce(state, action) {
  switch (action.type) {
    case 'view':
      return { ...state, view: action.view };

    case 'command':
      return { ...state, command: action.command };

    case 'modal':
      return { ...state, modal: action.modal };

    case 'event-filter':
      return { ...state, eventFilter: action.filter };

    case 'policy-open':
      return { ...state, policyOpen: action.open };

    case 'session':
      return { ...state, session: action.session };

    case 'server-robot':
      return {
        ...state,
        serverRobot: action.robot,
        serverWorkcell: (action.robot && action.robot.workcell) || null,
        // Gazebo 시연 상태와 **다른 축**이다. 하나로 합치지 않는다.
        hardwareReadiness:
          (action.robot && action.robot.hardware_readiness) || null,
        simulationE2e: (action.robot && action.robot.simulation_e2e) || null,
      };

    case 'server-config':
      return { ...state, serverConfig: action.config };

    case 'scene-available':
      return { ...state, sceneAvailable: action.available, sceneDetail: action.detail || '' };

    case 'scene-stream':
      return {
        ...state,
        sceneStream: action.state,
        sceneStreamDetail: action.detail === undefined
          ? state.sceneStreamDetail : (action.detail || ''),
        sceneAvailable: action.state === 'open' ? true : state.sceneAvailable,
      };

    case 'scene-fps':
      return { ...state, sceneFps: action.fps };

    case 'connection':
      return { ...state, connection: { state: action.state, detail: action.detail || '' } };

    case 'stt':
      return { ...state, stt: { ...state.stt, ...action.stt } };

    case 'robot':
      // 실행 중에는 로봇을 바꾸지 않는다.
      if (isActive(state)) return state;
      return {
        ...state,
        robotId: action.robotId,
        plan: null,
        validation: null,
        execution: initialState().execution,
        cancelRecord: null,
      };

    case 'event':
      return { ...state, events: [...state.events, action.event] };

    case 'error':
      return { ...state, lastError: action.error, planLoading: false };

    case 'plan-requested':
      return {
        ...state,
        planLoading: true,
        plan: null,
        validation: null,
        cancelRecord: null,
        execution: initialState().execution,
        lastError: null,
      };

    case 'plan-received':
      return {
        ...state,
        planBlock: null,
        planLoading: false,
        plan: action.plan,
        validation: action.validation,
        // 새 계획이 수락되면 전체 정지 래치가 풀린다(서버 계약과 같은 지점).
        stopLatched: action.stopLatchCleared === true ? false : state.stopLatched,
        stopRecord: action.stopLatchCleared === true ? null : state.stopRecord,
      };

    case 'plan-failed':
      return {
        ...state,
        planLoading: false,
        plan: null,
        validation: null,
        // 차단된 요청의 근거. 화면이 발화 자원·계획 초안·Reason Code·부족한
        // 입력을 함께 보여줄 수 있어야 한다.
        planBlock: action.block || null,
      };

    case 'execution-started':
      return {
        ...state,
        view: 'workspace',
        cancelRecord: null,
        execution: {
          status: EXECUTION.RUNNING,
          id: action.executionId,
          currentStep: action.step || 1,
          elapsedSec: 0,
          results: [],
        },
      };

    case 'tick':
      // 장면 영상 갱신은 실행 중이 아닐 때도 돌아야 한다.
      if (!isActive(state)) return { ...state, sceneSeq: state.sceneSeq + 1 };
      return {
        ...state,
        sceneSeq: state.sceneSeq + 1,
        execution: { ...state.execution, elapsedSec: state.execution.elapsedSec + 1 },
      };

    case 'step-started':
      return {
        ...state,
        execution: { ...state.execution, currentStep: action.step },
      };

    case 'step-result': {
      const results = state.execution.results.filter((r) => r.no !== action.result.no);
      return {
        ...state,
        execution: {
          ...state.execution,
          results: [...results, action.result].sort((a, b) => a.no - b.no),
        },
      };
    }

    case 'execution-completed':
      return {
        ...state,
        execution: { ...state.execution, status: EXECUTION.COMPLETED },
      };

    case 'execution-failed':
      return {
        ...state,
        execution: { ...state.execution, status: EXECUTION.FAILED },
      };

    case 'execution-stop-confirmed':
      return {
        ...state,
        execution: { ...state.execution, status: EXECUTION.STOP_CONFIRMED },
      };

    case 'execution-unverified':
      // 모션은 끝났으나 계측으로 확인하지 못했다. 이유를 함께 남긴다.
      return {
        ...state,
        execution: {
          ...state.execution,
          status: EXECUTION.UNVERIFIED,
          unverifiedReason: action.reasonCode || '',
          unverifiedDetail: action.detail || '',
        },
      };

    case 'cancel-requested':
      return {
        ...state,
        execution: { ...state.execution, status: EXECUTION.CANCELLING },
      };

    case 'cancel-result':
      return {
        ...state,
        execution: { ...state.execution, status: EXECUTION.CANCELLED },
        cancelRecord: action.record,
      };

    case 'stop-requested':
      return {
        ...state,
        // 요청 즉시 래치가 걸린다. 확인 여부와 별개다.
        stopLatched: true,
        execution: isActive(state)
          ? { ...state.execution, status: EXECUTION.STOPPING }
          : state.execution,
      };

    case 'stop-result':
      return {
        ...state,
        stopLatched: true,
        stopRecord: action.record,
        execution:
          state.execution.status === EXECUTION.STOPPING ||
          state.execution.status === EXECUTION.RUNNING
            ? { ...state.execution, status: EXECUTION.GLOBAL_STOPPED }
            : state.execution,
      };

    case 'stop-dismissed':
      return { ...state, stopRecord: null };

    case 'restore':
      return { ...state, ...action.state };

    default:
      return state;
  }
}

/** 작은 저장소. 프레임워크를 쓰지 않는다. */
export function createStore(state = initialState()) {
  let current = state;
  const listeners = new Set();
  return {
    get() {
      return current;
    },
    dispatch(action) {
      const next = reduce(current, action);
      if (next !== current) {
        current = next;
        listeners.forEach((fn) => fn(current, action));
      }
      return current;
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}
