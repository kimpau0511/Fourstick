"""FR3 작업 셀의 `transfer` Capability — 검증된 자세만 돌려준다.

공통 계약(`core/transfer_skill.py`)의 FR3 구현이다. 자세 값은 설정 파일에서만
온다(`fr3_2f85_workcell_poses.json`, `fr3_2f85_workcell_grasp.json`). 없는 자세를
만들거나 다른 자세로 대신하지 않는다.

| 출발 → 도착 | 파지 | 놓기 |
|---|---|---|
| 원래 팔레트 → 컨베이어 칸 | `poses.<m>_grasp`(팔레트 파지) | 칸 `place_joint_rad` |
| 컨베이어 칸 → 원래 팔레트 | 칸 `grasp_joint_rad_arm` | `pallet_place_poses.<m>_pallet_place` |
| 컨베이어 칸 → 다른 칸 | 칸 `grasp_joint_rad_arm` | 도착 칸 `place_joint_rad` |
| 팔레트 ↔ 다른 팔레트, 칸 ↔ 다른 팔레트 | `pallet_grasp_poses`(위치 파지) | `pallet_place_poses`(든 물체 출처별) |

팔레트 놓기 자세는 **그 파지로 든 물체**로 측정된 것만 쓴다(`held_object_source`).
측정되지 않은 조합은 BLOCK이다(다른 자세로 대신하지 않는다).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.transfer_skill import (
    Finding,
    Location,
    LocationKind,
    PoseRef,
    PoseRole,
    RoutePoses,
)

ROOT = Path(__file__).resolve().parents[2]
ROBOT_ID = "fr3wms_2f85_workcell"


class Fr3TransferCapability:
    robot_id = ROBOT_ID

    def __init__(self, *, workcell: Mapping[str, Any], grasp: Mapping[str, Any],
                 poses: Mapping[str, Any], clearance: Mapping[str, Any] | None = None):
        self._workcell = workcell
        #: 측정된 경로 여유(`scripts/derive_path_clearance.py`). 없으면 모름(None).
        self._clearance = dict((clearance or {}).get("routes") or {})
        self._grasp = grasp
        self._derived = {name: row for name, row in (poses.get("poses") or {}).items()
                         if row.get("status") == "verified"}
        rows = list(workcell.get("resource_map") or ())
        models = workcell.get("models") or {}
        self._rid_of_model = {r["gazebo_model"]: r["resource_id"] for r in rows}
        self._model_of_rid = {r["resource_id"]: r["gazebo_model"] for r in rows}
        self._label = {r["resource_id"]: r.get("korean") or r["resource_id"] for r in rows}
        self._materials = [r["gazebo_model"] for r in rows
                           if (models.get(r["gazebo_model"]) or {}).get("kind") == "material"]
        from server.sim_demo_jobs import materials_from_workcell
        self._origin = {m: self._rid_of_model.get(spec.get("support_model"))
                        for m, spec in materials_from_workcell(workcell).items()}
        locs: dict[str, Location] = {}
        for r in rows:
            if (models.get(r["gazebo_model"]) or {}).get("kind") == "pallet":
                locs[r["resource_id"]] = Location(r["resource_id"], LocationKind.PALLET,
                                                  r.get("korean") or "")
        from validation.conveyor_slots import load_slots
        self._slots = {s.name: s for s in load_slots(grasp)}
        slot_rows = (grasp.get("conveyor_slots") or {}).get("slots") or {}
        self._slot_rows = {n: slot_rows[n] for n in self._slots}
        for name, slot in self._slots.items():
            locs[name] = Location(name, LocationKind.CONVEYOR_SLOT, slot.label)
        self._locations = locs

    @classmethod
    def from_files(cls, root: Path = ROOT) -> "Fr3TransferCapability":
        base = root / "config/workcell"
        load = lambda n: json.loads((base / n).read_text(encoding="utf-8"))  # noqa: E731
        clearance_path = base / "fr3_2f85_workcell_path_clearance.json"
        return cls(workcell=load("fr3_2f85_workcell.json"),
                   grasp=load("fr3_2f85_workcell_grasp.json"),
                   poses=load("fr3_2f85_workcell_poses.json"),
                   clearance=(load(clearance_path.name) if clearance_path.exists() else None))

    # ── 계약 경계 ───────────────────────────────────────────────────
    def locations(self) -> Mapping[str, Location]:
        return self._locations

    def materials(self) -> Sequence[str]:
        return list(self._materials)

    def routes(self):
        return frozenset({(LocationKind.PALLET, LocationKind.CONVEYOR_SLOT),
                          (LocationKind.CONVEYOR_SLOT, LocationKind.PALLET),
                          (LocationKind.CONVEYOR_SLOT, LocationKind.CONVEYOR_SLOT),
                          # 경로 종류로는 받되, 자세가 없어 resolve가 막는다.
                          (LocationKind.PALLET, LocationKind.PALLET)})

    def origin_of(self, material: str) -> str | None:
        return self._origin.get(material)

    def path_obstructions(self, material: str, source: str,
                          destination: str) -> frozenset[str] | None:
        """측정된 경로가 스치는 자리. 측정이 없으면 None, 빈 셀에서도 충돌하면 {"*"}."""
        from core.transfer_skill import ALWAYS_BLOCKED

        row = self._clearance.get(f"{material}:{source}>{destination}")
        if not row:
            return None
        if row.get("status") == "blocked":
            return frozenset({ALWAYS_BLOCKED})
        if row.get("status") != "measured":
            return None
        return frozenset(row.get("blocked_by") or ())

    def resolve(self, material_id: str, source: Location,
                destination: Location) -> RoutePoses | Finding:
        grasp = self._grasp_at(material_id, source)
        if isinstance(grasp, Finding):
            return grasp
        place = self._place_at(material_id, destination, grasp.held_object_source)
        if isinstance(place, Finding):
            return place
        src_approach = self._approach_at(source)
        dst_approach = self._approach_at(destination)
        for item in (src_approach, dst_approach):
            if isinstance(item, Finding):
                return item
        return RoutePoses(source_approach=src_approach, grasp=grasp,
                          destination_approach=dst_approach, place=place)

    # ── 자세(검증된 것만) ─────────────────────────────────────────
    def _missing(self, what: str, material: str, where: Location) -> Finding:
        return Finding("BLOCK", "geometry.grasp_pose_unavailable",
                       f"{material}를 {where.label or where.id}에서 {what} 검증된 자세가 없다"
                       " — 다른 자세로 대신하지 않는다")

    def _approach_at(self, where: Location) -> PoseRef | Finding:
        if where.kind is LocationKind.CONVEYOR_SLOT:
            row = self._slot_rows.get(where.id) or {}
            joints = row.get("approach_joint_rad")
            name = f"conveyor_approach__{where.id}"
        else:
            name = f"{self._model_of_rid.get(where.id)}_approach"
            joints = (self._derived.get(name) or {}).get("joint_rad")
        if not joints:
            return Finding("BLOCK", "geometry.workspace_violation",
                           f"{where.id}에 검증된 접근 자세가 없다")
        return PoseRef(name, PoseRole.APPROACH, where.id,
                       {k: float(v) for k, v in joints.items()})

    def _grasp_at(self, material: str, where: Location) -> PoseRef | Finding:
        if where.kind is LocationKind.CONVEYOR_SLOT:
            joints = (self._slot_rows.get(where.id) or {}).get("grasp_joint_rad_arm")
            base = (self._grasp.get("conveyor_grasp_poses") or {}).get(
                f"{material}_conveyor_grasp") or {}
            if not joints or base.get("status") != "verified" or not base.get("attached_object"):
                return self._missing("집는", material, where)
            return PoseRef(f"{material}_conveyor_grasp__{where.id}", PoseRole.GRASP,
                           where.id, {k: float(v) for k, v in joints.items()},
                           held_object_source=f"conveyor_grasp_poses.{material}_conveyor_grasp",
                           held_object=dict(base["attached_object"]))
        # 자기 원래 팔레트: 측정된 팔레트 파지. 다른 팔레트: 위치 파지(--pallet-matrix).
        if self._origin.get(material) == where.id:
            key, name = "poses", f"{material}_grasp"
        else:
            key, name = "pallet_grasp_poses", f"{material}_grasp_at_{where.id}"
        row = (self._grasp.get(key) or {}).get(name) or {}
        if (row.get("status") != "verified" or not row.get("joint_rad")
                or not row.get("attached_object")):
            return self._missing("집는", material, where)
        return PoseRef(name, PoseRole.GRASP, where.id,
                       {k: float(v) for k, v in row["joint_rad"].items()},
                       held_object_source=f"{key}.{name}",
                       held_object=dict(row["attached_object"]))

    def _place_at(self, material: str, where: Location,
                  held_source: str = "") -> PoseRef | Finding:
        if where.kind is LocationKind.CONVEYOR_SLOT:
            joints = (self._slot_rows.get(where.id) or {}).get("place_joint_rad")
            if not joints:
                return self._missing("놓는", material, where)
            return PoseRef(f"conveyor_place__{where.id}", PoseRole.PLACE, where.id,
                           {k: float(v) for k, v in joints.items()},
                           held_object_source="conveyor_slots.place_held")
        rid = self._rid_of_model.get(material)
        for name, row in (self._grasp.get("pallet_place_poses") or {}).items():
            if (row.get("status") == "verified" and row.get("kind") == "place"
                    and row.get("object_resource_id") == rid
                    and row.get("support_resource_id") == where.id and row.get("joint_rad")
                    and row.get("held_object_source") == held_source):
                return PoseRef(name, PoseRole.PLACE, where.id,
                               {k: float(v) for k, v in row["joint_rad"].items()},
                               held_object_source=str(row.get("held_object_source") or ""))
        return Finding("BLOCK", "geometry.grasp_pose_unavailable",
                       f"{material}를 {where.label or where.id}에 놓는 검증된 자세가 없다"
                       f"(든 물체 출처 {held_source or '없음'}) — 다른 자세로 대신하지 않는다")
