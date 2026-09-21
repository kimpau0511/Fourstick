"""설정 검증 경계 (Pydantic).

Capability Profile과 Policy는 제품 코드 밖(파일·DB·환경)에서 들어온다.
계획.md 27장은 이 값들을 코드에 두지 못하게 하고, 설정이 없으면 임의
기본값으로 실행하지 말고 차단하라고 요구한다. 이 모듈이 그 경계다.

Pydantic이 형태를, core 계약이 의미를 검증한다:
  Pydantic  — 필수 키, 타입, 미지의 필드 거부, 숫자 범위
  core 계약 — SI 단위, dof와 관절 수 일치, 지원 스킬, 그리퍼 방향·여유값,
              근거(provenance) 존재

기본값을 두지 않는 것이 핵심이다. 빠진 키는 Pydantic이 필수로 걸러내고,
근거 없는 수치는 core 계약이 걸러낸다.
"""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.boundary import BoundaryValidationError, FieldIssue, issues_from_pydantic
from core.capability_profile import (
    CapabilityProfile,
    GripperSpec,
    JointLimit,
    ProfileError,
)
from core.frames import FrameKind, Vector3
from core.policy import (
    FreshnessPolicy,
    PolicyError,
    SafetyPolicy,
    LlmProviderConfig,
    PlanningPolicy,
    StructuredOutputMode,
    SttModelConfig,
    SttPolicy,
    SttProfileCatalog,
    SttVerification,
    StopPolicy,
    ThinkingMode,
)
from core.asset_manifest import (
    AssetEntry,
    AssetKind,
    AssetManifest,
    AssetManifestError,
    LicenseStatus,
    Redistribution,
)
from core.provenance import Measured, Provenance, ProvenanceError, VerificationStatus
from core.reason_codes import ReasonCode
from core.robot_profile import (
    ArmCapabilityProfile,
    ArmJointSpec,
    CompositeRobotProfile,
    Environment,
    GripperCapabilityProfile,
    JointKind,
    MountingProfile,
    RobotProfileError,
)
from core.skill_catalog import (
    SkillCatalog,
    SkillCatalogError,
    SkillEntry,
)
from core.resource_catalog import (
    CatalogError,
    ResourceCatalog,
    ResourceEntry,
    ResourceKind,
)

# strict=True — server/schemas.py와 같은 이유. 설정 파일의 타입 오기를
# 조용히 교정하지 않고 드러낸다.
STRICT = ConfigDict(extra="forbid", strict=True)


class Vector3Model(BaseModel):
    model_config = STRICT
    x: float
    y: float
    z: float


class JointLimitModel(BaseModel):
    model_config = STRICT
    name: str = Field(min_length=1)
    lower: float
    upper: float
    max_velocity: float = Field(gt=0.0)
    unit: str = Field(min_length=1)
    kind: str = Field(min_length=1)


class GripperModel(BaseModel):
    model_config = STRICT
    joint_name: str = Field(min_length=1)
    open_position: float
    close_position: float
    unit: str = Field(min_length=1)
    grasp_aperture_m: float = Field(gt=0.0)
    max_effort: float = Field(gt=0.0)
    fully_open_margin: float = Field(gt=0.0)
    mimic_joint_name: str = ""


class CapabilityProfileModel(BaseModel):
    model_config = STRICT
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    dof: int = Field(gt=0)
    joint_limits: list[JointLimitModel] = Field(min_length=1)
    #: FrameKind 값("world"/"base"/...) -> 실제 프레임 이름
    frames: dict[str, str]
    tcp_offset: Vector3Model
    work_radius_m: float = Field(gt=0.0)
    payload_kg: float | None = Field(default=None, gt=0.0)
    supported_skills: list[str] = Field(min_length=1)
    gripper: GripperModel | None = None
    provenance: dict[str, str] = Field(default_factory=dict)
    extras: dict[str, Any] = Field(default_factory=dict)


class SafetyPolicyModel(BaseModel):
    model_config = STRICT
    policy_version: str = Field(min_length=1)
    max_steps: int = Field(gt=0)
    provenance: dict[str, str]
    #: 계획이 끝나야 하는 스킬. null이면 종료 스킬 제약이 없다.
    required_final_skill: str | None = None
    #: 위치 작업 스텝 직전에 있어야 하는 이동 스킬.
    approach_skill: str | None = None


class FreshnessPolicyModel(BaseModel):
    model_config = STRICT
    policy_version: str = Field(min_length=1)
    robot_state_max_age_sec: float = Field(gt=0.0)
    environment_max_age_sec: float = Field(gt=0.0)
    provenance: dict[str, str]


