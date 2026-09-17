"""저장 레코드 (영속화 모델).

core 계약 타입과 저장 스키마를 분리하는 층이다. 저장소 구현체는 이 레코드만
주고받고, core 타입 ↔ 레코드 변환은 `storage/mappers.py`가 담당한다. 그래서
PostgreSQL 구현체를 추가할 때 core 계약과 API 스키마를 건드리지 않는다.

식별자 규칙: 모든 ID는 애플리케이션이 만든다. DB의 자동 증가 숫자에 의존하지
않는다 — 값이 DB마다 달라지고, 재현·감사에 쓸 수 없다. 순번(seq)도 트랜잭션
안에서 애플리케이션이 계산한다.

시각 규칙: 모든 시각은 UTC epoch 초(float)다. 타임존 의미가 DB마다 다르므로
DB의 타임스탬프 타입을 쓰지 않는다. 표시용 변환은 상위 계층이 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from core.execution_result import ExecutionResult
from core.execution_state import ExecutionState
from core.reason_codes import ReasonCode
from core.task_plan import TaskPlan


class SttExecutionPath(str, Enum):
    """전사가 어떤 경로에서 실행됐는가.

    평가 실행기는 `Transcriber`를 직접 호출한다 — VAD 게이트도, 세션의 시간
    예산도 거치지 않는다. 그 측정치를 운영 지연 통계에 섞으면 실제 사용자
    경험보다 나쁘게(또는 다르게) 보인다. 그래서 경로를 값으로 남기고 통계에서
    구분한다.
    """

    #: 실제 세션 경로. VAD 게이트와 TranscriptionGuard를 지난다.
    OPERATIONAL = "operational"
    #: 평가·시험 경로. Transcriber를 직접 호출한다.
    EVALUATION = "evaluation"


@dataclass(frozen=True)
class SttInferenceRecord:
    """STT 실행 시도 1회. **append-only** — 덮어쓰지 않는다.

    재전사·모델 교체·모델 비교는 모두 새 행을 더한다. 같은 세션에서 profile을
    바꿔 다시 돌리면 attempt_no가 늘어난 새 기록이 생기고, 이전 기록은 그대로
    남는다. 어떤 시도가 최종 요청이 됐는지는 `final_adopted`와
    `requests.selected_stt_inference_id`가 함께 가리킨다.

    담지 않는 것:
    - partial transcript — 확정 전 중간 산출물이라 남기지 않는다.
    - PCM 원본 — 평가용 음성은 설정된 별도 경로에만 둔다
      (`SttModelConfig.retain_eval_audio` / `eval_audio_dir`).

    처리 속도는 원자료(음성 길이·처리 시간·최초 로딩 시간)를 각각 저장하고,
    RTF는 거기서 파생한다. 따로 저장하면 서로 어긋날 수 있다.
    """

    stt_inference_id: str
    session_id: str
    #: 세션 안에서 몇 번째 시도인가. 1부터 센다.
    attempt_no: int
    schema_version: str
    #: 기록 생성 UTC epoch 초.
    created_at: float

    # ── 구성 ────────────────────────────────────────────────────────────
    model_name: str
    #: 모델 파일·리비전 식별자. 같은 이름의 모델이 갱신되는 것을 구분한다.
    model_version: str
    profile_id: str
    #: Profile 설정의 버전(`SttModelConfig.config_version`).
    profile_version: str
    #: "verified" | "candidate". 미검증 구성의 측정치를 검증된 것으로 읽지 않기 위해 남긴다.
    verification: str
    device: str
    compute_type: str
    language: str

    # ── 측정 ────────────────────────────────────────────────────────────
    #: 입력 음성 길이(ms).
    audio_duration_ms: int
    #: 전사 처리 시간(ms). 모델 로딩 시간을 포함하지 않는다.
    processing_duration_ms: int

    # ── 결과 ────────────────────────────────────────────────────────────
    transcript: str
    confidence: float
    #: 신뢰도를 어떻게 얻었는지. 예: "exp(avg_logprob) 길이가중평균".
    confidence_metric: str
    #: 이 시도가 최종 요청으로 채택됐는가.
    final_adopted: bool
    #: 모델 최초 로딩 시간(ms). 이미 로딩된 모델로 돌렸으면 None이다.
    model_load_duration_ms: int | None = None
    #: VAD가 음성으로 판정했는가. 측정하지 않았으면 None이다.
    vad_speech_detected: bool | None = None
    vad_max_probability: float | None = None
    #: 실패·되묻기 사유. 정상 확정이면 None이다.
    reason_code: ReasonCode | None = None
    #: 운영 경로인가 평가 경로인가. 통계에서 섞지 않기 위해 반드시 남긴다.
    execution_path: SttExecutionPath = SttExecutionPath.OPERATIONAL
    #: 최종 요청과 연결되는 식별자. 확정 전 시도는 None일 수 있다.
    request_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("stt_inference_id", "session_id", "schema_version", "model_name",
                     "model_version", "profile_id", "profile_version", "verification",
                     "device", "compute_type", "language", "confidence_metric"):
            if not getattr(self, name):
                raise ValueError(f"{name}이 비어 있다")
        if self.attempt_no < 1:
            raise ValueError(f"attempt_no는 1부터다: {self.attempt_no}")
        for name in ("audio_duration_ms", "processing_duration_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name}이 음수다")
        if self.model_load_duration_ms is not None and self.model_load_duration_ms < 0:
            raise ValueError("model_load_duration_ms가 음수다")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence가 0~1을 벗어난다: {self.confidence}")
        if self.final_adopted and self.request_id is None:
            raise ValueError("채택된 시도는 연결할 request_id가 있어야 한다")
        if not isinstance(self.execution_path, SttExecutionPath):
            raise ValueError(
                f"execution_path가 SttExecutionPath가 아니다: {self.execution_path!r}"
            )
        if self.final_adopted and self.execution_path is SttExecutionPath.EVALUATION:
            raise ValueError(
                "평가 경로의 전사를 최종 요청으로 채택할 수 없다"
                " — 운영 검증과 평가를 섞지 않는다"
            )
        if self.final_adopted and self.reason_code is not None:
            raise ValueError(
                f"실패 사유({self.reason_code})가 있는 시도를 채택으로 기록할 수 없다"
            )

    @property
    def rtf(self) -> float | None:
        """처리 시간 / 음성 길이. 1보다 크면 실시간보다 느리다."""
        if self.audio_duration_ms <= 0:
            return None
        return self.processing_duration_ms / self.audio_duration_ms

    @property
    def throughput(self) -> float | None:
        """음성 길이 / 처리 시간. 1보다 작으면 실시간보다 느리다."""
        if self.processing_duration_ms <= 0:
            return None
        return self.audio_duration_ms / self.processing_duration_ms


@dataclass(frozen=True)
class RequestRecord:
    """최종 확정된 사용자 요청.

    음성 요청이면 어떤 STT 실행 결과를 채택했는지 식별자만 갖는다. 전사 본문과
    측정값은 `stt_inferences`에 append-only로 쌓인다 — 여기 복사하면 재전사
    때 덮어쓰게 된다.

    후속 `plan_id`·`execution_id`는 `plans.request_id`·`executions.request_id`로
    이어붙는다 — 여기에 역참조를 두지 않는다(요청이 계획보다 먼저 생긴다).
    """

    request_id: str
    utterance: str
    schema_version: str
    created_at: float
    #: 채택된 STT 실행 기록의 id. 텍스트 입력 요청이면 None이다.
    selected_stt_inference_id: str | None = None
    #: 이 요청을 만든 세션. None이면 세션 개념이 생기기 전(마이그레이션 0009
    #: 이전)의 기록이다 — 어느 세션의 것이었는지 추정하지 않는다.
    session_id: str | None = None


@dataclass(frozen=True)
class PlanRecord:
    """계획 원본과 조회용 필드. `plan`이 원본이고 나머지는 검색 가능한 사본이다."""

    plan_id: str
    request_id: str
    plan_hash: str
    plan: TaskPlan
    stored_at: float


@dataclass(frozen=True)
class RuleResultRecord:
    rule_code: str
    status: str
    reason: ReasonCode | None
    message: str


@dataclass(frozen=True)
class ValidationRecord:
    """안전 검증 1회. 덮어쓰지 않고 시간순으로 쌓는다."""

    validation_id: str
    plan_id: str
    plan_hash: str
    decision: str
    policy_id: str
    policy_version: str
    evaluated_at: float
    rule_results: tuple[RuleResultRecord, ...] = ()


@dataclass(frozen=True)
class RobotProfileRecord:
    """조합형 로봇 Profile 한 버전 (8-02). **append-only.**

    Profile이 바뀌면 새 버전으로 새 행이 생긴다. 기존 행을 고치지 않는다 —
    과거 실행·승인이 어떤 Profile 버전 위에서 이뤄졌는지가 바뀌면 감사 기록이
    아니다.
    """

    composite_profile_id: str
    composite_profile_version: str
    display_name: str
    arm_profile_id: str
    arm_profile_version: str
    asset_manifest_version: str
    environment: str
    verified: bool
    supported_skills: tuple[str, ...]
    unverified_items: tuple[str, ...]
    recorded_at: float
    schema_version: str
    gripper_profile_id: str | None = None
    gripper_profile_version: str | None = None
    mounting_profile_id: str | None = None
    mounting_profile_version: str | None = None
    source_commit: str = ""
    source_checksum: str = ""

    def __post_init__(self) -> None:
        for name in ("composite_profile_id", "composite_profile_version",
                     "display_name", "arm_profile_id", "arm_profile_version",
                     "asset_manifest_version", "schema_version"):
            if not getattr(self, name):
                raise ValueError(f"{name}이 비어 있다")
        if self.environment not in ("simulation", "real"):
            raise ValueError(f"environment가 낯설다: {self.environment!r}")
        if self.verified and self.unverified_items:
            raise ValueError(
                "verified인데 미확인 항목이 남아 있다"
                f": {list(self.unverified_items)}"
            )

    def to_dict(self) -> dict:
        return {
            "composite_profile_id": self.composite_profile_id,
            "composite_profile_version": self.composite_profile_version,
            "display_name": self.display_name,
            "arm_profile_id": self.arm_profile_id,
            "arm_profile_version": self.arm_profile_version,
            "gripper_profile_id": self.gripper_profile_id,
            "gripper_profile_version": self.gripper_profile_version,
            "mounting_profile_id": self.mounting_profile_id,
            "mounting_profile_version": self.mounting_profile_version,
            "asset_manifest_version": self.asset_manifest_version,
            "source_commit": self.source_commit,
            "source_checksum": self.source_checksum,
            "environment": self.environment,
            "verified": self.verified,
            "supported_skills": list(self.supported_skills),
            "unverified_items": list(self.unverified_items),
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class SimVerificationRecord:
    """시뮬레이터 검증 한 스텝 (8-03~8-06). **append-only.**

    목표와 관측을 따로 담는다. `command_accepted`(명령 전송 성공)와
    `target_reached`(관측 기준 목표 도달)를 한 값으로 합치지 않는다.
    """

    verification_id: str
    recorded_at: float
    step: str
    composite_profile_id: str
    composite_profile_version: str
    arm_profile_id: str
    arm_profile_version: str
    asset_manifest_version: str
    ros_distro: str
    gazebo_version: str
    controller_versions: str
    adapter_kind: str
    arm_only: bool
    target: Mapping[str, Any]
    observed: Mapping[str, Any]
    command_accepted: bool
    state: str
    schema_version: str
    is_simulated: bool = True
    gripper_profile_id: str | None = None
    gripper_profile_version: str | None = None
    mounting_profile_id: str | None = None
    mounting_profile_version: str | None = None
    aperture_target: float | None = None
    aperture_observed: float | None = None
    contact: Mapping[str, Any] | None = None
    grasp: Mapping[str, Any] | None = None
    motion_completed: bool | None = None
    target_reached: bool | None = None
    task_succeeded: bool | None = None
    reason_code: ReasonCode | None = None
    validation_run_id: str | None = None
    approval_id: str | None = None
    execution_id: str | None = None
    attempt_no: int | None = None
    detail: str = ""
    #: MoveIt 설정과 기구학 solver 버전 (8-07). 설정이 바뀌면 값이 달라진다.
    moveit_config_version: str | None = None
    kinematics_solver: str | None = None
    kinematics_solver_version: str | None = None
    #: 검사에 쓴 planning scene. **본문이 아니라 식별자와 지문만 남긴다.**
    planning_scene_snapshot_id: str | None = None
    planning_scene_snapshot_version: str | None = None
    planning_scene_snapshot_hash: str | None = None
    planning_scene_checked_at: float | None = None
    #: 충돌 검사 판정과 이유. ALLOW에는 scene 식별자가 반드시 있어야 한다.
    collision_decision: str | None = None
    collision_reason_code: ReasonCode | None = None
    geometry_validator_id: str | None = None
    geometry_validator_version: str | None = None
    #: 명령 관절값과 관측 관절값을 따로 남긴다(합치지 않는다).
    commanded_joints: Mapping[str, Any] | None = None
    observed_joints: Mapping[str, Any] | None = None
    #: 목표 자세와 관측 자세(좌표). 관측이 없으면 None이다.
    target_pose: Mapping[str, Any] | None = None
    observed_pose: Mapping[str, Any] | None = None
    #: 시뮬레이션 전용 fixture 버전(검증용 물체 구성). 실제 셀 환경이 아니다.
    sim_fixture_version: str | None = None

    def __post_init__(self) -> None:
        if self.collision_decision is not None:
            if self.collision_decision not in ("allow", "block", "ask"):
                raise ValueError(
                    f"충돌 검사 판정이 낯설다: {self.collision_decision!r}")
            if self.collision_decision == "allow" and not (
                self.planning_scene_snapshot_id
                and self.planning_scene_snapshot_hash
                and self.planning_scene_checked_at
            ):
                # 검사하지 않은 상태를 통과로 기록하지 않는다.
                raise ValueError(
                    "충돌 검사 통과에는 planning scene snapshot이 필요하다")
        for name in ("verification_id", "step", "composite_profile_id",
                     "composite_profile_version", "arm_profile_id",
                     "arm_profile_version", "asset_manifest_version",
                     "ros_distro", "gazebo_version", "controller_versions",
                     "adapter_kind", "state", "schema_version"):
            if not getattr(self, name):
                raise ValueError(f"{name}이 비어 있다")
        if not self.is_simulated:
            raise ValueError(
                "시뮬레이터 검증 기록은 is_simulated=True다 — 실제 실행으로"
                " 기록하지 않는다"
            )
        if self.task_succeeded and not self.target_reached:
            raise ValueError(
                "목표에 도달하지 않았는데 작업 성공으로 기록할 수 없다"
            )

    def to_dict(self) -> dict:
        return {
            "verification_id": self.verification_id,
            "recorded_at": self.recorded_at,
            "step": self.step,
            "composite_profile": (
                f"{self.composite_profile_id} {self.composite_profile_version}"
            ),
            "arm_profile": f"{self.arm_profile_id} {self.arm_profile_version}",
            "gripper_profile": (
                None if self.gripper_profile_id is None
                else f"{self.gripper_profile_id} {self.gripper_profile_version}"
            ),
            "mounting_profile": (
                None if self.mounting_profile_id is None
                else f"{self.mounting_profile_id} {self.mounting_profile_version}"
            ),
            "asset_manifest_version": self.asset_manifest_version,
            "environment": {
                "ros_distro": self.ros_distro,
                "gazebo_version": self.gazebo_version,
                "controllers": self.controller_versions,
                "adapter_kind": self.adapter_kind,
                "arm_only": self.arm_only,
                "is_simulated": self.is_simulated,
            },
            "target": dict(self.target),
            "observed": dict(self.observed),
            "aperture": {
                "target": self.aperture_target,
                "observed": self.aperture_observed,
            },
            "contact": None if self.contact is None else dict(self.contact),
            "grasp": None if self.grasp is None else dict(self.grasp),
            "command_accepted": self.command_accepted,
            "motion_completed": self.motion_completed,
            "target_reached": self.target_reached,
            "task_succeeded": self.task_succeeded,
            "state": self.state,
            "reason_code": None if self.reason_code is None else self.reason_code.value,
            "validation_run_id": self.validation_run_id,
            "approval_id": self.approval_id,
            "execution_id": self.execution_id,
            "attempt_no": self.attempt_no,
            "detail": self.detail,
            # 8-07: MoveIt 계획·충돌 검사 관측. 환경 데이터 본문은 넣지 않는다.
            "moveit": {
                "config_version": self.moveit_config_version,
                "kinematics_solver": self.kinematics_solver,
                "kinematics_solver_version": self.kinematics_solver_version,
                "scene_snapshot_id": self.planning_scene_snapshot_id,
                "scene_snapshot_version": self.planning_scene_snapshot_version,
                "scene_snapshot_hash": self.planning_scene_snapshot_hash,
                "scene_checked_at": self.planning_scene_checked_at,
                "collision_decision": self.collision_decision,
                "collision_reason_code": (
                    None if self.collision_reason_code is None
                    else self.collision_reason_code.value
                ),
                "validator_id": self.geometry_validator_id,
                "validator_version": self.geometry_validator_version,
                "sim_fixture_version": self.sim_fixture_version,
            },
            "commanded_joints": (
                None if self.commanded_joints is None
                else dict(self.commanded_joints)
            ),
            "observed_joints": (
                None if self.observed_joints is None
                else dict(self.observed_joints)
            ),
            "target_pose": (
                None if self.target_pose is None else dict(self.target_pose)
            ),
            "observed_pose": (
                None if self.observed_pose is None else dict(self.observed_pose)
            ),
        }


class ValidationDecision(str, Enum):
    """검증 판정. 안전 검증 집계·기하 검사와 같은 3값이다."""

    ALLOW = "allow"
    BLOCK = "block"
    ASK = "ask"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ValidationRunRecord:
    """검증 실행 1회 (6-04·6-05). **append-only.**

    무엇이·무슨 버전으로·어떤 Profile·Policy·환경 snapshot 기준으로 검사해
    어떤 판정을 냈는지 남긴다. 환경 데이터 본문이나 기하 모델은 담지 않는다 —
    `snapshot_id`·`snapshot_version`·`snapshot_hash`로만 추적한다.

    `decision=ALLOW`인데 `input_complete=False`인 행은 저장할 수 없다.
    검사하지 않은 상태를 통과로 기록하는 경로를 만들지 않는다.
    """

    validation_run_id: str
    request_id: str
    plan_id: str
    plan_hash: str
    #: 무엇이 검사했는가: "safety" / "capability" / "geometry" / "consistency".
    validator_kind: str
    validator_id: str
    validator_version: str
    input_complete: bool
    decision: ValidationDecision
    started_at: float
    finished_at: float
    schema_version: str
    session_id: str | None = None
    #: 실행 직전 재검증이면 그 실행. 계획 단계 검증이면 None이다.
    execution_id: str | None = None
    robot_id: str | None = None
    profile_id: str | None = None
    profile_version: str | None = None
    policy_id: str | None = None
    policy_version: str | None = None
    snapshot_id: str | None = None
    snapshot_version: str | None = None
    snapshot_hash: str | None = None
    reason_code: ReasonCode | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        for name in ("validation_run_id", "request_id", "plan_id", "plan_hash",
                     "validator_kind", "validator_id", "validator_version",
                     "schema_version"):
            if not getattr(self, name):
                raise ValueError(f"{name}이 비어 있다")
        if not isinstance(self.decision, ValidationDecision):
            raise ValueError(f"decision이 ValidationDecision이 아니다: {self.decision!r}")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at이 started_at보다 이르다")
        if self.decision is ValidationDecision.ALLOW and not self.input_complete:
            raise ValueError(
                "입력이 불완전한데 allow다 — 검사하지 않은 상태를 통과로 기록하지 않는다"
            )
        if self.decision is not ValidationDecision.ALLOW and self.reason_code is None:
            raise ValueError(f"{self.decision}인데 reason_code가 없다")

    @property
    def duration_ms(self) -> int:
        return int(round((self.finished_at - self.started_at) * 1000))

    def to_dict(self) -> dict:
        return {
            "validation_run_id": self.validation_run_id,
            "validator_kind": self.validator_kind,
            "validator_id": self.validator_id,
            "validator_version": self.validator_version,
            "decision": self.decision.value,
            "input_complete": self.input_complete,
            "reason_code": None if self.reason_code is None else self.reason_code.value,
            "detail": self.detail,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "snapshot_id": self.snapshot_id,
            "snapshot_version": self.snapshot_version,
            "snapshot_hash": self.snapshot_hash,
            "execution_id": self.execution_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class PermitReasonRecord:
    reason: ReasonCode
    detail: str


@dataclass(frozen=True)
class PermitRecord:
    """실행 허가 판정 1회. 거부 사유를 전부 보존한다."""

    permit_id: str
    plan_id: str
    plan_hash: str
    granted: bool
    policy_id: str
    policy_version: str
    decided_at: float
    reasons: tuple[PermitReasonRecord, ...] = ()


def check_execution_environment(
    *, adapter_kind: str | None, is_simulated: bool | None,
    environment_confirmed_at: float | None,
) -> None:
    """실행 환경 값이 서로 모순되지 않는지 확인한다.

    같은 규칙을 SQLite 트리거도 강제한다(마이그레이션 0011). 여기서 먼저 막는
    이유는 저장 직전에야 알게 되는 것을 피하고, 이유를 분명한 ReasonCode로
    돌려주기 위해서다.

    - Fake 어댑터는 **반드시** simulated다.
    - 실제 어댑터가 real(`is_simulated=False`)이라고 기록되려면 **연결이 확인된
      시각**이 있어야 한다. 근거 없이 real로 적지 않는다.
    - 어댑터 종류를 모르면 실행 환경도 unknown(None)이다. 추정하지 않는다.
    """
    kind = (adapter_kind or "").strip()
    if not kind:
        if is_simulated is not None:
            raise ValueError(
                "adapter_kind가 없는데 is_simulated가 정해져 있다"
                " — 근거 없는 실행 환경은 unknown(None)이어야 한다"
            )
        return
    if kind == FAKE_ADAPTER_KIND:
        if is_simulated is not True:
            raise ValueError(
                f"adapter_kind={kind!r}는 반드시 시뮬레이션이다"
                f" (is_simulated={is_simulated!r})"
            )
        return
    if is_simulated is False and environment_confirmed_at is None:
        raise ValueError(
            f"adapter_kind={kind!r}를 real로 기록하려면 연결이 확인된 시각"
            " (environment_confirmed_at)이 필요하다"
        )


#: Fake 어댑터의 종류 이름. 저장 규칙이 이 값을 기준으로 판정한다.
FAKE_ADAPTER_KIND: str = "fake"


@dataclass(frozen=True)
class ExecutionRecord:
    """실행 시도 1회. 같은 요청을 재시도하면 새 execution_id와 attempt_no가 발급된다."""

    execution_id: str
    request_id: str
    plan_id: str
    plan_hash: str
    attempt_no: int
    adapter_id: str
    robot_id: str
    profile_id: str
    profile_version: str
    policy_id: str
    policy_version: str
    schema_version: str
    started_at: float
    #: 이 실행을 시작한 세션과 근거 승인.
    session_id: str | None = None
    approval_id: str | None = None
    #: 어댑터 종류("fake" 또는 실제 어댑터 종류)와 시뮬레이션 여부.
    #: completed여도 실제 로봇 실행으로 집계하지 않기 위해 기록에 남긴다.
    adapter_kind: str | None = None
    is_simulated: bool | None = None
    #: 실제 연결이 확인된 UTC 시각. `is_simulated=False`(real)의 유일한 근거다.
    environment_confirmed_at: float | None = None

    def __post_init__(self) -> None:
        check_execution_environment(
            adapter_kind=self.adapter_kind, is_simulated=self.is_simulated,
            environment_confirmed_at=self.environment_confirmed_at,
        )


@dataclass(frozen=True)
class StateTransitionRecord:
    """상태 전이 이력. append-only이며 seq로 순서를 고정한다."""

    execution_id: str
    seq: int
    from_state: ExecutionState | None
    to_state: ExecutionState
    reason: ReasonCode | None
    occurred_at: float


@dataclass(frozen=True)
class ObservationRecord:
    """실행 중 관측값. append-only. payload는 원본 JSON으로 보존한다."""

    execution_id: str
    seq: int
    kind: str
    observed_at: float
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResultRecord:
    """실행 결과. append-only이며 마지막 seq가 최종 결과다."""

    execution_id: str
    seq: int
    result: ExecutionResult
    recorded_at: float


@dataclass(frozen=True)
class ExecutionTrace:
    """한 실행 시도의 전체 이력. 재현·감사에 쓴다."""

    execution: ExecutionRecord
    transitions: Sequence[StateTransitionRecord]
    observations: Sequence[ObservationRecord]
    results: Sequence[ResultRecord]

    @property
    def final_result(self) -> ResultRecord | None:
        return self.results[-1] if self.results else None


class PlanningAttemptStatus(str, Enum):
    """계획 생성 시도의 결과 상태."""

    #: 모델이 계획을 만들어 계약·카탈로그 검증까지 통과했다.
    SUCCEEDED = "succeeded"
    #: 실패했다. 사유는 `reason_code`에 있다.
    FAILED = "failed"
    #: 정지 요청이라 모델을 호출하지 않고 stop 계획으로 직행했다.
    #: 공급자·모델 식별자가 없다 — 모델 통계에 섞지 않기 위해 상태로 구분한다.
    STOP_BYPASS = "stop_bypass"


@dataclass(frozen=True)
class PlanningPayload:
    """시도의 원본 payload. 버전이 붙고 비밀정보가 제거된 상태로만 들어온다.

    컬럼으로 두는 것은 검색에 필요한 식별자와 상태뿐이다. 프롬프트·출력 원본은
    구조가 자주 바뀌므로 버전이 포함된 JSON 한 덩어리로 보존한다.
    """

    payload_version: str
    body_json: str
    #: 길이 제한으로 잘렸는가. 조용히 자르지 않는다.
    truncated: bool
    #: 보존 기간(일). 정리 작업이 이 값을 본다.
    retention_days: int

    def __post_init__(self) -> None:
        if not self.payload_version:
            raise ValueError("payload_version이 비어 있다")
        if self.retention_days <= 0:
            raise ValueError("retention_days가 0 이하다 — 보존하지 않으려면 payload를 None으로 둔다")
        for banned in ("api_key", "authorization", "bearer "):
            if banned in self.body_json.lower():
                raise ValueError(
                    f"payload에 비밀정보로 보이는 문자열이 있다: {banned!r}"
                    " — planning/redaction.py를 거치지 않았다"
                )


class PlanningExecutionPath(str, Enum):
    """계획 생성 시도가 어떤 경로에서 났는가.

    평가 실행기는 같은 발화를 반복해서 돌리고 실패 사례를 일부러 포함한다.
    그 통계가 운영 요청 통계에 섞이면 실제 사용 성공률을 설명하지 못한다.
    """

    #: 실제 사용자 요청 경로.
    OPERATIONAL = "operational"
    #: 평가 실행기 경로. `evaluation_run_id`가 함께 채워진다.
    EVALUATION = "evaluation"


class ValidationStage(str, Enum):
    """계획 생성 응답이 통과해야 하는 검증 단계.

    구조화 출력을 써도 이 단계를 생략하지 않는다. 문법 제약은 필드 모양만
    보장하고, 리소스가 카탈로그에 있는지·스킬이 Profile에 있는지·계획이
    안전한지는 보장하지 않는다.
    """

    #: HTTP 호출과 모델 ID 대조.
    TRANSPORT = "transport"
    #: 응답 본문이 JSON으로 읽히는가(사고 과정 누출 검사 포함).
    JSON_PARSE = "json_parse"
    #: 출력 스키마 버전 일치.
    SCHEMA_VERSION = "schema_version"
    #: Pydantic strict 구조 검증.
    PYDANTIC_STRUCTURE = "pydantic_structure"
    #: core 계약·ResourceCatalog·SkillCatalog 의미 검증.
    CORE_SEMANTICS = "core_semantics"
    #: SafetyValidator 판정.
    SAFETY = "safety"
    #: ExecutionPermit 판정.
    PERMIT = "permit"


@dataclass(frozen=True)
class ValidationStageResult:
    stage: ValidationStage
    passed: bool
    reason_code: ReasonCode | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.stage, ValidationStage):
            raise ValueError(f"stage가 ValidationStage가 아니다: {self.stage!r}")
        if self.passed and self.reason_code is not None:
            raise ValueError(f"통과한 단계에 실패 사유({self.reason_code})가 붙어 있다")
        if not self.passed and self.reason_code is None:
            raise ValueError(f"실패한 단계({self.stage})에 이유 코드가 없다")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "passed": self.passed,
            "reason_code": None if self.reason_code is None else self.reason_code.value,
            "detail": self.detail,
        }

    @staticmethod
    def from_dict(row: Mapping[str, Any]) -> "ValidationStageResult":
        raw = row.get("reason_code")
        return ValidationStageResult(
            stage=ValidationStage(row["stage"]),
            passed=bool(row["passed"]),
            reason_code=None if raw is None else ReasonCode(raw),
            detail=row.get("detail", ""),
        )


@dataclass(frozen=True)
class PlanningAttemptRecord:
    """계획 생성 시도 1회. **append-only** — 덮어쓰지 않는다.

    같은 `request_id`의 재시도는 새 `planning_attempt_id`를 받고, 이전 시도는
    `previous_attempt_id`로 이어진다. 어떤 시도가 어떤 계획을 만들었는지는
    `plan_id`로 추적한다.

    담지 않는 것: API Key·토큰·인증 헤더·환경변수. `payload`는
    `planning/redaction.py`를 지난 것만 들어온다.
    """

    planning_attempt_id: str
    request_id: str
    attempt_no: int
    schema_version: str
    #: 카탈로그 버전. 같은 발화가 다른 카탈로그에서 다르게 해석되는 것을 추적한다.
    resource_catalog_version: str
    skill_catalog_version: str
    #: 시작·종료 UTC epoch 초와 처리 시간.
    started_at: float
    ended_at: float
    duration_ms: int
    status: PlanningAttemptStatus
    #: 공급자·모델·프롬프트·출력 스키마 식별자. STOP 우회는 모델을 부르지
    #: 않으므로 None이다.
    provider_id: str | None = None
    model_id: str | None = None
    prompt_template_version: str | None = None
    output_schema_version: str | None = None
    reason_code: ReasonCode | None = None
    detail: str = ""
    plan_id: str | None = None
    plan_hash: str | None = None
    #: Mock Provider의 결과인가. 실제 LLM 성공·운영 검증으로 읽지 않기 위해 남긴다.
    is_mock: bool = False
    is_retry: bool = False
    previous_attempt_id: str | None = None
    #: 이 계획의 입력이 된 STT 실행 기록. 텍스트 입력이면 None이다.
    stt_inference_id: str | None = None
    payload: PlanningPayload | None = None

    # ── 실제 호출 관측값 (5-02). Mock은 채우지 않는다. ──────────────────
    #: 서버가 실제로 제공한다고 알려준 모델 ID. 설정값이 아니라 관측값이다.
    served_model_id: str | None = None
    #: 추론 서버 버전(예: vLLM 0.28.0).
    server_version: str | None = None
    quantization: str | None = None
    #: 구조화 출력을 썼는가. 썼다고 검증을 생략하지 않는다.
    structured_output: bool | None = None
    #: thinking 모드(off / separate_channel).
    thinking_mode: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    #: 검증 단계별 결과. 통과한 단계와 막힌 단계를 함께 남긴다.
    validation_stages: tuple[ValidationStageResult, ...] = ()

    # ── 평가 실행 구분 (5-03) ───────────────────────────────────────────
    #: 운영 요청인가 평가 실행인가. None이면 이 컬럼이 생기기 전의 기록이다.
    execution_path: PlanningExecutionPath | None = None
    #: 평가 실행 묶음 식별자. 같은 실행의 시도들을 한데 모은다.
    evaluation_run_id: str | None = None
    #: 기준선인가 개선안인가(예: baseline / candidate). 프롬프트 버전별 비교의 키다.
    evaluation_label: str | None = None
    #: 평가 사례 식별자. 같은 사례를 프롬프트 버전 간에 맞춰 보기 위해 남긴다.
    evaluation_case_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("planning_attempt_id", "request_id", "schema_version",
                     "resource_catalog_version", "skill_catalog_version"):
            if not getattr(self, name):
                raise ValueError(f"{name}이 비어 있다")
        if self.attempt_no < 1:
            raise ValueError(f"attempt_no는 1부터다: {self.attempt_no}")
        if not isinstance(self.status, PlanningAttemptStatus):
            raise ValueError(f"status가 PlanningAttemptStatus가 아니다: {self.status!r}")
        if self.duration_ms < 0:
            raise ValueError("duration_ms가 음수다")
        if self.ended_at < self.started_at:
            raise ValueError("ended_at이 started_at보다 이르다")
        if self.status is PlanningAttemptStatus.SUCCEEDED:
            if not self.plan_id or not self.plan_hash:
                raise ValueError("성공한 시도는 plan_id와 plan_hash가 있어야 한다")
            if self.reason_code is not None:
                raise ValueError(
                    f"성공한 시도에 실패 사유({self.reason_code})가 붙어 있다"
                )
            for name in ("provider_id", "model_id", "prompt_template_version",
                         "output_schema_version"):
                if not getattr(self, name):
                    raise ValueError(f"성공한 시도에 {name}이 없다")
        if self.status is PlanningAttemptStatus.FAILED and self.reason_code is None:
            raise ValueError("실패한 시도는 이유 코드가 있어야 한다")
        if self.status is PlanningAttemptStatus.STOP_BYPASS:
            if self.provider_id is not None or self.model_id is not None:
                raise ValueError(
                    "STOP 우회는 모델을 호출하지 않는다 — 공급자·모델 식별자를 남기지 않는다"
                )
            if self.is_mock:
                raise ValueError("STOP 우회는 Mock 여부와 무관하다")
        if self.is_retry and not self.previous_attempt_id:
            raise ValueError("재시도는 이전 시도 식별자가 있어야 한다")
        if self.previous_attempt_id == self.planning_attempt_id:
            raise ValueError("이전 시도가 자기 자신이다")
        for name in ("prompt_tokens", "completion_tokens"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name}이 음수다")
        if self.is_mock and self.served_model_id is not None:
            raise ValueError(
                "Mock 결과에 실제 서버 모델 ID를 남기지 않는다"
                " — 실제 호출과 섞이면 집계가 무의미해진다"
            )
        if self.execution_path is not None and not isinstance(
            self.execution_path, PlanningExecutionPath
        ):
            raise ValueError(
                f"execution_path가 PlanningExecutionPath가 아니다: {self.execution_path!r}"
            )
        if self.evaluation_run_id is not None:
            if self.execution_path is not PlanningExecutionPath.EVALUATION:
                raise ValueError(
                    "evaluation_run_id가 있는 기록은 평가 경로여야 한다"
                    " — 평가 결과가 운영 통계에 섞이지 않게 한다"
                )
        if self.evaluation_label is not None and self.evaluation_run_id is None:
            raise ValueError("evaluation_label만 있고 evaluation_run_id가 없다")
        seen_stages = [r.stage for r in self.validation_stages]
        if len(seen_stages) != len(set(seen_stages)):
            raise ValueError(f"같은 검증 단계가 두 번 기록됐다: {seen_stages}")


class SessionStatus(str, Enum):
    """세션 상태.

    만료와 종료를 구분한다 — 사용자가 끝낸 것과 유휴로 만료된 것은 다른 사실이고,
    둘 다 "현재 계획"으로 복원되지 않아야 한다.
    """

    ACTIVE = "active"
    #: 사용자가 끝냈다.
    ENDED = "ended"
    #: 유휴 시간을 넘겨 만료됐다.
    EXPIRED = "expired"


@dataclass(frozen=True)
class SessionRecord:
    """세션 하나. 브라우저 탭·스크립트 실행 단위다.

    **인증이 아니다.** 세션은 작업 묶음을 구분하는 식별자이며, 현재 구성은
    localhost 전용이고 사용자 계정이 없다(`md/웹UI_구조.md`의 제한 절).
    """

    session_id: str
    created_at: float
    last_seen_at: float
    status: SessionStatus
    schema_version: str
    ended_at: float | None = None
    origin: str = ""

    def __post_init__(self) -> None:
        for name in ("session_id", "schema_version"):
            if not getattr(self, name):
                raise ValueError(f"{name}이 비어 있다")
        if not isinstance(self.status, SessionStatus):
            raise ValueError(f"status가 SessionStatus가 아니다: {self.status!r}")
        if self.last_seen_at < self.created_at:
            raise ValueError("last_seen_at이 created_at보다 이르다")
        if self.ended_at is not None and self.ended_at < self.created_at:
            raise ValueError("ended_at이 created_at보다 이르다")
        if self.status is SessionStatus.ACTIVE and self.ended_at is not None:
            raise ValueError("active 세션에 종료 시각이 있다")
        if self.status is not SessionStatus.ACTIVE and self.ended_at is None:
            raise ValueError(f"{self.status.value} 세션에 종료 시각이 없다")

    @property
    def usable(self) -> bool:
        return self.status is SessionStatus.ACTIVE


class ApprovalDecision(str, Enum):
    """작업자 승인 결정."""

    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ApprovalRecord:
    """작업자 승인·거부 1회. **append-only** — 재승인이 기존 기록을 덮지 않는다.

    같은 계획을 다시 승인하면 새 `approval_id`로 쌓인다. 누가 언제 무엇을
    보고 결정했는지가 사후에 바뀌면 감사 기록이 아니다.

    계획이 만들어진 근거(`planning_attempt_id`)와 요청↔계획 리소스 일치 검증
    결과를 함께 담는다. 승인 시점에 사용자가 무엇을 보고 눌렀는지 남긴다.
    """

    approval_id: str
    plan_id: str
    plan_hash: str
    request_id: str
    decision: ApprovalDecision
    #: 결정 UTC epoch 초.
    decided_at: float
    schema_version: str
    #: 이 계획을 만든 시도. 텍스트 경로여도 채운다.
    planning_attempt_id: str | None = None
    #: 이 결정을 내린 세션. 다른 세션의 승인으로 실행할 수 없게 하는 근거다.
    session_id: str | None = None
    #: 요청↔계획 리소스 일치 검증 상태(`ConsistencyStatus` 값).
    consistency_status: str | None = None
    consistency_reason_code: ReasonCode | None = None
    #: 승인 시점에 화면에 있던 로봇·Profile. 승인 요청이 보낸 값을 저장된
    #: 계획과 대조해 일치한 경우에만 채운다(마이그레이션 0010).
    robot_id: str | None = None
    profile_id: str | None = None
    profile_version: str | None = None
    #: 승인을 검증 당시 조건에 묶는 값들(마이그레이션 0012). 하나라도 달라지면
    #: 기존 승인을 재사용하지 않고 재검증·재승인을 받는다.
    policy_id: str | None = None
    policy_version: str | None = None
    snapshot_id: str | None = None
    snapshot_version: str | None = None
    snapshot_hash: str | None = None
    #: 이 승인이 근거로 삼은 검증 실행.
    validation_run_id: str | None = None
    #: 사용자가 남긴 메모나 거부 사유.
    note: str = ""

    def __post_init__(self) -> None:
        for name in ("approval_id", "plan_id", "plan_hash", "request_id",
                     "schema_version"):
            if not getattr(self, name):
                raise ValueError(f"{name}이 비어 있다")
        if not isinstance(self.decision, ApprovalDecision):
            raise ValueError(f"decision이 ApprovalDecision이 아니다: {self.decision!r}")
