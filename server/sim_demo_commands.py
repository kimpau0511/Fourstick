"""시뮬레이션 시연 **전용 명령 라우터** — 텍스트·STT final 발화 → 시연 작업.

LLM·계획 생성을 거치지 않는 좁은 규칙 해석기다. 일반 `/v1/plan`의 계획 생성과
pick/place 차단은 이 모듈과 무관하다. 여기서 만드는 것은 기존 시연 작업
(`server/sim_demo_jobs.py`)의 job spec뿐이다.

지원 발화(자재 이름은 셀 설정의 한글 이름에서 온다):

| 발화 | 결정 |
|---|---|
| "A 자재를 컨베이어로 옮겨줘" | RUN transfer(material_a) |
| "주황 자재를 컨베이어로 옮겨줘" | 같은 것 — 색 이름은 셀 설정 선언에서 온다 |
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
from typing import Any, Mapping, Sequence

from server.sim_demo_jobs import available_actions
from validation.conveyor_slots import (
    assign_slot,
    occupancy,
    record_on_conveyor,
    record_slot,
    slot_label,
    slots_full,
)
from validation.simulation_demo_state import HELD_ON_TARGET

SIMULATION_NOTICE = "Gazebo 시뮬레이션 · 실제 로봇 아님"
RUN, STOP, ASK, BLOCK = "RUN", "STOP", "ASK", "BLOCK"
#: 시뮬레이션 명령이 아니다 — 기존 계획 생성 경로로 보낸다.
PASS_THROUGH = "PASS_THROUGH"
#: 작업을 만들 수 있는 입력 출처. **partial은 없다** — 표시만 한다.
SOURCES = ("text", "stt_final")

# 시연 명령 경로의 정지 낱말 — 일반 경로 설정(stt_policy.stop_keywords)과 같은 목록(2026-10-08).
_STOP_WORDS = ("정지", "멈춰", "스톱", "스탑", "중지", "멈추", "stop", "그만")
_RESUME_WORDS = ("이어서", "이어가", "재개", "계속해")
_RETURN_WORDS = ("원래자리", "원래위치", "원위치", "제자리", "복귀", "되돌", "돌려놔",
                 "돌려놓")
#: 자재를 말하지 않아도 **자재 복귀**로 보는 말. "복귀"는 여기 없다 — "안전
#: 위치로 복귀해줘"는 일반 명령이고 계획 생성이 맡는다.
_BARE_RETURN_WORDS = ("원래자리", "원래위치", "원위치", "제자리", "돌려놔", "돌려놓",
                      "되돌려")
_TRANSFER_VERBS = ("옮겨", "옮기", "올려", "이송", "가져다", "갖다", "놓아", "놔")
#: "2번 자리/위치/칸" · "슬롯2" — 컨베이어의 **지정된 자리**를 가리키는 말.
#: 정규화된(공백 없는) 문장에서 찾는다. "1번 팔레트"는 `_PALLET`가 따로 잡는다.
_SLOT_AT = re.compile(r"(\d+)번(?:자리|위치|칸|슬롯)")
_SLOT_N = re.compile(r"슬롯(\d+)")
#: "컨베이어 2번에"처럼 뒤에 위치·칸이 없는 꼴도 칸 지정이다. 놓치면 첫 빈 칸으로
#: 확인 없이 실행된다(실측 2026-09-24 평가: n02·d15). "N번 팔레트"는 아니다.
_CONVEYOR_N = re.compile(r"컨베이어(?:의)?(\d+)번(?!팔레트)")
_PALLET = re.compile(r"(\d+)\s*번\s*팔레트")
#: 번호 없이 "팔레트로/팔레트에"(출발지 "팔레트에서"는 아님) — 자재의 **자기 원래 팔레트**.
#: "초록자재다시팔레트로가져다놔"처럼 복귀 낱말 없이 돌려놓으라는 말이다.
_OWN_PALLET_DEST = re.compile(r"(?<!번)팔레트(?:로|에)(?!서)")
#: 라틴 글자의 한글 읽기. STT가 "에이 자재"처럼 적는 경우를 받는다.
_LETTER_READINGS = {"a": ("에이",), "b": ("비",), "c": ("씨", "시"),
                    "d": ("디",), "e": ("이",), "f": ("에프",)}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def material_aliases(workcell: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """자재 모델 → 발화에서 찾을 이름들. 셀 설정의 한글 이름에서만 만든다.

    색 이름도 셀 설정이 선언한 것(`resource_map[].korean_colors`)만 쓴다. 모델의
    `color_rgba`에서 색 이름을 **추측하지 않는다** — RGB를 한국어 낱말로 옮기는
    규칙을 코드가 만들면 그건 선언이 아니라 추측이다.

    색은 항상 `<색>자재` 꼴로만 붙인다. 낱말 하나("주황")가 자재를 가리키게 하면
    다른 대상을 말한 발화까지 자재 언급으로 끌려온다.
    """
    out: dict[str, tuple[str, ...]] = {}
    for row in workcell.get("resource_map", ()):
        model = row.get("gazebo_model")
        if (workcell["models"].get(model) or {}).get("kind") != "material":
            continue
        # 표시 이름(예: "원형 자재")과 예전 이름(korean_aliases, 예: "C자재") 모두. 예전 이름의 글자 읽기
        # ("씨자재")와 모양 낱말("원형자재", "동그라미자재")도 붙인다(2026-10-07 형상 변경).
        korean = _normalize(row.get("korean") or "")        # 예: "원형자재"
        names = {korean}
        for alias in (korean, *(_normalize(a) for a in row.get("korean_aliases") or ())):
            names.add(alias)
            match = re.fullmatch(r"([a-z])자재", alias)
            if match:
                for reading in _LETTER_READINGS.get(match.group(1), ()):
                    names.add(f"{reading}자재")
        for shape in row.get("korean_shapes") or ():
            shape = _normalize(shape)
            if shape:
                names.add(f"{shape}자재")
        for color in row.get("korean_colors") or ():
            color = _normalize(color)
            if color:
                names.add(f"{color}자재")
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


def pallet_resources(workcell: Mapping[str, Any]) -> dict[str, str]:
    """"N번 팔레트" 번호 → **선언된 자원 id**. id를 코드가 짓지 않는다."""
    out: dict[str, str] = {}
    for row in workcell.get("resource_map", ()):
        match = re.fullmatch(r"(\d+)번팔레트", _normalize(row.get("korean") or ""))
        if match and row.get("resource_id"):
            out[match.group(1)] = str(row["resource_id"])
    return out


def spoken_places(text: str, workcell: Mapping[str, Any],
                  slots: Sequence[Any] = ()) -> dict[str, str | None]:
    """발화가 부른 자리 → 자원 id. **없는 자리는 만들지 않는다.**

    컨베이어 자리는 검증된 슬롯 이름(`slot_N`)이고, 슬롯이 없는 구성에서는
    번호를 말해도 자리가 잡히지 않는다(그 경우 `decide`가 BLOCK으로 끝낸다).
    돌려주는 값은 `{"slot": ..., "pallet": ..., "conveyor": bool}`이다.
    """
    normalized = _normalize(text)
    number = None
    match = (_SLOT_AT.search(normalized) or _SLOT_N.search(normalized)
             or _CONVEYOR_N.search(normalized))
    if match:
        number = match.group(1)
    slot_id = None
    if number is not None:
        wanted = f"slot_{number}"
        names = {getattr(slot, "name", None) for slot in slots or ()}
        # 선언된 슬롯이면 그 이름, 아니면 **말한 그대로** 넘긴다 — 없는 자리라는
        # 사실은 자리 검증이 말해야 한다(여기서 조용히 지우지 않는다).
        slot_id = wanted if (not names or wanted in names) else wanted
    pallets = pallet_resources(workcell)
    pallet_id = None
    numbers = _PALLET.findall(normalized)
    if len(numbers) == 1:
        pallet_id = pallets.get(numbers[0])
    return {"slot": slot_id, "pallet": pallet_id,
            "conveyor": mentions_conveyor(text, workcell)}


def stop_intent(text: str):
    """정지 판정(planning/stop_intent, 2026-10-08 리뷰 10번). **띄어쓰기를 지우기 전 원문**을 본다."""
    from planning.stop_intent import classify_stop

    return classify_stop(text, _STOP_WORDS)


def is_stop_command(text: str) -> bool:
    """분명한 정지 명령인가. 다른 해석보다 먼저 본다 — LLM을 거치지 않는다.
    '스톱워치'(다른 낱말의 일부)·'정지하지 마'(부정)·'정지'라는 말(인용)은 정지가 아니다."""
    return stop_intent(text).is_stop


def parse_command(text: str, workcell: Mapping[str, Any],
                  slots: Sequence[Any] = ()) -> dict:
    """발화만 보고 의도와 **말한 자리**를 정한다. 상태는 보지 않는다(`decide`)."""
    normalized = _normalize(text)
    if not normalized:
        return {"intent": None, "decision": PASS_THROUGH, "material": None,
                "reason": "발화가 비어 있다"}
    stop = stop_intent(text)
    if stop.is_stop:
        # 정지는 다른 해석보다 먼저다. 계획·LLM을 거치지 않는다.
        return {"intent": "stop", "decision": STOP, "material": None}
    if stop.kind == "ambiguous":
        from planning.stop_intent import AMBIGUOUS_STOP_MESSAGE

        return {"intent": None, "decision": ASK, "material": None, "reason": AMBIGUOUS_STOP_MESSAGE}
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
    spoken = spoken_places(text, workcell, slots)
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
    elif (not pallets and not spoken["conveyor"] and not spoken["slot"]
            and _OWN_PALLET_DEST.search(normalized)
            and any(v in normalized for v in _TRANSFER_VERBS)):
        intent = "return"
    elif ((spoken["conveyor"] or spoken["slot"])
            and any(v in normalized for v in _TRANSFER_VERBS)):
        intent = "transfer"
    else:
        return {"intent": None, "decision": ASK, "material": material,
                "reason": "동작을 알 수 없습니다 — \"컨베이어로 옮겨줘\" 또는"
                          " \"원래 자리로 돌려놔\"라고 말해 주세요"}
    return {"intent": intent, "decision": RUN, "material": material,
            "mentioned_pallets": pallets,
            **_spoken_route(intent, spoken)}


def _spoken_route(intent: str, spoken: Mapping[str, Any]) -> dict:
    """말한 자리를 동작에 맞춰 출발·도착으로 놓는다.

    이송은 팔레트가 출발이고 컨베이어 자리가 도착이다. 복귀는 그 반대다.
    말하지 않은 쪽은 `None`으로 둔다 — 서버가 채워 넣지 않는다.
    """
    from server.sim_demo_places import CONVEYOR_ID

    conveyor = spoken["slot"] or (CONVEYOR_ID if spoken["conveyor"] else None)
    if intent == "transfer":
        return {"source": spoken["pallet"], "destination": conveyor}
    if intent == "return":
        return {"source": conveyor, "destination": spoken["pallet"]}
    return {"source": None, "destination": None}


#: 옮길 **대상**을 가리키는 말. 규칙이 ASK/PASS_THROUGH로 끝났을 때 **분류기에
#: 보낼지**만 정한다. 여기 걸린다고 실행되지 않는다 — 분류·검증·확인을 모두
#: 지나야 작업이 만들어진다.
#:
#: "팔레트"는 **없다.** 팔레트는 일반 이동 명령의 목적지로도 쓰여서("1번
#: 팔레트로 이동한 뒤 안전 위치로 복귀해줘") 넣으면 일반 명령이 분류기로
#: 끌려온다(실측: 기존 PASS_THROUGH 테스트가 ASK로 바뀌었다).
#: "자리"는 넣는다 — "저쪽 빈자리에 옮겨줘"처럼 자재 이름 없이 자리만 말하는
#: 자연어 이송을 분류기로 보내기 위해서다. "복귀"는 여전히 없다(일반 명령의
#: "안전 위치로 복귀"와 구분되지 않는다).
_MATERIAL_WORK_HINTS = ("자재", "컨베이어", "블록", "물건", "박스", "자리",
                        "물체", "모형", "벨트",
                        "이어서", "이어가", "재개", "계속해")
#: 분류기로 보낼지 정할 때만 더 보는 옮김 동사. 규칙 해석(`_TRANSFER_VERBS`)은 바꾸지 않는다.
_CLASSIFIER_ONLY_VERBS = ("보내", "치워")


#: 컨베이어 자원 id. 셀 설정이 선언한 한글 이름을 여기서 찾는다.
CONVEYOR_RESOURCE = "loc_conveyor"


def conveyor_names(workcell: Mapping[str, Any]) -> tuple[str, ...]:
    """컨베이어를 부르는 **선언된** 이름. 낱말을 만들어 내지 않는다."""
    out = []
    for row in workcell.get("resource_map", ()):
        if row.get("resource_id") == CONVEYOR_RESOURCE:
            korean = _normalize(row.get("korean") or "")
            if korean:
                out.append(korean)
    return tuple(out)


def mentions_conveyor(text: str, workcell: Mapping[str, Any]) -> bool:
    normalized = _normalize(text)
    return any(name in normalized for name in conveyor_names(workcell))


def named_pallets(text: str, workcell: Mapping[str, Any]) -> dict[str, str | None]:
    """발화가 부른 "N번 팔레트" → 팔레트 모델. 규칙 경로와 같은 해석이다."""
    numbers = pallet_numbers(workcell)
    return {n: numbers.get(n) for n in _PALLET.findall(_normalize(text))}


def looks_like_material_work(text: str, workcell: Mapping[str, Any]) -> bool:
    """이 발화를 자재 작업 분류기에 보낼 만한가.

    자재 이름·색 이름을 말했거나, 옮길 대상을 가리키는 낱말과 옮기는 동사가
    함께 나오면 참이다. **"안전 위치로 복귀해줘"·"1번 팔레트로 이동"은 거짓**
    이다 — 기존 계획 생성 경로가 그대로 맡는다.

    복귀 낱말은 `_BARE_RETURN_WORDS`만 본다. "복귀"는 여기 없다 — 일반 명령의
    "안전 위치로 복귀"와 구분되지 않기 때문이고, 이는 `parse_command`가 쓰는
    기준과 같다.
    """
    normalized = _normalize(text)
    if not normalized:
        return False
    for names in material_aliases(workcell).values():
        if any(name in normalized for name in names):
            return True
    if any(word in normalized for word in _RESUME_WORDS):
        return True
    hinted = any(word in normalized for word in _MATERIAL_WORK_HINTS)
    acting = (any(verb in normalized for verb in _TRANSFER_VERBS)
              or any(verb in normalized for verb in _CLASSIFIER_ONLY_VERBS)
              or any(word in normalized for word in _BARE_RETURN_WORDS))
    return hinted and acting


#: 발화·모델이 말한 자리를 **지원하는 작업**과 맞춰 본다. 여기서 막히면
#: 작업을 만들지 않는다. 실행기는 자재의 선언된 팔레트에서만 집고 그 팔레트로만
#: 돌려놓는다(`server/sim_demo_jobs.build_argv`) — 자유 pick/place가 아니다.
def check_places(intent: str, model: str, spec: Mapping[str, Any],
                 parsed: Mapping[str, Any], places: Sequence[Any],
                 objects: Mapping[str, Any]) -> str | None:
    """막을 이유가 있으면 그 문장을, 없으면 None을 돌려준다."""
    from server.sim_demo_places import (
        CONVEYOR,
        CONVEYOR_SLOT,
        PALLET,
        SAFE,
        resolve,
    )

    if not places:
        # 자리 어휘가 없는 구성이다. 여기서 자리를 판정할 근거가 없으므로
        # 검사를 하지 않는다 — 기존(자리를 말하지 않던) 동작 그대로다.
        return None
    korean = spec.get("korean", model)
    for field, label in (("source", "출발"), ("destination", "도착")):
        value = parsed.get(field)
        if value and resolve(places, value) is None:
            return f"이 작업 셀에 없는 {label} 위치입니다: {value}"
    source = resolve(places, parsed.get("source"))
    destination = resolve(places, parsed.get("destination"))

    if source is not None and source.kind == SAFE:
        return "안전 위치에서 자재를 집을 수 없습니다"
    if destination is not None and destination.kind == SAFE:
        return "안전 위치는 자재를 놓는 자리가 아닙니다"

    if intent == "transfer":
        if source is not None and source.kind == PALLET \
                and source.model != spec.get("support_model"):
            return f"{korean}은(는) {source.label}에 있지 않습니다"
        if source is not None and source.kind in (CONVEYOR, CONVEYOR_SLOT):
            return f"{korean}을(를) 컨베이어에서 컨베이어로 옮기지 않습니다"
        if destination is not None and destination.kind == PALLET:
            return (f"{destination.label}로 옮기는 작업은 지원하지 않습니다 —"
                    " 지원하는 이송 목적지는 컨베이어입니다")
    elif intent == "return":
        if source is not None and source.kind == PALLET:
            return f"원래 자리로 돌려놓는 것은 컨베이어에 있는 자재뿐입니다"
        if source is not None and source.kind == CONVEYOR_SLOT:
            held = record_slot(objects.get(model))
            if held and source.slot != held:
                return (f"{korean}은(는) {source.label}에 있지 않습니다"
                        f" ({slot_label(held)})")
        if destination is not None and destination.kind in (CONVEYOR,
                                                            CONVEYOR_SLOT):
            return f"{korean}을(를) 컨베이어에서 컨베이어로 돌려놓지 않습니다"
        if destination is not None and destination.kind == PALLET \
                and destination.model != spec.get("support_model"):
            return (f"{korean}의 원래 자리는 {destination.label}가 아닙니다")
    return None


def requested_slot(parsed: Mapping[str, Any], places: Sequence[Any]) -> str | None:
    """발화가 **콕 집어 말한** 컨베이어 자리. 말하지 않았으면 None이다."""
    from server.sim_demo_places import CONVEYOR_SLOT, resolve

    found = resolve(places, parsed.get("destination"))
    return found.slot if found is not None and found.kind == CONVEYOR_SLOT else None


def decide(parsed: Mapping[str, Any], status: Mapping[str, Any],
           materials: Mapping[str, Mapping[str, Any]],
           slots: Sequence[Any] = (), places: Sequence[Any] = ()) -> dict:
    """해석 결과를 **지금 상태**에 맞춰 RUN/ASK/BLOCK과 job spec으로 정한다.

    컨베이어 슬롯은 **서버가 고른다.** 이송은 비어 있는 첫 슬롯을 받고, 셋이
    모두 차면 BLOCK이다 — 겹쳐 놓거나 남의 자리를 빼앗지 않는다. 복귀는 그
    자재가 배정받았던 슬롯에서 간다. 발화나 모델이 슬롯·좌표를 정하지 않는다.
    """
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
        resume_slot = record_slot((state.get("objects") or {}).get(model))
        return {**parsed, "decision": RUN, "material": model,
                "slot": resume_slot if slots else None,
                "slot_label": slot_label(resume_slot) if slots and resume_slot else None,
                "job_spec": {"action": "resume", "material": model,
                             "checkpoint_id": checkpoint.get("checkpoint_id"),
                             "slot": resume_slot if slots else None}}
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
    objects = state.get("objects") or {}
    # 발화·모델이 말한 출발·도착을 **선언된 자리**와 맞춰 본다. 여기서 막히면
    # 슬롯을 배정하지도, 작업을 만들지도 않는다.
    blocked = check_places(intent, model, spec, parsed, places, objects)
    if blocked:
        return {**parsed, "decision": BLOCK, "reason": blocked}
    actions = available_actions(state, model, slots)
    if not actions[intent]:
        if intent == "return" and objects.get(model) is None:
            # 기록이 없다 = 원래 자리다. 옮길 것이 없으니 움직이지 않는다.
            home = spec.get("support_korean")
            reason = (f"{spec.get('korean', model)}은(는) 이미 원래 자리"
                      f"{f'({home})' if home else ''}에 있습니다 — 옮길 것이 없어 움직이지"
                      " 않습니다")
        elif intent == "return":
            reason = ("원래 자리로 돌릴 수 있는 것은 컨베이어에 유지 중"
                      "(held_on_target)인 자재뿐입니다")
        elif slots and slots_full(slots, objects):
            # 요구된 동작: 네 번째 이송은 막는다. 자동으로 누구도 되돌리지 않는다.
            busy = ", ".join(
                f"{slot_label(name)}={(materials.get(who) or {}).get('korean', who)}"
                for name, who in occupancy(slots, objects).items() if who)
            reason = (f"컨베이어 {len(tuple(slots))}개 위치가 모두 찼습니다 —"
                      f" 먼저 하나를 원래 자리로 돌려놔 주세요 ({busy})")
        elif slots and record_on_conveyor(slots, objects.get(model)):
            # **기록된 pose가 그 자리일 때만** 컨베이어에 있다고 말한다.
            reason = (f"{spec.get('korean', model)}은(는) 이미 컨베이어에 있습니다"
                      f" ({slot_label(record_slot(objects.get(model)))})")
        elif state.get("checkpoints"):
            reason = ("정지 체크포인트가 남아 있습니다 — 먼저 이어서 하거나"
                      " 복구해 주세요 (" + ", ".join(state["checkpoints"]) + ")")
        elif slots and objects.get(model):
            # 기록은 남았지만 자재는 그 자리에 놓이지 않았다 — 집기 전에 멈춘
            # 경우다. "이미 컨베이어에 있습니다"라고 말하면 화면과 실물이
            # 어긋나 보인다(실측 2026-09-22).
            reason = (f"{spec.get('korean', model)}은(는) 컨베이어에 놓이지 않았지만"
                      f" 끝나지 않은 작업 기록이 남아 있습니다"
                      f" ({objects[model].get('state')}) — 먼저 복구해 주세요")
        else:
            reason = ("셀이 초기 상태가 아닙니다 — 남은 기록: "
                      + (", ".join(objects) or "없음"))
        return {**parsed, "decision": BLOCK, "reason": reason}
    # 슬롯 배정 — 이송은 **발화가 집어 말한 자리**가 있으면 그 자리, 없으면 빈
    # 첫 자리. 복귀는 배정받았던 자리. 자리를 말했는데 차 있으면 BLOCK이다 —
    # 다른 자리로 슬쩍 바꿔 주지 않는다.
    slot = None
    if slots:
        if intent == "transfer":
            asked = requested_slot(parsed, places)
            if asked is not None:
                taken = occupancy(slots, objects).get(asked)
                if taken is not None:
                    who = (materials.get(taken) or {}).get("korean", taken)
                    return {**parsed, "decision": BLOCK,
                            "reason": f"{slot_label(asked)}에는 이미 {who}이(가)"
                                      " 있습니다 — 빈 자리를 말해 주세요"}
                slot = asked
            else:
                picked = assign_slot(slots, objects)
                if picked is None:
                    return {**parsed, "decision": BLOCK,
                            "reason": "컨베이어에 빈 위치가 없습니다"}
                slot = picked.name
        else:
            slot = record_slot(objects.get(model))
    return {**parsed, "decision": RUN, "slot": slot,
            "slot_label": slot_label(slot) if slot else None,
            "material": model,
            "job_spec": {"action": intent, "material": model,
                         "checkpoint_id": None, "slot": slot,
                         "source": parsed.get("source"),
                         "destination": parsed.get("destination")}}
