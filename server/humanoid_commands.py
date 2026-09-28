"""G1(휴머노이드) 웹 명령 해석 — **규칙만**, 선언된 지점만.

지원(이번 범위):
| 뜻 | 예 | 결과 |
|---|---|---|
| 정지 | “멈춰”, “정지”, “스톱” | STOP — 확인 없이 즉시 |
| 지점으로 가기 | “컨베이어 앞에 가” | CONFIRM(goto conveyor_front) |
| 찍고 오기 | “컨베이어 한번 찍고 와” | CONFIRM(지금 위치 저장 → 지점 → 저장한 출발 위치) |
| 출발 위치로 | “출발 위치로 돌아와” | CONFIRM(이 세션 마지막 작업의 출발 위치) / 없으면 ASK |

- 목적지가 없거나 선언되지 않은 곳(“저기로 가”, “2미터 앞으로”)이면 ASK — 좌표를 만들지 않는다.
- 팔·손 작업(“집어”, “들어”)은 BLOCK(범위 밖).
- 숫자 거리·각도 이동은 BLOCK(임의 목적지 이동 범위 밖).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

STOP_WORDS = ("멈춰", "멈춤", "멈추", "정지", "스톱", "stop", "그만")
TAG_WORDS = ("찍고와", "찍고오", "찍고돌아", "갔다와", "갔다오", "다녀와", "다녀오",
             "터치하고와", "들렀다와", "들렀다오")
RETURN_PLACES = ("출발위치", "출발점", "출발지", "처음위치", "처음자리", "원래위치", "원래자리")
RETURN_VERBS = ("돌아와", "돌아가", "돌아오", "복귀")
MOVE_VERBS = ("가", "가줘", "가자", "이동", "걸어", "와")
ARM_WORDS = ("집어", "잡아", "들어올", "들고", "옮겨", "놓아", "내려놔", "팔을", "팔로", "팔작업",
             "손을", "손으로")         # “팔레트”와 겹치지 않게 “팔” 단독은 쓰지 않는다
#: 선언된 지점의 별칭 — site.json 이름에 묶는다(여기서 좌표를 만들지 않는다).
PLACE_ALIASES = {"conveyor_front": ("컨베이어",)}


@dataclass
class HumanoidSpec:
    decision: str                   # STOP | CONFIRM | ASK | BLOCK
    intent: str | None = None       # stop | goto | tag | return
    target: str | None = None       # 선언된 지점 이름
    reason: str | None = None
    evidence: list[str] = field(default_factory=list)
    normalized: str = ""

    def to_dict(self) -> dict:
        return {"decision": self.decision, "intent": self.intent, "target": self.target,
                "reason": self.reason, "evidence": list(self.evidence),
                "normalized": self.normalized}


def normalize(text: str) -> str:
    return re.sub(r"[\s.,!?~·'\"“”‘’]+", "", (text or "").lower())


def _find(text: str, words) -> str | None:
    return next((w for w in words if w in text), None)


def parse(utterance: str, safe_points: dict) -> HumanoidSpec:
    t = normalize(utterance)
    if not t:
        return HumanoidSpec("ASK", reason="명령이 비어 있다", normalized=t)
    stop = _find(t, STOP_WORDS)
    if stop:
        return HumanoidSpec("STOP", "stop", evidence=[f"정지 낱말 '{stop}'"], normalized=t)
    arm = _find(t, ARM_WORDS)
    if arm:
        return HumanoidSpec("BLOCK", reason=f"팔·손 작업('{arm}')은 이번 범위 밖이다 — G1은 다리 12관절만 "
                            "제어하고 팔·허리는 고정이다", normalized=t)
    if re.search(r"\d", t):
        return HumanoidSpec("BLOCK", reason="거리·각도를 지정한 임의 이동은 지원하지 않는다 — "
                            "선언된 지점으로만 간다", normalized=t)
    place = None
    for name, aliases in PLACE_ALIASES.items():
        alias = _find(t, aliases)
        if alias and name in safe_points:
            place = (name, alias)
            break
    tag = _find(t, TAG_WORDS)
    ret_place = _find(t, RETURN_PLACES)
    ret_verb = _find(t, RETURN_VERBS)
    if tag:
        if place is None:
            return HumanoidSpec("ASK", "tag", reason="어디를 찍고 올지 모르겠다 — 선언된 지점: "
                                + _names(safe_points), evidence=[f"왕복 낱말 '{tag}'"], normalized=t)
        return HumanoidSpec("CONFIRM", "tag", place[0],
                            evidence=[f"지점 낱말 '{place[1]}' → {place[0]}", f"왕복 낱말 '{tag}'"],
                            normalized=t)
    if ret_verb and (ret_place or place is None):
        if place is not None and ret_place is None:
            pass                     # “컨베이어로 돌아가” — 아래 지점 이동으로 본다
        else:
            ev = [f"복귀 낱말 '{ret_verb}'"] + ([f"출발 위치 낱말 '{ret_place}'"] if ret_place else [])
            return HumanoidSpec("CONFIRM", "return", "start", evidence=ev, normalized=t)
    verb = _find(t, MOVE_VERBS + RETURN_VERBS)
    if place is not None and verb:
        return HumanoidSpec("CONFIRM", "goto", place[0],
                            evidence=[f"지점 낱말 '{place[1]}' → {place[0]}", f"이동 낱말 '{verb}'"],
                            normalized=t)
    if verb:
        return HumanoidSpec("ASK", "goto", reason="목적지가 불명확하다 — 선언된 지점: "
                            + _names(safe_points), normalized=t)
    return HumanoidSpec("ASK", reason="G1이 할 수 있는 명령: 컨베이어 앞에 가 · 컨베이어 한번 찍고 와 · "
                        "출발 위치로 돌아와 · 멈춰", normalized=t)


def _names(safe_points: dict) -> str:
    return ", ".join(f"{p.get('label_ko', n)}({n})" for n, p in safe_points.items())