class StopPolicyModel(BaseModel):
    model_config = STRICT
    policy_version: str = Field(min_length=1)
    position_tolerance: dict[str, float] = Field(min_length=1)
    hold_sec: float = Field(gt=0.0)
    cancel_ack_timeout_sec: float = Field(gt=0.0)
    max_sample_gap_sec: float = Field(gt=0.0)
    provenance: dict[str, str]


def _shape(model: type[BaseModel], payload: dict[str, Any], what: str) -> BaseModel:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise BoundaryValidationError(
            ReasonCode.CONFIG_INVALID,
            (FieldIssue(what, "설정 형태 오류"),) + issues_from_pydantic(exc),
        ) from exc


def _meaning(build, what: str):
    try:
        return build()
    except (ProfileError, PolicyError, CatalogError, SkillCatalogError) as exc:
        raise BoundaryValidationError(exc.reason, [FieldIssue(what, str(exc))]) from exc


def load_capability_profile(payload: dict[str, Any]) -> CapabilityProfile:
    m: CapabilityProfileModel = _shape(CapabilityProfileModel, payload, "capability_profile")

    unknown_frames = sorted(set(m.frames) - {k.value for k in FrameKind})
    if unknown_frames:
        raise BoundaryValidationError(
            ReasonCode.PLAN_UNKNOWN_FRAME,
            [FieldIssue("frames", f"알 수 없는 프레임 역할: {unknown_frames}")],
        )

    return _meaning(
        lambda: CapabilityProfile(
            profile_id=m.profile_id,
            profile_version=m.profile_version,
            dof=m.dof,
            joint_limits=tuple(
                JointLimit(j.name, j.lower, j.upper, j.max_velocity, j.unit, j.kind)
                for j in m.joint_limits
            ),
            frames={FrameKind(k): v for k, v in m.frames.items()},
            tcp_offset=Vector3(m.tcp_offset.x, m.tcp_offset.y, m.tcp_offset.z),
            work_radius_m=m.work_radius_m,
            payload_kg=m.payload_kg,
            supported_skills=tuple(m.supported_skills),
            gripper=(
                GripperSpec(
                    joint_name=m.gripper.joint_name,
                    open_position=m.gripper.open_position,
                    close_position=m.gripper.close_position,
                    unit=m.gripper.unit,
                    grasp_aperture_m=m.gripper.grasp_aperture_m,
                    max_effort=m.gripper.max_effort,
                    fully_open_margin=m.gripper.fully_open_margin,
                    mimic_joint_name=m.gripper.mimic_joint_name,
                )
                if m.gripper is not None
                else None
            ),
            provenance=dict(m.provenance),
            extras=dict(m.extras),
        ),
        "capability_profile",
    )


def load_safety_policy(payload: dict[str, Any]) -> SafetyPolicy:
    m: SafetyPolicyModel = _shape(SafetyPolicyModel, payload, "safety_policy")
    return _meaning(
        lambda: SafetyPolicy(
            m.policy_version, m.max_steps, dict(m.provenance),
            required_final_skill=m.required_final_skill,
            approach_skill=m.approach_skill,
        ),
        "safety_policy",
    )


def load_freshness_policy(payload: dict[str, Any]) -> FreshnessPolicy:
    m: FreshnessPolicyModel = _shape(FreshnessPolicyModel, payload, "freshness_policy")
    return _meaning(
        lambda: FreshnessPolicy(
            m.policy_version,
            m.robot_state_max_age_sec,
            m.environment_max_age_sec,
            dict(m.provenance),
        ),
        "freshness_policy",
    )


def load_stop_policy(payload: dict[str, Any]) -> StopPolicy:
    m: StopPolicyModel = _shape(StopPolicyModel, payload, "stop_policy")
    return _meaning(
        lambda: StopPolicy(
            m.policy_version,
            dict(m.position_tolerance),
            m.hold_sec,
            m.cancel_ack_timeout_sec,
            m.max_sample_gap_sec,
            dict(m.provenance),
        ),
        "stop_policy",
    )


def config_json_schemas() -> dict[str, dict[str, Any]]:
    """설정 파일 작성자를 위한 JSON Schema 모음."""
    return {
        "capability_profile": CapabilityProfileModel.model_json_schema(),
        "safety_policy": SafetyPolicyModel.model_json_schema(),
        "freshness_policy": FreshnessPolicyModel.model_json_schema(),
        "stop_policy": StopPolicyModel.model_json_schema(),
        "stt_policy": SttPolicyModel.model_json_schema(),
        "stt_model_config": SttModelConfigModel.model_json_schema(),
        "stt_profile_catalog": SttProfileCatalogModel.model_json_schema(),
        "resource_catalog": ResourceCatalogModel.model_json_schema(),
        "skill_catalog": SkillCatalogModel.model_json_schema(),
        "planning_policy": PlanningPolicyModel.model_json_schema(),
        "llm_provider": LlmProviderModel.model_json_schema(),
    }


