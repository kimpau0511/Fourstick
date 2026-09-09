"""
L3 Safety Guard — Task Plan이 완성된 뒤, 실행 가능한 형태로 넘어가기 전에
결정론적 규칙으로 최종 검사한다.

⚠️ 여기 있는 8개 규칙(E-SEQ-001~004, E-HOLD-001~002, E-LIMIT-001, E-ARG-001)은
기획서의 정확한 7개 안전규칙 목록을 아직 재확인 못 해서 만든 합리적인 추정 세트다.
원본 기획서 확정되면 코드/메시지를 거기에 맞춰 교체해야 한다.

설계 원칙: 이 계층은 상위 레이어(L1/L2/Pydantic 검증)를 신뢰하지 않는다.
이미 한 번 통과한 계획이라도 여기서 처음부터 다시 독립적으로 검사한다 —
나중에 다른 경로(예: 사람이 직접 수정한 계획, 다른 소스에서 온 계획)로
Task Plan이 들어와도 이 검사만은 항상 걸리도록 하기 위함.
"""

from typing import Optional

from pipeline import CAPABILITY_PROFILE

MAX_STEPS = 20


def check_plan_safety(task_plan: dict) -> list[dict]:
    """위반 사항 목록을 반환. 비어 있으면 안전하다고 판단."""
    steps = task_plan.get("steps", [])
    violations: list[dict] = []

    # E-SEQ-001: 빈 계획 금지
    if not steps:
        violations.append({"code": "E-SEQ-001", "message": "빈 작업 계획"})
        return violations  # 더 검사할 스텝이 없음

    # E-SEQ-002: 반드시 home 스킬로 종료
    if steps[-1].get("skill") != "home":
        violations.append({"code": "E-SEQ-002", "message": "계획이 home 스킬로 끝나지 않음"})

    # E-LIMIT-001: 스텝 수 상한 (폭주/루프성 계획 방지)
    if len(steps) > MAX_STEPS:
        violations.append({
            "code": "E-LIMIT-001",
            "message": f"스텝 수 {len(steps)}개가 상한 {MAX_STEPS}개를 초과함",
        })

    # E-ARG-001: 모든 위치/물건이 Capability Profile 안에 있는지 재검증 (방어적 재확인)
    for i, step in enumerate(steps):
        skill = step.get("skill")
        args = step.get("args", {}) or {}
        if skill == "move" and args.get("target") not in CAPABILITY_PROFILE["locations"]:
            violations.append({"code": "E-ARG-001",
                                "message": f"steps[{i}] move.target이 Capability Profile 밖: {args.get('target')!r}"})
        if skill in ("pick", "place") and args.get("object") not in CAPABILITY_PROFILE["objects"]:
            violations.append({"code": "E-ARG-001",
                                "message": f"steps[{i}] {skill}.object가 Capability Profile 밖: {args.get('object')!r}"})
        if skill == "pick" and args.get("from") is not None and args.get("from") not in CAPABILITY_PROFILE["locations"]:
            violations.append({"code": "E-ARG-001",
                                "message": f"steps[{i}] pick.from이 Capability Profile 밖: {args.get('from')!r}"})
        if skill == "place" and args.get("to") not in CAPABILITY_PROFILE["locations"]:
            violations.append({"code": "E-ARG-001",
                                "message": f"steps[{i}] place.to가 Capability Profile 밖: {args.get('to')!r}"})

    # E-HOLD-001/002: 그리퍼 상태 추적 — 들고 있지 않은데 놓거나, 들고 있는데 또 집으면 안 됨
    held: Optional[str] = None
    for i, step in enumerate(steps):
        skill = step.get("skill")
        args = step.get("args", {}) or {}
        if skill == "pick":
            if held is not None:
                violations.append({"code": "E-HOLD-001",
                                    "message": f"steps[{i}]: 이미 {held!r}를 들고 있는 상태에서 다시 pick 시도"})
            held = args.get("object")
        elif skill == "place":
            if held is None:
                violations.append({"code": "E-HOLD-002",
                                    "message": f"steps[{i}]: 아무것도 들고 있지 않은 상태에서 place 시도"})
            elif args.get("object") != held:
                violations.append({"code": "E-HOLD-002",
                                    "message": f"steps[{i}]: {held!r}를 들고 있는데 {args.get('object')!r}를 놓으려 함"})
            held = None

    # E-SEQ-003: pick 직전에는 반드시 같은 위치로의 move가 있어야 함
    for i, step in enumerate(steps):
        if step.get("skill") == "pick":
            prev = steps[i - 1] if i > 0 else None
            expected = (step.get("args") or {}).get("from")
            if not prev or prev.get("skill") != "move" or (prev.get("args") or {}).get("target") != expected:
                violations.append({"code": "E-SEQ-003",
                                    "message": f"steps[{i}]: pick 직전에 같은 위치({expected!r})로의 move가 없음"})

    # E-SEQ-004: place 직전에는 반드시 같은 위치로의 move가 있어야 함
    for i, step in enumerate(steps):
        if step.get("skill") == "place":
            prev = steps[i - 1] if i > 0 else None
            expected = (step.get("args") or {}).get("to")
            if not prev or prev.get("skill") != "move" or (prev.get("args") or {}).get("target") != expected:
                violations.append({"code": "E-SEQ-004",
                                    "message": f"steps[{i}]: place 직전에 같은 위치({expected!r})로의 move가 없음"})

    return violations
