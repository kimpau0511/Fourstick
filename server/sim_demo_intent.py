"""모호한 자재 작업 발화의 **좁은 분류기** — Qwen 출력은 JSON schema만 받는다.

규칙 해석기(`server/sim_demo_commands.py`)가 먼저다. 명확한 A/B/C 이송·복귀·
STOP·resume 발화는 여기 오지 않는다. 규칙이 `ASK`(자재 작업인데 불완전)나
`PASS_THROUGH`(시뮬레이션 명령이 아님)로 끝났고, 그 발화가 **자재 작업처럼
보일 때만** 이 분류기를 부른다.

모델이 하는 일은 분류 하나다. **자유 계획을 만들지 않는다.**

모델은 **선언형 작업 하나**만 낸다. 관절값·좌표·trajectory를 낼 자리가 없다.

```json
{"intent": "transfer|return|move_slot|resume|restore|unknown",
 "material_id": "<등록된 자재 id>|null",
 "source_id": "<선언된 자리 id>|null",
 "destination_id": "<선언된 자리 id>|null",
 "confidence": 0.0 ~ 1.0,
 "reason": "짧은 근거(판정에 쓰지 않는다)"}
```

입력에는 발화 원문과 함께 자재 목록(id·이름·색·**현재 위치**), 선언된 자리 목록(팔레트·
컨베이어·빈 곳 표면), 지원 동작, 대화 맥락(직전에 말한 자재)이 들어간다.

`source_id`·`destination_id`는 **셀이 선언한 자리**(`server/
sim_demo_places.py`)의 id만 받는다 — 팔레트·검증된 컨베이어 자리·자리 미지정
컨베이어·안전 자세뿐이다. 그 밖의 값은 스키마에서 막히고, 막지 못해도
`validate`가 후보 밖으로 걸러낸다.

`confidence`는 작업 서술이 아니라 **기존 임계값 관문**이다. 이 값이 없으면
낮은 확신으로 만든 해석이 그대로 확인 카드까지 올라온다.

받은 뒤에는 모델을 믿지 않고 서버가 다시 본다(`validate`).

1. JSON으로 파싱되는가 · 필드가 다섯뿐인가
2. `intent`가 열거값인가 · `material_id`가 **이 셀의 자재 후보**인가
3. 출발·도착이 **이 셀이 선언한 자리**인가
4. `confidence`가 0~1 수이고 임계값 이상인가
5. 그 작업을 **지금 시연 상태**에서 할 수 있는가(`decide`)

하나라도 어긋나면 `ok=False`다. 통과해도 작업을 만들지 않는다 —
확인 카드(`server/sim_demo_confirm.py`)를 거쳐 사람이 누른 뒤에만 실행된다.

`restore`는 분류 결과로는 받되 실행 후보로 쓰지 않는다. 복구는 시연 카드의
명시적 버튼이 맡는다 — 발화 해석으로 복구를 돌리지 않는다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

#: 모델이 낼 수 있는 의도. 이 밖의 값은 받지 않는다.
#: `move_slot`은 컨베이어의 지정한 칸이나 표면 빈 곳으로 옮기는 것이다(서버가 기존 동작으로 바꾼다).
INTENTS = ("transfer", "return", "move_slot", "resume", "restore", "unknown")
#: 발화 해석으로 **실행 후보가 되는** 의도. `restore`는 여기 없다.
EXECUTABLE_INTENTS = ("transfer", "return", "move_slot", "resume")
#: 모델 출력에 허용하는 필드. 더 있으면 스키마 위반이다. 결과 객체(`IntentResult`)는
#: 기존 소비자 이름(`source_resource`·`destination_resource`)으로 담는다.
FIELDS = ("intent", "material_id", "source_id", "destination_id", "confidence", "reason")
#: 모델이 적는 짧은 근거의 최대 길이. 화면·기록에만 쓰고 판정에는 쓰지 않는다.
REASON_MAX = 120

#: 서버가 요구하는 구조화 출력 스키마(OpenAI `response_format`).
def output_json_schema(material_ids: Sequence[str],
                       place_ids: Sequence[str] = ()) -> dict:
    """이 셀의 자재·자리만 열거한 스키마. 후보를 코드에 고정하지 않는다."""
    place_enum = [*place_ids, None]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(FIELDS),
        "properties": {
            "intent": {"type": "string", "enum": list(INTENTS)},
            "material_id": {"type": ["string", "null"],
                            "enum": [*material_ids, None]},
            "source_id": {"type": ["string", "null"], "enum": place_enum},
            "destination_id": {"type": ["string", "null"], "enum": place_enum},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "reason": {"type": "string", "maxLength": REASON_MAX},
        },
    }


SYSTEM_PROMPT = """너는 로봇 시뮬레이션 작업 셀의 명령 해석기다.
사용자 발화를 읽고 아래 JSON 하나만 출력한다. 설명·계획·문장을 쓰지 않는다.

