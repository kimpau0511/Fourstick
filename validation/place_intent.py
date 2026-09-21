"""물체가 확인되지 않은 '놓기(place)' 요청은 반드시 되묻는다 (8-15).

문제(8-15 실측, dev_098 "1번 팔레트에 내려놔"): 발화가 **놓기 동사**인데 놓을
물체를 말하지 않았을 때, 모델이 위치만 보고 `[move, home]`(단순 이동)으로 바꿔
버리면 관문이 그 계획을 안전한 이동으로 보고 통과시킨다(false-PASS).

이 검증기는 **발화의 놓기 의도**를 결정론적으로 읽어, 놓을 물체나 놓을 곳이
확인되지 않은 놓기 요청을 ASK로 되돌린다. 계획을 자동으로 고치거나 기본 위치를
배정하지 않는다 — 무엇을 놓을지 물어볼 뿐이다.

**물체 보유 추정 금지.** 발화에 물체가 없을 때, 시뮬레이션 attachment·개구
(aperture)·과거 계획으로 "지금 무엇을 쥐고 있다"를 추정하지 않는다. 실제 파지
관측(`grasp.object_held`)이 **측정값(MEASURED)으로 특정 물체를 지목**하고 목적지도
확인된 경우에만 그 물체를 확인된 것으로 본다. 이 셀의 파지 관측은 `unavailable`
이므로 실제로는 항상 되묻는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.resource_catalog import ResourceCatalog, ResourceKind, normalize

#: 한국어 '놓기/싣기' 의도를 나타내는 표현. 스킬·자원 이름이 아니라 언어 단서다.
#: 정규화(공백 제거·NFKC·소문자)한 발화에서 부분 문자열로 찾는다. 물체가 함께
#: 확인되면 이 목록은 아무 영향을 주지 않는다 — 물체가 빠졌을 때만 되묻는 근거다.
PLACE_MARKERS: tuple[str, ...] = (
    "놓아", "놔", "내려놓", "내려놔", "얹", "실어", "싣",
    "올려놓", "올려놔", "갖다놓", "갖다놔",
)


@dataclass(frozen=True)
class PlaceIntentResult:
    is_place_request: bool
    object_confirmed: bool
    destination_confirmed: bool

    @property
    def underspecified(self) -> bool:
        """되물어야 하는 놓기 요청인가.

        놓기 의도가 있는데 놓을 물체나 놓을 곳 중 하나라도 확인되지 않았으면 True.
        """
        return self.is_place_request and not (
            self.object_confirmed and self.destination_confirmed
        )


def has_place_intent(utterance: str) -> bool:
    text = normalize(utterance or "")
    return any(marker in text for marker in PLACE_MARKERS)


def evaluate_place_intent(
    *,
    utterance: str,
    slots,
    catalog: ResourceCatalog,
    held_object_id: str | None = None,
) -> PlaceIntentResult:
    """발화의 놓기 의도와 물체·목적지 확인 여부를 본다.

    `held_object_id`는 **실제 파지 관측이 측정값으로 지목한 물체**일 때만 넘긴다.
    시뮬레이션 attachment·개구·과거 계획으로 채우지 않는다. None이면 물체는
    발화에서 확인된 경우에만 인정된다.
    """
    is_place = has_place_intent(utterance)
    objects: list[str] = []
    locations: list[str] = []
    if slots is not None:
        for match in slots.matches:
            if match.kind is ResourceKind.OBJECT and match.resource_id not in objects:
                objects.append(match.resource_id)
            elif match.kind is ResourceKind.LOCATION and match.resource_id not in locations:
                locations.append(match.resource_id)
    object_confirmed = bool(objects) or (
        held_object_id is not None and catalog.has(held_object_id)
        and catalog.kind_of(held_object_id) is ResourceKind.OBJECT
    )
    destination_confirmed = bool(locations)
    return PlaceIntentResult(
        is_place_request=is_place,
        object_confirmed=object_confirmed,
        destination_confirmed=destination_confirmed,
    )