class SttPolicyModel(BaseModel):
    model_config = STRICT
    policy_version: str = Field(min_length=1)
    max_audio_sec: float = Field(gt=0.0)
    window_sec: float = Field(gt=0.0)
    window_stride_sec: float = Field(gt=0.0)
    silence_end_sec: float = Field(gt=0.0)
    speech_start_sec: float = Field(gt=0.0)
    min_final_confidence: float = Field(ge=0.0, le=1.0)
    stop_keywords: list[str] = Field(min_length=1)
    transcribe_deadline_sec: float = Field(gt=0.0)
    max_concurrent_transcriptions: int = Field(ge=1)
    provenance: dict[str, str]


def load_stt_policy(payload: dict[str, Any]) -> SttPolicy:
    m: SttPolicyModel = _shape(SttPolicyModel, payload, "stt_policy")
    return _meaning(
        lambda: SttPolicy(
            policy_version=m.policy_version,
            max_audio_sec=m.max_audio_sec,
            window_sec=m.window_sec,
            window_stride_sec=m.window_stride_sec,
            silence_end_sec=m.silence_end_sec,
            speech_start_sec=m.speech_start_sec,
            min_final_confidence=m.min_final_confidence,
            stop_keywords=tuple(m.stop_keywords),
            transcribe_deadline_sec=m.transcribe_deadline_sec,
            max_concurrent_transcriptions=m.max_concurrent_transcriptions,
            provenance=dict(m.provenance),
        ),
        "stt_policy",
    )


class SttModelConfigModel(BaseModel):
    model_config = STRICT
    config_version: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    # 문자열로 받아 의미 계층에서 열거형으로 바꾼다. 오타는 CONFIG_INVALID다.
    verification: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    device: str = Field(min_length=1)
    compute_type: str = Field(min_length=1)
    language: str = Field(min_length=2)
    sample_rate_hz: int = Field(gt=0)
    vad_threshold: float = Field(gt=0.0, lt=1.0)
    load_timeout_sec: float = Field(gt=0.0)
    transcribe_timeout_sec: float = Field(gt=0.0)
    cache_dir: str | None = None
    retain_eval_audio: bool = False
    eval_audio_dir: str | None = None
    provenance: dict[str, str]


def load_stt_model_config(payload: dict[str, Any]) -> SttModelConfig:
    m: SttModelConfigModel = _shape(SttModelConfigModel, payload, "stt_model_config")
    return _meaning(
        lambda: SttModelConfig(
            config_version=m.config_version,
            profile_id=m.profile_id,
            verification=_verification(m.verification),
            model_name=m.model_name,
            device=m.device,
            compute_type=m.compute_type,
            language=m.language,
            sample_rate_hz=m.sample_rate_hz,
            vad_threshold=m.vad_threshold,
            load_timeout_sec=m.load_timeout_sec,
            transcribe_timeout_sec=m.transcribe_timeout_sec,
            cache_dir=m.cache_dir,
            retain_eval_audio=m.retain_eval_audio,
            eval_audio_dir=m.eval_audio_dir,
            provenance=dict(m.provenance),
        ),
        "stt_model_config",
    )


def _verification(raw: str) -> SttVerification:
    try:
        return SttVerification(raw)
    except ValueError:
        allowed = ", ".join(v.value for v in SttVerification)
        raise PolicyError(
            ReasonCode.CONFIG_INVALID,
            f"verification이 {raw!r}다 — 허용: {allowed}",
        ) from None


class SttProfileCatalogModel(BaseModel):
    model_config = STRICT
    catalog_version: str = Field(min_length=1)
    default_profile_id: str = Field(min_length=1)
    profiles: list[SttModelConfigModel] = Field(min_length=1)


def load_stt_profile_catalog(payload: dict[str, Any]) -> SttProfileCatalog:
    """Profile 목록을 읽는다. 기본 Profile이 미검증이면 여기서 거부된다."""
    m: SttProfileCatalogModel = _shape(
        SttProfileCatalogModel, payload, "stt_profile_catalog"
    )
    return _meaning(
        lambda: SttProfileCatalog(
            catalog_version=m.catalog_version,
            default_profile_id=m.default_profile_id,
            profiles={
                p.profile_id: load_stt_model_config(p.model_dump())
                for p in m.profiles
            },
        ),
        "stt_profile_catalog",
    )


class ResourceEntryModel(BaseModel):
    model_config = STRICT
    resource_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    aliases: list[str] = Field(min_length=1)
    provenance: str = ""