**관절값·좌표·경로를 쓰지 않는다.** 네가 내는 것은 아래 다섯 칸뿐이다.
네 결과는 그대로 실행되지 않는다 — 서버가 다시 검사하고 사람이 확인한 뒤에만 움직인다.

intent
- transfer: 원래 팔레트에 있는 자재를 컨베이어로 옮긴다("벨트"도 컨베이어다)
- return: 컨베이어·다른 자리에 있는 자재를 원래 팔레트(원래 자리)로 돌려놓는다
- move_slot: 자재를 컨베이어의 지정한 칸이나 표면의 빈 곳으로 옮긴다
- resume: 정지된 작업을 체크포인트에서 이어서 한다
- restore: 어긋난 자재를 원래 자리로 복구한다
- unknown: 위 어느 것도 아니거나 확신할 수 없다

material_id
- 발화가 가리키는 자재. 자재 목록에 있는 것만 쓴다. 알 수 없으면 null이다.
- 이름이나 색으로 가리켰으면 그 이름·색을 가진 자재다. 색이 같은 자재가 여럿이면 null이다.
- "그거"·"그 물건"처럼 가리키기만 하면 대화 맥락의 직전 자재다. 맥락이 없으면 null이다.

source_id / destination_id
- 자리 목록에 있는 id만 쓴다. **목록에 없는 자리를 지어내지 않는다.**
- 발화가 말하지 않은 자리는 null이다. 짐작해서 채우지 않는다.
- "컨베이어에 올려줘"처럼 자리를 말하지 않은 이송은 자리 미지정 컨베이어 id다.
- "컨베이어 2번 위치"처럼 자리를 말하면 그 자리의 id다.
- "저쪽 빈자리"처럼 어느 자리인지 정할 수 없으면 자리 미지정 컨베이어 id다.

confidence
- 0.0~1.0. 발화만으로 확신할 수 없으면 낮게 쓴다. 추측해서 올리지 않는다.

reason
- 왜 그렇게 읽었는지 한 문장(120자 이내). 예: "초록색 = C자재, 벨트 = 컨베이어".
- 목록에 없는 자재·자리·동작을 새로 만들지 않는다. 고를 수 없으면 null·unknown과 이유를 쓴다.

