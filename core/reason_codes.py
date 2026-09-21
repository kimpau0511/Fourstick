"""공통 이유 코드 (md/개발플랜.md 1-07).

STT·계획·검증·실행·로봇·설정의 실패 원인을 한 곳에서 정의한다. 이 모듈이
이유 코드의 유일한 출처이며, 다른 계층에서 문자열을 직접 만들어 쓰지 않는다.

설계 근거:
- 값이 없을 때 임의 기본값으로 계속 실행하지 않고 코드로 거부한다(계획.md 27장).
- "확인 불가"는 성공도 실패도 아닌 별도 코드로 둔다. 성공으로 변환되면
  안 되기 때문이다(개발플랜.md 2단계 완료 조건).
"""

from __future__ import annotations

from enum import Enum


class ReasonCategory(str, Enum):
    """이유 코드의 분류. UI와 감사 로그가 묶어서 보여줄 때 쓴다."""

    CONFIG = "config"
    ASSET = "asset"
    STT = "stt"
    PLAN = "plan"
    SAFETY = "safety"
    CAPABILITY = "capability"
    GEOMETRY = "geometry"
    SESSION = "session"
    ROBOT = "robot"
    EXEC = "exec"
    #: 실기(실제 하드웨어) 전환 준비. 시뮬레이터 결과로는 채울 수 없는 조건들이다.
    HARDWARE = "hardware"


