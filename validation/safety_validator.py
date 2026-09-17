"""규칙 기반 안전 검증 (요구정의서의 "안전 검증 게이트").

forstick `safety_guard.py`의 8개 규칙과 4단계 판정 체계를 가져와 forstick2의
core 계약(TaskPlan / CapabilityProfile / ReasonCode) 위에 다시 썼다.

이관한 원칙:
- 규칙별 판정을 PASS / BLOCK / INSUFFICIENT_DATA / NOT_APPLICABLE 4단계로 낸다.
  위반만 모으지 않고 통과도 기록해, 무엇을 실제로 검사했는지 남긴다.
- 집계는 BLOCK 하나라도 있으면 BLOCK, 없고 INSUFFICIENT_DATA가 있으면 ASK,
  전부 PASS/NOT_APPLICABLE이면 ALLOW다. **INSUFFICIENT_DATA를 ALLOW로
  승격하지 않는다** — 정보 부족을 통과로 치지 않는다.
- 검증할 데이터가 없으면 추정하지 않고 INSUFFICIENT_DATA로 남긴다.

forstick과 달라진 점:
- 스텝 상한과 **종료 스킬**은 코드 상수가 아니라 SafetyPolicy에서 받는다
  (계획.md 27장). 종료 스킬을 검증기에 상수로 두면 계획 생성 프롬프트가 같은
  규칙을 알 수 없어 두 곳이 어긋난다 — 5-02 실측에서 모델이 마지막 복귀 스텝을
  절반의 사례에서 빠뜨려 안전 검증에 막혔다.
- 허용 위치·물체 목록은 Resource Catalog로 주입받는다. 코드에 목록이 없다.
- E-HOLD-003을 추가했다. forstick에서는 계획을 따라간 파지 상태가 계획의 목표
  종료 상태와 어긋나도 통과하는 공백이 실측으로 확인됐다.
  이 규칙은 "쥔 채 종료"를 일괄 차단하지 않는다 — 쥔 상태로 끝나는 것이 목표인
  계획(예: 집어서 들고 대기)도 정상이기 때문이다. 대신 스텝을 따라간 결과의
  파지 상태가 `TaskPlan.terminal_hold`(계획이 명시한 목표 종료 상태)와
  일치하는지만 본다. 목표를 명시하지 않은 계획은 `terminal_hold=None`이므로
  "아무것도 쥐지 않고 종료"가 목표가 된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from core.constants import SKILL_MOVE, SKILL_PICK, SKILL_PLACE
from core.policy import SafetyPolicy
from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceCatalog
from core.task_plan import TaskPlan


class RuleStatus(str, Enum):
    PASS = "pass"
    BLOCK = "block"
    INSUFFICIENT_DATA = "insufficient_data"
    NOT_APPLICABLE = "not_applicable"

    def __str__(self) -> str:
        return self.value


class SafetyDecision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    BLOCK = "block"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class RuleResult:
    code: str
    status: RuleStatus
    message: str
    reason: ReasonCode | None = None


#: 규칙 코드와 공통 이유 코드의 매핑. UI·감사 로그가 같은 코드를 쓰게 한다.
RULE_REASONS: dict[str, ReasonCode] = {
    "E-SEQ-001": ReasonCode.SAFETY_SEQUENCE_INVALID,
    "E-SEQ-002": ReasonCode.SAFETY_SEQUENCE_INVALID,
    "E-SEQ-003": ReasonCode.SAFETY_SEQUENCE_INVALID,
    "E-SEQ-004": ReasonCode.SAFETY_SEQUENCE_INVALID,
    "E-HOLD-001": ReasonCode.SAFETY_HOLD_INVALID,
    "E-HOLD-002": ReasonCode.SAFETY_HOLD_INVALID,
    "E-HOLD-003": ReasonCode.SAFETY_HOLD_INVALID,
    "E-LIMIT-001": ReasonCode.SAFETY_LIMIT_EXCEEDED,
    "E-ARG-001": ReasonCode.SAFETY_ARG_OUT_OF_PROFILE,
}

ALL_RULES: tuple[str, ...] = tuple(RULE_REASONS)


def _ok(code: str, message: str) -> RuleResult:
    return RuleResult(code, RuleStatus.PASS, message)


def _block(code: str, message: str) -> RuleResult:
    return RuleResult(code, RuleStatus.BLOCK, message, RULE_REASONS[code])


def _na(code: str, message: str) -> RuleResult:
    return RuleResult(code, RuleStatus.NOT_APPLICABLE, message)


def _insufficient(code: str, message: str) -> RuleResult:
    return RuleResult(code, RuleStatus.INSUFFICIENT_DATA, message,
                      ReasonCode.SAFETY_INSUFFICIENT_DATA)


def evaluate(
    plan: TaskPlan,
    policy: SafetyPolicy,
    catalog: ResourceCatalog | None,
) -> list[RuleResult]:
    """규칙 8+1개를 전부 평가한다. 통과도 결과에 남긴다.

    catalog가 None이면 E-ARG-001은 INSUFFICIENT_DATA다 — 목록을 모르는 상태를
    통과로 처리하지 않는다.
    """
    steps = plan.steps

    # E-SEQ-001: 빈 계획 금지. 비면 나머지는 검사 대상 자체가 없다.
    if not steps:
        results = [_block("E-SEQ-001", "빈 작업 계획")]
        results += [
            _na(c, "계획이 비어 있어 해당 없음(E-SEQ-001이 이미 차단)")
            for c in ALL_RULES if c != "E-SEQ-001"
        ]
        return results

    results = [_ok("E-SEQ-001", "빈 계획 아님")]

    # E-SEQ-002: 정책이 정한 스킬로 종료. 코드가 스킬 이름을 갖지 않는다.
    required_final = policy.required_final_skill
    if required_final is None:
        results.append(
            _na("E-SEQ-002", "정책에 종료 스킬 제약이 없다")
        )
    elif steps[-1].skill != required_final:
        results.append(
            _block(
                "E-SEQ-002",
                f"마지막 스텝이 {steps[-1].skill!r} — 정책이 요구하는 종료 스킬은"
                f" {required_final!r}다 (policy {policy.policy_version})",
            )
        )
    else:
        results.append(_ok("E-SEQ-002", f"{required_final}로 종료됨"))

    # E-LIMIT-001: 스텝 상한 (값은 Policy에서 받는다)
    if len(steps) > policy.max_steps:
        results.append(_block("E-LIMIT-001", f"스텝 {len(steps)}개 > 상한 {policy.max_steps}개"))
    else:
        results.append(_ok("E-LIMIT-001", f"스텝 {len(steps)}개 <= 상한 {policy.max_steps}개"))

    # E-ARG-001: 위치·물체가 Catalog 안에 있는지
    if catalog is None:
        results.append(_insufficient("E-ARG-001", "Resource Catalog가 없어 인자를 확인할 수 없다"))
    else:
        issues: list[str] = []
        for i, s in enumerate(steps):
            for key in ("to", "from"):
                name = s.args.get(key)
                if name is not None and name not in catalog.locations:
                    issues.append(f"스텝{i+1} {s.skill}.{key}={name!r}는 셀에 없는 위치")
            name = s.args.get("object")
            if name is not None and name not in catalog.objects:
                issues.append(f"스텝{i+1} {s.skill}.object={name!r}는 셀에 없는 물체")
        results.append(_block("E-ARG-001", "; ".join(issues)) if issues
                       else _ok("E-ARG-001", "모든 인자가 Catalog 안에 있음"))

    # E-HOLD-001/002/003: 파지 상태 추적
    held: str | None = None
    hold1: list[str] = []   # 들고 있지 않은데 place
    hold2: list[str] = []   # 들고 있는데 또 pick
    for i, s in enumerate(steps):
        if s.skill == SKILL_PICK:
            if held is not None:
                hold2.append(f"스텝{i+1}: {held!r}를 든 채 {s.args['object']!r}를 또 집는다")
            held = s.args["object"]
        elif s.skill == SKILL_PLACE:
            if held is None:
                hold1.append(f"스텝{i+1}: 아무것도 들지 않은 채 {s.args['object']!r}를 놓는다")
            elif held != s.args["object"]:
                hold1.append(f"스텝{i+1}: {held!r}를 들었는데 {s.args['object']!r}를 놓는다")
                held = None
            else:
                held = None

    results.append(_block("E-HOLD-001", "; ".join(hold1)) if hold1
                   else _ok("E-HOLD-001", "들지 않은 물체를 놓는 스텝 없음"))
    results.append(_block("E-HOLD-002", "; ".join(hold2)) if hold2
                   else _ok("E-HOLD-002", "이미 든 상태에서 집는 스텝 없음"))
    # E-HOLD-003: 스텝을 따라간 종료 파지 상태가 계획의 목표 종료 상태와 같은지.
    # "쥔 채 종료"라는 사실만으로 차단하지 않는다 — 목표가 그것이면 정상이다.
    expected = plan.terminal_hold
    if held == expected:
        results.append(
            _ok(
                "E-HOLD-003",
                f"종료 파지 상태가 목표와 일치({held!r})"
                if expected is not None
                else "목표대로 아무것도 쥐지 않고 종료",
            )
        )
    else:
        results.append(
            _block(
                "E-HOLD-003",
                f"종료 파지 상태 불일치 — 목표 {expected!r}, 계획 수행 결과 {held!r}",
            )
        )

    # E-SEQ-003/004: pick/place 직전에 같은 위치로의 move가 있어야 한다.
    for code, skill, arg, label in (
        ("E-SEQ-003", SKILL_PICK, "from", "pick"),
        ("E-SEQ-004", SKILL_PLACE, "to", "place"),
    ):
        issues = []
        for i, s in enumerate(steps):
            if s.skill != skill:
                continue
            target = s.args[arg]
            prev = steps[i - 1] if i > 0 else None
            if prev is None or prev.skill != SKILL_MOVE or prev.args.get("to") != target:
                issues.append(f"스텝{i+1}: {label} 직전에 {target!r}로의 move가 없다")
        results.append(_block(code, "; ".join(issues)) if issues
                       else _ok(code, f"모든 {label} 직전에 같은 위치로의 move가 있음"))

    return results


def aggregate(results: Sequence[RuleResult]) -> SafetyDecision:
    """BLOCK > INSUFFICIENT_DATA > ALLOW. 정보 부족을 통과로 승격하지 않는다."""
    if any(r.status is RuleStatus.BLOCK for r in results):
        return SafetyDecision.BLOCK
    if any(r.status is RuleStatus.INSUFFICIENT_DATA for r in results):
        return SafetyDecision.ASK
    return SafetyDecision.ALLOW


def blocking_results(results: Sequence[RuleResult]) -> list[RuleResult]:
    return [r for r in results
            if r.status in (RuleStatus.BLOCK, RuleStatus.INSUFFICIENT_DATA)]