class ResourceCatalogModel(BaseModel):
    model_config = STRICT
    catalog_version: str = Field(min_length=1)
    entries: list[ResourceEntryModel] = Field(min_length=1)


def load_resource_catalog(payload: dict[str, Any]) -> ResourceCatalog:
    """셀의 위치·자재 카탈로그를 읽는다 (3-03).

    별칭 충돌은 의미 계층에서 거부된다 — 실행 시점에 추측하지 않기 위해서다.
    """
    m: ResourceCatalogModel = _shape(ResourceCatalogModel, payload, "resource_catalog")

    def entry(row: ResourceEntryModel) -> ResourceEntry:
        try:
            kind = ResourceKind(row.kind)
        except ValueError:
            allowed = ", ".join(k.value for k in ResourceKind)
            raise CatalogError(
                ReasonCode.CONFIG_INVALID,
                f"{row.resource_id}의 kind가 {row.kind!r}다 — 허용: {allowed}",
            ) from None
        return ResourceEntry(
            resource_id=row.resource_id, kind=kind, display_name=row.display_name,
            aliases=tuple(row.aliases), provenance=row.provenance,
        )

    return _meaning(
        lambda: ResourceCatalog(
            catalog_version=m.catalog_version,
            entries=tuple(entry(r) for r in m.entries),
        ),
        "resource_catalog",
    )


class SkillEntryModel(BaseModel):
    model_config = STRICT
    skill: str = Field(min_length=1)
    description: str = Field(min_length=1)
    #: 인자 이름 -> 리소스 종류 문자열. 의미 계층에서 열거형으로 바꾼다.
    arg_kinds: dict[str, str] = Field(default_factory=dict)
    #: 선행·후속 조건(선택). 모델에게 계약을 문장으로 보여주기 위한 것이며
    #: 자동 삽입의 근거가 아니다.
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)


class SkillCatalogModel(BaseModel):
    model_config = STRICT
    catalog_version: str = Field(min_length=1)
    entries: list[SkillEntryModel] = Field(min_length=1)


def load_skill_catalog(payload: dict[str, Any]) -> SkillCatalog:
    """스킬 카탈로그를 읽는다.

    계약(`core/constants.py`)에 없는 스킬·인자는 의미 계층에서 거부된다 —
    카탈로그가 계약을 우회하는 경로가 되면 안 된다.
    """
    m: SkillCatalogModel = _shape(SkillCatalogModel, payload, "skill_catalog")

    def kind(raw: str, skill: str, arg: str) -> ResourceKind:
        try:
            return ResourceKind(raw)
        except ValueError:
            allowed = ", ".join(k.value for k in ResourceKind)
            raise SkillCatalogError(
                ReasonCode.CONFIG_INVALID,
                f"{skill}.{arg}의 종류가 {raw!r}다 — 허용: {allowed}",
            ) from None

    return _meaning(
        lambda: SkillCatalog(
            catalog_version=m.catalog_version,
            entries=tuple(
                SkillEntry(
                    skill=e.skill, description=e.description,
                    arg_kinds={
                        arg: kind(raw, e.skill, arg) for arg, raw in e.arg_kinds.items()
                    },
                    preconditions=tuple(e.preconditions),
                    postconditions=tuple(e.postconditions),
                )
                for e in m.entries
            ),
        ),
        "skill_catalog",
    )


class PlanningPolicyModel(BaseModel):
    model_config = STRICT
    policy_version: str = Field(min_length=1)
    max_attempts: int = Field(ge=1)
    request_timeout_sec: float = Field(gt=0.0)
    #: 재시도 대상 이유 코드 문자열. 의미 계층에서 열거형으로 바꾼다.
    retryable_reasons: list[str] = Field(default_factory=list)
    store_payloads: bool
    payload_max_chars: int = Field(ge=0)
    payload_retention_days: int = Field(ge=0)
    provenance: dict[str, str]


def load_planning_policy(payload: dict[str, Any]) -> PlanningPolicy:
    """계획 생성 재시도·제한시간·원본 보존 정책을 읽는다."""
    m: PlanningPolicyModel = _shape(PlanningPolicyModel, payload, "planning_policy")

    def reason(raw: str) -> ReasonCode:
        try:
            return ReasonCode(raw)
        except ValueError:
            raise PolicyError(
                ReasonCode.CONFIG_INVALID, f"알 수 없는 이유 코드: {raw!r}"
            ) from None

    return _meaning(
        lambda: PlanningPolicy(
            policy_version=m.policy_version,
            max_attempts=m.max_attempts,
            request_timeout_sec=m.request_timeout_sec,
            retryable_reasons=tuple(reason(r) for r in m.retryable_reasons),
            store_payloads=m.store_payloads,
            payload_max_chars=m.payload_max_chars,
            payload_retention_days=m.payload_retention_days,
            provenance=dict(m.provenance),
        ),
        "planning_policy",
    )


