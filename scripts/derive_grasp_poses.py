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

## 컨베이어 위 자재 파지 (`--conveyor`)

시뮬레이션 시연 복귀(컨베이어 → 원래 슬롯)는 **컨베이어에 놓인** 자재를 다시
집어야 한다. 팔레트 파지 자세나 `conveyor_place`(놓기 높이)로 대신하지 않고
같은 방식으로 측정한다.

- 자재 위치: 선언된 `conveyor_frame`(벨트 상면 중심) + 자재 높이 절반.
- planning scene은 자재를 원래 슬롯에만 담고 있다. **공유 scene을 바꾸지
  않도록** 컨베이어 위 자재는 질의 로봇 상태에만 붙인 탐침 물체
  (`base_link` 고정, 선언 치수)로 넣는다. 탐침에는 닿아도 되는 링크가 없다.
- 후보마다: IK 수렴 · 연 상태 접촉 없음 · 닫은 상태 패드↔자재 접촉만 ·
  몸체↔자재/컨베이어 충돌 없음. 자재↔벨트 접촉만 받침 예외다(기존 규칙).
- 최저 유효 높이를 고른 뒤 접근(열림, 탐침 있음)·파지 적재·lift(자재를 든 채
  `conveyor_approach`)를 다시 묻는다. 모두 깨끗해야 등록한다.
- 결과는 grasp 파일의 **별도 키** `conveyor_grasp_poses`에만 쓴다. 기존
  `poses`(팔레트 파지)는 건드리지 않는다 — 로더가 자재별 파지 자세를 그
  키에서 잇기 때문이다. 유효 구간이 없는 자재는 등록하지 않는다. 전체 훑기
  기록은 `reports/workcell/conveyor_grasp_sweep.json`에 남긴다.

## 원래 슬롯 놓기 (`--pallet-place --materials mat_a mat_c`)

복귀 경로는 컨베이어에서 집은 자재를 원래 팔레트 슬롯에 놓는다. 팔레트
**파지** 자세에서 놓으면 든 자재 바닥이 트레이 윗면에 닿는다(실측:
`pallet_1__tray↔material_a__held`). 들고 있는 동안의 받침 접촉은 허용 예외가
아니므로 규칙을 넓히지 않고 놓기 높이를 측정한다.

- 든 자재 선언: 복귀가 실제로 쓰는 컨베이어 파지의 `attached_object`.
- 슬롯: 자재 프레임(선언된 팔레트 부모) 중심. 5 mm 간격으로 TCP 높이를 훑는다.
- 후보마다: IK 수렴 · 든 채 놓기 자세 접촉 없음(트레이·팔레트 포함, 받침 예외
  없음) · 해제(열림, 빈손) 접촉 없음 · 해제 시 자재 중심이 슬롯 중심에서
  허용치(0.02 m) 안이고 자재 바닥이 트레이 위에 있음.
- 가장 낮은 유효 높이를 고른 뒤 든 채 접근(`pallet_N_approach`)과 빈손
  retreat을 다시 묻는다. 모두 깨끗해야 등록한다.
- 결과는 **별도 키** `pallet_place_poses`에만 쓴다. `poses`(팔레트 파지)와
  `conveyor_grasp_poses`는 건드리지 않는다. 전체 기록은
  `reports/workcell/pallet_place_sweep.json`.
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


CONVEYOR_REPORT = ROOT / "reports/workcell/conveyor_grasp_sweep.json"
CONVEYOR_RESOURCE = "loc_conveyor"


def conveyor_object_center(data: dict, model: str) -> np.ndarray:
    """컨베이어에 놓인 자재 중심. 선언된 벨트 상면 중심 + 자재 높이 절반."""
    conveyor = next(row for row in data["resource_map"]
                    if row["resource_id"] == CONVEYOR_RESOURCE)
    top = resolve_frame(data["frames"], conveyor["frame"])
    height = float(data["models"][model]["size_m"][2])
    return top + np.array([0.0, 0.0, height / 2])


def support_contacts(validity, support_model: str) -> tuple[str, ...]:
    names = {name for pair in validity.contacts for name in pair}
    return tuple(sorted(n for n in names
                        if n == support_model or n.startswith(f"{support_model}__")))


