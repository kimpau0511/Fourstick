"""
L3 Safety Guard 단독 검증. LLM을 거치지 않고 손으로 만든 위반 계획을
check_plan_safety()에 직접 넣어서, 8개 규칙이 실제로 걸리는지 확인한다.

EXAONE은 대체로 무난한 계획만 만들어서 자연스럽게 위반 케이스가 잘 안 나오기
때문에, 규칙 코드 자체의 버그는 이렇게 직접 테스트하지 않으면 못 잡는다.

실행: python3 test_safety_guard.py
"""

from safety_guard import check_plan_safety


def _codes(plan: dict) -> set[str]:
    return {v["code"] for v in check_plan_safety(plan)}


CASES = [
    ("정상 계획 (위반 없어야 함)",
     {"steps": [
         {"skill": "move", "args": {"target": "2번 팔레트"}},
         {"skill": "pick", "args": {"object": "A자재", "from": "2번 팔레트"}},
         {"skill": "move", "args": {"target": "컨베이어"}},
         {"skill": "place", "args": {"object": "A자재", "to": "컨베이어"}},
         {"skill": "home", "args": {}},
     ]},
     set()),

    ("E-SEQ-001: 빈 계획",
     {"steps": []},
     {"E-SEQ-001"}),

    ("E-SEQ-002: home으로 안 끝남",
     {"steps": [
         {"skill": "move", "args": {"target": "2번 팔레트"}},
     ]},
     {"E-SEQ-002"}),

    ("E-SEQ-003: pick 직전에 move 없음",
     {"steps": [
         {"skill": "pick", "args": {"object": "A자재", "from": "2번 팔레트"}},
         {"skill": "home", "args": {}},
     ]},
     {"E-SEQ-003"}),

    ("E-SEQ-004: place 직전에 move 없음 (다른 위치에서 바로 place)",
     {"steps": [
         {"skill": "move", "args": {"target": "2번 팔레트"}},
         {"skill": "pick", "args": {"object": "A자재", "from": "2번 팔레트"}},
         {"skill": "place", "args": {"object": "A자재", "to": "컨베이어"}},
         {"skill": "home", "args": {}},
     ]},
     {"E-SEQ-004"}),

    ("E-HOLD-001: 이미 들고 있는데 또 pick",
     {"steps": [
         {"skill": "move", "args": {"target": "2번 팔레트"}},
         {"skill": "pick", "args": {"object": "A자재", "from": "2번 팔레트"}},
         {"skill": "move", "args": {"target": "3번 팔레트"}},
         {"skill": "pick", "args": {"object": "B자재", "from": "3번 팔레트"}},
         {"skill": "home", "args": {}},
     ]},
     {"E-HOLD-001"}),

    ("E-HOLD-002: 아무것도 안 들고 place",
     {"steps": [
         {"skill": "move", "args": {"target": "컨베이어"}},
         {"skill": "place", "args": {"object": "A자재", "to": "컨베이어"}},
         {"skill": "home", "args": {}},
     ]},
     {"E-HOLD-002"}),

    ("E-LIMIT-001: 스텝 수 초과 (21개)",
     {"steps": [{"skill": "move", "args": {"target": "2번 팔레트"}} for _ in range(20)]
              + [{"skill": "home", "args": {}}]},
     {"E-LIMIT-001"}),

    ("E-ARG-001: Capability Profile 밖의 위치",
     {"steps": [
         {"skill": "move", "args": {"target": "존재하지않는위치"}},
         {"skill": "home", "args": {}},
     ]},
     {"E-ARG-001"}),
]


if __name__ == "__main__":
    passed = 0
    for name, plan, expected_codes in CASES:
        actual_codes = _codes(plan)
        # "위반 없어야 함" 케이스는 정확히 일치해야 하고,
        # 위반 케이스는 기대한 코드가 결과 안에 포함되기만 하면 통과
        # (한 계획이 여러 규칙에 동시에 걸릴 수 있어서 부분집합으로 체크).
        ok = expected_codes.issubset(actual_codes) if expected_codes else (actual_codes == set())
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        print(f"[{status}] {name}")
        print(f"       기대: {expected_codes or '(없음)'} / 실제: {actual_codes or '(없음)'}")

    print(f"\n{passed}/{len(CASES)} 통과")