class LlmProviderModel(BaseModel):
    model_config = STRICT
    config_version: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    quantization: str = Field(min_length=1)
    max_model_len: int = Field(gt=0)
    max_tokens: int = Field(gt=0)
    request_timeout_sec: float = Field(gt=0.0)
    temperature: float = Field(ge=0.0, le=2.0)
    top_p: float = Field(gt=0.0, le=1.0)
    seed: int | None = None
    thinking_mode: str = Field(min_length=1)
    structured_output: str = Field(min_length=1)
    api_key_env: str | None = None
    verify_model_id: bool = True
    provenance: dict[str, str]


def load_llm_provider_config(payload: dict[str, Any]) -> LlmProviderConfig:
    """LLM 공급자 접속 설정을 읽는다.

    **API Key를 받지 않는다.** 키가 들어 있는 환경변수 이름(`api_key_env`)만
    받는다. 설정 파일에 키를 적는 경로를 만들지 않는다.
    """
    m: LlmProviderModel = _shape(LlmProviderModel, payload, "llm_provider")

    def thinking(raw: str) -> ThinkingMode:
        try:
            return ThinkingMode(raw)
        except ValueError:
            allowed = ", ".join(v.value for v in ThinkingMode)
            raise PolicyError(
                ReasonCode.CONFIG_INVALID,
                f"thinking_mode가 {raw!r}다 — 허용: {allowed}",
            ) from None

    def structured(raw: str) -> StructuredOutputMode:
        try:
            return StructuredOutputMode(raw)
        except ValueError:
            allowed = ", ".join(v.value for v in StructuredOutputMode)
            raise PolicyError(
                ReasonCode.CONFIG_INVALID,
                f"structured_output이 {raw!r}다 — 허용: {allowed}",
            ) from None

    return _meaning(
        lambda: LlmProviderConfig(
            config_version=m.config_version, base_url=m.base_url,
            model_id=m.model_id, quantization=m.quantization,
            max_model_len=m.max_model_len, max_tokens=m.max_tokens,
            request_timeout_sec=m.request_timeout_sec,
            temperature=m.temperature, top_p=m.top_p, seed=m.seed,
            thinking_mode=thinking(m.thinking_mode),
            structured_output=structured(m.structured_output),
            api_key_env=m.api_key_env, verify_model_id=m.verify_model_id,
            provenance=dict(m.provenance),
        ),
        "llm_provider",
    )


# ── 8단계: 조합형 로봇 Profile과 자산 manifest ──────────────────────────
class ProvenanceModel(BaseModel):
    model_config = STRICT

    source_kind: str
    source: str
    source_version: str = ""
    source_commit: str = ""
    status: str
    checked_at: float
    note: str = ""


class MeasuredModel(BaseModel):
    model_config = STRICT

    value: float | None
    unit: str
    provenance: ProvenanceModel


class ArmJointModel(BaseModel):
    model_config = STRICT

    name: str
    kind: str
    lower: MeasuredModel
    upper: MeasuredModel
    max_velocity: MeasuredModel
    max_acceleration: MeasuredModel


class ArmProfileModel(BaseModel):
    model_config = STRICT

    arm_profile_id: str
    arm_profile_version: str
    display_name: str
    joints: list[ArmJointModel] = Field(min_length=1)
    payload: MeasuredModel
    reach: MeasuredModel
    frames: dict[str, str]
    controller_requirement: str
    declared_skills: list[str]
    state_max_age: MeasuredModel
    connect_timeout: MeasuredModel
    environment: str
    verified: bool
    unverified_items: list[str] = Field(default_factory=list)
    notes: str = ""