def derive_conveyor(out_path: Path, report_path: Path) -> int:
    """컨베이어 위 자재의 파지 자세를 측정한다. 값을 만들지 않는다."""
    data = json.loads(WORKCELL.read_text(encoding="utf-8"))
    derived = json.loads(POSES.read_text(encoding="utf-8"))
    if not out_path.is_file():
        raise SystemExit(f"팔레트 파지 측정 결과가 없다: {out_path}")
    grasp = json.loads(out_path.read_text(encoding="utf-8"))
    spec = data["grasp"]
    pad_links = tuple(spec["pad_links"])
    tcp_link = spec["tcp_link"]
    step = float(spec["sweep_step_m"])
    tool_down = data["tcp_targets"]["tool_down_rpy_rad"]
    grasp_joint = grasp.get("gripper", {}).get("grasp_joint_rad")
    if grasp.get("gripper", {}).get("status") != "measured" or grasp_joint is None:
        raise SystemExit("측정된 파지 개구가 없다 — 값을 만들지 않고 멈춘다")
    conveyor_model = _model_of(data, CONVEYOR_RESOURCE)
    verified = {name: pose["joint_rad"] for name, pose in derived["poses"].items()
                if pose.get("status") == "verified"}
    approach_name = f"{conveyor_model}_approach"
    if approach_name not in verified:
        raise SystemExit(f"검증된 {approach_name} 자세가 없다")

    joints, _links, mimics, limits = load_urdf()
    gripper_limits = {"robotiq_85_left_knuckle_joint": {"lower": 0.0, "upper": 0.8}}
    client, node = build_client({**limits, **gripper_limits})
    before = client.snapshot()
    base_rotation, base_world = link_transforms(
        joints, _joints_with(verified[approach_name], 0.0, mimics, spec))["base_link"]

    report = {
        "schema": "forstick2.workcell_conveyor_grasp_sweep/1",
        "derived_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "is_simulated": True, "real_hardware_verified": False,
        "method": "moveit_contact_sweep_plus_numerical_ik",
        "support_resource_id": CONVEYOR_RESOURCE, "sweep_step_m": step,
        "scene": {"content_hash": before.content_hash,
                  "world_object_count": before.summary.get("world_object_count")},
        "probe": "컨베이어 위 자재는 질의 로봇 상태에만 붙인 탐침(base_link 고정,"
                 " 선언 치수)이다. 공유 planning scene은 바꾸지 않았다",
        "materials": {},
    }
    poses: dict[str, dict] = {}
    for row in sorted(data["resource_map"], key=lambda r: r["resource_id"]):
        model = row["gazebo_model"]
        item = data["models"].get(model) or {}
        if item.get("kind") != "material":
            continue
        rid = row["resource_id"]
        size = [float(v) for v in item["size_m"]]
        center = conveyor_object_center(data, model)
        probe = AttachedObject(
            object_id=f"{model}__on_conveyor_probe", link="base_link",
            size_m=tuple(size),
            offset_m=tuple(float(v) for v in base_rotation.T @ (center - base_world)),
            touch_links=(), source="config/workcell/fr3_2f85_workcell.json")

        def ask(arm, gripper, *, pads, attached=(probe,), object_id=probe.object_id,
                world_instance_id=None, allow_support=True):
            state = client.check_state(_joints_with(arm, gripper, mimics, spec),
                                       attached=list(attached))
            supports = (support_contacts(state, conveyor_model)
                        if allow_support else ())
            return classify(state, object_id=object_id, pad_links=pads,
                            world_instance_id=world_instance_id,
                            support_ids=supports)

        rows = []
        height = size[2]
        count = int(round((height / 2 + 0.06) / step)) + 1
        for index in range(count):
            dz = round(-height / 2 + index * step, 6)
            target = center + np.array([0.0, 0.0, dz])
            arm, distance, angle = solve_ik(
                joints, mimics, limits, target, tool_down, dict(verified[approach_name]))
            reachable = (distance <= IK_POSITION_TOLERANCE_M
                         and angle <= IK_ORIENTATION_TOLERANCE_RAD)
            out = {"dz_m": dz, "tcp_world_z_m": round(float(target[2]), 6),
                   "position_error_m": round(distance, 6),
                   "orientation_error_rad": round(angle, 6),
                   "reachable": reachable, "usable": False}
            if reachable:
                out["open"] = ask(arm, GRIPPER_OPEN_RAD, pads=())
                out["closed"] = ask(arm, grasp_joint, pads=pad_links)
                out["usable"] = bool(out["open"]["clean"] and out["closed"]["clean"]
                                     and out["closed"]["expected_contacts"])
                out["joint_rad"] = {k: round(v, 6) for k, v in arm.items()}
            rows.append(out)
        usable = [r for r in rows if r["usable"]]
        entry = {
            "object": model, "object_world_center_m":
                [round(float(v), 6) for v in center],
            "steps": [{k: v for k, v in r.items() if k != "joint_rad"} for r in rows],
            "reachable_dz_m": [r["dz_m"] for r in rows if r["reachable"]],
            "usable_dz_m": [r["dz_m"] for r in usable],
        }
        report["materials"][rid] = entry
        if not usable:
            entry["status"] = "blocked"
            entry["reason_code"] = ("geometry.workspace_violation"
                                    if not entry["reachable_dz_m"]
                                    else "geometry.collision")
            continue

        best = usable[0]
        arm = best["joint_rad"]
        offset, tcp_world = _object_offset_in_tcp(
            joints, mimics, spec, arm, center, tcp_link)
        held = AttachedObject(
            object_id=f"{model}__held", link=tcp_link, size_m=tuple(size),
            offset_m=tuple(float(v) for v in offset), touch_links=pad_links,
            source="config/workcell/fr3_2f85_workcell.json models[*].size_m")
        path_checks = {
            # 접근: 그리퍼를 연 채 자재가 아직 컨베이어에 있다.
            "approach_open": ask(verified[approach_name], GRIPPER_OPEN_RAD, pads=()),
            # 파지 자세에서 든 상태: 자재↔벨트는 받침 예외(아직 놓여 있다).
            "grasp_held": ask(arm, grasp_joint, pads=pad_links, attached=(held,),
                              object_id=held.object_id, world_instance_id=model),
            # lift: 자재를 든 채 접근 자세. 받침 예외 없음.
            "lift_held": ask(verified[approach_name], grasp_joint, pads=pad_links,
                             attached=(held,), object_id=held.object_id,
                             world_instance_id=model, allow_support=False),
        }
        entry["path_checks"] = path_checks
        if not all(check["clean"] for check in path_checks.values()):
            entry["status"] = "blocked"
            entry["reason_code"] = "geometry.collision"
            entry["detail"] = "파지 구간은 있으나 접근·적재·lift 검사가 통과하지 않았다"
            continue
        entry["status"] = "verified"
        entry["selected_dz_m"] = best["dz_m"]
        poses[f"{model}_conveyor_grasp"] = {
            "status": "verified", "kind": "grasp",
            "object_resource_id": rid, "support_resource_id": CONVEYOR_RESOURCE,
            "target_frame": _frame_of(data, CONVEYOR_RESOURCE),
            "object_world_center_m": entry["object_world_center_m"],
            "object_center_basis": "conveyor_frame(벨트 상면 중심) + 자재 높이 절반"
                                   " (선언값)",
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
            "sweep_step_m": step,
            "approach_pose": approach_name,
            "path_checks": {k: {"clean": v["clean"], "collisions": v["collisions"],
                                "expected_contacts": v["expected_contacts"]}
                            for k, v in path_checks.items()},
            "attached_object": {
                "object_id": held.object_id, "world_instance_id": model,
                "link": tcp_link, "size_m": size,
                "offset_m": [round(float(v), 6) for v in offset],
                "touch_links": list(pad_links),
            },
            "measurement_method": "moveit_contact_sweep_plus_numerical_ik",
            "source": "scripts/derive_grasp_poses.py --conveyor",
            "sweep_report": str(report_path.relative_to(ROOT)),
            "scene_content_hash": before.content_hash,
            "verified": True, "captured_at": time.strftime("%Y-%m-%d"),
            "note": "컨베이어 위 자재는 탐침으로 넣었다(공유 scene 미변경)."
                    " 파지 높이는 측정한 구간의 최저점이다. 물체를 실제로 쥐었는지는"
                    " 판정하지 않는다",
        }

    after = client.snapshot()
    stable = after.content_hash == before.content_hash
    report["scene"]["stable_during_measurement"] = stable
    report["scene"]["content_hash_after"] = after.content_hash
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    client_close(node)

    for rid, entry in report["materials"].items():
        usable = entry["usable_dz_m"]
        span = f"{usable[0]:+.3f}..{usable[-1]:+.3f} m" if usable else "없음"
        print(f"  {rid}: {entry['status']} · 유효 dz {span}"
              f" · 선택 {entry.get('selected_dz_m')}")
    if not stable:
        print("측정 중 planning scene이 바뀌었다 — 설정에 쓰지 않는다")
        return 1
    if not poses:
        print("유효한 컨베이어 파지 자세가 없다 — 설정을 만들지 않는다")
        return 1
    grasp["conveyor_grasp_poses"] = poses
    grasp["conveyor_grasp_derived_at"] = report["derived_at"]
    out_path.write_text(json.dumps(grasp, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"컨베이어 파지 자세 {len(poses)}개 등록 → {out_path}")
    return 0 if len(poses) == len(report["materials"]) else 1


PALLET_PLACE_REPORT = ROOT / "reports/workcell/pallet_place_sweep.json"
#: 해제 시 자재 중심이 슬롯 중심에서 벗어나도 되는 거리(m). 시연 복귀 판정
#: 허용치(`validation.simulation_demo_state.ORIGIN_TOLERANCE_M`)와 같다.
SLOT_TOLERANCE_M = 0.02


def derive_pallet_place(out_path: Path, report_path: Path,
                        materials: list[str]) -> int:
    """원래 슬롯에 **든 자재**를 놓는 높이를 측정한다. 값을 만들지 않는다."""
    from validation.simulation_demo_state import ORIGIN_TOLERANCE_M

    assert SLOT_TOLERANCE_M == ORIGIN_TOLERANCE_M
    data = json.loads(WORKCELL.read_text(encoding="utf-8"))
    derived = json.loads(POSES.read_text(encoding="utf-8"))
    if not out_path.is_file():
        raise SystemExit(f"파지 측정 결과가 없다: {out_path}")
    grasp = json.loads(out_path.read_text(encoding="utf-8"))
    spec = data["grasp"]
    frames = data["frames"]
    pad_links = tuple(spec["pad_links"])
    tcp_link = spec["tcp_link"]
    step = float(spec["sweep_step_m"])
    tool_down = data["tcp_targets"]["tool_down_rpy_rad"]
    grasp_joint = grasp.get("gripper", {}).get("grasp_joint_rad")
    if grasp.get("gripper", {}).get("status") != "measured" or grasp_joint is None:
        raise SystemExit("측정된 파지 개구가 없다 — 값을 만들지 않고 멈춘다")
    verified = {name: pose["joint_rad"] for name, pose in derived["poses"].items()
                if pose.get("status") == "verified"}

    joints, _links, mimics, limits = load_urdf()
    gripper_limits = {"robotiq_85_left_knuckle_joint": {"lower": 0.0, "upper": 0.8}}
    client, node = build_client({**limits, **gripper_limits})
    before = client.snapshot()
    report = {
        "schema": "forstick2.workcell_pallet_place_sweep/1",
        "derived_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "is_simulated": True, "real_hardware_verified": False,
        "method": "moveit_contact_sweep_plus_numerical_ik",
        "sweep_step_m": step, "slot_tolerance_m": SLOT_TOLERANCE_M,
        "scene": {"content_hash": before.content_hash,
                  "world_object_count": before.summary.get("world_object_count")},
        "materials": {},
    }
    poses: dict[str, dict] = {}
    for rid in materials:
        row = next((r for r in data["resource_map"] if r["resource_id"] == rid), None)
        model = None if row is None else row["gazebo_model"]
        item = (data["models"].get(model) or {}) if model else {}
        if item.get("kind") != "material":
            report["materials"][rid] = {"status": "blocked",
                                        "reason_code": "plan.unknown_resource"}
            continue
        support_frame = support_of(frames, row["frame"])
        support_rid = next((r["resource_id"] for r in data["resource_map"]
                            if r["frame"] == support_frame), None)
        support_model = _model_of(data, support_rid)
        approach_name = f"{support_model}_approach"
        conveyor_pose = (grasp.get("conveyor_grasp_poses") or {}).get(
            f"{model}_conveyor_grasp") or {}
        held_spec = conveyor_pose.get("attached_object")
        if approach_name not in verified or not held_spec:
            report["materials"][rid] = {
                "status": "blocked", "reason_code": "geometry.grasp_pose_unavailable",
                "detail": "검증된 접근 자세 또는 복귀용 컨베이어 파지(든 자재 선언)가 없다"}
            continue
        held = AttachedObject(
            object_id=held_spec["object_id"], link=held_spec["link"],
            size_m=tuple(held_spec["size_m"]),
            offset_m=tuple(held_spec["offset_m"]),
            touch_links=tuple(held_spec["touch_links"]),
            source="conveyor_grasp_poses.attached_object")
        size = [float(v) for v in item["size_m"]]
        slot_center = resolve_frame(frames, row["frame"])
        tray_top = float(slot_center[2] - size[2] / 2)

        def ask(arm, gripper, *, holding: bool):
            state = client.check_state(
                _joints_with(arm, gripper, mimics, spec),
                attached=[held] if holding else [])
            # 받침 예외 없음(support_ids 비움). 든 사본과 원래 world 인스턴스의
            # 겹침만 기존 선언 예외다.
            return classify(state, object_id=held.object_id if holding else model,
                            pad_links=pad_links if holding else (),
                            world_instance_id=model if holding else None)

        def released_center(arm):
            rotation, tcp_world = link_transforms(
                joints, _joints_with(arm, 0.0, mimics, spec))[tcp_link]
            return tcp_world + rotation @ np.asarray(held.offset_m)

        rows = []
        count = int(round((size[2] / 2 + 0.06) / step)) + 1
        for index in range(count):
            dz = round(-size[2] / 2 + index * step, 6)
            target = slot_center + np.array([0.0, 0.0, dz])
            arm, distance, angle = solve_ik(
                joints, mimics, limits, target, tool_down, dict(verified[approach_name]))
            reachable = (distance <= IK_POSITION_TOLERANCE_M
                         and angle <= IK_ORIENTATION_TOLERANCE_RAD)
            out = {"dz_m": dz, "tcp_world_z_m": round(float(target[2]), 6),
                   "position_error_m": round(distance, 6),
                   "orientation_error_rad": round(angle, 6),
                   "reachable": reachable, "usable": False}
            if reachable:
                center = released_center(arm)
                xy_gap = float(np.linalg.norm(center[:2] - slot_center[:2]))
                drop = float(center[2] - size[2] / 2 - tray_top)
                out["place_held"] = ask(arm, grasp_joint, holding=True)
                out["release_open"] = ask(arm, GRIPPER_OPEN_RAD, holding=False)
                out["release"] = {
                    "object_center_m": [round(float(v), 6) for v in center],
                    "xy_gap_m": round(xy_gap, 6),
                    "drop_to_tray_m": round(drop, 6),
                    "settles_in_slot": bool(xy_gap <= SLOT_TOLERANCE_M and drop > 0.0),
                }
                out["usable"] = bool(out["place_held"]["clean"]
                                     and out["release_open"]["clean"]
                                     and out["release"]["settles_in_slot"])
                out["joint_rad"] = {k: round(v, 6) for k, v in arm.items()}
            rows.append(out)
        usable = [r for r in rows if r["usable"]]
        entry = {
            "object": model, "support_resource_id": support_rid,
            "slot_center_m": [round(float(v), 6) for v in slot_center],
            "tray_top_z_m": round(tray_top, 6),
            "steps": [{k: v for k, v in r.items() if k != "joint_rad"} for r in rows],
            "reachable_dz_m": [r["dz_m"] for r in rows if r["reachable"]],
            "usable_dz_m": [r["dz_m"] for r in usable],
        }
        report["materials"][rid] = entry
        if not usable:
            entry["status"] = "blocked"
            entry["reason_code"] = ("geometry.workspace_violation"
                                    if not entry["reachable_dz_m"]
                                    else "geometry.collision")
            continue
        best = usable[0]
        path_checks = {
            # 든 채 원래 팔레트 접근.
            "approach_held": ask(verified[approach_name], grasp_joint, holding=True),
            # 해제 뒤 빈손으로 접근 자세까지 retreat.
            "retreat_open": ask(verified[approach_name], GRIPPER_OPEN_RAD,
                                holding=False),
        }
        entry["path_checks"] = path_checks
        if not all(check["clean"] for check in path_checks.values()):
            entry["status"] = "blocked"
            entry["reason_code"] = "geometry.collision"
            entry["detail"] = "놓기 높이는 있으나 접근·retreat 검사가 통과하지 않았다"
            continue
        entry["status"] = "verified"
        entry["selected_dz_m"] = best["dz_m"]
        poses[f"{model}_pallet_place"] = {
            "status": "verified", "kind": "place",
            "object_resource_id": rid, "support_resource_id": support_rid,
            "target_frame": row["frame"],
            "slot_center_m": entry["slot_center_m"],
            "target_offset_m": [0.0, 0.0, best["dz_m"]],
            "target_world_xyz_m": [round(float(slot_center[0]), 6),
                                   round(float(slot_center[1]), 6),
                                   best["tcp_world_z_m"]],
            "target_rpy_rad": tool_down,
            "joint_rad": best["joint_rad"],
            "gripper_grasp_joint_rad": grasp_joint,
            "gripper_open_joint_rad": GRIPPER_OPEN_RAD,
            "position_error_m": best["position_error_m"],
            "orientation_error_rad": best["orientation_error_rad"],
            "release": best["release"],
            "usable_dz_range_m": [usable[0]["dz_m"], usable[-1]["dz_m"]],
            "usable_step_count": len(usable),
            "sweep_step_m": step,
            "approach_pose": approach_name,
            "held_object_source": f"conveyor_grasp_poses.{model}_conveyor_grasp",
            "path_checks": {k: {"clean": v["clean"], "collisions": v["collisions"],
                                "expected_contacts": v["expected_contacts"]}
                            for k, v in path_checks.items()},
            "measurement_method": "moveit_contact_sweep_plus_numerical_ik",
            "source": "scripts/derive_grasp_poses.py --pallet-place",
            "sweep_report": str(report_path.relative_to(ROOT)),
            "scene_content_hash": before.content_hash,
            "verified": True, "captured_at": time.strftime("%Y-%m-%d"),
            "note": "든 자재가 트레이에 닿지 않는 가장 낮은 측정 높이다. 받침 접촉"
                    " 예외를 쓰지 않았다. 해제 뒤 안착은 Gazebo 관측으로 판정한다",
        }

    after = client.snapshot()
    stable = after.content_hash == before.content_hash
    report["scene"]["stable_during_measurement"] = stable
    report["scene"]["content_hash_after"] = after.content_hash
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    client_close(node)
    for rid, entry in report["materials"].items():
        usable = entry.get("usable_dz_m") or []
        span = f"{usable[0]:+.3f}..{usable[-1]:+.3f} m" if usable else "없음"
        print(f"  {rid}: {entry['status']} · 유효 dz {span}"
              f" · 선택 {entry.get('selected_dz_m')}")
    if not stable:
        print("측정 중 planning scene이 바뀌었다 — 설정에 쓰지 않는다")
        return 1
    if not poses:
        print("유효한 원래 슬롯 놓기 자세가 없다 — 설정을 만들지 않는다")
        return 1
    merged = dict(grasp.get("pallet_place_poses") or {})
    merged.update(poses)
    grasp["pallet_place_poses"] = merged
    grasp["pallet_place_derived_at"] = report["derived_at"]
    out_path.write_text(json.dumps(grasp, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"원래 슬롯 놓기 자세 {len(poses)}개 등록 → {out_path}")
    return 0 if len(poses) == len(materials) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--conveyor", action="store_true",
                        help="컨베이어 위 자재 파지 자세만 측정해 conveyor_grasp_poses에"
                             " 등록한다(기존 팔레트 파지 결과는 그대로 둔다)")
    parser.add_argument("--pallet-place", action="store_true",
                        help="든 자재를 원래 팔레트 슬롯에 놓는 높이를 측정해"
                             " pallet_place_poses에 등록한다")
    parser.add_argument("--materials", nargs="+", default=None,
                        help="--pallet-place 대상 자재 자원 id(명시 필수)")
    args = parser.parse_args()
    if args.pallet_place:
        if not args.materials:
            parser.error("--pallet-place에는 --materials를 명시한다")
        return derive_pallet_place(Path(args.out), PALLET_PLACE_REPORT,
                                   list(args.materials))
    if args.conveyor:
        return derive_conveyor(Path(args.out), CONVEYOR_REPORT)

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


def _frame_of(data: dict, resource_id: str) -> str:
    for row in data["resource_map"]:
        if row["resource_id"] == resource_id:
            return row["frame"]
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
