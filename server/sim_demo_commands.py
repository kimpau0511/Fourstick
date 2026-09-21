"""시뮬레이션 시연 **전용 명령 라우터** — 텍스트·STT final 발화 → 시연 작업.

LLM·계획 생성을 거치지 않는 좁은 규칙 해석기다. 일반 `/v1/plan`의 계획 생성과
pick/place 차단은 이 모듈과 무관하다. 여기서 만드는 것은 기존 시연 작업
(`server/sim_demo_jobs.py`)의 job spec뿐이다.

지원 발화(자재 이름은 셀 설정의 한글 이름에서 온다):

| 발화 | 결정 |
|---|---|
| "A 자재를 컨베이어로 옮겨줘" | RUN transfer(material_a) |
| "A 자재를 원래 자리로 돌려놔" | RUN return(material_a) — held_on_target일 때만 |
| "돌려놔"(자재 생략) | RUN return — 컨베이어에 유지 중인 자재가 **하나**일 때만 |
| "멈춰" / "정지" / "스톱" | STOP — 즉시 시연 정지 요청 |
| "이어서 해줘" | RUN resume — 체크포인트가 **하나**일 때만 |

모호하거나 지금 상태로 할 수 없으면 ASK/BLOCK만 돌려주고 작업을 만들지 않는다.
컨베이어는 단일 배치 위치다 — "N번 슬롯/자리/위치" 발화는 ASK로 안내한다.

시뮬레이션 명령이 **아닌** 발화(예: "안전 위치로 복귀해줘", "1번 팔레트로
이동")는 `PASS_THROUGH`다. 화면은 그때만 기존 계획 생성(`/v1/plan`)으로 간다.
자재를 가리키는 발화인데 불완전하면 계획 생성으로 넘기지 않고 ASK로 끝낸다.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from server.sim_demo_jobs import available_actions
from validation.simulation_demo_state import HELD_ON_TARGET

SIMULATION_NOTICE = "Gazebo 시뮬레이션 · 실제 로봇 아님"
RUN, STOP, ASK, BLOCK = "RUN", "STOP", "ASK", "BLOCK"
#: 시뮬레이션 명령이 아니다 — 기존 계획 생성 경로로 보낸다.
PASS_THROUGH = "PASS_THROUGH"
#: 작업을 만들 수 있는 입력 출처. **partial은 없다** — 표시만 한다.
SOURCES = ("text", "stt_final")

_STOP_WORDS = ("멈춰", "멈춤", "정지", "스톱", "stop")
_RESUME_WORDS = ("이어서", "이어가", "재개", "계속해")
_RETURN_WORDS = ("원래자리", "원래위치", "원위치", "제자리", "복귀", "되돌", "돌려놔",
                 "돌려놓")
#: 자재를 말하지 않아도 **자재 복귀**로 보는 말. "복귀"는 여기 없다 — "안전
#: 위치로 복귀해줘"는 일반 명령이고 계획 생성이 맡는다.
_BARE_RETURN_WORDS = ("원래자리", "원래위치", "원위치", "제자리", "돌려놔", "돌려놓",
                      "되돌려")
_TRANSFER_VERBS = ("옮겨", "옮기", "올려", "이송", "가져다", "갖다", "놓아", "놔")
_SLOT = re.compile(r"(슬롯|\d+\s*번\s*(자리|위치|칸))")
_PALLET = re.compile(r"(\d+)\s*번\s*팔레트")
#: 라틴 글자의 한글 읽기. STT가 "에이 자재"처럼 적는 경우를 받는다.
_LETTER_READINGS = {"a": ("에이",), "b": ("비",), "c": ("씨", "시"),
                    "d": ("디",), "e": ("이",), "f": ("에프",)}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def material_aliases(workcell: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """자재 모델 → 발화에서 찾을 이름들. 셀 설정의 한글 이름에서만 만든다."""
    out: dict[str, tuple[str, ...]] = {}
    for row in workcell.get("resource_map", ()):
        model = row.get("gazebo_model")
        if (workcell["models"].get(model) or {}).get("kind") != "material":
            continue
        korean = _normalize(row.get("korean") or "")        # 예: "a자재"
        names = {korean}
        match = re.fullmatch(r"([a-z])자재", korean)
        if match:
            for reading in _LETTER_READINGS.get(match.group(1), ()):
                names.add(f"{reading}자재")
        out[model] = tuple(sorted(n for n in names if n))
    return out


def pallet_numbers(workcell: Mapping[str, Any]) -> dict[str, str]:
    """"N번 팔레트" 번호 → 팔레트 모델. 셀 설정의 한글 이름에서만 만든다."""
    out: dict[str, str] = {}
    for row in workcell.get("resource_map", ()):
        match = re.fullmatch(r"(\d+)번팔레트", _normalize(row.get("korean") or ""))
        if match:
            out[match.group(1)] = row.get("gazebo_model")
    return out


def parse_command(text: str, workcell: Mapping[str, Any]) -> dict:
    """발화만 보고 의도를 정한다. 상태는 보지 않는다(`decide`가 본다)."""
    normalized = _normalize(text)
    if not normalized:
        return {"intent": None, "decision": PASS_THROUGH, "material": None,
                "reason": "발화가 비어 있다"}
    if any(word in normalized for word in _STOP_WORDS):
        # 정지는 다른 해석보다 먼저다. 계획·LLM을 거치지 않는다.
        return {"intent": "stop", "decision": STOP, "material": None}
    aliases = material_aliases(workcell)
    mentioned = sorted(model for model, names in aliases.items()
                       if any(name in normalized for name in names))
    material = mentioned[0] if len(mentioned) == 1 else None
    resuming = any(word in normalized for word in _RESUME_WORDS)
    bare_return = any(word in normalized for word in _BARE_RETURN_WORDS)
    # 자재를 가리키지 않고 이어서·정지도 아니면 **시뮬레이션 명령이 아니다.**
    # (예: "안전 위치로 복귀해줘" — 기존 계획 생성이 맡는다)
    about_material = bool(mentioned) or (
        "자재" in normalized and ("컨베이어" in normalized
                                  or any(w in normalized for w in _RETURN_WORDS)
                                  or any(v in normalized for v in _TRANSFER_VERBS)))
    if not about_material and not resuming and not bare_return:
        return {"intent": None, "decision": PASS_THROUGH, "material": None,
                "reason": "시뮬레이션 시연 명령이 아니다 — 일반 계획 생성으로 간다"}
    if _SLOT.search(normalized):
        return {"intent": None, "decision": ASK, "material": material,
                "reason": "컨베이어는 단일 배치 위치입니다 — 슬롯·번호 위치는 지정할 수"
                          " 없습니다. \"A 자재를 컨베이어로 옮겨줘\"처럼 말해 주세요"}
    if resuming:
        if len(mentioned) > 1:
            return {"intent": "resume", "decision": ASK, "material": None,
                    "reason": f"자재가 여럿입니다: {', '.join(mentioned)}"}
        return {"intent": "resume", "decision": RUN, "material": material}
    if len(mentioned) != 1:
        if not mentioned and bare_return:
            # 자재를 말하지 않은 복귀. 컨베이어에 유지 중인 자재가 하나면 그것이다.
            return {"intent": "return", "decision": RUN, "material": None,
                    "mentioned_pallets": {}}
        return {"intent": None, "decision": ASK, "material": None,
                "reason": ("어느 자재인지 알 수 없습니다 — A·B·C 자재 중 하나를 말해 주세요"
                           if not mentioned else
                           f"자재를 하나만 말해 주세요: {', '.join(mentioned)}")}
    pallets = {number: pallet_numbers(workcell).get(number)
               for number in _PALLET.findall(normalized)}
    if any(word in normalized for word in _RETURN_WORDS):
        intent = "return"
    elif "컨베이어" in normalized and any(v in normalized for v in _TRANSFER_VERBS):
        intent = "transfer"
    else:
        return {"intent": None, "decision": ASK, "material": material,
                "reason": "동작을 알 수 없습니다 — \"컨베이어로 옮겨줘\" 또는"
                          " \"원래 자리로 돌려놔\"라고 말해 주세요"}
    return {"intent": intent, "decision": RUN, "material": material,
            "mentioned_pallets": pallets}


def decide(parsed: Mapping[str, Any], status: Mapping[str, Any],
           materials: Mapping[str, Mapping[str, Any]]) -> dict:
    """해석 결과를 **지금 상태**에 맞춰 RUN/ASK/BLOCK과 job spec으로 정한다."""
    if parsed.get("decision") in (ASK, STOP) or parsed.get("intent") is None:
        return dict(parsed)
    state = status.get("state") or {}
    running = status.get("running_job")
    if running:
        return {**parsed, "decision": BLOCK,
                "reason": f"다른 시연 작업이 실행 중입니다({running.get('job_id')}) —"
                          " \"멈춰\"로 정지할 수 있습니다"}
    intent = parsed["intent"]
    if intent == "resume":
        models = list(state.get("checkpoints") or [])
        if not models:
            return {**parsed, "decision": BLOCK, "reason": "이어서 할 STOP 체크포인트가 없습니다"}
        if len(models) > 1:
            return {**parsed, "decision": ASK,
                    "reason": f"체크포인트가 여럿입니다: {', '.join(models)}"}
        model = models[0]
        checkpoint = state.get("checkpoint") or {}
        if parsed.get("material") and parsed["material"] != model:
            return {**parsed, "decision": BLOCK,
                    "reason": f"체크포인트는 {model}의 것입니다"}
        if checkpoint.get("model") != model or not available_actions(state, model)["resume"]:
            return {**parsed, "decision": BLOCK,
                    "reason": "체크포인트에서 이어서 할 수 없는 상태입니다(자재가 들려 있지 않음)"}
        return {**parsed, "decision": RUN, "material": model,
                "job_spec": {"action": "resume", "material": model,
                             "checkpoint_id": checkpoint.get("checkpoint_id")}}
    model = parsed["material"]
    if model is None and intent == "return":
        # 자재를 말하지 않았다 — 지금 컨베이어에 유지 중인 자재에서 찾는다.
        held = sorted(name for name, row in (state.get("objects") or {}).items()
                      if (row or {}).get("state") == HELD_ON_TARGET)
        if not held:
            return {**parsed, "decision": BLOCK,
                    "reason": "컨베이어에 유지 중인 자재가 없습니다 — 돌려놓을 것이 없습니다"}
        if len(held) > 1:
            names = ", ".join((materials.get(m) or {}).get("korean", m) for m in held)
            return {**parsed, "decision": ASK,
                    "reason": f"컨베이어에 자재가 여럿입니다 — 어느 것을 돌려놓을까요? ({names})"}
        model = held[0]
        parsed = {**parsed, "material": model}
    spec = materials.get(model) or {}
    for number, pallet in (parsed.get("mentioned_pallets") or {}).items():
        if pallet != spec.get("support_model"):
            return {**parsed, "decision": BLOCK,
                    "reason": f"{spec.get('korean', model)}은(는) {number}번 팔레트에"
                              " 있지 않습니다"}
    actions = available_actions(state, model)
    if not actions[intent]:
        reason = ("원래 자리로 돌릴 수 있는 것은 컨베이어에 유지 중(held_on_target)인"
                  " 자재뿐입니다" if intent == "return" else
                  "셀이 초기 상태가 아닙니다 — 컨베이어가 비어 있고 모든 자재가 제자리여야"
                  " 합니다(남은 기록: " + (", ".join(state.get("objects") or {}) or
                                          ", ".join(state.get("checkpoints") or [])
                                          or "없음") + ")")
        return {**parsed, "decision": BLOCK, "reason": reason}
    return {**parsed, "decision": RUN,
            "job_spec": {"action": intent, "material": model, "checkpoint_id": None}}
