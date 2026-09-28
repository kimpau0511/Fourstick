"""컨베이어 **다중 슬롯** 모델 — 배정·점유·표시.

컨베이어는 배치 위치가 하나였다. 이제 검증된 슬롯이 최대 셋이다
(활성 작업 셀의 파지 설정 `<작업셀>_grasp.json`에 있는 `conveyor_slots`).
**이 모듈은 로봇·셀 이름을 모른다** — 설정을 읽어 온 dict만 받는다.

**좌표를 여기서 만들지 않는다.** 슬롯은 `scripts/derive_conveyor_slots.py`가
선언된 기하에서 후보를 만들고 MoveIt(IK·충돌·attached-object·이웃 점유)으로
검증한 것만 설정에 들어온다. 이 모듈은 그중 `status=verified`이고 **모든 검사가
clean인 슬롯만** 읽는다. 검증되지 않은 자리는 없는 자리다.

## 배정 규칙

- 이송은 **비어 있는 첫 슬롯**에 배정한다(`slot_1` → `slot_2` → `slot_3`).
- 셋이 모두 차면 네 번째는 **BLOCK**이다. 겹쳐 놓거나 덮어쓰지 않고, 자동으로
  누구를 되돌리지도 않는다.
- 복귀는 그 자재가 **배정받았던 슬롯**에서 원래 팔레트로 간다.

## 점유는 두 곳을 함께 본다

기록(simulation_demo 상태)만 믿지 않는다. Gazebo를 다시 띄우면 기록만 남고
실제로는 비어 있을 수 있다. `reconcile_occupancy()`가 기록과 관측을 맞춰 보고
어긋난 슬롯을 집어낸다 — 맞추는 일 자체는 기존 정합 경로가 한다.

## 옛 기록과의 호환

슬롯 개념이 없던 기록에는 `slot`이 없다. 그때 컨베이어 배치 위치는 하나였고
그 자리가 지금의 `slot_1`이므로, `slot` 없는 기록은 `slot_1`로 읽는다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

#: 설정에서 슬롯을 읽어 오는 키.
CONFIG_KEY = "conveyor_slots"
#: 슬롯이 없던 시절의 기록이 가리키는 자리.
LEGACY_SLOT = "slot_1"
#: 슬롯이 모두 찼을 때의 이유 코드.
REASON_SLOTS_FULL = "exec.sim_target_occupied"

#: 슬롯 이름 → 사람에게 보이는 말. 화면·TTS·확인 카드가 모두 이것을 쓴다.
#: 이름을 각자 만들면 화면과 소리가 갈라진다.
_LABELS = {"slot_1": "컨베이어 1번 위치",
           "slot_2": "컨베이어 2번 위치",
           "slot_3": "컨베이어 3번 위치"}


def slot_label(name: str | None) -> str:
    """슬롯 이름 → 표시용 말. 모르는 이름은 만들어 내지 않는다."""
    if not name:
        return "컨베이어"
    return _LABELS.get(name, f"컨베이어({name})")


def slot_number(name: str | None) -> int | None:
    """슬롯 번호. `slot_2` → 2. 모르면 None이다."""
    if not name or not name.startswith("slot_"):
        return None
    try:
        return int(name.split("_", 1)[1])
    except ValueError:
        return None


@dataclass(frozen=True)
class ConveyorSlot:
    """검증된 슬롯 하나. 좌표는 설정에서 온 것을 그대로 담는다."""

    name: str
    center_m: tuple[float, float, float]
    x_offset_m: float

    @property
    def label(self) -> str:
        return slot_label(self.name)

    @property
    def number(self) -> int | None:
        return slot_number(self.name)

    def to_dict(self) -> dict:
        return {"slot": self.name, "label": self.label, "number": self.number,
                "center_m": list(self.center_m), "x_offset_m": self.x_offset_m}


def load_slots(grasp_config: Mapping[str, Any]) -> tuple[ConveyorSlot, ...]:
    """설정에서 **검증된** 슬롯만 순서대로 읽는다.

    `status`가 verified가 아니거나 검사 중 하나라도 clean이 아니면 뺀다.
    설정에 `conveyor_slots`가 없으면 빈 튜플이다 — 그때는 호출자가 기존 단일
    배치 경로를 그대로 쓴다(슬롯을 만들어 내지 않는다).
    """
    block = (grasp_config or {}).get(CONFIG_KEY) or {}
    if block.get("status") != "verified":
        return ()
    out: list[ConveyorSlot] = []
    for name, row in sorted((block.get("slots") or {}).items()):
        checks = (row or {}).get("checks") or {}
        if not checks or not all(c.get("clean") for c in checks.values()):
            continue
        center = row.get("object_world_center_m")
        if not center or len(center) != 3:
            continue
        out.append(ConveyorSlot(
            name=name,
            center_m=tuple(float(v) for v in center),
            x_offset_m=float(row.get("x_offset_m") or 0.0)))
    out.sort(key=lambda s: (s.number if s.number is not None else 99, s.name))
    return tuple(out)


def slot_by_name(slots: Iterable[ConveyorSlot], name: str | None) -> ConveyorSlot | None:
    if not name:
        return None
    return next((s for s in slots if s.name == name), None)


# ── 점유 ──────────────────────────────────────────────────────────────
#: 컨베이어에 남아 있다고 보는 기록 상태. 이 상태의 자재가 슬롯을 차지한다.
#: 실패·정지로 어긋난 기록도 **자리를 비우지 않는다** — 사람이 복구할 때까지
#: 그 자리를 다른 자재에 주면 겹친다.
OCCUPYING_STATES = ("held_on_target", "stopped_unrestored",
                    "fault_unrestored", "return_stopped", "return_failed")


def record_slot(record: Mapping[str, Any] | None) -> str | None:
    """기록 한 줄이 가리키는 슬롯. 옛 기록(`slot` 없음)은 `slot_1`이다."""
    if not record:
        return None
    if record.get("pallet"):
        # 다른 팔레트에 놓인 자재 기록이다 — 컨베이어 칸이 아니다(옛 기록 규칙으로
        # 1번 칸으로 읽으면 칸 점유가 틀어진다).
        return None
    name = record.get("slot")
    if name:
        return str(name)
    return LEGACY_SLOT


def occupancy(slots: Iterable[ConveyorSlot],
              records: Mapping[str, Mapping[str, Any]] | None) -> dict[str, str | None]:
    """슬롯 이름 → 점유한 자재 모델(없으면 None). **기록 기준**이다."""
    taken: dict[str, str | None] = {s.name: None for s in slots}
    for model, record in (records or {}).items():
        state = (record or {}).get("state")
        if state not in OCCUPYING_STATES:
            continue
        name = record_slot(record)
        if name in taken and taken[name] is None:
            taken[name] = model
    return taken


def assign_slot(slots: Iterable[ConveyorSlot],
                records: Mapping[str, Mapping[str, Any]] | None) -> ConveyorSlot | None:
    """이송을 받을 **비어 있는 첫 슬롯**. 모두 차 있으면 None이다."""
    taken = occupancy(slots, records)
    for slot in slots:
        if taken.get(slot.name) is None:
            return slot
    return None


def slots_full(slots: Iterable[ConveyorSlot],
               records: Mapping[str, Mapping[str, Any]] | None) -> bool:
    return assign_slot(slots, records) is None


def occupancy_rows(slots: Iterable[ConveyorSlot],
                   records: Mapping[str, Mapping[str, Any]] | None,
                   materials: Mapping[str, Mapping[str, Any]] | None = None) -> list[dict]:
    """화면·API가 그대로 쓰는 슬롯별 현황."""
    taken = occupancy(slots, records)
    rows = []
    for slot in slots:
        model = taken.get(slot.name)
        record = (records or {}).get(model) if model else None
        rows.append({
            **slot.to_dict(),
            "occupied": model is not None,
            "model": model,
            "korean": ((materials or {}).get(model) or {}).get("korean") if model else None,
            "state": (record or {}).get("state") if record else None,
        })
    return rows


# ── 기록 ↔ 관측 ───────────────────────────────────────────────────────
def slot_at(slots: Iterable[ConveyorSlot], pose_m: Sequence[float] | None,
            tolerance_m: float) -> ConveyorSlot | None:
    """관측된 pose가 어느 슬롯인가. 허용 반경 밖이면 None이다."""
    if pose_m is None or len(pose_m) != 3:
        return None
    best, best_gap = None, None
    for slot in slots:
        gap = math.dist(tuple(float(v) for v in pose_m), slot.center_m)
        if best_gap is None or gap < best_gap:
            best, best_gap = slot, gap
    if best is None or best_gap is None or best_gap > tolerance_m:
        return None
    return best


#: 기록에 남은 pose를 "그 자리에 놓였다"로 볼 반경. 자재 한 변(0.05 m)의 절반이다 —
#: 그보다 멀면 그 자리에 놓인 것이 아니다.
RECORD_MATCH_TOLERANCE_M = 0.025


def record_on_conveyor(slots: Iterable[ConveyorSlot],
                       record: Mapping[str, Any] | None,
                       *, tolerance_m: float = RECORD_MATCH_TOLERANCE_M) -> bool:
    """기록이 남은 자재가 **실제로 컨베이어 자리에 놓였는가**.

    기록이 있다는 것과 컨베이어에 있다는 것은 다르다. 집기 전에 정지하면
    `stopped_unrestored` 기록과 슬롯 배정은 남지만 자재는 원래 자리에 그대로
    있다(실측 2026-09-22: pre-grasp에서 정지 → 기록 pose가 팔레트 좌표).

    그 둘을 기록 안의 pose로 가른다 — **관측을 새로 하지 않는다.** 기록이
    가리키는 자리와 기록된 pose가 같은 곳일 때만 참이다.
    """
    if not record:
        return False
    pose = record.get("pose_m")
    name = record_slot(record)
    found = slot_at(slots, pose, tolerance_m)
    return found is not None and found.name == name


def reconcile_occupancy(slots: Iterable[ConveyorSlot],
                        records: Mapping[str, Mapping[str, Any]] | None,
                        observed: Mapping[str, Sequence[float] | None] | None,
                        *, tolerance_m: float) -> list[dict]:
    """기록과 Gazebo 관측을 맞춰 본다. **맞추지는 않는다 — 어긋난 것만 적는다.**

    `verdict`:
      - `agrees`      기록도 관측도 같은 슬롯
      - `record_only` 기록은 점유인데 관측이 그 자리에 없다(기록이 낡았다)
      - `moved`       기록과 다른 슬롯에서 관측됐다
      - `unobserved`  관측값이 없다 — **비었다고 단정하지 않는다**
    """
    slots = tuple(slots)
    findings = []
    for model, record in sorted((records or {}).items()):
        if (record or {}).get("state") not in OCCUPYING_STATES:
            continue
        recorded = record_slot(record)
        pose = (observed or {}).get(model)
        if model not in (observed or {}):
            verdict = "unobserved"
            seen = None
        else:
            found = slot_at(slots, pose, tolerance_m)
            seen = found.name if found else None
            if seen is None:
                verdict = "record_only"
            elif seen == recorded:
                verdict = "agrees"
            else:
                verdict = "moved"
        findings.append({
            "model": model, "recorded_slot": recorded, "observed_slot": seen,
            "observed_pose_m": (None if pose is None
                                else [round(float(v), 6) for v in pose]),
            "verdict": verdict,
            "record_state": (record or {}).get("state"),
        })
    return findings


# ── 실행 목표 ↔ 배정 슬롯 일치 검사 ───────────────────────────────────
#: 컨베이어 쪽 팔 목표가 나오는 단계. 이 둘만 슬롯에 따라 달라진다.
PLACE_STAGES = ("place_approach", "place_descend")
#: 관절값이 같다고 볼 허용치(rad). 설정에 적힌 값과 stage에 구워진 값은
#: 같은 출처에서 오므로 반올림 오차만 허용한다.
JOINT_TOLERANCE_RAD = 1e-6


def binding_mismatch(stages: Iterable[Any], slot_entry: Mapping[str, Any] | None,
                     *, slot_name: str | None) -> str | None:
    """**실행 목표가 배정된 슬롯의 자세인지** 확인한다. 맞으면 None.

    왜 필요한가. 단계(stage)는 만들어질 때 관절값이 구워지고, 실행기는 그
    값을 그대로 보낸다. 그래서 슬롯 결속을 단계 생성 **뒤에** 하면 검사만
    슬롯 기준이 되고 로봇은 예전 자리로 간다 — 실측으로 겪었다(slot_2를
    요청했는데 slot_1에 놓여 옆 자재와 겹쳤다).

    이 함수는 그 어긋남을 **로봇이 움직이기 전에** 잡는다. 돌려주는 값은
    사람이 읽는 사유이며, 호출자는 `plan.resource_mismatch`로 실패시킨다.
    """
    if slot_entry is None or not slot_name:
        return None
    expected = {
        "place_approach": slot_entry.get("approach_joint_rad"),
        "place_descend": slot_entry.get("place_joint_rad"),
    }
    seen: dict[str, Any] = {}
    for stage in stages:
        name = getattr(stage, "stage", None)
        if name not in PLACE_STAGES:
            continue
        seen[name] = stage
        goal = expected.get(name)
        if not goal:
            return (f"{slot_name}의 검증된 {name} 관절값이 설정에 없다 —"
                    " 값을 만들지 않는다")
        actual = dict(getattr(stage, "joint_rad", {}) or {})
        arm = {k: v for k, v in actual.items() if k in goal}
        if set(arm) != set(goal):
            return (f"{name} 목표 관절이 {slot_name} 설정과 다르다"
                    f" (계획 {sorted(actual)}, 설정 {sorted(goal)})")
        worst = max((abs(float(arm[k]) - float(goal[k])) for k in goal), default=0.0)
        if worst > JOINT_TOLERANCE_RAD:
            pose = getattr(stage, "pose_name", None)
            return (f"{name} 실행 목표가 {slot_name}의 자세가 아니다"
                    f" (최대 차이 {worst:.6f} rad, 자세 이름 {pose!r})")
    missing = [s for s in PLACE_STAGES if s not in seen]
    if missing:
        return f"컨베이어 배치 단계가 계획에 없다: {', '.join(missing)}"
    return None
