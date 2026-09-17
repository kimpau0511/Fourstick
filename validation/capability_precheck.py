"""Capability 기반 사전 검사 (md/개발플랜.md 6-04).

특정 로봇과 무관한 계약 검사다. 여기에는 로봇 이름·관절명·수치가 없고, 모든
값은 `SkillCatalog`·`ResourceCatalog`·`CapabilityProfile`·해석된 모션에서 온다.

규칙 (판정은 안전 검증과 같은 4단계다):

| 코드 | 검사 |
|---|---|
| E-CAP-001 | 스킬이 SkillCatalog에 등록돼 있는가 |
| E-CAP-002 | Profile이 그 스킬을 지원하는가 |
| E-CAP-003 | 필수 인자가 있고, 허용되지 않은 인자가 없는가 |
| E-CAP-004 | 인자가 받는 리소스 종류가 카탈로그와 맞는가 |
| E-CAP-005 | 해석된 관절 목표가 Profile 범위 안인가 |
| E-CAP-006 | 해석된 속도가 Profile 한계 안인가 |
| E-CAP-007 | 해석된 가속도가 Profile 한계 안인가 |
| E-CAP-008 | 해석된 그리퍼 위치·힘이 Profile 범위 안인가 |

지키는 것:

- **입력값을 범위 안으로 잘라서 통과시키지 않는다.** 벗어나면 BLOCK이다.
- **검사에 필요한 Profile 값이 없으면 통과시키지 않는다.** 예를 들어 가속도
  값이 요청됐는데 Profile에 `max_acceleration`이 없으면
  INSUFFICIENT_DATA(집계에서 ASK)다. 0이나 임의 기본값을 만들지 않는다.
- **단위를 추정하지 않는다.** 해석된 값에 단위 선언이 없거나 Profile의 단위와
  다르면 BLOCK이다(`capability.unit_mismatch`).
- Profile에 없는 관절·그리퍼를 지목하면 BLOCK이다
  (`capability.unknown_joint`) — 이름을 고쳐 맞추지 않는다.
- 해석된 모션이 아예 없는 계획(기호 계획)에서 E-CAP-005~008은
  NOT_APPLICABLE이다. 이는 "검사할 수치가 계약에 존재하지 않는다"는 사실이고,
  Adapter 내부 해석값에 대한 보증이 아니다 — 실제 로봇 경로는 8단계에서
  해석값을 이 검사에 넣어야 한다(문서에 미검증으로 남긴다).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from core.capability_profile import CapabilityProfile
from core.constants import SKILL_ALLOWED_ARGS, SKILL_REQUIRED_ARGS
from core.motion import MotionRequest, Quantity
from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceCatalog
from core.skill_catalog import SkillCatalog
from core.task_plan import TaskPlan
from validation.safety_validator import RuleResult, RuleStatus

CAP_RULES: tuple[str, ...] = (
    "E-CAP-001", "E-CAP-002", "E-CAP-003", "E-CAP-004",
    "E-CAP-005", "E-CAP-006", "E-CAP-007", "E-CAP-008",
)

#: 규칙별 대표 이유 코드. 세부 원인은 message에 적는다.
CAP_RULE_REASONS: dict[str, ReasonCode] = {
    "E-CAP-001": ReasonCode.CAPABILITY_SKILL_UNSUPPORTED,
    "E-CAP-002": ReasonCode.CAPABILITY_SKILL_UNSUPPORTED,
    "E-CAP-003": ReasonCode.PLAN_ARG_MISSING,
    "E-CAP-004": ReasonCode.PLAN_ARG_UNKNOWN,
    "E-CAP-005": ReasonCode.CAPABILITY_LIMIT_EXCEEDED,
    "E-CAP-006": ReasonCode.CAPABILITY_LIMIT_EXCEEDED,
    "E-CAP-007": ReasonCode.CAPABILITY_LIMIT_EXCEEDED,
    "E-CAP-008": ReasonCode.CAPABILITY_LIMIT_EXCEEDED,
}


@dataclass(frozen=True)
class _Finding:
    """규칙 하나에 모인 문제들. 상태가 다른 문제를 섞지 않는다."""

    blocks: list[str]
    insufficient: list[str]

    @classmethod
    def empty(cls) -> "_Finding":
        return cls([], [])

    def result(self, code: str, ok_message: str,
               reason: ReasonCode | None = None) -> RuleResult:
        if self.blocks:
            return RuleResult(
                code, RuleStatus.BLOCK, "; ".join(self.blocks),
                reason or CAP_RULE_REASONS[code],
            )
        if self.insufficient:
            return RuleResult(
                code, RuleStatus.INSUFFICIENT_DATA, "; ".join(self.insufficient),
                ReasonCode.CAPABILITY_PROFILE_INCOMPLETE,
            )
        return RuleResult(code, RuleStatus.PASS, ok_message)


def _na(code: str, message: str) -> RuleResult:
    return RuleResult(code, RuleStatus.NOT_APPLICABLE, message)


def evaluate_capability(
    plan: TaskPlan,
    profile: CapabilityProfile | None,
    skill_catalog: SkillCatalog | None,
    resource_catalog: ResourceCatalog | None = None,
    resolved_motion: Mapping[int, MotionRequest] | None = None,
) -> list[RuleResult]:
    """8개 규칙을 전부 평가한다. 통과도 남긴다.

    `profile`이 없으면 Profile이 필요한 규칙은 INSUFFICIENT_DATA다 —
    Profile 없는 상태를 통과로 처리하지 않는다.
    """
    motion: Mapping[int, MotionRequest] = dict(resolved_motion or {})
    results: list[RuleResult] = []

    # ── E-CAP-001: SkillCatalog 등록 ────────────────────────────────────
    if skill_catalog is None:
        results.append(RuleResult(
            "E-CAP-001", RuleStatus.INSUFFICIENT_DATA,
            "SkillCatalog가 없어 스킬 등록 여부를 확인할 수 없다",
            ReasonCode.CAPABILITY_PROFILE_INCOMPLETE,
        ))
    else:
        finding = _Finding.empty()
        for index, step in enumerate(plan.steps, start=1):
            if not skill_catalog.has(step.skill):
                finding.blocks.append(
                    f"스텝{index}: {step.skill!r}는 SkillCatalog"
                    f"({skill_catalog.catalog_version})에 없다"
                )
        results.append(finding.result(
            "E-CAP-001",
            f"모든 스킬이 카탈로그 {skill_catalog.catalog_version}에 있음",
        ))

    # ── E-CAP-002: Profile 지원 ─────────────────────────────────────────
    if profile is None:
        results.append(RuleResult(
            "E-CAP-002", RuleStatus.INSUFFICIENT_DATA,
            "CapabilityProfile이 없어 지원 스킬을 확인할 수 없다",
            ReasonCode.CAPABILITY_PROFILE_INCOMPLETE,
        ))
    else:
        finding = _Finding.empty()
        for index, step in enumerate(plan.steps, start=1):
            if not profile.supports(step.skill):
                finding.blocks.append(
                    f"스텝{index}: Profile {profile.profile_id}"
                    f" {profile.profile_version}가 {step.skill!r}를 지원하지 않는다"
                    f" (지원: {list(profile.supported_skills)})"
                )
        results.append(finding.result(
            "E-CAP-002",
            f"Profile {profile.profile_id} {profile.profile_version}가 모든 스킬을 지원",
        ))

    # ── E-CAP-003: 필수·허용 인자 ───────────────────────────────────────
    finding = _Finding.empty()
    for index, step in enumerate(plan.steps, start=1):
        required = SKILL_REQUIRED_ARGS.get(step.skill)
        allowed = SKILL_ALLOWED_ARGS.get(step.skill)
        if required is None or allowed is None:
            continue   # E-CAP-001이 이미 잡는다
        for name in required:
            if name not in step.args or step.args[name] in (None, ""):
                finding.blocks.append(f"스텝{index} {step.skill}: 필수 인자 {name!r}가 없다")
        extra = sorted(set(step.args) - set(allowed))
        if extra:
            finding.blocks.append(
                f"스텝{index} {step.skill}: 계약이 허용하지 않는 인자 {extra}"
            )
    results.append(finding.result("E-CAP-003", "필수 인자가 모두 있고 허용 인자만 쓴다"))

    # ── E-CAP-004: 인자의 리소스 종류 ───────────────────────────────────
    if skill_catalog is None or resource_catalog is None:
        results.append(RuleResult(
            "E-CAP-004", RuleStatus.INSUFFICIENT_DATA,
            "Skill/Resource 카탈로그가 없어 인자 종류를 대조할 수 없다",
            ReasonCode.CAPABILITY_PROFILE_INCOMPLETE,
        ))
    else:
        finding = _Finding.empty()
        for index, step in enumerate(plan.steps, start=1):
            if not skill_catalog.has(step.skill):
                continue   # E-CAP-001이 잡는다
            entry = skill_catalog.get(step.skill)
            for name, value in step.args.items():
                kind = entry.arg_kinds.get(name)
                if kind is None:
                    continue   # E-CAP-003이 잡는다
                if not resource_catalog.has(str(value)):
                    finding.blocks.append(
                        f"스텝{index} {step.skill}.{name}={value!r}는 카탈로그에 없다"
                    )
                    continue
                resource = resource_catalog.get(str(value))
                if resource.kind is not kind:
                    finding.blocks.append(
                        f"스텝{index} {step.skill}.{name}={value!r}는"
                        f" {resource.kind.value}인데 {kind.value}가 필요하다"
                    )
        results.append(finding.result(
            "E-CAP-004", "모든 인자가 카탈로그의 리소스 종류와 맞음",
            ReasonCode.PLAN_ARG_UNKNOWN,
        ))

    # ── E-CAP-005~008: 해석된 수치 ──────────────────────────────────────
    if not motion or all(request.is_empty for request in motion.values()):
        note = (
            "해석된 모션 수치가 없다(기호 계획) — 검사할 값이 계약에 존재하지 않는다."
            " Adapter 내부 해석값에 대한 보증이 아니다"
        )
        results.extend(_na(code, note) for code in
                       ("E-CAP-005", "E-CAP-006", "E-CAP-007", "E-CAP-008"))
        return results

    if profile is None:
        missing = "CapabilityProfile이 없어 해석된 수치를 대조할 수 없다"
        results.extend(
            RuleResult(code, RuleStatus.INSUFFICIENT_DATA, missing,
                       ReasonCode.CAPABILITY_PROFILE_INCOMPLETE)
            for code in ("E-CAP-005", "E-CAP-006", "E-CAP-007", "E-CAP-008")
        )
        return results

    results.append(_joint_positions(profile, motion))
    results.append(_joint_rates(
        profile, motion, quantity=Quantity.JOINT_VELOCITY, code="E-CAP-006",
        label="속도", getter=lambda r: r.joint_velocities,
        limit=lambda j: j.max_velocity, limit_name="max_velocity",
    ))
    results.append(_joint_rates(
        profile, motion, quantity=Quantity.JOINT_ACCELERATION, code="E-CAP-007",
        label="가속도", getter=lambda r: r.joint_accelerations,
        limit=lambda j: j.max_acceleration, limit_name="max_acceleration",
    ))
    results.append(_gripper(profile, motion))
    return results


def _unit_problem(
    request: MotionRequest, quantity: Quantity, expected: str, index: int,
) -> str | None:
    declared = request.unit_for(quantity)
    if declared is None:
        return f"스텝{index}: {quantity} 단위가 선언되지 않았다 — 단위를 추정하지 않는다"
    if declared != expected:
        return (
            f"스텝{index}: {quantity} 단위가 {declared!r}인데 Profile은 {expected!r}다"
        )
    return None


def _joint_positions(
    profile: CapabilityProfile, motion: Mapping[int, MotionRequest],
) -> RuleResult:
    finding = _Finding.empty()
    checked = 0
    for index, request in sorted(motion.items()):
        if not request.joint_targets:
            continue
        for name, value in request.joint_targets.items():
            limit = profile.joint(name)
            if limit is None:
                finding.blocks.append(
                    f"스텝{index}: Profile에 없는 관절 {name!r}"
                )
                continue
            problem = _unit_problem(request, Quantity.JOINT_POSITION, limit.unit, index)
            if problem:
                finding.blocks.append(problem)
                continue
            checked += 1
            if value < limit.lower or value > limit.upper:
                finding.blocks.append(
                    f"스텝{index}: 관절 {name} 목표 {value}{limit.unit}가 범위"
                    f" [{limit.lower}, {limit.upper}]를 벗어났다"
                    " — 범위 안으로 자르지 않는다"
                )
    if finding.blocks and any("없는 관절" in m for m in finding.blocks):
        return RuleResult(
            "E-CAP-005", RuleStatus.BLOCK, "; ".join(finding.blocks),
            ReasonCode.CAPABILITY_UNKNOWN_JOINT
            if all("없는 관절" in m for m in finding.blocks)
            else ReasonCode.CAPABILITY_LIMIT_EXCEEDED,
        )
    if finding.blocks and any("단위" in m for m in finding.blocks):
        return RuleResult(
            "E-CAP-005", RuleStatus.BLOCK, "; ".join(finding.blocks),
            ReasonCode.CAPABILITY_UNIT_MISMATCH
            if all("단위" in m for m in finding.blocks)
            else ReasonCode.CAPABILITY_LIMIT_EXCEEDED,
        )
    return finding.result("E-CAP-005", f"관절 목표 {checked}개가 모두 Profile 범위 안")


def _joint_rates(
    profile: CapabilityProfile, motion: Mapping[int, MotionRequest], *,
    quantity: Quantity, code: str, label: str, getter, limit, limit_name: str,
) -> RuleResult:
    finding = _Finding.empty()
    checked = 0
    unit_wrong = False
    unknown_joint = False
    for index, request in sorted(motion.items()):
        values: Mapping[str, float] = getter(request)
        if not values:
            continue
        for name, value in values.items():
            joint = profile.joint(name)
            if joint is None:
                unknown_joint = True
                finding.blocks.append(f"스텝{index}: Profile에 없는 관절 {name!r}")
                continue
            # 속도·가속도의 단위는 "위치 단위/s", "위치 단위/s^2"다. 선언을
            # 그대로 요구하고 여기서 만들어 붙이지 않는다.
            expected = f"{joint.unit}/s" if quantity is Quantity.JOINT_VELOCITY else (
                f"{joint.unit}/s^2"
            )
            problem = _unit_problem(request, quantity, expected, index)
            if problem:
                unit_wrong = True
                finding.blocks.append(problem)
                continue
            bound = limit(joint)
            if bound is None:
                finding.insufficient.append(
                    f"스텝{index}: 관절 {name}의 {limit_name}가 Profile에 없다"
                    f" — {label} {value}를 통과시키지 않는다"
                )
                continue
            checked += 1
            if abs(value) > bound:
                finding.blocks.append(
                    f"스텝{index}: 관절 {name} {label} {value}가 한계 {bound}를"
                    " 넘었다 — 한계로 낮춰 통과시키지 않는다"
                )
    if finding.blocks:
        reason = ReasonCode.CAPABILITY_LIMIT_EXCEEDED
        if unknown_joint and all("없는 관절" in m for m in finding.blocks):
            reason = ReasonCode.CAPABILITY_UNKNOWN_JOINT
        elif unit_wrong and all("단위" in m for m in finding.blocks):
            reason = ReasonCode.CAPABILITY_UNIT_MISMATCH
        return RuleResult(code, RuleStatus.BLOCK, "; ".join(finding.blocks), reason)
    return finding.result(code, f"{label} {checked}개가 모두 Profile 한계 안")


def _gripper(
    profile: CapabilityProfile, motion: Mapping[int, MotionRequest],
) -> RuleResult:
    requests = [
        (index, request) for index, request in sorted(motion.items())
        if request.gripper_position is not None or request.gripper_effort is not None
    ]
    if not requests:
        return _na("E-CAP-008", "해석된 그리퍼 값이 없다")
    spec = profile.gripper
    if spec is None:
        return RuleResult(
            "E-CAP-008", RuleStatus.INSUFFICIENT_DATA,
            "Profile에 그리퍼 설정이 없어 그리퍼 값을 대조할 수 없다",
            ReasonCode.CAPABILITY_PROFILE_INCOMPLETE,
        )
    finding = _Finding.empty()
    unit_wrong = False
    low, high = sorted((spec.open_position, spec.close_position))
    for index, request in requests:
        if request.gripper_position is not None:
            problem = _unit_problem(
                request, Quantity.GRIPPER_POSITION, spec.unit, index
            )
            if problem:
                unit_wrong = True
                finding.blocks.append(problem)
            elif not (low <= request.gripper_position <= high):
                finding.blocks.append(
                    f"스텝{index}: 그리퍼 위치 {request.gripper_position}{spec.unit}가"
                    f" 개방·닫힘 구간 [{low}, {high}]를 벗어났다"
                )
        if request.gripper_effort is not None:
            problem = _unit_problem(
                request, Quantity.GRIPPER_EFFORT, "N", index
            )
            if problem:
                unit_wrong = True
                finding.blocks.append(problem)
            elif request.gripper_effort < 0 or request.gripper_effort > spec.max_effort:
                finding.blocks.append(
                    f"스텝{index}: 그리퍼 힘 {request.gripper_effort}N이 한계"
                    f" {spec.max_effort}N을 벗어났다"
                )
    if finding.blocks:
        reason = (
            ReasonCode.CAPABILITY_UNIT_MISMATCH
            if unit_wrong and all("단위" in m for m in finding.blocks)
            else ReasonCode.CAPABILITY_LIMIT_EXCEEDED
        )
        return RuleResult("E-CAP-008", RuleStatus.BLOCK, "; ".join(finding.blocks), reason)
    return finding.result(
        "E-CAP-008", f"그리퍼 값 {len(requests)}건이 Profile 범위 안"
    )


def capability_decision(results: Sequence[RuleResult]) -> RuleStatus:
    """집계. BLOCK > INSUFFICIENT_DATA > PASS. 정보 부족을 통과로 올리지 않는다."""
    if any(r.status is RuleStatus.BLOCK for r in results):
        return RuleStatus.BLOCK
    if any(r.status is RuleStatus.INSUFFICIENT_DATA for r in results):
        return RuleStatus.INSUFFICIENT_DATA
    return RuleStatus.PASS