class ReasonCode(str, Enum):
    """실패·거부·확인불가의 원인. 접두사가 ReasonCategory와 1:1로 대응한다."""

    # ── 설정 ────────────────────────────────────────────────────────────
    # 값이 없으면 추정하지 않고 이 코드로 차단한다(계획.md 27장).
    CONFIG_MISSING = "config.missing"
    CONFIG_INVALID = "config.invalid"
    CONFIG_UNIT_MISMATCH = "config.unit_mismatch"
    CONFIG_VERSION_MISMATCH = "config.version_mismatch"

    # ── STT ─────────────────────────────────────────────────────────────
    STT_NO_SPEECH = "stt.no_speech"
    STT_LOW_CONFIDENCE = "stt.low_confidence"
    STT_STREAM_ABORTED = "stt.stream_aborted"
    STT_BACKEND_UNAVAILABLE = "stt.backend_unavailable"
    #: 한 번의 전사가 운영 예산(SttPolicy.transcribe_deadline_sec)을 넘겼다.
    #: 세션은 즉시 놓아주지만 추론 스레드는 남아 있다 — 슬롯은 그때까지 점유된다.
    STT_TRANSCRIBE_TIMEOUT = "stt.transcribe_timeout"
    #: 동시 전사 한도(SttPolicy.max_concurrent_transcriptions)가 가득 찼다.
    #: 대기열에 무기한 넣지 않고 바로 거절한다.
    STT_CAPACITY_EXCEEDED = "stt.capacity_exceeded"

    # ── 계획 ────────────────────────────────────────────────────────────
    PLAN_SCHEMA_INVALID = "plan.schema_invalid"
    PLAN_VERSION_UNSUPPORTED = "plan.version_unsupported"
    PLAN_UNSUPPORTED_SKILL = "plan.unsupported_skill"
    PLAN_ARG_MISSING = "plan.arg_missing"
    PLAN_ARG_UNKNOWN = "plan.arg_unknown"
    PLAN_UNKNOWN_FRAME = "plan.unknown_frame"
    PLAN_UNKNOWN_RESOURCE = "plan.unknown_resource"
    PLAN_SLOT_INCOMPLETE = "plan.slot_incomplete"
    PLAN_AMBIGUOUS = "plan.ambiguous"
    PLAN_EXPIRED = "plan.expired"
    PLAN_HASH_MISMATCH = "plan.hash_mismatch"
    PLAN_LLM_UNAVAILABLE = "plan.llm_unavailable"
    #: 모델 응답이 JSON으로 읽히지 않는다(형식 자체가 깨졌다).
    PLAN_LLM_OUTPUT_UNPARSEABLE = "plan.llm_output_unparseable"
    #: JSON은 읽혔지만 출력 스키마를 위반했다(필드 누락·타입 오류).
    PLAN_LLM_OUTPUT_SCHEMA_INVALID = "plan.llm_output_schema_invalid"
    #: 모델 호출이 제한시간을 넘겼다.
    PLAN_LLM_TIMEOUT = "plan.llm_timeout"
    #: 재시도 한도를 다 썼다. 마지막 실패 사유는 시도 기록에 남는다.
    PLAN_LLM_RETRY_EXHAUSTED = "plan.llm_retry_exhausted"
    #: 프롬프트 템플릿 또는 출력 스키마 버전이 설정과 다르다.
    PLAN_PROMPT_VERSION_MISMATCH = "plan.prompt_version_mismatch"
    #: 계획이 쓰는 리소스가 요청에서 확인된 리소스와 다르다.
    #: 모델이 없는 리소스를 유효한 다른 리소스로 치환한 경우가 여기 걸린다.
    PLAN_RESOURCE_MISMATCH = "plan.resource_mismatch"
    #: 요청이 모호하거나 정보가 부족해 사용자 확인이 필요하다.
    #: 실패가 아니라 "그럴듯한 계획을 만들지 않고 되묻는다"는 결말이다.
    PLAN_CLARIFICATION_REQUIRED = "plan.clarification_required"
    #: 서버가 제공하는 모델이 설정된 모델 ID와 다르다. 호출하지 않는다.
    PLAN_LLM_MODEL_MISMATCH = "plan.llm_model_mismatch"
    #: 모델이 사고 과정(reasoning)을 응답 본문에 섞어 보냈다. 보정하지 않고 거부한다.
    PLAN_LLM_REASONING_LEAKED = "plan.llm_reasoning_leaked"

    # ── 안전 ────────────────────────────────────────────────────────────
    SAFETY_SEQUENCE_INVALID = "safety.sequence_invalid"
    SAFETY_HOLD_INVALID = "safety.hold_invalid"
    SAFETY_LIMIT_EXCEEDED = "safety.limit_exceeded"
    SAFETY_ARG_OUT_OF_PROFILE = "safety.arg_out_of_profile"
    SAFETY_INSUFFICIENT_DATA = "safety.insufficient_data"
    SAFETY_APPROVAL_REQUIRED = "safety.approval_required"

    # ── 제3자 자산 (8-01) ───────────────────────────────────────────────
    #: manifest에 없거나 로컬에 없는 자산. 대체하지 않는다.
    ASSET_MISSING = "asset.missing"
    #: 라이선스가 확인되지 않았다. 복사·배포하지 않는다.
    ASSET_LICENSE_UNVERIFIED = "asset.license_unverified"
    #: 로컬 파일이 manifest의 checksum과 다르다.
    ASSET_CHECKSUM_MISMATCH = "asset.checksum_mismatch"

    # ── Capability 사전 검사 (6-04) ─────────────────────────────────────
    #: 스킬이 SkillCatalog에 없거나 Profile이 지원하지 않는다.
    CAPABILITY_SKILL_UNSUPPORTED = "capability.skill_unsupported"
    #: 요청한 관절·속도·가속도·그리퍼 값이 Profile 범위를 벗어났다.
    #: **범위 안으로 잘라서 통과시키지 않는다.**
    CAPABILITY_LIMIT_EXCEEDED = "capability.limit_exceeded"
    #: 검사에 필요한 Profile 값이 없다(예: 가속도 한계 미제공).
    #: 정보 부족이므로 허용으로 승격하지 않는다.
    CAPABILITY_PROFILE_INCOMPLETE = "capability.profile_incomplete"
    #: 단위가 선언되지 않았거나 Profile의 단위와 다르다.
    CAPABILITY_UNIT_MISMATCH = "capability.unit_mismatch"
    #: 계획이 Profile에 없는 관절·그리퍼를 지목했다.
    CAPABILITY_UNKNOWN_JOINT = "capability.unknown_joint"

    # ── 기하 검사 (6-05) ────────────────────────────────────────────────
    #: 충돌이 발견됐다.
    GEOMETRY_COLLISION = "geometry.collision"
    #: 작업공간을 벗어났다(도달 불가 포함).
    GEOMETRY_WORKSPACE_VIOLATION = "geometry.workspace_violation"
    #: 그 위치에 놓인 물체를 집는 **측정·검증된 파지 자세가 없다.** 자세를
    #: 만들거나 다른 위치의 파지 자세로 대신하지 않는다(IK 불가와 다르다).
    GEOMETRY_GRASP_POSE_UNAVAILABLE = "geometry.grasp_pose_unavailable"
    #: 좌표계를 알 수 없거나 계획·환경·Profile의 좌표계가 서로 다르다.
    GEOMETRY_FRAME_UNKNOWN = "geometry.frame_unknown"
    #: 환경 정보(snapshot)가 없다.
    GEOMETRY_ENVIRONMENT_UNAVAILABLE = "geometry.environment_unavailable"
    #: 환경 snapshot이 만료됐다.
    GEOMETRY_SNAPSHOT_EXPIRED = "geometry.snapshot_expired"
    #: 기하 검사 구현체가 없다. **검사하지 않은 상태를 안전으로 보지 않는다.**
    GEOMETRY_VALIDATOR_UNAVAILABLE = "geometry.validator_unavailable"
    #: 기하 검사가 제한시간을 넘겼다.
    GEOMETRY_VALIDATOR_TIMEOUT = "geometry.validator_timeout"
    #: 기하 검사 구현체가 오류를 냈다.
    GEOMETRY_VALIDATOR_ERROR = "geometry.validator_error"
    #: 구현체가 계약을 어긴 판정을 돌려줬다(예: 입력이 불완전한데 ALLOW).
    GEOMETRY_VERDICT_INVALID = "geometry.verdict_invalid"

    # ── 세션 (7단계 다중 세션 격리) ──────────────────────────────────────
    #: 같은 session_id를 이미 다른 살아 있는 클라이언트가 쓰고 있다.
    #: 탭 복제처럼 sessionStorage가 복사된 경우가 여기 걸린다.
    SESSION_CLIENT_CONFLICT = "session.client_conflict"
    #: 세션의 클라이언트 등록이 없거나 다른 클라이언트의 것이다.
    SESSION_CLIENT_UNKNOWN = "session.client_unknown"

    # ── 로봇 ────────────────────────────────────────────────────────────
    ROBOT_NOT_REGISTERED = "robot.not_registered"
    ROBOT_NOT_CONNECTED = "robot.not_connected"
    ROBOT_CONNECTION_LOST = "robot.connection_lost"
    ROBOT_SKILL_UNSUPPORTED = "robot.skill_unsupported"
    ROBOT_STATE_STALE = "robot.state_stale"
    ROBOT_STATE_UNAVAILABLE = "robot.state_unavailable"
    ROBOT_PROFILE_MISMATCH = "robot.profile_mismatch"

    # ── 실행 ────────────────────────────────────────────────────────────
    EXEC_PERMIT_DENIED = "exec.permit_denied"
    EXEC_GOAL_REJECTED = "exec.goal_rejected"
    EXEC_SEND_TIMEOUT = "exec.send_timeout"
    EXEC_RESULT_TIMEOUT = "exec.result_timeout"
    EXEC_ABORTED = "exec.aborted"
    EXEC_CANCELED = "exec.canceled"
    EXEC_GOAL_NOT_REACHED = "exec.goal_not_reached"
    EXEC_TASK_FAILED = "exec.task_failed"
    EXEC_STOPPED = "exec.stopped"
    EXEC_STOP_UNCONFIRMED = "exec.stop_unconfirmed"
    EXEC_ENVIRONMENT_CHANGED = "exec.environment_changed"
    # 계측이 불가능해 성공/실패를 말할 수 없는 경우. 절대 성공으로 바꾸지 않는다.
    EXEC_UNVERIFIABLE = "exec.unverifiable"
    # ── 시뮬레이터 전용 이송 시연 (8-11) ──────────────────────────────
    # 이 네 코드는 **시뮬레이터 안에서만** 의미가 있다. 실기 실행 판정에
    # 쓰이지 않는다 — 이름에 sim이 들어가 있어 기록에서도 구분된다.
    #: 옮길 물체가 선언된 자리에 없다.
    EXEC_SIM_OBJECT_ABSENT = "exec.sim_object_absent"
    #: 놓을 자리가 이미 점유돼 있다.
    EXEC_SIM_TARGET_OCCUPIED = "exec.sim_target_occupied"
    #: 시뮬레이션 고정 장치가 물체를 붙이거나 떼지 못했다(실제 파지가 아니다).
    EXEC_SIM_FIXTURE_FAILED = "exec.sim_fixture_failed"
    #: 놓은 뒤 물체 pose가 선언된 배치 허용 구역 밖이다.
    EXEC_SIM_PLACEMENT_OUT_OF_ZONE = "exec.sim_placement_out_of_zone"

    # ── 실기 전환 준비 (8-12) ──────────────────────────────────────────
    # 이 코드들은 **시뮬레이터 결과로 충족될 수 없다.** 각각 사람이 무엇을
    # 가져와야 하는지가 정해져 있다(validation/hardware_readiness.py).
    #: 필수 입력의 값이 없다.
    HARDWARE_INPUT_MISSING = "hardware.input_missing"
    #: 값은 있으나 근거 파일·출처가 없다.
    HARDWARE_EVIDENCE_MISSING = "hardware.evidence_missing"
    #: 값·근거가 있으나 확인되지 않았다(verified=false).
    HARDWARE_UNVERIFIED = "hardware.unverified"
    #: 시뮬레이터 선언값을 실기 근거로 쓸 수 없다.
    HARDWARE_SIMULATED_VALUE_REJECTED = "hardware.simulated_value_rejected"
    #: 선언만 있고 측정이 없다(예: 도면에 적힌 yaw만 있는 경우).
    HARDWARE_DECLARED_ONLY = "hardware.declared_only"
    #: 실기 파지 관측 수단이 없다.
    HARDWARE_OBSERVATION_UNAVAILABLE = "hardware.observation_unavailable"
    #: 실기 어댑터 설정(주소·엔드포인트·한계값)이 없다.
    HARDWARE_ADAPTER_UNCONFIGURED = "hardware.adapter_unconfigured"
    #: 장착 전 체크리스트가 끝나지 않았다.
    HARDWARE_CHECKLIST_INCOMPLETE = "hardware.checklist_incomplete"
    #: 실기 연결이 확인되지 않았다.
    HARDWARE_NOT_CONNECTED = "hardware.not_connected"

    @property
    def category(self) -> ReasonCategory:
        return ReasonCategory(self.value.split(".", 1)[0])

    def __str__(self) -> str:  # 로그·API에서 그대로 쓰도록
        return self.value


def codes_in(category: ReasonCategory) -> tuple[ReasonCode, ...]:
    """해당 분류의 이유 코드 전체. UI 필터와 문서 생성에 쓴다."""
    return tuple(c for c in ReasonCode if c.category is category)
