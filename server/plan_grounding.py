"""일반 경로(`/v1/plan`) 발화의 **서버 검증 근거**를 슬롯에 더한다 (2026-10-06).

슬롯 추출(`planning/slot_extractor.py`)은 카탈로그 **별칭 문자열**이 발화에 그대로 있을 때만
리소스를 확인한다. 그래서 "초록색 모형"·"연두 물체"처럼 별칭 목록에 없는 표현은 확인된
리소스가 0개가 되고, 모델(Qwen)은 3-1 규칙 때문에 되묻고, 요청↔계획 일치 검증은 그 물체를
쓰는 계획을 막았다. 고정 표현 목록이 해석을 끝내 버린 것이다.

여기서는 고정 표현이 아니라 **작업 셀의 실제 값**으로 근거를 더한다. 더한 근거는 모델 입력의
"발화에서 확인된 식별자"와 요청↔계획 일치 검증에 **같이** 쓰인다(같은 슬롯 객체).

지키는 것:
- 색 근거: 발화의 색 낱말이 셀 설정에 **등록된** 색이고, 그 색을 가진 자재가 **정확히 하나**일
  때만 그 자재를 확인한다. 둘 이상이거나 등록되지 않은 색이면 더하지 않는다(→ 모델이 되묻거나
  일치 검증이 막는다).
- 현재 위치 근거: 확인된 자재의 **지금 기록된 자리**(관측·기록, `SimDemoJobs.transfer_world`)를
  출발지로 확인한다. 사용자가 말하지 않았어도 서버가 아는 사실이다. 위치를 모르면 더하지 않는다.
- 지시어("그거")·없는 자재는 근거를 만들지 않는다. 모델이 무엇을 고르든 일치 검증이 막는다.
- 실행 판단이 아니다. 출발지 관측 대조·목적지 점유·충돌·안전은 기존 검증이 그대로 본다.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from core.resource_catalog import ResourceCatalog, ResourceKind
from planning.slot_extractor import SlotExtraction, SlotMatch

#: 컨베이어 칸(`slot_N`)은 카탈로그에서 자리 미지정 컨베이어 하나다.
_CONVEYOR_LOCATION = "loc_conveyor"


def _catalog_location(place: str | None, catalog: ResourceCatalog) -> str | None:
    """시연 기록의 자리 id → 카탈로그 위치 id. 대응이 없으면 None(표면 빈 위치 등)."""
    if not place:
        return None
    if catalog.has(place) and catalog.kind_of(place) is ResourceKind.LOCATION:
        return place
    if place.startswith("slot_") and catalog.has(_CONVEYOR_LOCATION):
        return _CONVEYOR_LOCATION
    return None


def object_facts(catalog: ResourceCatalog, workcell: Mapping[str, Any],
                 locations: Mapping[str, str | None]) -> dict[str, dict]:
    """카탈로그 물체 id → {model, korean, colors, location}. 셀 설정과 기록에서만 온다."""
    from server.material_colors import ColorRegistry

    registry = ColorRegistry.from_workcell(workcell)
    out: dict[str, dict] = {}
    for row in workcell.get("resource_map", ()):
        rid, model = row.get("resource_id"), row.get("gazebo_model")
        if not rid or not model or not catalog.has(rid):
            continue
        if catalog.kind_of(rid) is not ResourceKind.OBJECT:
            continue
        item = registry.materials.get(model)
        labels = item.to_dict()["color_labels"] if item is not None else []
        out[rid] = {
            "model": model,
            "korean": catalog.get(rid).display_name,
            "colors": list(labels),
            "canonical": list(item.colors) if item is not None else [],
            "location": _catalog_location(locations.get(model), catalog),
        }
    return out


def ground_slots(slots: SlotExtraction, *, catalog: ResourceCatalog,
                 facts: Mapping[str, Mapping[str, Any]]) -> SlotExtraction:
    """색으로 가리킨 자재(유일할 때만)와 확인된 자재의 현재 위치를 슬롯에 더한다."""
    from server.material_colors import canonical_color, stray_color_words

    utterance = slots.utterance or ""
    compact = "".join(utterance.split())
    added: list[SlotMatch] = []
    present = set(slots.resource_ids)
    for word in stray_color_words(utterance):
        color = canonical_color(word)
        owners = [rid for rid, fact in facts.items() if color and color in fact["canonical"]]
        if len(owners) != 1 or owners[0] in present:
            continue
        start = max(compact.find(word), 0)
        added.append(SlotMatch(resource_id=owners[0], kind=ResourceKind.OBJECT,
                               surface=word, start=start, end=start + len(word)))
        present.add(owners[0])
    matches = list(slots.matches) + added
    for match in list(matches):
        if match.kind is not ResourceKind.OBJECT:
            continue
        location = (facts.get(match.resource_id) or {}).get("location")
        if location and location not in present:
            # 출발지는 물체보다 앞선 위치로 둔다 — "말한 순서" 규칙에서 목적지보다 먼저다.
            matches.append(SlotMatch(
                resource_id=location, kind=ResourceKind.LOCATION,
                surface=f"{catalog.get(match.resource_id).display_name}의 현재 위치",
                start=match.start, end=match.start))
            present.add(location)
    if len(matches) == len(slots.matches):
        return slots
    return replace(slots, matches=tuple(sorted(matches, key=lambda m: (m.start, m.end))))