class GripperProfileModel(BaseModel):
    model_config = STRICT

    gripper_profile_id: str
    gripper_profile_version: str
    display_name: str
    two_finger_parallel: bool
    command_joint: str
    mimic_joints: dict[str, float]
    open_position: MeasuredModel
    closed_position: MeasuredModel
    joint_lower: MeasuredModel
    joint_upper: MeasuredModel
    max_velocity: MeasuredModel
    max_effort: MeasuredModel
    pad_aperture_open: MeasuredModel
    pad_aperture_closed: MeasuredModel
    fully_open_margin: MeasuredModel
    command_interfaces: list[str] = Field(default_factory=list)
    state_interfaces: list[str] = Field(default_factory=list)
    object_detection_real: bool
    object_detection_simulation: bool
    activation_interfaces: list[str] = Field(default_factory=list)
    fault_interfaces: list[str] = Field(default_factory=list)
    command_timeout: MeasuredModel | None = None
    base_frame: str = ""
    selection_status: str = "candidate"
    max_finger_speed: MeasuredModel | None = None
    max_grasp_force: MeasuredModel | None = None
    aperture_travel: MeasuredModel | None = None
    tip_separation_open: MeasuredModel | None = None
    tip_separation_closed: MeasuredModel | None = None
    mass: MeasuredModel | None = None
    min_grasp_force: MeasuredModel | None = None
    min_finger_speed: MeasuredModel | None = None
    rated_payload: MeasuredModel | None = None
    model_tolerance: dict[str, Any] = Field(default_factory=dict)
    bus_semantics: dict[str, str] = Field(default_factory=dict)
    bus_defaults: dict[str, str] = Field(default_factory=dict)
    object_detection_states: list[str] = Field(default_factory=list)
    environment: str
    verified: bool
    unverified_items: list[str] = Field(default_factory=list)
    notes: str = ""


class MountingProfileModel(BaseModel):
    model_config = STRICT

    mounting_profile_id: str
    mounting_profile_version: str
    arm_flange_frame: str
    gripper_base_frame: str
    xyz: list[MeasuredModel] = Field(min_length=3, max_length=3)
    rpy: list[MeasuredModel] = Field(min_length=3, max_length=3)
    adapter_plate_id: str = ""
    coupling: dict[str, Any] = Field(default_factory=dict)
    gripper_official: dict[str, Any] = Field(default_factory=dict)
    comparison: dict[str, Any] = Field(default_factory=dict)
    transform_derivation: dict[str, Any] = Field(default_factory=dict)
    tcp_frame: str = ""
    tcp_xyz: list[MeasuredModel] | None = None
    environment: str
    verified: bool
    unverified_items: list[str] = Field(default_factory=list)
    notes: str = ""


class CompositeProfileModel(BaseModel):
    model_config = STRICT

    composite_profile_id: str
    composite_profile_version: str
    arm_profile: str
    gripper_profile: str | None = None
    mounting_profile: str | None = None
    asset_manifest_version: str
    environment: str
    verified: bool
    notes: str = ""


class AssetEntryModel(BaseModel):
    model_config = STRICT

    asset_id: str
    kind: str
    source: str
    path: str = ""
    version: str = ""
    commit: str = ""
    checksum: str = ""
    license_spdx: str = ""
    license_status: str
    redistribution: str
    local_path: str = ""
    usage: str = ""
    note: str = ""


class AssetManifestModel(BaseModel):
    model_config = STRICT

    manifest_version: str
    note: str = ""
    assets: list[AssetEntryModel]


def _measured(model: MeasuredModel, label: str) -> Measured:
    try:
        status = VerificationStatus(model.provenance.status)
    except ValueError:
        raise BoundaryValidationError(
            ReasonCode.CONFIG_INVALID,
            [FieldIssue(f"{label}.provenance.status", f"낯선 확인 상태 ({model.provenance.status!r})")],
        ) from None
    try:
        return Measured(
            value=model.value, unit=model.unit,
            provenance=Provenance(
                source_kind=model.provenance.source_kind,
                source=model.provenance.source,
                source_version=model.provenance.source_version,
                source_commit=model.provenance.source_commit,
                status=status, checked_at=model.provenance.checked_at,
                note=model.provenance.note,
            ),
        )
    except ProvenanceError as exc:
        raise BoundaryValidationError(
            ReasonCode.CONFIG_INVALID, [FieldIssue(label, str(exc))]
        ) from None


def _parse(model_cls, payload: dict[str, Any], label: str):
    try:
        return model_cls.model_validate(payload)
    except ValidationError as exc:
        raise BoundaryValidationError(
            ReasonCode.CONFIG_INVALID, issues_from_pydantic(exc)
        ) from None


