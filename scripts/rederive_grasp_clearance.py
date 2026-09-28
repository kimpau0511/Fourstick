"""파지 높이 재측정 — **위치 오차 여유**를 기준으로 (검사 전용, 로봇을 움직이지 않는다).

    source /opt/ros/lyrical/setup.bash && ROS_DOMAIN_ID=44 \\
        python3 scripts/rederive_grasp_clearance.py [--required-mm 4.0] [--dry-run]

왜. 기존 유도는 쓸 수 있는 파지 높이 구간의 **가장 낮은 dz**(패드 물림이 가장 깊은 자세)를
골랐다. 그 높이에서는 내측 너클이 자재 옆면 바로 옆에 있어, 물리 배치 오차 0.5 mm만으로도
MoveIt이 너클↔자재 접촉을 보고했다(`reports/workcell/grasp_clearance_before.json`).
반복 시연에서 파지가 막힌 원인이다(1번 팔레트 C·원래 팔레트 A·컨베이어 칸).

무엇을 바꾸나. 막힌 파지만 다시 고른다 — A·C의 원래 팔레트 파지, 컨베이어 칸 파지
(칸마다). B의 파지(2번 팔레트)는 이미 ±6 mm를 견뎌 건드리지 않는다.

고르는 기준(측정):
  기존 쓸 수 있는 구간 안에서 dz를 아래부터 올리며
    1) IK가 허용치 안에서 풀리고
    2) 연 상태에서 자재와 아무것도 닿지 않고
    3) 닫은 상태에서 선언된 패드만 자재에 닿으며(패드 접촉이 있어야 한다)
    4) 자재를 수평으로 ±R mm(8방향)·yaw ±1° 옮겨도 2)·3)이 유지되는
  **가장 낮은 dz**를 쓴다. 없으면 기존 값을 두고 `clearance.status=insufficient`로 적는다
  (실행 직전 검사가 여전히 막는다 — 허용 범위를 넓히지 않는다).

바뀌는 값: 파지 관절값·목표 높이·든 물체 offset(`attached_object.offset_m`). 다른 팔레트
위치 파지와 놓기 자세는 이 값에서 이어지므로 이 스크립트 뒤에
`derive_grasp_poses.py --pallet-matrix`, `--pallet-place`를 다시 돌린다.
live planning scene의 자재 상자를 잠시 옮기고, 끝나면 되돌린다.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

GRASP = ROOT / "config/workcell/fr3_2f85_workcell_grasp.json"
WORKCELL = ROOT / "config/workcell/fr3_2f85_workcell.json"
REPORT = ROOT / "reports/workcell/grasp_clearance_rederive.json"
DIRECTIONS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]
YAWS_DEG = (0.0, -1.0, 1.0)
OWN_TARGETS = ("material_a", "material_c")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--required-mm", type=float, default=4.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from derive_grasp_poses import _joints_with, _object_offset_in_tcp, build_client
    from derive_workcell_poses import (GRIPPER_OPEN_RAD, IK_ORIENTATION_TOLERANCE_RAD,
                                       IK_POSITION_TOLERANCE_M, load_urdf, solve_ik)
    from validation.pick_place_plan import classify_contacts

    data = json.loads(WORKCELL.read_text(encoding="utf-8"))
    grasp = json.loads(GRASP.read_text(encoding="utf-8"))
    spec = data["grasp"]
    pad_links = tuple(spec["pad_links"])
    tcp_link = spec["tcp_link"]
    step = float(spec["sweep_step_m"])
    tool_down = data["tcp_targets"]["tool_down_rpy_rad"]
    joints, _links, mimics, limits = load_urdf()
    client, node = build_client({**limits, "robotiq_85_left_knuckle_joint":
                                 {"lower": 0.0, "upper": 0.8}})
    materials = [m for m, item in data["models"].items() if item.get("kind") == "material"]
    before = client.world_object_poses()
    far = {m: (-2.0, 2.0 + 0.3 * i, 0.05) for i, m in enumerate(materials)}
    required = args.required_mm

    def place(model, xyz, yaw_deg=0.0):
        half = math.radians(yaw_deg) / 2
        moves = {m: ("world", tuple(far[m]) + (0.0, 0.0, 0.0, 1.0))
                 for m in materials if m != model}
        moves[model] = ("world", tuple(float(v) for v in xyz)
                        + (0.0, 0.0, math.sin(half), math.cos(half)))
        if not client.move_world_objects(moves):
            raise RuntimeError("scene 상자를 옮기지 못했다")

    def states(model, arm, grip):
        """(연 상태 깨끗, 닫은 상태 패드만, 패드 접촉 있음, 나쁜 쌍)."""
        open_v = client.check_state(_joints_with(arm, GRIPPER_OPEN_RAD, mimics, spec))
        closed_v = client.check_state(_joints_with(arm, grip, mimics, spec))
        open_pairs = [list(p) for p in open_v.contacts if model in p]
        closed_pairs = [list(p) for p in closed_v.contacts if model in p]
        _a, open_bad = classify_contacts(open_pairs, object_ids=(model,), pad_links=())
        allowed, closed_bad = classify_contacts(closed_pairs, object_ids=(model,),
                                                pad_links=pad_links)
        return (not open_bad and not open_v.out_of_bounds,
                not closed_bad and not closed_v.out_of_bounds, bool(allowed),
                [list(b) for b in open_bad + closed_bad])

    def tolerance(model, center, arm, grip) -> tuple[float, dict]:
        """모든 방향·yaw에서 깨끗한 가장 큰 반경(mm, 0.5 mm 간격, 최대 required+2)."""
        radius, clean, fail = 0.0, None, {}
        while radius <= required + 2.0 + 1e-9:
            ok_ring = True
            for ux, uy in ([(0, 0)] if radius == 0 else DIRECTIONS):
                norm = math.hypot(ux, uy) or 1.0
                d = np.array([ux / norm * radius, uy / norm * radius, 0.0]) / 1000
                for yaw in YAWS_DEG:
                    place(model, np.asarray(center) + d, yaw)
                    ok_open, ok_closed, _pads, bad = states(model, arm, grip)
                    if not (ok_open and ok_closed):
                        ok_ring = False
                        fail = {"radius_mm": radius, "dir": [ux, uy], "yaw_deg": yaw,
                                "pairs": bad}
                        break
                if not ok_ring:
                    break
            if not ok_ring:
                break
            clean = radius
            radius += 0.5
        return (clean if clean is not None else -1.0), fail

    def reselect(label, model, center, seed_arm, grip, dz_candidates):
        rows, chosen = [], None
        for dz in dz_candidates:
            target = np.asarray(center, dtype=float) + np.array([0.0, 0.0, dz])
            arm, dist, ang = solve_ik(joints, mimics, limits, target, tool_down, dict(seed_arm))
            row = {"dz_m": round(dz, 6), "ik_position_error_m": round(dist, 6),
                   "ik_orientation_error_rad": round(ang, 6)}
            if dist > IK_POSITION_TOLERANCE_M or ang > IK_ORIENTATION_TOLERANCE_RAD:
                row["usable"] = False
                rows.append(row)
                continue
            place(model, center)
            ok_open, ok_closed, pads, bad = states(model, arm, grip)
            row.update(open_clean=ok_open, closed_pads_only=ok_closed, pad_contact=pads)
            if not (ok_open and ok_closed and pads):
                row.update(usable=False, bad=bad)
                rows.append(row)
                continue
            radius, fail = tolerance(model, center, arm, grip)
            row.update(usable=True, clean_radius_mm=radius, first_failure=fail)
            rows.append(row)
            print(f"  {label} dz {dz:.3f} → 여유 {radius} mm", flush=True)
            if radius >= required and chosen is None:
                chosen = (dz, arm, radius)
                break
        return chosen, rows

    report = {"schema": "forstick2.grasp_clearance_rederive/1", "is_simulated": True,
              "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "required_mm": required, "yaws_deg": list(YAWS_DEG),
              "criterion": ("IK · 연 상태 무접촉 · 닫은 상태 패드만(패드 접촉 필요) · 수평 ±R mm"
                            "(8방향)·yaw ±1°에서도 유지되는 가장 낮은 dz"),
              "own": {}, "conveyor": {}}
    changed = False
    try:
        # ── 원래 팔레트 파지 ─────────────────────────────────────────
        for model in OWN_TARGETS:
            name = f"{model}_grasp"
            entry = grasp["poses"][name]
            obj_rid = entry["object_resource_id"]
            center = grasp["objects"][obj_rid]["world_center_m"]
            lo, hi = entry["usable_dz_range_m"]
            candidates = list(np.round(np.arange(lo, hi + 1e-9, step), 6))
            chosen, rows = reselect(name, model, center, entry["joint_rad"],
                                    float(entry["gripper_grasp_joint_rad"]), candidates)
            report["own"][name] = {"previous_dz_m": entry["target_offset_m"][2],
                                   "rows": rows,
                                   "chosen_dz_m": None if chosen is None else chosen[0]}
            if chosen is None:
                entry["clearance"] = {"status": "insufficient", "required_mm": required,
                                      "measured_at": report["measured_at"]}
                continue
            dz, arm, radius = chosen
            offset, tcp_world = _object_offset_in_tcp(joints, mimics, spec, arm, center,
                                                      tcp_link)
            previous = {"dz_m": entry["target_offset_m"][2],
                        "joint_rad": entry["joint_rad"],
                        "offset_m": entry["attached_object"]["offset_m"]}
            entry.update(
                target_offset_m=[0.0, 0.0, dz],
                target_world_xyz_m=[round(float(center[0]), 6), round(float(center[1]), 6),
                                    round(float(center[2]) + dz, 6)],
                joint_rad={k: round(v, 6) for k, v in arm.items()},
                fk_tcp_world_m=[round(float(v), 6) for v in tcp_world],
                captured_at=time.strftime("%Y-%m-%d"),
                clearance={"status": "verified", "clean_radius_mm": radius,
                           "required_mm": required, "yaws_deg": list(YAWS_DEG),
                           "previous": previous,
                           "selection": "위치 오차 여유를 만족하는 가장 낮은 dz(재측정)",
                           "report": str(REPORT.relative_to(ROOT))})
            entry["attached_object"]["offset_m"] = [round(float(v), 6) for v in offset]
            entry["note"] = ("파지 높이는 측정한 구간에서 위치 오차 여유(±"
                             f"{required} mm, yaw ±1°)를 만족하는 가장 낮은 dz다")
            changed = True
        # ── 컨베이어 칸 파지 ─────────────────────────────────────────
        slots = grasp["conveyor_slots"]["slots"]
        conv_key = next(iter(grasp["conveyor_grasp_poses"]))
        conv = grasp["conveyor_grasp_poses"][conv_key]
        lo, hi = conv["usable_dz_range_m"]
        candidates = list(np.round(np.arange(lo, hi + 1e-9, step), 6))
        grip = float(conv["gripper_grasp_joint_rad"])
        new_dz: dict[str, float] = {}
        new_arms: dict[str, dict] = {}
        for slot, srow in slots.items():
            center = srow["object_world_center_m"]
            chosen, rows = reselect(f"conveyor {slot}", "material_a", center,
                                    srow["grasp_joint_rad_arm"], grip, candidates)
            report["conveyor"][slot] = {"rows": rows,
                                        "chosen_dz_m": None if chosen is None else chosen[0]}
            if chosen is not None:
                new_dz[slot] = chosen[0]
                new_arms[slot] = chosen[1]
        if len(new_dz) == len(slots) and len(set(new_dz.values())) == 1:
            # 칸 파지는 같은 든 물체 offset을 공유한다 — 모든 칸이 같은 dz일 때만 바꾼다.
            dz = next(iter(new_dz.values()))
            center = slots["slot_1"]["object_world_center_m"]
            offset, _t = _object_offset_in_tcp(joints, mimics, spec, new_arms["slot_1"],
                                               center, tcp_link)
            for slot, arm in new_arms.items():
                slots[slot]["grasp_joint_rad_arm_previous"] = slots[slot]["grasp_joint_rad_arm"]
                slots[slot]["grasp_joint_rad_arm"] = {k: round(v, 6) for k, v in arm.items()}
                slots[slot]["grasp_dz_m"] = dz
            for key, row in grasp["conveyor_grasp_poses"].items():
                row["clearance"] = {"status": "verified", "dz_m": dz,
                                    "previous_offset_m": row["attached_object"]["offset_m"],
                                    "required_mm": required,
                                    "report": str(REPORT.relative_to(ROOT))}
                row["attached_object"]["offset_m"] = [round(float(v), 6) for v in offset]
            changed = True
        else:
            report["conveyor_note"] = f"칸마다 고른 dz가 다르거나 없다: {new_dz} — 바꾸지 않았다"
    finally:
        client.move_world_objects({m: before[m] for m in materials if m in before})
        node.destroy_node()
        import rclpy
        rclpy.try_shutdown()
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if changed and not args.dry_run:
        grasp["grasp_clearance_rederived_at"] = report["measured_at"]
        GRASP.write_text(json.dumps(grasp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"설정 갱신: {GRASP}")
    print(f"보고서: {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
