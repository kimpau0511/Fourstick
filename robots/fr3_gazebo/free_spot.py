"""FR3 작업 셀의 **표면 빈 위치 놓기** 자세 계산·검증 (로봇별 몫).

공통 후보 계산(`validation/free_spot.py`)이 형상·점유로 거른 자리마다:

1. 출발 쪽 자세는 계약(`Fr3TransferCapability.source_poses`)의 **검증된 것만** 쓴다.
2. 도착 쪽 접근·놓기 자세를 **그 자리에서** IK로 푼다(`robots/moveit/chain_ik.py`).
   TCP 높이는 검증된 컨베이어 놓기·접근과 **같은 받침 기준 높이**다
   (`fr3_2f85_workcell_poses.json`의 `conveyor_place`·`conveyor_approach` `target_offset_m`).
   기존 칸·팔레트 놓기 자세를 다른 좌표에 쓰지 않는다.
3. 실행기와 같은 12단계(`validation/transfer_stages`)를 만들고, 실행기와 같은 검사
   (`check_stages` — 든 물체·손가락 포함, `check_path` — 단계 사이 표본)를 MoveIt에 묻는다.
   planning scene이 다른 자재의 실제 위치를 아직 반영하지 않았으면 그 자재를 탐침으로 넣는다.

여기서 통과한 자리도 실행 허가가 아니다. 확인 카드 → 실행 직전 재검사(서버) → 실행기의 장면
동기화·사전 검사·경로 검사를 다시 거친다.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from core.geometry import AttachedObject
from core.transfer_skill import Finding, PoseRef, PoseRole, RoutePoses
from robots.moveit.chain_ik import ChainIk
from robots.moveit.kinematics import link_transforms, parse_urdf

ROOT = Path(__file__).resolve().parents[2]
ARM_JOINTS = ("j1", "j2", "j3", "j4", "j5", "j6")
#: 탐침이 받침면에 닿지 않게 바닥을 띄우는 높이는 셀 여유값(`environment_clearance_min_m`)을 쓴다.
SPOT_PREFIX = "spot"


def urdf_path() -> Path:
    """MoveIt이 쓰는 조립 URDF(실행 스크립트가 띄울 때 만든다). 실행기와 같은 파일."""
    log_dir = Path(os.environ.get("FORSTICK2_WORKCELL_LOG_DIR", "/tmp/forstick2_workcell"))
    return log_dir / "workcell" / "fr3wms_with_2f85.moveit.urdf"


def spot_id(surface_id: str, center_xy: Sequence[float]) -> str:
    """빈 위치 식별자. 표면과 중심(mm)으로 만든다 — 같은 자리는 같은 id다."""
    return (f"{SPOT_PREFIX}:{surface_id}:{round(float(center_xy[0]) * 1000):d}"
            f":{round(float(center_xy[1]) * 1000):d}")


@dataclass(frozen=True)
class SpotPlan:
    spot_id: str
    surface_id: str
    center_m: tuple[float, float, float]
    route: RoutePoses
    ik: Mapping[str, Any]

    def to_dict(self) -> dict:
        return {"spot_id": self.spot_id, "surface_id": self.surface_id,
                "center_m": [round(v, 6) for v in self.center_m],
                "source_approach": self.route.source_approach.name,
                "grasp": self.route.grasp.name,
                "destination_approach": {"name": self.route.destination_approach.name,
                                         "joint_rad": dict(self.route.destination_approach.joint_rad)},
                "place": {"name": self.route.place.name,
                          "joint_rad": dict(self.route.place.joint_rad)},
                "held_object_source": self.route.grasp.held_object_source,
                "ik": dict(self.ik)}


class Fr3FreeSpotPlanner:
    def __init__(self, *, workcell: Mapping[str, Any], poses: Mapping[str, Any],
                 grasp: Mapping[str, Any], mounting: Mapping[str, Any], capability,
                 urdf: Path | None = None):
        self.workcell, self.poses, self.grasp = workcell, poses, grasp
        self.capability = capability
        derived = poses.get("poses") or {}
        self.place_dz = float(derived["conveyor_place"]["target_offset_m"][2])
        self.approach_dz = float(derived["conveyor_approach"]["target_offset_m"][2])
        self.tool_rpy = tuple(float(v) for v in workcell["tcp_targets"]["tool_down_rpy_rad"])
        self.tcp_link = str(workcell["grasp"]["tcp_link"])
        self.margin = float(workcell["collision_margins_m"]["environment_clearance_min_m"])
        self.open_aperture_m = float(mounting["gripper_official"]["aperture_at_open_m"])
        path = urdf or urdf_path()
        joints, _links = parse_urdf(path)
        self._urdf_joints = joints
        self.ik = ChainIk(joints, root="world", tip=self.tcp_link, arm_joints=ARM_JOINTS)
        base_rotation, base_world = link_transforms(joints, {})["base_link"]
        self.base_rotation, self.base_world = base_rotation, base_world
        self.urdf = str(path)
        # IK 시작점: 검증된 접근 자세들(가까운 순으로 정렬해 쓴다). 결과가 아니라 시작점이다.
        self._seeds = {name: row["joint_rad"] for name, row in derived.items()
                       if row.get("status") == "verified" and name.endswith("_approach")}
        for name, row in ((grasp.get("conveyor_slots") or {}).get("slots") or {}).items():
            if row.get("approach_joint_rad"):
                self._seeds[f"{name}_approach"] = row["approach_joint_rad"]
        self._seed_tips = {n: self.ik.tip_pose(q)[1] for n, q in self._seeds.items()}

    @classmethod
    def from_files(cls, capability, root: Path = ROOT, urdf: Path | None = None):
        base = root / "config/workcell"
        load = lambda p: json.loads(p.read_text(encoding="utf-8"))  # noqa: E731
        return cls(workcell=load(base / "fr3_2f85_workcell.json"),
                   poses=load(base / "fr3_2f85_workcell_poses.json"),
                   grasp=load(base / "fr3_2f85_workcell_grasp.json"),
                   mounting=load(root / "config/profiles/fr3wms_to_robotiq_2f85_mounting.json"),
                   capability=capability, urdf=urdf)

    # ── 공통 후보 계산에 넘기는 로봇 값 ─────────────────────────────
    def material_size(self, model: str) -> tuple[float, float, float]:
        return tuple(float(v) for v in self.workcell["models"][model]["size_m"])

    def sufficient_gap_m(self, model: str) -> float:
        """다른 자재와 이만큼 떨어지면 열린 손가락이 닿지 않는다(정렬 기준).
        근거: 슬롯 간격 하한과 같은 식 — 자재 반폭 + 열린 개구 반폭 + 셀 여유."""
        return self.material_size(model)[0] / 2 + self.open_aperture_m / 2 + self.margin

    def robot_base_xy(self) -> tuple[float, float]:
        return float(self.base_world[0]), float(self.base_world[1])

    # ── 자세 ──────────────────────────────────────────────────────────
    def _solve(self, xyz: Sequence[float], first: Mapping[str, float] | None = None):
        order = sorted(self._seeds, key=lambda n: math.dist(self._seed_tips[n], xyz))
        seeds = ([first] if first else []) + [self._seeds[n] for n in order]
        return self.ik.solve(xyz, self.tool_rpy, seeds)

    def plan(self, *, material: str, source: str, surface_id: str, surface_top_z: float,
             center_xy: Sequence[float]) -> SpotPlan | Finding:
        """그 자리에 놓는 경로 자세. IK가 허용치에 들지 못하면 Finding(BLOCK)."""
        poses = self.capability.source_poses(material, source)
        if isinstance(poses, Finding):
            return poses
        source_approach, grasp = poses
        x, y = float(center_xy[0]), float(center_xy[1])
        object_z = surface_top_z + self.material_size(material)[2] / 2
        sid = spot_id(surface_id, (x, y))
        approach = self._solve((x, y, surface_top_z + self.approach_dz))
        if not approach.converged:
            return Finding("BLOCK", "geometry.workspace_violation",
                           f"{sid} 접근 자세 IK가 허용치에 들지 않는다"
                           f" (위치 오차 {approach.position_error_m:.4f} m)")
        place = self._solve((x, y, surface_top_z + self.place_dz), first=approach.joints)
        if not place.converged:
            return Finding("BLOCK", "geometry.workspace_violation",
                           f"{sid} 놓기 자세 IK가 허용치에 들지 않는다"
                           f" (위치 오차 {place.position_error_m:.4f} m)")
        # 칸·팔레트에 검증된 놓기 자세를 다른 좌표에 쓰지 않는다 — 같은 값이면 막는다.
        for name, joints in self._verified_place_poses().items():
            if all(abs(float(joints[k]) - float(place.joints[k])) < 1e-6 for k in ARM_JOINTS):
                return Finding("BLOCK", "geometry.grasp_reused_as_place",
                               f"계산된 놓기 자세가 기존 {name}과 같다")
        route = RoutePoses(
            source_approach=source_approach, grasp=grasp,
            destination_approach=PoseRef(f"{sid}:approach", PoseRole.APPROACH, sid,
                                         dict(approach.joints)),
            place=PoseRef(f"{sid}:place", PoseRole.PLACE, sid, dict(place.joints),
                          held_object_source=grasp.held_object_source))
        return SpotPlan(spot_id=sid, surface_id=surface_id, center_m=(x, y, object_z),
                        route=route,
                        ik={"approach": approach.to_dict(), "place": place.to_dict(),
                            "tcp_relation": {"place_dz_m": self.place_dz,
                                             "approach_dz_m": self.approach_dz,
                                             "basis": "conveyor_place·conveyor_approach"
                                                      " target_offset_m(받침 상면 기준)"},
                            "tool_rpy_rad": list(self.tool_rpy), "urdf": self.urdf})

    def plan_from_spot(self, spot: Mapping[str, Any], *, material: str,
                       source: str) -> SpotPlan | Finding:
        """확인 카드가 담아 둔 자리 → SpotPlan. 출발 쪽은 **지금 계약**에서 다시 받는다."""
        poses = self.capability.source_poses(material, source)
        if isinstance(poses, Finding):
            return poses
        source_approach, grasp = poses
        if (source_approach.name, grasp.name) != (spot.get("source_approach"), spot.get("grasp")):
            return Finding("BLOCK", "plan.resource_mismatch",
                           "확인 때의 출발 자세가 지금 계약의 자세와 다르다")
        sid = str(spot["spot_id"])
        route = RoutePoses(
            source_approach=source_approach, grasp=grasp,
            destination_approach=PoseRef(spot["destination_approach"]["name"], PoseRole.APPROACH,
                                         sid, dict(spot["destination_approach"]["joint_rad"])),
            place=PoseRef(spot["place"]["name"], PoseRole.PLACE, sid,
                          dict(spot["place"]["joint_rad"]),
                          held_object_source=grasp.held_object_source))
        return SpotPlan(spot_id=sid, surface_id=str(spot["surface_id"]),
                        center_m=tuple(float(v) for v in spot["center_m"]), route=route,
                        ik=dict(spot.get("ik") or {}))

    def _verified_place_poses(self) -> dict:
        out = {}
        for name, row in ((self.grasp.get("conveyor_slots") or {}).get("slots") or {}).items():
            if row.get("place_joint_rad"):
                out[f"conveyor_slot:{name}"] = row["place_joint_rad"]
        for name, row in (self.grasp.get("pallet_place_poses") or {}).items():
            if row.get("joint_rad"):
                out[name] = row["joint_rad"]
        for name, row in (self.poses.get("poses") or {}).items():
            if name.endswith("_place") and row.get("joint_rad"):
                out[name] = row["joint_rad"]
        return out

    def fk_check(self, plan_dict: Mapping[str, Any], *, surface_top_z: float) -> list[str]:
        """실행기가 받은 자세가 정말 그 자리의 자세인가(FK로 다시 본다). 문제 목록."""
        from robots.moveit.chain_ik import ORIENTATION_TOLERANCE_RAD, POSITION_TOLERANCE_M
        from robots.moveit.kinematics import rpy_matrix

        problems = []
        cx, cy = plan_dict["center_m"][0], plan_dict["center_m"][1]
        target_rotation = rpy_matrix(*self.tool_rpy)
        for key, dz in (("destination_approach", self.approach_dz), ("place", self.place_dz)):
            joints = plan_dict[key]["joint_rad"]
            rotation, position = self.ik.tip_pose(joints)
            distance = math.dist(position, (cx, cy, surface_top_z + dz))
            relative = target_rotation @ rotation.T
            angle = float(np.arccos(np.clip((np.trace(relative) - 1) / 2, -1, 1)))
            if distance > POSITION_TOLERANCE_M or angle > ORIENTATION_TOLERANCE_RAD:
                problems.append(f"{key} 자세의 TCP가 자리에서 {distance:.4f} m·{angle:.3f} rad"
                                " 벗어난다")
        return problems

    # ── 검증(MoveIt) ──────────────────────────────────────────────────
    def probes(self, *, others: Mapping[str, Sequence[float]],
               scene_poses: Mapping[str, Sequence[float]], tolerance_m: float) -> list:
        """scene 상자가 관측과 다른 자재를 탐침으로. 바닥은 여유만큼 띄워 받침에 닿지 않게."""
        out = []
        for model, pose in others.items():
            scene = scene_poses.get(model)
            if scene is not None and math.dist(scene[:3], pose[:3]) <= tolerance_m:
                continue   # scene이 이미 그 자리에 상자를 두고 있다
            size = self.material_size(model)
            height = size[2] - self.margin
            center = np.array([pose[0], pose[1], pose[2] + self.margin / 2], dtype=float)
            offset = self.base_rotation.T @ (center - self.base_world)
            out.append(AttachedObject(
                object_id=f"{model}__observed_probe", link="base_link",
                size_m=(size[0], size[1], height), offset_m=tuple(float(v) for v in offset),
                touch_links=(), source="Gazebo 관측 pose(scene 미반영 자재)"))
        return out

    def verify(self, plan: SpotPlan, *, material: str, source: str, bindings, client,
               obstacles: Sequence[Any] = (), start_joints: Mapping[str, float] | None = None
               ) -> tuple[bool, dict]:
        """실행기와 같은 12단계·단계 사이 경로 검사. (통과, 근거)."""
        from validation.pick_place_plan import check_path, check_stages
        from validation.transfer_stages import bind_route, build_route_stages

        object_id = next(r["resource_id"] for r in self.workcell["resource_map"]
                         if r.get("gazebo_model") == material)
        held = dict(plan.route.grasp.held_object or {})
        held["source"] = plan.route.grasp.held_object_source
        source_rid = source if not source.startswith("slot_") else "loc_conveyor"
        bound = replace(bindings, attached={**bindings.attached, object_id: held},
                        object_support={**bindings.object_support, object_id: source_rid})
        try:
            bound = bind_route(bound, plan.route)
        except ValueError as exc:
            return False, {"problem": str(exc)}
        stages, findings = build_route_stages(bound, object_id=object_id, route=plan.route,
                                              source_resource_id=source_rid,
                                              destination_resource_id=plan.spot_id)
        if findings:
            return False, {"problem": "; ".join(t for _, t in findings)}
        checks, bad = check_stages(stages, bindings=bound, client=client, obstacles=obstacles)
        evidence = {"stages": len(stages), "stage_checks_passed": sum(c.passed for c in checks),
                    "probes": [o.object_id for o in obstacles]}
        if bad:
            evidence["problem"] = "; ".join(f.detail for f in bad[:3])
            return False, evidence
        start = start_joints or bound.poses[bound.home_pose]
        _path, path_bad, count = check_path(stages, bindings=bound, client=client,
                                            start_joints=start, obstacles=obstacles)
        evidence.update(path_samples=count, path_failures=len(path_bad))
        if path_bad:
            evidence["problem"] = "경로: " + "; ".join(f.detail for f in path_bad[:3])
            return False, evidence
        return True, evidence