def load_arm_profile(payload: dict[str, Any]) -> ArmCapabilityProfile:
    model = _parse(ArmProfileModel, payload, "arm_profile")
    joints = []
    for index, joint in enumerate(model.joints):
        try:
            kind = JointKind(joint.kind)
        except ValueError:
            raise BoundaryValidationError(
                ReasonCode.CONFIG_INVALID,
                [FieldIssue(f"joints[{index}].kind", f"낯선 관절 종류 ({joint.kind!r})")],
            ) from None
        joints.append(ArmJointSpec(
            name=joint.name, kind=kind,
            lower=_measured(joint.lower, f"joints[{index}].lower"),
            upper=_measured(joint.upper, f"joints[{index}].upper"),
            max_velocity=_measured(joint.max_velocity, f"joints[{index}].max_velocity"),
            max_acceleration=_measured(
                joint.max_acceleration, f"joints[{index}].max_acceleration"
            ),
        ))
    frames: dict[FrameKind, str] = {}
    for key, name in model.frames.items():
        try:
            frames[FrameKind(key)] = name
        except ValueError:
            raise BoundaryValidationError(
                ReasonCode.CONFIG_INVALID,
                [FieldIssue(f"frames.{key}", f"낯선 프레임 역할 ({key!r})")],
            ) from None
    try:
        return ArmCapabilityProfile(
            arm_profile_id=model.arm_profile_id,
            arm_profile_version=model.arm_profile_version,
            display_name=model.display_name, joints=tuple(joints),
            payload=_measured(model.payload, "payload"),
            reach=_measured(model.reach, "reach"),
            frames=frames, controller_requirement=model.controller_requirement,
            declared_skills=tuple(model.declared_skills),
            state_max_age=_measured(model.state_max_age, "state_max_age"),
            connect_timeout=_measured(model.connect_timeout, "connect_timeout"),
            environment=Environment(model.environment), verified=model.verified,
            unverified_items=tuple(model.unverified_items), notes=model.notes,
        )
    except (RobotProfileError, ValueError) as exc:
        raise BoundaryValidationError(
            ReasonCode.CONFIG_INVALID, [FieldIssue("arm_profile", str(exc))]
        ) from None


def load_gripper_profile(payload: dict[str, Any]) -> GripperCapabilityProfile:
    model = _parse(GripperProfileModel, payload, "gripper_profile")
    try:
        return GripperCapabilityProfile(
            gripper_profile_id=model.gripper_profile_id,
            gripper_profile_version=model.gripper_profile_version,
            display_name=model.display_name,
            two_finger_parallel=model.two_finger_parallel,
            command_joint=model.command_joint,
            mimic_joints=dict(model.mimic_joints),
            open_position=_measured(model.open_position, "open_position"),
            closed_position=_measured(model.closed_position, "closed_position"),
            joint_lower=_measured(model.joint_lower, "joint_lower"),
            joint_upper=_measured(model.joint_upper, "joint_upper"),
            max_velocity=_measured(model.max_velocity, "max_velocity"),
            max_effort=_measured(model.max_effort, "max_effort"),
            pad_aperture_open=_measured(model.pad_aperture_open, "pad_aperture_open"),
            pad_aperture_closed=_measured(
                model.pad_aperture_closed, "pad_aperture_closed"
            ),
            fully_open_margin=_measured(model.fully_open_margin, "fully_open_margin"),
            command_interfaces=tuple(model.command_interfaces),
            state_interfaces=tuple(model.state_interfaces),
            object_detection_real=model.object_detection_real,
            object_detection_simulation=model.object_detection_simulation,
            activation_interfaces=tuple(model.activation_interfaces),
            fault_interfaces=tuple(model.fault_interfaces),
            command_timeout=(
                None if model.command_timeout is None
                else _measured(model.command_timeout, "command_timeout")
            ),
            base_frame=model.base_frame,
            selection_status=model.selection_status,
            max_finger_speed=(
                None if model.max_finger_speed is None
                else _measured(model.max_finger_speed, "max_finger_speed")
            ),
            max_grasp_force=(
                None if model.max_grasp_force is None
                else _measured(model.max_grasp_force, "max_grasp_force")
            ),
            aperture_travel=(
                None if model.aperture_travel is None
                else _measured(model.aperture_travel, "aperture_travel")
            ),
            tip_separation_open=(
                None if model.tip_separation_open is None
                else _measured(model.tip_separation_open, "tip_separation_open")
            ),
            tip_separation_closed=(
                None if model.tip_separation_closed is None
                else _measured(model.tip_separation_closed, "tip_separation_closed")
            ),
            mass=(
                None if model.mass is None else _measured(model.mass, "mass")
            ),
            min_grasp_force=(
                None if model.min_grasp_force is None
                else _measured(model.min_grasp_force, "min_grasp_force")
            ),
            min_finger_speed=(
                None if model.min_finger_speed is None
                else _measured(model.min_finger_speed, "min_finger_speed")
            ),
            rated_payload=(
                None if model.rated_payload is None
                else _measured(model.rated_payload, "rated_payload")
            ),
            model_tolerance=dict(model.model_tolerance),
            bus_semantics=dict(model.bus_semantics),
            bus_defaults=dict(model.bus_defaults),
            object_detection_states=tuple(model.object_detection_states),
            environment=Environment(model.environment), verified=model.verified,
            unverified_items=tuple(model.unverified_items), notes=model.notes,
        )
    except (RobotProfileError, ValueError) as exc:
        raise BoundaryValidationError(
            ReasonCode.CONFIG_INVALID, [FieldIssue("gripper_profile", str(exc))]
        ) from None