자재를 지정하지 않은 이송(transfer)은 확신할 수 없다 — confidence를 낮게 둔다.
발화에 없는 자재를 고르지 않는다. 모르면 unknown과 null이다."""


#: 프롬프트에 적는 지원 동작. 서버가 실제로 실행할 수 있는 것만 적는다.
SUPPORTED_ACTIONS = ("이송(transfer) — 원래 팔레트 → 컨베이어",
                     "원래 자리 복귀(return) — 컨베이어 → 원래 팔레트",
                     "빈자리 이동(move_slot) — 컨베이어의 빈 칸 또는 표면의 빈 곳",
                     "이어하기(resume) — 정지된 작업을 체크포인트에서")


def user_prompt(utterance: str, candidates: Sequence[Mapping[str, Any]],
                places: str = "", context: Mapping[str, Any] | None = None,
                original: str | None = None) -> str:
    """발화 원문 · 자재(현재 위치 포함) · 자리 · 지원 동작 · 대화 맥락을 준다.

    실행 가능 여부는 서버가 본다 — 여기 적은 위치는 해석을 돕는 근거일 뿐이다.
    """
    lines = []
    for row in candidates:
        line = f"- {row['model']}: {row.get('korean') or row['model']}"
        if row.get("korean_colors"):
            line += f" (색: {', '.join(row['korean_colors'])})"
        if "location" in row:
            line += f" — 현재 위치: {row.get('location_label') or row.get('location') or '확인 안 됨'}"
        lines.append(line)
    parts = ["자재 목록\n" + "\n".join(lines)]
    if places:
        parts.append("자리 목록\n" + places)
    parts.append("지원 동작\n" + "\n".join(f"- {row}" for row in SUPPORTED_ACTIONS))
    focus = list((context or {}).get("focus") or ())
    parts.append("대화 맥락\n" + (f"- 직전에 말한 자재: {', '.join(focus)}" if focus
                                   else "- 없음"))
    said = (original or "").strip()
    if said and said != utterance.strip():
        parts.append("사용자가 말한 원문\n" + said)
        parts.append("발화(정리한 문장)\n" + utterance.strip())
    else:
        parts.append("발화\n" + utterance.strip())
    return "\n\n".join(parts) + "\n\nJSON만 출력한다."


@dataclass(frozen=True)
class IntentResult:
    """분류 결과. `ok=False`면 실행 후보가 아니다."""

    ok: bool
    #: 모델의 `intent`.
    intent: str | None = None
    material_id: str | None = None
    #: 모델이 고른 **선언된 자리** id. 발화에 없으면 None이다.
    source_resource: str | None = None
    destination_resource: str | None = None
    confidence: float | None = None
    #: 실패 이유(사람이 읽는 말). 화면에 그대로 나간다.
    reason: str = ""
    #: 모델이 적은 짧은 근거. 판정에 쓰지 않는다(화면·기록용).
    model_reason: str = ""
    #: 실패 종류. `schema` | `candidate` | `confidence` | `unknown` | `unavailable`
    failure: str | None = None
    #: 모델이 낸 원문(앞부분만). 해석 근거로 화면에 보인다.
    raw: str = ""
    model_id: str = ""
    latency_sec: float | None = None


#: 원문을 화면·기록에 담을 때의 최대 길이.
RAW_LIMIT = 400
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_output(content: str) -> dict:
    """모델 본문 → dict. 코드펜스만 벗긴다. **고쳐 주지 않는다.**"""
    text = _FENCE.sub("", str(content or "")).strip()
    return json.loads(text)


def validate(payload: Any, *, candidates: Sequence[str],
             min_confidence: float, places: Sequence[str] = ()) -> IntentResult:
    """모델 출력을 스키마·후보·임계값으로 본다. 상태는 보지 않는다.

    `places`는 이 셀이 **선언한 자리** id다. 비우면 자리 검사를 하지 않는다
    (자리 어휘가 없는 구성에서 기존 동작 그대로다).
    """
    if not isinstance(payload, dict):
        return IntentResult(False, failure="schema",
                            reason="해석 결과가 JSON 객체가 아닙니다")
    extra = sorted(set(payload) - set(FIELDS))
    missing = [name for name in FIELDS if name not in payload]
    if missing or extra:
        detail = (f"없는 필드: {', '.join(missing)}" if missing else "") \
            + (" · " if missing and extra else "") \
            + (f"허용하지 않는 필드: {', '.join(extra)}" if extra else "")
        return IntentResult(False, failure="schema",
                            reason=f"해석 결과의 형식이 맞지 않습니다 — {detail}")
    intent = payload.get("intent")
    if intent not in INTENTS:
        return IntentResult(False, failure="schema",
                            reason=f"알 수 없는 intent입니다: {intent!r}")
    material = payload.get("material_id")
    if material is not None and material not in candidates:
        return IntentResult(False, failure="candidate", intent=intent,
                            reason=f"이 작업 셀에 없는 자재입니다: {material!r}")
    source = payload.get("source_id")
    destination = payload.get("destination_id")
    # **이 셀이 선언한 자리만 받는다.** 모델이 지어낸 자리는 여기서 끝난다.
    for label, value in (("출발", source), ("도착", destination)):
        if value is not None and places and value not in places:
            return IntentResult(False, failure="place", intent=intent,
                                material_id=material,
                                reason=f"이 작업 셀에 없는 {label} 위치입니다:"
                                       f" {value!r}")
    model_reason = payload.get("reason")
    if not isinstance(model_reason, str) or len(model_reason) > REASON_MAX:
        return IntentResult(False, failure="schema", intent=intent, material_id=material,
                            reason=f"reason이 {REASON_MAX}자 이내 문자열이 아닙니다")
    where = {"source_resource": source, "destination_resource": destination,
             "model_reason": model_reason}
    confidence = payload.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return IntentResult(False, failure="schema", intent=intent,
                            material_id=material, **where,
                            reason="confidence가 수가 아닙니다")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        return IntentResult(False, failure="schema", intent=intent,
                            material_id=material, **where,
                            reason=f"confidence가 0~1 밖입니다: {confidence}")
    if intent == "unknown":
        return IntentResult(False, failure="unknown", intent=intent,
                            material_id=material, confidence=confidence, **where,
                            reason="발화의 뜻을 정하지 못했습니다")
    if intent not in EXECUTABLE_INTENTS:
        return IntentResult(False, failure="unknown", intent=intent,
                            material_id=material, confidence=confidence, **where,
                            reason=f"발화 해석으로는 실행하지 않는 동작입니다: {intent}"
                                   " — 복구는 시연 카드의 버튼으로 합니다")
    if confidence < min_confidence:
        return IntentResult(False, failure="confidence", intent=intent,
                            material_id=material, confidence=confidence, **where,
                            reason=f"확신이 낮습니다(confidence {confidence:.2f} <"
                                   f" {min_confidence:.2f})")
    return IntentResult(True, intent=intent, material_id=material,
                        confidence=confidence, **where)


def _places_prompt(places: Sequence[Any]) -> str:
    """자리 목록을 프롬프트 줄로. 자리가 없으면 빈 문자열이다."""
    if not places:
        return ""
    from server.sim_demo_places import prompt_lines

    return prompt_lines(places)


@dataclass
class IntentClassifier:
    """Qwen(vLLM OpenAI 호환) 분류기. 호출·검증만 한다.

    `client`는 `planning/openai_compat.py`의 `OpenAiCompatClient`와 같은 모양이면
    된다(테스트는 대역을 넣는다). 서버가 없으면 `unavailable`로 끝나고, 화면은
    작업을 만들지 않는다.
    """

    client: Any
    min_confidence: float
    #: 서버 로그에서 구분할 스키마 이름.
    schema_name: str = "sim_demo_intent"

    @property
    def model_id(self) -> str:
        config = getattr(self.client, "config", None)
        return str(getattr(config, "model_id", "") or "")

    def classify(self, utterance: str,
                 candidates: Sequence[Mapping[str, Any]],
                 places: Sequence[Any] = (),
                 context: Mapping[str, Any] | None = None,
                 original: str | None = None) -> IntentResult:
        """발화 하나 → 선언형 작업. `places`는 이 셀이 선언한 자리,
        `context`는 대화 맥락(`{"focus": [자재 id…]}`)이다."""
        ids = [str(row["model"]) for row in candidates]
        place_ids = [str(getattr(place, "id", place)) for place in places or ()]
        try:
            completion = self.client.chat(
                system=SYSTEM_PROMPT,
                user=user_prompt(utterance, candidates,
                                 _places_prompt(places), context, original),
                json_schema=output_json_schema(ids, place_ids),
                schema_name=self.schema_name,
            )
        except Exception as exc:  # noqa: BLE001 — 서버가 없으면 기능을 끈다
            return IntentResult(False, failure="unavailable", model_id=self.model_id,
                                reason=f"해석기를 쓸 수 없습니다: {exc}"[:200])
        raw = str(getattr(completion, "content", "") or "")[:RAW_LIMIT]
        latency = getattr(completion, "latency_sec", None)
        try:
            payload = parse_output(raw)
        except (ValueError, TypeError):
            return IntentResult(False, failure="schema", raw=raw,
                                model_id=self.model_id, latency_sec=latency,
                                reason="해석 결과를 JSON으로 읽지 못했습니다")
        result = validate(payload, candidates=ids,
                          min_confidence=self.min_confidence,
                          places=place_ids)
        return IntentResult(**{**result.__dict__, "raw": raw,
                               "model_id": self.model_id,
                               "latency_sec": latency})
