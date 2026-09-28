"""자재를 두고 집을 수 있는 **선언된 자리**의 어휘.

자연어 pick/place에서 모델이 고를 수 있는 출발·도착은 여기서 만든 목록뿐이다.
목록은 **작업 셀 설정과 검증된 슬롯에서만** 온다 — 코드가 자리를 만들어 내지
않고, 모델이 자유 좌표를 말할 자리도 없다.

들어가는 것:
 - 팔레트: `resource_map`이 선언한 `loc_pallet_*`
 - 컨베이어 자리: `validation/conveyor_slots.py`가 **검증된 것만** 실은 슬롯
 - 컨베이어(자리 미지정): 어느 자리인지 말하지 않은 이송. 서버가 빈 자리를 고른다
 - 안전 위치: 셀이 선언한 로봇 자세. **자재를 놓는 자리가 아니다** — 어휘에는
   넣되(모델이 그 말을 하면 그대로 받아야 한다) 자재 목적지로는 지원하지 않는다고
   답한다. 어휘에서 빼면 모델이 비슷한 다른 값을 골라 버린다.

여기 없는 자리는 존재하지 않는 자리다. `resolve()`가 `None`을 돌려주고, 부르는
쪽은 ASK/BLOCK으로 끝낸다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from validation.conveyor_slots import slot_label

#: 자리 종류.
PALLET, CONVEYOR_SLOT, CONVEYOR, SAFE = (
    "pallet", "conveyor_slot", "conveyor", "safe")

#: 자리 미지정 컨베이어. `resource_map`의 컨베이어 자원 id와 같은 값을 쓴다.
CONVEYOR_ID = "loc_conveyor"
#: 안전 위치. 셀 설정의 자세 이름(`safe_home`)에 맞춘 자원 id다.
SAFE_ID = "loc_safe_home"
SAFE_LABEL = "안전 위치"


@dataclass(frozen=True)
class Place:
    """자재가 있을 수 있는 자리 하나."""

    id: str
    kind: str
    label: str
    #: 팔레트면 그 Gazebo 모델 이름. 그 밖에는 None이다.
    model: str | None = None
    #: 컨베이어 자리면 슬롯 이름(`slot_1`…). 그 밖에는 None이다.
    slot: str | None = None

    @property
    def is_material_target(self) -> bool:
        """자재를 **놓을 수 있는** 자리인가. 안전 위치는 아니다."""
        return self.kind in (PALLET, CONVEYOR_SLOT, CONVEYOR)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "model": self.model, "slot": self.slot}


def _pallets(workcell: Mapping[str, Any]) -> list[Place]:
    models = workcell.get("models") or {}
    out = []
    for row in workcell.get("resource_map", ()):
        model = row.get("gazebo_model")
        if (models.get(model) or {}).get("kind") != "pallet":
            continue
        resource_id = row.get("resource_id")
        korean = row.get("korean") or resource_id
        if resource_id:
            out.append(Place(id=str(resource_id), kind=PALLET,
                             label=str(korean), model=model))
    return out


def declared_places(workcell: Mapping[str, Any],
                    slots: Sequence[Any] = (),
                    poses: Mapping[str, Any] | None = None) -> tuple[Place, ...]:
    """이 셀에서 말할 수 있는 자리 전부. 순서는 화면·프롬프트에 그대로 쓴다.

    `poses`는 셀의 자세 설정(`*_poses.json`)이다. 안전 위치는 거기 선언된
    `safe_home`이 있을 때만 어휘에 들어간다 — 없으면 넣지 않는다.
    """
    places = _pallets(workcell)
    for slot in slots or ():
        name = getattr(slot, "name", None)
        if name:
            places.append(Place(id=name, kind=CONVEYOR_SLOT,
                                label=slot_label(name), slot=name))
    has_conveyor = any(row.get("resource_id") == CONVEYOR_ID
                       for row in workcell.get("resource_map", ()))
    if has_conveyor:
        conveyor_korean = next(
            (row.get("korean") for row in workcell.get("resource_map", ())
             if row.get("resource_id") == CONVEYOR_ID), None)
        places.append(Place(id=CONVEYOR_ID, kind=CONVEYOR,
                            label=f"{conveyor_korean or '컨베이어'} (자리 미지정)"))
    # 안전 자세는 자세 설정의 **최상위**에 선언돼 있다(`poses` 아래가 아니다).
    # 두 자리를 다 보되, 없으면 어휘에 넣지 않는다.
    config = poses or workcell
    declared = config.get("safe_home") or (config.get("poses") or {}).get("safe_home")
    if declared is not None:
        places.append(Place(id=SAFE_ID, kind=SAFE, label=SAFE_LABEL))
    return tuple(places)


def place_ids(places: Iterable[Place]) -> tuple[str, ...]:
    return tuple(place.id for place in places)


def resolve(places: Iterable[Place], place_id: str | None) -> Place | None:
    """자리 id → 자리. **모르는 값은 만들어 내지 않는다.**"""
    if not place_id:
        return None
    for place in places:
        if place.id == place_id:
            return place
    return None


def place_label(places: Iterable[Place], place_id: str | None) -> str:
    found = resolve(places, place_id)
    return found.label if found else str(place_id or "—")


def prompt_lines(places: Iterable[Place]) -> str:
    """분류기 프롬프트에 넣을 자리 목록. 설명을 코드가 지어내지 않는다."""
    rows = []
    for place in places:
        note = {PALLET: "팔레트", CONVEYOR_SLOT: "컨베이어의 지정된 자리",
                CONVEYOR: "컨베이어 — 자리를 말하지 않은 경우",
                SAFE: "로봇 안전 자세 — 자재를 두는 자리가 아니다"}.get(place.kind, "")
        rows.append(f"- {place.id}: {place.label}" + (f" ({note})" if note else ""))
    return "\n".join(rows)