def load_mounting_profile(payload: dict[str, Any]) -> MountingProfile:
    model = _parse(MountingProfileModel, payload, "mounting_profile")
    try:
        return MountingProfile(
            mounting_profile_id=model.mounting_profile_id,
            mounting_profile_version=model.mounting_profile_version,
            arm_flange_frame=model.arm_flange_frame,
            gripper_base_frame=model.gripper_base_frame,
            xyz=tuple(_measured(v, f"xyz[{i}]") for i, v in enumerate(model.xyz)),
            rpy=tuple(_measured(v, f"rpy[{i}]") for i, v in enumerate(model.rpy)),
            adapter_plate_id=model.adapter_plate_id,
            coupling=dict(model.coupling),
            gripper_official=dict(model.gripper_official),
            comparison=dict(model.comparison),
            transform_derivation=dict(model.transform_derivation),
            tcp_frame=model.tcp_frame,
            tcp_xyz=(
                None if model.tcp_xyz is None
                else tuple(
                    _measured(v, f"tcp_xyz[{i}]") for i, v in enumerate(model.tcp_xyz)
                )
            ),
            environment=Environment(model.environment), verified=model.verified,
            unverified_items=tuple(model.unverified_items), notes=model.notes,
        )
    except (RobotProfileError, ValueError) as exc:
        raise BoundaryValidationError(
            ReasonCode.CONFIG_INVALID, [FieldIssue("mounting_profile", str(exc))]
        ) from None


def load_composite_profile(
    payload: dict[str, Any], *,
    arms: Mapping[str, ArmCapabilityProfile],
    grippers: Mapping[str, GripperCapabilityProfile],
    mountings: Mapping[str, MountingProfile],
) -> CompositeRobotProfile:
    """구성 Profile을 조립한다. 참조하는 Profile이 없으면 막는다."""
    model = _parse(CompositeProfileModel, payload, "composite_profile")
    arm = arms.get(model.arm_profile)
    if arm is None:
        raise BoundaryValidationError(
            ReasonCode.CONFIG_MISSING,
            [FieldIssue("arm_profile", f"등록되지 않은 팔 Profile ({model.arm_profile!r})")],
        )
    gripper = None if model.gripper_profile is None else grippers.get(
        model.gripper_profile
    )
    if model.gripper_profile is not None and gripper is None:
        raise BoundaryValidationError(
            ReasonCode.CONFIG_MISSING,
            [FieldIssue("gripper_profile", f"등록되지 않은 그리퍼 Profile ({model.gripper_profile!r})")],
        )
    mounting = None if model.mounting_profile is None else mountings.get(
        model.mounting_profile
    )
    if model.mounting_profile is not None and mounting is None:
        raise BoundaryValidationError(
            ReasonCode.CONFIG_MISSING,
            [FieldIssue("mounting_profile", f"등록되지 않은 장착 Profile ({model.mounting_profile!r})")],
        )
    try:
        return CompositeRobotProfile(
            composite_profile_id=model.composite_profile_id,
            composite_profile_version=model.composite_profile_version,
            arm=arm, gripper=gripper, mounting=mounting,
            asset_manifest_version=model.asset_manifest_version,
            environment=Environment(model.environment), verified=model.verified,
            notes=model.notes,
        )
    except (RobotProfileError, ValueError) as exc:
        raise BoundaryValidationError(
            ReasonCode.CONFIG_INVALID, [FieldIssue("composite_profile", str(exc))]
        ) from None


def load_asset_manifest(payload: dict[str, Any]) -> AssetManifest:
    model = _parse(AssetManifestModel, payload, "asset_manifest")
    entries = []
    for index, asset in enumerate(model.assets):
        try:
            entries.append(AssetEntry(
                asset_id=asset.asset_id, kind=AssetKind(asset.kind),
                source=asset.source, path=asset.path, version=asset.version,
                commit=asset.commit, checksum=asset.checksum,
                license_spdx=asset.license_spdx,
                license_status=LicenseStatus(asset.license_status),
                redistribution=Redistribution(asset.redistribution),
                local_path=asset.local_path, usage=asset.usage, note=asset.note,
            ))
        except (AssetManifestError, ValueError) as exc:
            raise BoundaryValidationError(
                ReasonCode.CONFIG_INVALID,
                [FieldIssue(f"assets[{index}]", str(exc))],
            ) from None
    try:
        return AssetManifest(
            manifest_version=model.manifest_version, entries=tuple(entries),
            note=model.note,
        )
    except AssetManifestError as exc:
        raise BoundaryValidationError(
            ReasonCode.CONFIG_INVALID, [FieldIssue("asset_manifest", str(exc))]
        ) from None
