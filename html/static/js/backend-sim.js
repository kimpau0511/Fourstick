/** 시뮬레이션 백엔드 — 서버 없이 화면 흐름을 검증한다.
 *
 * 서버가 없거나 `?backend=sim`일 때 쓴다. **여기서 나온 결과는 로봇 실행이
 * 아니다** — 화면은 이 백엔드를 쓸 때 "시뮬레이션 모드"를 표시한다.
 *
 * 판정은 실제 계약과 같은 값(PASS/BLOCK/ASK)과 같은 ReasonCode를 쓴다.
 * 시나리오는 명령 문구로 고르거나 `?scenario=`로 고정한다:
 *
 *   pass              → 이동 계획, 안전 판단 PASS
 *   block_skill       → pick/place가 들어간 계획, capability.skill_unsupported
 *   block_resource    → 요청에 없는 자원, plan.resource_mismatch
 *   ask               → 정보 부족, plan.clarification_required
 *   stop_unconfirmed  → 전체 정지의 실제 정지 확인 실패
 *   cancel_unconfirmed→ 개별 취소의 정지 확인 실패
 */

import { describeStep, setResourceLabels } from './catalog.js';

const STEP_MS = 2200;
const CONFIRM_MS = 1200;
const PLAN_MS = 900;

function nowTime() {
  return new Date().toLocaleTimeString('ko-KR', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

function uid(prefix) {
  return `${prefix}_${Math.random().toString(16).slice(2, 10)}`;
}

function hash8() {
  return Math.random().toString(16).slice(2, 10);
}

// 시뮬레이션 모드에도 서버와 같은 자원 id를 쓴다. 이름은 여기서 채운다.
setResourceLabels([
  { resource_id: 'loc_pallet_1', display_name: '1번 팔레트' },
  { resource_id: 'loc_pallet_2', display_name: '2번 팔레트' },
  { resource_id: 'loc_conveyor', display_name: '컨베이어' },
  { resource_id: 'obj_a', display_name: 'A자재' },
  { resource_id: 'obj_b', display_name: 'B자재' },
]);

const MOVE_STEPS = [
  { skill: 'home', args: {} },
  { skill: 'move', args: { to: 'loc_pallet_1' } },
  { skill: 'move', args: { to: 'loc_conveyor' } },
  { skill: 'home', args: {} },
];

const TRANSFER_STEPS = [
  { skill: 'home', args: {} },
  { skill: 'move', args: { to: 'loc_pallet_1' } },
  { skill: 'pick', args: { object: 'obj_a', from: 'loc_pallet_1' } },
  { skill: 'move', args: { to: 'loc_conveyor' } },
  { skill: 'place', args: { object: 'obj_a', to: 'loc_conveyor' } },
  { skill: 'home', args: {} },
];

function buildPlan(utterance, steps, resources) {
  const created = Date.now() / 1000;
  return {
    requestId: uid('req'),
    planId: uid('plan'),
    planHash: hash8(),
    createdAt: created,
    ttlSec: 300,
    utterance,
    steps: steps.map((step, index) => ({
      no: index + 1,
      skill: step.skill,
      args: step.args,
      description: describeStep(step.skill, step.args),
    })),
    resources,
  };
}

/** 명령 문구에서 시나리오를 고른다. 화면 검증을 재현할 수 있게 규칙을 적어 둔다. */
export function scenarioFor(utterance, override) {
  if (override) return override;
  const text = (utterance || '').trim();
  if (!text) return 'ask';
  if (/그거|저기|거기|그것|알아서/.test(text)) return 'ask';
  if (/집어|올려|놓아|담아|pick|place/i.test(text)) return 'block_skill';
  if (/3번|4번|없는|창고/.test(text)) return 'block_resource';
  return 'pass';
}

export class SimulationBackend {
  constructor(options = {}) {
    this.kind = 'simulation';
    this.label = '시뮬레이션 모드 (서버 미연결)';
    this.scenarioOverride = options.scenario || null;
    // 테스트는 대기 시간을 줄여 쓴다. 화면 기본값은 위 상수다.
    this.stepMs = options.stepMs || STEP_MS;
    this.confirmMs = options.confirmMs == null ? CONFIRM_MS : options.confirmMs;
    this.planMs = options.planMs == null ? PLAN_MS : options.planMs;
    this.listeners = new Set();
    this.timer = null;
    this.plan = null;
    this.executionId = null;
    this.currentStep = 0;
  }

  onEvent(handler) {
    this.listeners.add(handler);
    return () => this.listeners.delete(handler);
  }

  emit(event) {
    this.listeners.forEach((fn) => fn(event));
  }

  async connect() {
    return {
      session: { session_id: `sim_${hash8()}`, client_id: `cli_${hash8()}` },
      config: {
        schema_version: '2.0',
        session: { client_grace_sec: null },
        features: { stt: { available: false, detail: '시뮬레이션 모드' } },
      },
      sttAvailable: false,
    };
  }

  async restore() {
    return null;
  }

  async createPlan(utterance) {
    const scenario = scenarioFor(utterance, this.scenarioOverride);
    await new Promise((resolve) => setTimeout(resolve, this.planMs));

    if (scenario === 'ask') {
      const plan = buildPlan(utterance, MOVE_STEPS.slice(0, 2), [
        { utterance: '(위치 미지정)', plan: '—', match: false },
      ]);
      this.plan = plan;
      return {
        ok: true,
        plan,
        validation: {
          verdict: 'ASK',
          reasonCodes: ['plan.clarification_required'],
          detail: '요청에서 확인된 자원이 없어 계획을 확정할 수 없다.',
          rules: [
            { code: 'E-REQ-001', status: 'insufficient_data', message: '요청에서 확인된 자원이 없다' },
            { code: 'E-GEOM-001', status: 'insufficient_data', message: '환경 snapshot 없음' },
          ],
          missing: ['대상 위치 (예: 1번 팔레트, 컨베이어)', '동작 대상이 자재인지 위치인지'],
          fixes: [
            '위치 이름을 카탈로그에 있는 표현으로 말하거나 입력한다.',
            '예: "1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘"',
          ],
        },
        stopLatchCleared: true,
      };
    }

    if (scenario === 'block_skill') {
      const plan = buildPlan(utterance, TRANSFER_STEPS, [
        { utterance: '1번 팔레트 → loc_pallet_1', plan: 'loc_pallet_1', match: true },
        { utterance: 'A자재 → obj_a', plan: 'obj_a', match: true },
        { utterance: '컨베이어 → loc_conveyor', plan: 'loc_conveyor', match: true },
      ]);
      this.plan = plan;
      return {
        ok: true,
        plan,
        validation: {
          verdict: 'BLOCK',
          reasonCodes: ['capability.skill_unsupported'],
          detail:
            'pick·place는 이 로봇 구성에서 지원하지 않는다: custom adapter, TCP transform,'
            + ' 충돌 형상, 파지 확인 정보 미확보',
          rules: [
            { code: 'E-CAP-001', status: 'block', message: 'pick: 지원 스킬 아님' },
            { code: 'E-CAP-001', status: 'block', message: 'place: 지원 스킬 아님' },
          ],
          blocked: [
            '3단계 pick — 그리퍼가 결합되지 않았다',
            '5단계 place — 파지 확인 정보를 만들 수 없다',
          ],
          missing: [],
          fixes: [],
        },
        stopLatchCleared: true,
      };
    }

    if (scenario === 'block_resource') {
      const plan = buildPlan(utterance, MOVE_STEPS, [
        { utterance: '3번 팔레트 → (카탈로그 없음)', plan: 'loc_pallet_1', match: false },
        { utterance: '컨베이어 → loc_conveyor', plan: 'loc_conveyor', match: true },
      ]);
      this.plan = plan;
      return {
        ok: true,
        plan,
        validation: {
          verdict: 'BLOCK',
          reasonCodes: ['plan.resource_mismatch'],
          detail: '요청에 없는 자원이 계획에 들어 있다: loc_pallet_1',
          rules: [{ code: 'E-REQ-001', status: 'block', message: '계획 자원 ⊄ 요청 자원' }],
          blocked: ['요청에서 확인되지 않은 위치를 계획이 사용한다 (loc_pallet_1)'],
          missing: [],
          fixes: [],
        },
        stopLatchCleared: true,
      };
    }

    const plan = buildPlan(utterance, MOVE_STEPS, [
      { utterance: '1번 팔레트 → loc_pallet_1', plan: 'loc_pallet_1', match: true },
      { utterance: '컨베이어 → loc_conveyor', plan: 'loc_conveyor', match: true },
    ]);
    this.plan = plan;
    return {
      ok: true,
      plan,
      validation: {
        verdict: 'PASS',
        reasonCodes: [],
        detail: '안전 검증·자원 일치·Capability·기하 검사를 모두 통과했다.',
        rules: [
          { code: 'E-SEQ-001', status: 'pass', message: '스텝 순서 적합' },
          { code: 'E-HOLD-002', status: 'pass', message: '종료 자세 적합' },
          { code: 'E-LIMIT-001', status: 'pass', message: '관절 제한 안' },
          { code: 'E-REQ-001', status: 'pass', message: '요청-계획 자원 일치' },
          { code: 'E-CAP-001', status: 'pass', message: 'home·move·stop 지원' },
          { code: 'E-GEOM-001', status: 'pass', message: '기하 검사 통과' },
        ],
        blocked: [],
        missing: [],
        fixes: [],
      },
      stopLatchCleared: true,
    };
  }

  async startExecution() {
    if (!this.plan) return { ok: false, reasonCode: 'config.missing', detail: '계획이 없다' };
    this.executionId = uid('exec');
    this.currentStep = 1;
    const total = this.plan.steps.length;

    this.emit({ kind: 'step-started', step: 1 });
    this.timer = setInterval(() => {
      const step = this.plan.steps[this.currentStep - 1];
      if (!step) return;
      this.emit({
        kind: 'step-result',
        result: {
          no: step.no,
          skill: step.skill,
          status: 'done',
          requestAccepted: true,
          motionDone: true,
          goalReached: true,
          taskSuccess: true,
          reasonCode: '',
          retry: 0,
        },
      });
      this.emit({
        kind: 'log',
        level: 'execution',
        message: `${step.no}단계 완료: ${step.skill} — ${step.description}`,
      });
      if (this.currentStep >= total) {
        this.stopTimer();
        this.emit({ kind: 'completed' });
        return;
      }
      this.currentStep += 1;
      this.emit({ kind: 'step-started', step: this.currentStep });
    }, this.stepMs);

    return { ok: true, executionId: this.executionId, total };
  }

  stopTimer() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  async cancelExecution(executionId) {
    this.stopTimer();
    const requestTime = nowTime();
    const step = this.currentStep;
    const unconfirmed = this.scenarioOverride === 'cancel_unconfirmed';
    const plan = this.plan;
    setTimeout(() => {
      const current = plan && plan.steps[step - 1];
      if (current) {
        this.emit({
          kind: 'step-result',
          result: {
            no: current.no,
            skill: current.skill,
            status: 'cancelled',
            requestAccepted: true,
            motionDone: false,
            goalReached: false,
            taskSuccess: false,
            reasonCode: 'exec.canceled',
            retry: 0,
          },
        });
      }
      this.emit({
        kind: 'cancel-result',
        record: {
          scope: 'execution',
          executionId,
          requestTime,
          confirmedTime: unconfirmed ? null : nowTime(),
          stoppedAtStep: step,
          reasonCode: 'exec.canceled',
          result: unconfirmed ? 'unconfirmed' : 'confirmed',
          wasRunning: true,
        },
      });
    }, this.confirmMs);
    return { ok: true, requested: true };
  }

  async stopAll() {
    this.stopTimer();
    const requestTime = nowTime();
    const unconfirmed = this.scenarioOverride === 'stop_unconfirmed';
    const step = this.currentStep;
    const wasRunning = Boolean(this.executionId) && step > 0;
    setTimeout(() => {
      this.emit({
        kind: 'stop-result',
        record: {
          scope: 'global',
          requestTime,
          confirmedTime: unconfirmed ? null : nowTime(),
          confirmed: !unconfirmed,
          reasonCode: 'exec.stopped',
          result: unconfirmed ? 'unconfirmed' : 'confirmed',
          affectedExecutionCount: wasRunning ? 1 : 0,
          yourExecutionIds: wasRunning ? [this.executionId] : [],
          stoppedAtStep: step,
          detail: unconfirmed
            ? '정지 요청은 전송했지만 실제 정지를 확인하지 못했다.'
            : '로봇 정지를 확인했다.',
        },
      });
    }, 400);
    return { ok: true, requested: true };
  }

  async startVoice() {
    return { ok: false, detail: '시뮬레이션 모드에서는 마이크를 쓰지 않는다' };
  }

  async stopVoice() {
    return { ok: false };
  }
}
