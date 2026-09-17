#!/usr/bin/env python3
"""파지·적재 자세를 **측정해서** 유도한다 (8-10 pick/place 계획 검증).

왜 필요한가. 기존 `pallet_n_pick` 자세는 TCP를 자재 **중심**에 두는 목표였다.
IK는 그 자세를 허용치 안에서 풀었지만, MoveIt에 물어보니 자재 높이 0.1 m에
대해 그리퍼 **몸통과 너클이 자재를 파고든다**(실측: `robotiq_85_base_link`,
`robotiq_85_left/right_inner_knuckle_link` ↔ `material_*`). 자세 파일도 그
판정을 pick/place 관문으로 미뤄 두었다(`clearance_check_note`).

그래서 여기서 파지 높이를 **고르지 않고 측정한다.**

    자재 중심 위 dz를 sweep_step_m 간격으로 훑으면서
      1) IK가 허용치 안에서 풀리는가
      2) 그리퍼를 연 상태로 넣었을 때 **아무것과도 닿지 않는가**
      3) 자재 폭만큼 닫았을 때 **선언된 패드만** 자재에 닿는가
    세 조건을 모두 만족하는 dz 구간을 기록하고,
    그 구간의 **가장 낮은 dz**를 파지 높이로 쓴다(패드 물림이 가장 깊다).

값을 만들지 않는다. 구간이 비어 있으면 `status=blocked`로 적고 이유를 남긴다.

적재(물체를 든) 상태도 같이 측정한다. 물체를 들고 나면 그 물체가 무엇과
닿는지 봐야 하므로, 측정한 파지 자세에서 물체 중심을 TCP 링크 좌표계로 옮겨
`AttachedObject`로 붙인 뒤 나머지 자세를 다시 묻는다.

한계(측정할 수 없는 것)도 함께 적는다.
- 현재 planning scene에는 자재가 **원래 자리에** 있다. 들어 올린 상태를 scene이
  모르므로, 붙인 사본과 원래 물체가 겹쳐 보고된다 — 그 쌍만 선언된 예외로
  기록하고(`world_instance_id`) 다른 접촉은 모두 충돌로 본다.
- place 이후 자재의 새 위치는 scene에 없다. 해제 이후 단계는 **빈손 기준**으로만
  검사했다고 적는다.
- 물체를 실제로 쥐었는지는 여기서 알 수 없다(`grasp.object_held` 미확보).

실행하지 않는다. 로봇을 움직이지 않는다 — IK와 MoveIt 질의만 한다.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.geometry import AttachedObject  # noqa: E402
from robots.moveit.kinematics import link_transforms  # noqa: E402
from validation.pick_place_plan import classify_contacts  # noqa: E402
from scripts.derive_workcell_poses import (  # noqa: E402
    ARM_JOINTS,
    GRIPPER_OPEN_RAD,
    IK_ORIENTATION_TOLERANCE_RAD,
    IK_POSITION_TOLERANCE_M,
    WORKCELL,
    angles_for,
    load_urdf,
    solve_ik,
)

OUT = ROOT / "config/workcell/fr3_2f85_workcell_grasp.json"
POSES = ROOT / "config/workcell/fr3_2f85_workcell_poses.json"
MOUNTING = ROOT / "config/profiles/fr3wms_to_robotiq_2f85_mounting.json"


def resolve_frame(frames: dict, name: str) -> np.ndarray:
    """프레임 원점을 world 기준으로 푼다(선언된 부모 관계만 따른다)."""
    out = np.zeros(3)
    while name is not None:
        out = out + np.asarray(frames[name]["xyz_m"])
        name = frames[name]["parent"]
    return out


def support_of(frames: dict, object_frame: str) -> str | None:
    """자재가 **선언상** 어느 프레임 위에 있는가. 이름 규칙으로 추정하지 않는다."""
    return frames.get(object_frame, {}).get("parent")


def aperture_joint(width_m: float) -> tuple[float | None, dict]:
    """자재 폭에 해당하는 그리퍼 명령 관절값. 공식 FK 개구 표에서만 온다."""
    from core.aperture_model import ApertureModel

    if not MOUNTING.is_file():
        return None, {"detail": f"장착 Profile이 없다: {MOUNTING}"}
    mounting = json.loads(MOUNTING.read_text(encoding="utf-8"))
    rows = mounting.get("transform_derivation", {}).get("aperture_table")
    if not rows:
        return None, {"detail": "장착 Profile에 개구 표가 없다"}
    model = ApertureModel.from_rows(
        rows,
        source=f"{mounting['mounting_profile_id']}"
                f" {mounting['mounting_profile_version']}")
    joint = model.joint_for(width_m)
    evidence = {
        "aperture_target_m": width_m,
        "joint_rad": joint,
        "aperture_back_m": None if joint is None else model.aperture_for(joint),
        "source": model.source,
        "max_interpolation_error_m": model.max_interpolation_error_m,
        "method": "official_fk_aperture_table_inverse",
    }
    return joint, evidence


def classify(validity, *, object_id: str, pad_links: tuple[str, ...],
             world_instance_id: str | None = None,
             support_ids: tuple[str, ...] = ()) -> dict:
    """MoveIt 판정을 접촉 대상별로 나눈다.

    허용 규칙은 **검증 계층과 같은 구현**을 쓴다
    (`validation.pick_place_plan.classify_contacts`) — 두 곳에 두면 갈라진다.
    """
    objects = [object_id] + ([world_instance_id] if world_instance_id else [])
    allowed, forbidden = classify_contacts(
        [list(pair) for pair in validity.contacts],
        object_ids=objects, pad_links=pad_links, support_ids=support_ids)
    return {
        "valid": bool(validity.valid),
        "out_of_bounds": list(validity.out_of_bounds),
        "expected_contacts": [list(pair) for pair in allowed],
        "collisions": [list(pair) for pair in forbidden],
        "clean": not forbidden and not validity.out_of_bounds,
    }


def build_client(limits: dict):
    """MoveIt planning scene 클라이언트. 붙지 않으면 측정하지 않는다."""
    import rclpy
    from rclpy.node import Node

    from robots.moveit.ros_client import RosPlanningSceneClient

    if not rclpy.ok():
        rclpy.init()
    node = Node("forstick2_grasp_derivation")
    client = RosPlanningSceneClient(
        node, group_name="fr3wms_arm", frame_id="base_link",
        joint_limits={name: (info["lower"], info["upper"])
                      for name, info in limits.items()},
        model_hash="grasp_derivation", ttl_sec=5.0,
        source="scripts/derive_grasp_poses.py")
    if not client.wait(timeout_sec=30.0):
        node.destroy_node()
        raise SystemExit(
            "MoveIt planning scene 서비스가 없다 — 먼저 실행한다:"
            " ./scripts/run_moveit_workcell.sh")
    return client, node


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    data = json.loads(WORKCELL.read_text(encoding="utf-8"))
    derived = json.loads(POSES.read_text(encoding="utf-8"))
    spec = data["grasp"]
    frames = data["frames"]
    pad_links = tuple(spec["pad_links"])
    tcp_link = spec["tcp_link"]
    step = float(spec["sweep_step_m"])
    tool_down = data["tcp_targets"]["tool_down_rpy_rad"]

    joints, _links, mimics, limits = load_urdf()
    gripper_limits = {"robotiq_85_left_knuckle_joint": {"lower": 0.0, "upper": 0.8}}
    client, node = build_client({**limits, **gripper_limits})

    before = client.snapshot()
    verified_poses = {name: pose["joint_rad"]
                      for name, pose in derived["poses"].items()
                      if pose.get("status") == "verified"}
    verified_poses["safe_home"] = derived["safe_home"]["joint_rad"]

    # 자재 → (선언된) 받침 위치. 프레임 부모 관계가 유일한 출처다.
    objects = {}
    for row in data["resource_map"]:
        model = data["models"].get(row["gazebo_model"])
        if model is None or model.get("kind") != "material":
            continue
        support_frame = support_of(frames, row["frame"])
        support_resource = next(
            (r["resource_id"] for r in data["resource_map"]
             if r["frame"] == support_frame), None)
        objects[row["resource_id"]] = {
            "resource_id": row["resource_id"],
            "gazebo_model": row["gazebo_model"],
            "frame": row["frame"],
            "size_m": list(model["size_m"]),
            "support_frame": support_frame,
            "support_resource_id": support_resource,
            "world_center_m": [round(float(v), 6)
                               for v in resolve_frame(frames, row["frame"])],
        }

    report: dict = {
        "schema": "forstick2.workcell_grasp/1",
        "workcell_id": data["workcell_id"],
        "workcell_version": data["workcell_version"],
        "derived_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "is_simulated": True,
        "real_hardware_verified": False,
        "method": "moveit_contact_sweep_plus_numerical_ik",
        "scene": {
            "snapshot_id": before.snapshot_id,
            "snapshot_version": before.snapshot_version,
            "content_hash": before.content_hash,
            "frame_id": before.frame_id,
            "world_object_count": before.summary.get("world_object_count"),
        },
        "declared": {
            # mimic 관절은 URDF 선언에서 온다. 검증 계층이 URDF를 읽지 않아도
            # 같은 관절 상태를 만들 수 있게 여기 적는다.
            "gripper_mimic": {name: list(values) for name, values in mimics.items()},
            "gripper_mimic_source": "공식 robotiq_description URDF의 mimic 선언",
            "pad_links": list(pad_links),
            "pad_links_source": spec["pad_links_source"],
            "tcp_link": tcp_link,
            "gripper_command_joint": spec["gripper_command_joint"],
            "sweep_step_m": step,
        },
        "object_support": {rid: item["support_resource_id"]
                           for rid, item in objects.items()},
        "objects": objects,
        "gripper": {},
        "poses": {},
        "sweeps": {},
        "limitations": [],
    }

    # ── 1. 그리퍼 명령값: 열림과 자재 폭 파지 ─────────────────────────
    # 파지 개구는 **선언된 자재 폭**을 공식 FK 개구 표로 역산한 값이다.
    widths = sorted({round(float(item["size_m"][0]), 6) for item in objects.values()})
    if len(widths) != 1:
        client_close(node)
        raise SystemExit(
            f"자재 폭이 서로 다르다: {widths} — 폭별로 따로 측정해야 한다")
    width = widths[0]
    grasp_joint, aperture_evidence = aperture_joint(width)
    report["gripper"] = {
        "open_joint_rad": GRIPPER_OPEN_RAD,
        "open_source": "reports/workcell/gripper_control.json 01_open"
                       " (명령 0.0 rad → 관측 개구 0.084996 m, 오차 1 µm)",
        "grasp_joint_rad": grasp_joint,
        "grasp_target_aperture_m": width,
        "grasp_aperture_evidence": aperture_evidence,
        "status": "measured" if grasp_joint is not None else "blocked",
        "note": "개구가 자재 폭과 맞는다는 것이 **물체를 쥐었다는 뜻이 아니다.**"
                " 파지 여부는 grasp.object_held 관측이 있어야 판정한다",
    }
    if grasp_joint is None:
        report["limitations"].append(
            "자재 폭에 해당하는 그리퍼 관절값을 개구 표에서 얻지 못했다")
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        client_close(node)
        print("파지 개구를 측정하지 못했다 — 값을 만들지 않고 멈춘다")
        return 1

    # ── 2. 파지 높이 훑기 ──────────────────────────────────────────────
    for rid, item in sorted(objects.items()):
        model_id = item["gazebo_model"]
        center = np.asarray(item["world_center_m"])
        height = float(item["size_m"][2])
        support_rid = item["support_resource_id"]
        seed_pose = next(
            (verified_poses[name] for name in verified_poses
             if name.startswith(f"{_model_of(data, support_rid)}_approach")),
            derived["safe_home"]["joint_rad"])

        rows = []
        # 자재 중심 기준 dz. 아래(중심 아래)부터 자재 높이 절반 위까지 훑는다.
        count = int(round((height / 2 + 0.06) / step)) + 1
        for index in range(count):
            dz = round(-height / 2 + index * step, 6)
            target = center + np.array([0.0, 0.0, dz])
            arm, distance, angle = solve_ik(
                joints, mimics, limits, target, tool_down, dict(seed_pose))
            reachable = (distance <= IK_POSITION_TOLERANCE_M
                         and angle <= IK_ORIENTATION_TOLERANCE_RAD)
            row = {
                "dz_m": dz,
                "tcp_world_z_m": round(float(target[2]), 6),
                "position_error_m": round(distance, 6),
                "orientation_error_rad": round(angle, 6),
                "reachable": reachable,
            }
            if reachable:
                open_state = client.check_state(
                    _joints_with(arm, GRIPPER_OPEN_RAD, mimics, spec))
                row["open"] = classify(open_state, object_id=model_id,
                                       pad_links=())
                closed_state = client.check_state(
                    _joints_with(arm, grasp_joint, mimics, spec))
                row["closed"] = classify(closed_state, object_id=model_id,
                                         pad_links=pad_links)
                row["usable"] = bool(row["open"]["clean"]
                                     and row["closed"]["clean"]
                                     and row["closed"]["expected_contacts"])
                row["joint_rad"] = {k: round(v, 6) for k, v in arm.items()}
            else:
                row["usable"] = False
            rows.append(row)

        usable = [row for row in rows if row["usable"]]
        report["sweeps"][rid] = {
            "object": model_id,
            "support_resource_id": support_rid,
            "steps": [{k: v for k, v in row.items() if k != "joint_rad"}
                      for row in rows],
            "usable_dz_m": [row["dz_m"] for row in usable],
            "criterion": "연 상태에서 아무것과도 닿지 않고, 자재 폭만큼 닫았을"
                         " 때 선언된 패드만 자재에 닿는 dz",
        }
        pose_name = f"{model_id}_grasp"
        if not usable:
            report["poses"][pose_name] = {
                "status": "blocked",
                "reason_code": "geometry.collision",
                "detail": "패드만 닿는 파지 높이가 훑기 구간에 없다",
                "object_resource_id": rid,
            }
            continue
        # **가장 낮은** dz를 쓴다 — 패드 물림이 가장 깊은 자세다.
        best = usable[0]
        arm = best["joint_rad"]
        offset, tcp_world = _object_offset_in_tcp(
            joints, mimics, spec, arm, center, tcp_link)
        report["poses"][pose_name] = {
            "status": "verified",
            "kind": "grasp",
            "object_resource_id": rid,
            "support_resource_id": support_rid,
            "target_frame": item["frame"],
            "target_offset_m": [0.0, 0.0, best["dz_m"]],
            "target_world_xyz_m": [round(float(center[0]), 6),
                                   round(float(center[1]), 6),
                                   best["tcp_world_z_m"]],
            "target_rpy_rad": tool_down,
            "joint_rad": arm,
            "gripper_open_joint_rad": GRIPPER_OPEN_RAD,
            "gripper_grasp_joint_rad": grasp_joint,
            "position_error_m": best["position_error_m"],
            "orientation_error_rad": best["orientation_error_rad"],
            "fk_tcp_world_m": [round(float(v), 6) for v in tcp_world],
            "expected_contacts": best["closed"]["expected_contacts"],
            "usable_dz_range_m": [usable[0]["dz_m"], usable[-1]["dz_m"]],
            "usable_step_count": len(usable),
            "attached_object": {
                "object_id": f"{model_id}__held",
                "world_instance_id": model_id,
                "link": tcp_link,
                "size_m": item["size_m"],
                "offset_m": [round(float(v), 6) for v in offset],
                "touch_links": list(pad_links),
            },
            "measurement_method": "moveit_contact_sweep_plus_numerical_ik",
            "source": "scripts/derive_grasp_poses.py",
            "verified": True,
            "captured_at": time.strftime("%Y-%m-%d"),
            "note": "파지 높이는 고른 값이 아니라 측정한 구간의 최저점이다."
                    " 물체를 실제로 쥐었는지는 판정하지 않는다",
        }

    # ── 3. 적재 상태 검사 (물체를 붙여 다시 묻는다) ───────────────────
    for pose_name, pose in sorted(report["poses"].items()):
        if pose.get("status") != "verified":
            continue
        spec_attached = pose["attached_object"]
        attached = AttachedObject(
            object_id=spec_attached["object_id"],
            link=spec_attached["link"],
            size_m=tuple(spec_attached["size_m"]),
            offset_m=tuple(spec_attached["offset_m"]),
            touch_links=tuple(spec_attached["touch_links"]),
            source="config/workcell/fr3_2f85_workcell.json models[*].size_m")
        # **계획이 실제로 쓰는 자세만** 적재 상태로 묻는다. 다른 팔레트의
        # pick 자세를 이 물체를 든 상태로 재는 것은 계획에 없는 조합이다.
        support_model = _model_of(data, pose["support_resource_id"])
        targets = [(pose_name, pose["joint_rad"], True)]
        for name in (f"{support_model}_approach", "conveyor_approach",
                     "conveyor_place"):
            if name in verified_poses:
                targets.append((name, verified_poses[name], False))
        held = {}
        for name, arm, at_support in targets:
            state = client.check_state(
                _joints_with(arm, pose["gripper_grasp_joint_rad"], mimics, spec),
                attached=[attached])
            # 받침면 접촉은 **파지 자세에서만** 선언된 예외다(아직 놓여 있다).
            supports = tuple(
                n for n in {a for pair in state.contacts for a in pair}
                if support_model and (n == support_model
                                      or n.startswith(f"{support_model}__"))
            ) if at_support else ()
            held[name] = classify(
                state, object_id=spec_attached["object_id"],
                pad_links=tuple(spec_attached["touch_links"]),
                world_instance_id=spec_attached["world_instance_id"],
                support_ids=supports)
        pose["held_state_checks"] = held

    after = client.snapshot()
    report["scene"]["stable_during_measurement"] = (
        after.content_hash == before.content_hash)
    report["scene"]["content_hash_after"] = after.content_hash
    if not report["scene"]["stable_during_measurement"]:
        report["limitations"].append(
            "측정 중 planning scene이 바뀌었다 — 이 결과를 환경에 붙일 수 없다")

    report["limitations"].extend([
        "planning scene은 자재를 **원래 자리에** 담고 있다. 들어올림을 모르므로"
        " 붙인 사본과 원래 물체가 겹치는 쌍만 선언된 예외로 뒀다"
        " (attached_object.world_instance_id)",
        "place 이후 자재의 새 위치는 scene에 없다 — 해제 이후 단계는 빈손"
        " 기준으로만 검사했다",
        "물체를 실제로 쥐었는지는 측정하지 않았다(grasp.object_held 미확보)."
        " 개구가 폭과 맞는 것을 파지 근거로 쓰지 않는다",
        "장착 yaw와 커플링 실측 질량이 미확보다 — 이 자세들은 계획 검증용이며"
        " 실행 경로를 열지 않는다",
    ])

    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    client_close(node)

    verified = [n for n, p in report["poses"].items() if p.get("status") == "verified"]
    blocked = [n for n, p in report["poses"].items() if p.get("status") != "verified"]
    print(f"파지 자세 {len(verified)}개 측정, {len(blocked)}개 차단 → {args.out}")
    for name in verified:
        pose = report["poses"][name]
        lo, hi = pose["usable_dz_range_m"]
        print(f"  {name}: dz={pose['target_offset_m'][2]:+.3f} m"
              f" (사용 가능 구간 {lo:+.3f}..{hi:+.3f} m,"
              f" {pose['usable_step_count']}단계)"
              f" 그리퍼 파지 {pose['gripper_grasp_joint_rad']:.6f} rad")
        bad = {k: v["collisions"] for k, v in pose["held_state_checks"].items()
               if v["collisions"]}
        print(f"    적재 상태 충돌: {bad if bad else '없음'}")
    for name in blocked:
        print(f"  {name}: {report['poses'][name]['detail']}")
    return 0 if verified and not blocked else 1


def _model_of(data: dict, resource_id: str | None) -> str:
    if resource_id is None:
        return ""
    for row in data["resource_map"]:
        if row["resource_id"] == resource_id:
            return row["gazebo_model"]
    return ""


def _joints_with(arm, gripper: float, mimics: dict, spec: dict) -> dict:
    """팔 관절 + 그리퍼 명령 관절. mimic은 URDF 선언대로 채운다."""
    out = angles_for(dict(arm), float(gripper), mimics)
    return out


def _object_offset_in_tcp(joints, mimics, spec, arm, center, tcp_link):
    """물체 중심을 TCP 링크 좌표계로 옮긴다(붙일 때 쓸 오프셋).

    `link_transforms`는 URDF의 `world` 링크를 뿌리로 잡으므로 결과가 이미
    world 기준이다(FK로 확인: base_link = [0, 0, 0.73]). 높이를 더 보정하지
    않는다 — 두 번 더하면 조용히 틀린다.
    """
    transforms = link_transforms(joints, _joints_with(arm, 0.0, mimics, spec))
    rotation, tcp_world = transforms[tcp_link]
    return rotation.T @ (np.asarray(center) - tcp_world), tcp_world


def client_close(node) -> None:
    import rclpy

    node.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
