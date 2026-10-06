"""시뮬레이션 전용 Profile — 실물 Profile과 **따로** 두는 Gazebo 근거 (8-08 이후).

실물 Profile(`config/profiles/*.json`)은 근거가 없는 값을 `null`로 둔다(정격 페이로드,
완전 닫힘 패드 간격, 장착 yaw). 이 모듈은 그 `null`을 채우지 않는다. 대신 **같은 기본
구성을 복사한 시뮬레이션 전용 구성**을 만들고, 그 사본에만 Gazebo 측정값을 넣는다.

지키는 것:

- 값마다 `Measured` 근거가 붙는다. 출처 종류는 `simulation_measurement`만 받는다 —
  데이터시트·실측으로 위장하지 않는다.
- `environment`는 항상 `simulation`, `real_hardware_claim`은 항상 False다. 둘 중 하나라도
  다르면 읽지 않는다(승격 금지, CODE_RULES 9번).
- 적용 대상(`applies_to`)이 시뮬레이션 셀이 아니면 쓰지 않는다. 판정은 호출자가 하되
  필요한 사실(`is_simulated`)을 여기서 강제한다.
- 적재 한계는 **반복 이송으로 확인된 자재 질량의 최댓값**이다. 제조사 정격이 아니다.
- 만든 구성은 `verified=False`다. 시뮬레이션 근거로 구성 전체를 검증됨으로 올리지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from core.provenance import Measured, Provenance, VerificationStatus
from core.reason_codes import ReasonCode
from core.robot_profile import CompositeRobotProfile, Environment, RobotProfileError

SCHEMA = "forstick2.simulation_profile/1"
#: 시뮬레이션 근거로 받는 출처 종류. 다른 종류는 실물 근거로 오해될 수 있어 받지 않는다.
SIMULATION_SOURCE_KIND = "simulation_measurement"
#: 이 Profile이 채울 수 있는 항목. 나머지는 실물 Profile 값을 그대로 쓴다.
FILLABLE = ("arm.payload", "gripper.pad_aperture_closed", "mounting.rpy.yaw")
#: 버전 문자열에 붙여 실물 구성과 섞이지 않게 한다.
VERSION_MARK = "+sim"


@dataclass(frozen=True)
class SimulationProfile:
    sim_profile_id: str
    sim_profile_version: str
    base: Mapping[str, str]
    applies_to: Mapping[str, Any]
    values: Mapping[str, Measured]
    #: 이송이 확인된 자재 → 질량(kg). 적재 판정의 유일한 근거.
    verified_materials_kg: Mapping[str, float]
    max_payload_kg: float
    gripper_max_effort: Measured
    coupling_model_mass_kg: float | None
    grasp_observation: Mapping[str, Any] = field(default_factory=dict)
    revalidation: Mapping[str, Any] = field(default_factory=dict)
    evidence_report: str = ""

    def payload_allows(self, material: str, mass_kg: float | None) -> tuple[bool, str]:
        """그 자재를 들어도 되는가. 질량을 모르거나 확인 범위 밖이면 False."""
        if mass_kg is None:
            return False, f"{material}의 질량이 선언되지 않았다 — 적재 한계와 비교할 수 없다"
        if mass_kg > self.max_payload_kg:
            return False, (f"{material} {mass_kg} kg > 시뮬레이션 적재 한계"
                           f" {self.max_payload_kg} kg(이송으로 확인한 자재 질량의 최댓값)")
        return True, f"{material} {mass_kg} kg ≤ {self.max_payload_kg} kg"

    def to_dict(self) -> dict:
        return {
            "sim_profile_id": self.sim_profile_id,
            "sim_profile_version": self.sim_profile_version,
            "environment": Environment.SIMULATION.value,
            "real_hardware_claim": False,
            "base": dict(self.base), "applies_to": dict(self.applies_to),
            "values": {k: v.to_dict() for k, v in self.values.items()},
            "payload_scope": {"verified_materials_kg": dict(self.verified_materials_kg),
                              "max_kg": self.max_payload_kg},
            "gripper_max_effort": self.gripper_max_effort.to_dict(),
            "coupling_model_mass_kg": self.coupling_model_mass_kg,
            "grasp_observation": dict(self.grasp_observation),
            "revalidation": dict(self.revalidation),
            "evidence_report": self.evidence_report,
        }


def _measured(raw: Mapping[str, Any], label: str, *, simulation_only: bool) -> Measured:
    prov = dict(raw.get("provenance") or {})
    try:
        status = VerificationStatus(str(prov.get("status", "")))
    except ValueError:
        raise RobotProfileError(ReasonCode.CONFIG_INVALID,
                                f"{label}: 낯선 확인 상태 {prov.get('status')!r}") from None
    if simulation_only and prov.get("source_kind") != SIMULATION_SOURCE_KIND:
        raise RobotProfileError(
            ReasonCode.CONFIG_INVALID,
            f"{label}: 출처 종류가 {SIMULATION_SOURCE_KIND}가 아니다"
            f" ({prov.get('source_kind')!r}) — 시뮬레이션 Profile에 다른 근거를 섞지 않는다")
    try:
        return Measured(
            value=None if raw.get("value") is None else float(raw["value"]),
            unit=str(raw.get("unit") or ""),
            provenance=Provenance(
                source_kind=str(prov.get("source_kind") or ""),
                source=str(prov.get("source") or ""),
                source_version=str(prov.get("source_version") or ""),
                source_commit=str(prov.get("source_commit") or ""),
                status=status, checked_at=float(prov.get("checked_at") or 0.0),
                note=str(prov.get("note") or "")))
    except Exception as exc:  # noqa: BLE001 — 근거 계약 위반은 설정 오류다
        raise RobotProfileError(ReasonCode.CONFIG_INVALID, f"{label}: {exc}") from None


def load_simulation_profile(payload: Mapping[str, Any]) -> SimulationProfile:
    """설정 dict → SimulationProfile. 계약을 어기면 읽지 않는다."""
    if payload.get("schema") != SCHEMA:
        raise RobotProfileError(ReasonCode.CONFIG_INVALID,
                                f"시뮬레이션 Profile 스키마가 아니다: {payload.get('schema')!r}")
    if payload.get("environment") != Environment.SIMULATION.value:
        raise RobotProfileError(ReasonCode.CONFIG_INVALID,
                                "environment가 simulation이 아니다 — 시뮬레이션 근거를 실물로 쓰지 않는다")
    if payload.get("real_hardware_claim") is not False:
        raise RobotProfileError(ReasonCode.CONFIG_INVALID,
                                "real_hardware_claim이 false가 아니다 — 시뮬레이션 근거를 승격하지 않는다")
    applies_to = dict(payload.get("applies_to") or {})
    if applies_to.get("is_simulated") is not True:
        raise RobotProfileError(ReasonCode.CONFIG_INVALID,
                                "applies_to.is_simulated가 true가 아니다")
    raw_values = dict(payload.get("values") or {})
    unknown = sorted(set(raw_values) - set(FILLABLE))
    if unknown:
        raise RobotProfileError(ReasonCode.CONFIG_INVALID,
                                f"시뮬레이션 Profile이 채울 수 없는 항목: {unknown}")
    values = {key: _measured(raw, key, simulation_only=True) for key, raw in raw_values.items()}
    for key, item in values.items():
        if not item.available:
            raise RobotProfileError(ReasonCode.CONFIG_MISSING, f"{key}: 값이 없다")
    scope = dict(payload.get("payload_scope") or {})
    verified = {str(k): float(v) for k, v in (scope.get("verified_materials_kg") or {}).items()}
    if not verified:
        raise RobotProfileError(ReasonCode.CONFIG_MISSING,
                                "payload_scope.verified_materials_kg가 비어 있다 — 적재 범위 근거가 없다")
    max_kg = float(scope.get("max_kg") or 0.0)
    if max_kg <= 0 or max_kg > max(verified.values()):
        raise RobotProfileError(
            ReasonCode.CONFIG_INVALID,
            f"payload_scope.max_kg({max_kg})가 확인된 자재 질량 최댓값"
            f"({max(verified.values())})을 넘거나 0 이하다")
    payload_value = values.get("arm.payload")
    if payload_value is not None and payload_value.value != max_kg:
        raise RobotProfileError(ReasonCode.CONFIG_INVALID,
                                "arm.payload가 payload_scope.max_kg와 다르다")
    coupling = dict(payload.get("coupling_model_mass") or {})
    return SimulationProfile(
        sim_profile_id=str(payload.get("sim_profile_id") or ""),
        sim_profile_version=str(payload.get("sim_profile_version") or ""),
        base={str(k): str(v) for k, v in (payload.get("base") or {}).items()},
        applies_to=applies_to, values=values, verified_materials_kg=verified,
        max_payload_kg=max_kg,
        gripper_max_effort=_measured(dict(payload.get("gripper_max_effort") or {}),
                                     "gripper_max_effort", simulation_only=False),
        coupling_model_mass_kg=(None if coupling.get("mass_kg") is None
                                else float(coupling["mass_kg"])),
        grasp_observation=dict(payload.get("grasp_observation") or {}),
        revalidation=dict(payload.get("revalidation") or {}),
        evidence_report=str(payload.get("evidence_report") or ""),
    )


def apply_to_composite(composite: CompositeRobotProfile,
                       sim: SimulationProfile) -> CompositeRobotProfile:
    """기본 구성의 **사본**에 시뮬레이션 값을 넣는다. 원본은 바뀌지 않는다(frozen)."""
    base = dict(sim.base)
    if base.get("composite") != composite.composite_profile_id:
        raise RobotProfileError(
            ReasonCode.ROBOT_PROFILE_MISMATCH,
            f"시뮬레이션 Profile의 기본 구성({base.get('composite')!r})이"
            f" {composite.composite_profile_id!r}가 아니다")
    if composite.gripper is None or composite.mounting is None:
        raise RobotProfileError(ReasonCode.CONFIG_MISSING,
                                "기본 구성에 그리퍼·장착 Profile이 없다")
    arm, gripper, mounting = composite.arm, composite.gripper, composite.mounting
    for label, have, want in (("arm", arm.arm_profile_id, base.get("arm")),
                              ("gripper", gripper.gripper_profile_id, base.get("gripper")),
                              ("mounting", mounting.mounting_profile_id, base.get("mounting"))):
        if have != want:
            raise RobotProfileError(ReasonCode.ROBOT_PROFILE_MISMATCH,
                                    f"{label} Profile {have!r} != 시뮬레이션 기본 {want!r}")
    values = sim.values
    if "arm.payload" in values:
        arm = replace(arm, payload=values["arm.payload"],
                      arm_profile_version=arm.arm_profile_version + VERSION_MARK)
    if "gripper.pad_aperture_closed" in values:
        gripper = replace(gripper, pad_aperture_closed=values["gripper.pad_aperture_closed"],
                          gripper_profile_version=gripper.gripper_profile_version + VERSION_MARK)
    if "mounting.rpy.yaw" in values:
        roll, pitch, _ = mounting.rpy
        mounting = replace(mounting, rpy=(roll, pitch, values["mounting.rpy.yaw"]),
                           mounting_profile_version=mounting.mounting_profile_version
                           + VERSION_MARK)
    return replace(
        composite, composite_profile_id=sim.sim_profile_id,
        composite_profile_version=sim.sim_profile_version,
        arm=arm, gripper=gripper, mounting=mounting,
        environment=Environment.SIMULATION, verified=False,
        notes=(f"{composite.composite_profile_id} {composite.composite_profile_version}의"
               " 시뮬레이션 전용 사본. Gazebo 측정 근거만 더했다 — 실물 근거가 아니다."))
