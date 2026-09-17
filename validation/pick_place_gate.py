"""pick·place 활성화 관문 (md/개발플랜.md 8-08·8-09).

사용자가 정한 7개 조건이 **모두 충족된 경우에만** pick·place를 지원 스킬로
연다. 하나라도 근거가 없으면 열지 않고, 무엇이 왜 막혔는지 이유 코드와 함께
돌려준다.

```
1. 장착 transform 근거 확보        MountingProfile의 xyz·rpy가 모두 값과 근거를 갖는다
2. adapter·gripper 질량/관성 확보  두 값이 verified이고 물리적으로 타당하다
3. 충돌 형상 반영                  adapter·gripper 충돌 형상이 모델에 들어 있다
4. TCP 정의 및 검증                TCP 오프셋이 있고 관측으로 확인됐다
5. open/close 제어 확인            관측 개구가 목표 허용치 안에 들어온다
6. aperture 또는 파지 상태 관측     관측이 신선하고 표 범위 안이다
7. pick/place 계획·충돌·재검증 통과  기하 검사 ALLOW + 실행 전 재검증
```

**시뮬레이션 결과를 실기 검증으로 승격하지 않는다.** 조건이 시뮬레이션에서만
확인됐으면 그 사실이 판정에 남는다(`environment`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from core.reason_codes import ReasonCode


class ConditionStatus(str, Enum):
    """조건 하나의 상태."""

    MET = "met"
    #: 근거가 없다(값 미확보·관측 없음).
    MISSING = "missing"
    #: 값은 있으나 확인되지 않았다(선언·자리표·시뮬레이션 전용).
    UNVERIFIED = "unverified"
    #: 확인 결과가 실패다.
    FAILED = "failed"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Condition:
    """조건 하나의 판정. 근거와 이유 코드를 함께 담는다."""

    key: str
    label: str
    status: ConditionStatus
    reason_code: ReasonCode | None
    detail: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def met(self) -> bool:
        return self.status is ConditionStatus.MET

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status.value,
            "reason_code": None if self.reason_code is None else self.reason_code.value,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class GateResult:
    """관문 전체 판정."""

    conditions: tuple[Condition, ...]
    environment: str

    @property
    def enabled(self) -> bool:
        """pick·place를 열 수 있는가. **하나라도 미충족이면 열지 않는다.**"""
        return bool(self.conditions) and all(item.met for item in self.conditions)

    @property
    def blocking(self) -> tuple[Condition, ...]:
        return tuple(item for item in self.conditions if not item.met)

    @property
    def reason_codes(self) -> tuple[ReasonCode, ...]:
        out: list[ReasonCode] = []
        for item in self.blocking:
            if item.reason_code is not None and item.reason_code not in out:
                out.append(item.reason_code)
        return tuple(out)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "environment": self.environment,
            "conditions": [item.to_dict() for item in self.conditions],
            "blocking": [item.key for item in self.blocking],
            "reason_codes": [code.value for code in self.reason_codes],
        }


def _mounting_conditions(mounting) -> list[Condition]:
    """1·3·4 조건: 장착 변환·충돌 형상·TCP."""
    missing = tuple(mounting.missing)
    transform_keys = [name for name in missing if name.startswith(("xyz.", "rpy."))]
    out = [
        Condition(
            key="mounting_transform",
            label="장착 transform 근거 확보",
            status=ConditionStatus.MET if not transform_keys else ConditionStatus.MISSING,
            reason_code=None if not transform_keys else ReasonCode.CONFIG_MISSING,
            detail=("장착 변환 6개 값이 모두 근거를 갖는다"
                    if not transform_keys
                    else f"근거 없는 항목: {', '.join(transform_keys)}"),
            evidence={
                "profile": f"{mounting.mounting_profile_id}"
                           f" {mounting.mounting_profile_version}",
                "missing": list(missing),
                "arm_flange_frame": mounting.arm_flange_frame,
                "gripper_base_frame": mounting.gripper_base_frame,
            },
        ),
    ]

    coupling = dict(mounting.coupling or {})
    mass_status = str(coupling.get("mass_status", ""))
    mass_ok = mass_status == "verified"
    out.append(Condition(
        key="masses_and_inertia",
        label="adapter·gripper 질량/관성 확보",
        status=ConditionStatus.MET if mass_ok else ConditionStatus.UNVERIFIED,
        reason_code=None if mass_ok else ReasonCode.CAPABILITY_PROFILE_INCOMPLETE,
        detail=("어댑터·그리퍼 질량과 관성이 확인됐다" if mass_ok
                else f"어댑터 질량 상태: {mass_status or '미기록'}"
                     f" ({coupling.get('mass_note', '근거 없음')})"),
        evidence={
            "coupling_mass_kg": coupling.get("mass_kg"),
            "coupling_mass_status": mass_status,
            "gripper_mass_kg": (mounting.gripper_official or {}).get("total_mass_kg"),
        },
    ))

    collision_ok = bool(coupling.get("collision_mesh")) and bool(
        (mounting.gripper_official or {}).get("pad_face")
    )
    out.append(Condition(
        key="collision_geometry",
        label="충돌 형상 반영",
        status=ConditionStatus.MET if collision_ok else ConditionStatus.MISSING,
        reason_code=None if collision_ok else ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE,
        detail=("어댑터·그리퍼 충돌 형상이 모델에 있다" if collision_ok
                else "어댑터 또는 그리퍼 충돌 형상 근거가 없다"),
        evidence={"coupling_collision_mesh": coupling.get("collision_mesh")},
    ))

    tcp_missing = [name for name in missing if name.startswith("tcp.")]
    tcp_defined = not tcp_missing and bool(mounting.tcp_frame)
    out.append(Condition(
        key="tcp_defined_and_verified",
        label="TCP 정의 및 검증",
        status=ConditionStatus.MET if tcp_defined else ConditionStatus.MISSING,
        reason_code=None if tcp_defined else ReasonCode.CONFIG_MISSING,
        detail=(f"TCP 프레임 {mounting.tcp_frame}가 정의돼 있다" if tcp_defined
                else f"TCP 근거 없음: {', '.join(tcp_missing) or 'tcp_frame'}"),
        evidence={"tcp_frame": mounting.tcp_frame},
    ))
    return out


def _workcell_observation_conditions(
    verification: Mapping[str, Any], checks: Mapping[str, Any],
) -> list[Condition]:
    """작업 셀 그리퍼 검증 보고서로 5·6 조건을 판정한다.

    보고서가 **명령값과 관측 개구를 따로** 담으므로 그 둘을 각각 본다.
    목표를 지나친 단계(`no_overshoot=False`)는 관측이 맞아도 실패로 본다 —
    실물에서는 물체를 으깨는 동작이다.
    """
    control_keys = ("01_open", "02_close_30mm", "03_close_official", "04_reopen")
    present = [key for key in control_keys if key in checks]
    control_rows = [checks[key] for key in present]
    control_ok = bool(present) and all(row.get("passed") for row in control_rows)
    overshot = [key for key in present if checks[key].get("no_overshoot") is False]
    aperture_bad = [key for key in present
                    if checks[key].get("aperture_within_tolerance") is not True]

    unobservable = dict(verification.get("unobservable_joints") or {})
    stop_row = checks.get("05_stop_arm_and_gripper", {})

    return [
        Condition(
            key="open_close_control",
            label="open/close 제어 확인",
            status=ConditionStatus.MET if control_ok else ConditionStatus.FAILED,
            reason_code=None if control_ok else ReasonCode.EXEC_UNVERIFIABLE,
            detail=(f"open·30 mm·닫힘·재개방 {len(present)}단계가 관측으로"
                    " 확인됐고 목표를 지나치지 않았다" if control_ok
                    else f"미통과 단계: {', '.join(k for k in present if not checks[k].get('passed')) or '기록 없음'}"
                         + (f" · 목표 초과: {', '.join(overshot)}" if overshot else "")),
            evidence={
                "controller": (verification.get("controller") or {}).get("gripper"),
                "steps": present,
                "overshot_steps": overshot,
                "stop_passed": stop_row.get("passed"),
            },
        ),
        Condition(
            key="aperture_or_grasp_observation",
            label="aperture 또는 파지 상태 관측 확인",
            # 개구 관측은 확인됐지만 **파지 상태 관측 수단은 없다.**
            # 개구가 맞는다는 것이 물체를 쥐었다는 뜻이 아니다 — 그래서
            # 개구만으로 이 조건을 MET으로 올리지 않는다.
            status=(ConditionStatus.UNVERIFIED if not aperture_bad
                    else ConditionStatus.FAILED),
            reason_code=ReasonCode.EXEC_UNVERIFIABLE,
            detail=("개구는 관측으로 확인됐으나(오차 허용치 안)"
                    " **파지 상태 관측 수단이 없다** — 개구가 맞는다는 것이"
                    " 물체를 쥐었다는 뜻이 아니다" if not aperture_bad
                    else f"개구가 허용치를 벗어난 단계: {', '.join(aperture_bad)}"),
            evidence={
                "aperture_checked_steps": present,
                "aperture_out_of_tolerance": aperture_bad,
                "aperture_model": verification.get("aperture_model"),
                "grasp_observation_available": False,
                "unobservable_joints": unobservable.get("joints"),
                "unobservable_reason": unobservable.get("reason"),
            },
        ),
    ]


def _observation_conditions(verification: Mapping[str, Any] | None) -> list[Condition]:
    """5·6 조건: open/close 제어와 개구·파지 관측."""
    if not verification:
        return [
            Condition(
                key="open_close_control",
                label="open/close 제어 확인",
                status=ConditionStatus.MISSING,
                reason_code=ReasonCode.EXEC_UNVERIFIABLE,
                detail="그리퍼 검증 기록이 없다",
            ),
            Condition(
                key="aperture_or_grasp_observation",
                label="aperture 또는 파지 상태 관측 확인",
                status=ConditionStatus.MISSING,
                reason_code=ReasonCode.EXEC_UNVERIFIABLE,
                detail="관측 기록이 없다",
            ),
        ]

    checks = verification.get("checks", {})
    schema = str(verification.get("schema", ""))

    def passed(*keys: str) -> bool:
        return all(bool(checks.get(key, {}).get("passed")) for key in keys)

    # 작업 셀 검증 보고서(8-08 이후)는 형식이 다르다. **스키마로 구분한다** —
    # 키 이름을 추측하지 않는다. 형식을 모르면 아래 기존 경로로 떨어진다.
    if schema.startswith("forstick2.workcell_gripper_control"):
        return _workcell_observation_conditions(verification, checks)

    control_ok = passed("03_gripper_open", "04_gripper_close", "06_sequence")
    sequence = checks.get("06_sequence", {})
    failed_steps = [
        step["step"] for step in sequence.get("steps", [])
        if not step.get("observed_ok")
    ]
    observation_ok = passed("02_aperture_model") and not failed_steps

    return [
        Condition(
            key="open_close_control",
            label="open/close 제어 확인",
            status=ConditionStatus.MET if control_ok else ConditionStatus.FAILED,
            reason_code=None if control_ok else ReasonCode.EXEC_UNVERIFIABLE,
            detail=("open·close·연속 순서가 관측으로 확인됐다" if control_ok
                    else f"관측 실패 단계: {', '.join(failed_steps) or '순서 미확인'}"),
            evidence={
                "open": checks.get("03_gripper_open", {}).get("passed"),
                "close": checks.get("04_gripper_close", {}).get("passed"),
                "sequence": sequence.get("passed"),
                "failed_steps": failed_steps,
            },
        ),
        Condition(
            key="aperture_or_grasp_observation",
            label="aperture 또는 파지 상태 관측 확인",
            status=ConditionStatus.MET if observation_ok else ConditionStatus.FAILED,
            reason_code=None if observation_ok else ReasonCode.EXEC_UNVERIFIABLE,
            detail=("개구 관측이 표 범위 안에서 확인됐다" if observation_ok
                    else "개구 관측이 일부 단계에서 확인되지 않았다"),
            evidence={
                "aperture_model": checks.get("02_aperture_model", {}).get("passed"),
                "object_width_close": checks.get(
                    "04b_close_for_object_width", {}
                ).get("passed"),
            },
        ),
    ]


def _plan_condition(geometry_decision: str | None,
                    revalidated: bool,
                    plan_validation: Mapping[str, Any] | None = None) -> Condition:
    """7 조건: 계획·충돌 검사·재검증.

    8-10에서 pick/place 계획 **사전 검증**이 붙었다(`validation/pick_place_plan.py`).
    그 결과가 오면 여기서 그것을 근거로 판정한다. 단, 사전 검증을 지났다고
    이 조건이 MET이 되지는 않는다 — **실행 직전 재검증**은 실행 시점에만 할 수
    있고 실행이 열려 있지 않다. 그래서 "값은 있으나 확인되지 않았다"
    (`UNVERIFIED`)로 남긴다. 통과로 올리려고 재검증 요구를 없애지 않는다.
    """
    evidence: dict[str, Any] = {
        "geometry_decision": geometry_decision,
        "revalidated": revalidated,
    }
    if plan_validation is not None:
        evidence.update({
            "plan_checked": bool(plan_validation.get("available", True)),
            "plan_verified": bool(plan_validation.get("plan_verified")),
            "stage_count": plan_validation.get("stage_count"),
            "checks_passed": plan_validation.get("checks_passed"),
            "resources_matched": plan_validation.get("resources_matched"),
            "scene_stable": plan_validation.get("scene_stable"),
            "snapshot_id": (plan_validation.get("snapshot") or {}).get("snapshot_id"),
            "plan_reason_codes": list(plan_validation.get("reason_codes") or ()),
            # 사전 검증 결과가 실행 허가가 아니라는 사실을 판정에도 남긴다.
            "execution_allowed": False,
        })
        if not plan_validation.get("available", True):
            return Condition(
                key="plan_collision_revalidation",
                label="pick/place 계획·충돌·재검증 통과",
                status=ConditionStatus.MISSING,
                reason_code=ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE,
                detail=str(plan_validation.get("detail")
                           or "pick/place 계획 사전 검증을 돌리지 못했다"),
                evidence=evidence,
            )
        if not plan_validation.get("plan_verified"):
            codes = list(plan_validation.get("reason_codes") or ())
            findings = list(plan_validation.get("findings") or ())
            first = findings[0].get("detail") if findings else "사유 기록 없음"
            reason = ReasonCode(codes[0]) if codes else ReasonCode.GEOMETRY_COLLISION
            return Condition(
                key="plan_collision_revalidation",
                label="pick/place 계획·충돌·재검증 통과",
                status=ConditionStatus.FAILED,
                reason_code=reason,
                detail=f"계획 사전 검증이 통과하지 않았다: {first}",
                evidence=evidence,
            )
        if not revalidated:
            passed = plan_validation.get("checks_passed")
            total = plan_validation.get("stage_count")
            return Condition(
                key="plan_collision_revalidation",
                label="pick/place 계획·충돌·재검증 통과",
                # 사전 검증은 지났지만 재검증 기록이 없다. **통과가 아니다.**
                status=ConditionStatus.UNVERIFIED,
                reason_code=ReasonCode.EXEC_PERMIT_DENIED,
                detail=f"자원·도달·관절 제한·충돌 사전 검증은 통과했다"
                       f"(단계 {passed}/{total}). 실행 직전 재검증은 실행"
                       " 시점에만 할 수 있고, 실행이 열리지 않아 기록이 없다",
                evidence=evidence,
            )
        return Condition(
            key="plan_collision_revalidation",
            label="pick/place 계획·충돌·재검증 통과",
            status=ConditionStatus.MET,
            reason_code=None,
            detail="계획 사전 검증과 실행 직전 재검증을 모두 통과했다",
            evidence=evidence,
        )

    ok = geometry_decision == "allow" and revalidated
    if geometry_decision is None:
        detail = "pick/place 계획에 대한 기하 검사 결과가 없다"
        reason = ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE
    elif geometry_decision != "allow":
        detail = f"기하 검사 판정이 {geometry_decision}다"
        reason = ReasonCode.GEOMETRY_COLLISION
    elif not revalidated:
        detail = "실행 직전 재검증 기록이 없다"
        reason = ReasonCode.EXEC_PERMIT_DENIED
    else:
        detail = "계획·충돌 검사·재검증을 통과했다"
        reason = None
    return Condition(
        key="plan_collision_revalidation",
        label="pick/place 계획·충돌·재검증 통과",
        status=ConditionStatus.MET if ok else (
            ConditionStatus.MISSING if geometry_decision is None
            else ConditionStatus.FAILED
        ),
        reason_code=reason,
        detail=detail,
        evidence=evidence,
    )


def evaluate(
    *,
    mounting,
    verification: Mapping[str, Any] | None = None,
    geometry_decision: str | None = None,
    revalidated: bool = False,
    environment: str = "simulation",
    plan_validation: Mapping[str, Any] | None = None,
    grasp_observation: Mapping[str, Any] | None = None,
) -> GateResult:
    """7개 조건을 판정한다. **하나라도 미충족이면 pick/place를 열지 않는다.**"""
    conditions = [
        *_mounting_conditions(mounting),
        *_observation_conditions(verification),
        _plan_condition(geometry_decision, revalidated, plan_validation),
    ]
    if grasp_observation is not None:
        conditions = [_with_grasp_observation(item, grasp_observation)
                      for item in conditions]
    order = [
        "mounting_transform", "masses_and_inertia", "collision_geometry",
        "tcp_defined_and_verified", "open_close_control",
        "aperture_or_grasp_observation", "plan_collision_revalidation",
    ]
    conditions.sort(key=lambda item: order.index(item.key))
    return GateResult(conditions=tuple(conditions), environment=environment)


def _with_grasp_observation(
    item: Condition, observation: Mapping[str, Any],
) -> Condition:
    """파지 관측 기록을 6번 조건의 근거에 붙인다.

    관측이 `measured`가 아니면 조건을 올리지 않는다 — 시뮬레이터 관측은
    실기 검증으로 승격되지 않고, 개구 일치는 파지 근거가 아니다.
    """
    if item.key != "aperture_or_grasp_observation":
        return item
    evidence = dict(item.evidence)
    evidence["grasp_observation"] = dict(observation)
    counts = bool(observation.get("counts_for_gate"))
    availability = str(observation.get("availability", "unavailable"))
    if counts and item.status is not ConditionStatus.FAILED:
        return Condition(
            key=item.key, label=item.label, status=ConditionStatus.MET,
            reason_code=None,
            detail=f"{item.detail} · 실기 파지 관측이 확보됐다",
            evidence=evidence,
        )
    # 파지 관측 기록을 받았는데 실기 관측이 아니면 **조건을 올리지 않는다.**
    # 검증 보고서의 개구 관측이 통과였더라도 개구는 파지 근거가 아니다.
    if item.status is ConditionStatus.MET:
        return Condition(
            key=item.key, label=item.label, status=ConditionStatus.UNVERIFIED,
            reason_code=ReasonCode.EXEC_UNVERIFIABLE,
            detail=f"{item.detail} · 그러나 파지 상태 관측이"
                   f" {availability}다 — 개구 관측을 파지 근거로 쓰지 않는다",
            evidence=evidence,
        )
    return Condition(
        key=item.key, label=item.label, status=item.status,
        reason_code=item.reason_code,
        detail=f"{item.detail} · 파지 관측: {availability}",
        evidence=evidence,
    )


def blocking_summary(result: GateResult) -> Sequence[str]:
    """화면·기록에 쓸 짧은 이유 목록."""
    return [f"{item.label}: {item.detail}" for item in result.blocking]
