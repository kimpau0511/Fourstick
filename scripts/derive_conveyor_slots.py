#!/usr/bin/env python3
"""컨베이어 **다중 슬롯** 후보를 유도하고 MoveIt으로 검증한다.

왜 필요한가. 컨베이어는 지금까지 배치 위치가 **하나**(벨트 상면 중심)였다.
A/B/C를 동시에 올려 두려면 슬롯이 셋 필요한데, 좌표를 손으로 정하면 그건
측정이 아니라 추측이다. 그래서 여기서 **후보를 만들고 물어본다.**

    1) 선언된 기하에서만 후보를 만든다
       - 벨트 치수·가드레일 안쪽면·다리 위치 (`config/workcell/*.json`)
       - 자재 치수, `collision_margins_m.environment_clearance_min_m`
       - 측정된 그리퍼 개구 (`*_grasp.json`의 `gripper`)
    2) 후보마다 MoveIt에 묻는다 — IK 수렴 · 충돌 · attached-object
    3) **모든 검사를 통과한 후보만** 채택한다. 하나라도 걸리면 등록하지 않는다

`slot_1`은 기존 `conveyor_place`와 **같은 자리**다(벨트 상면 중심). 기존 A 단일
이송 동작과 호환을 유지하기 위해 위치를 바꾸지 않는다.

## 무엇을 검사하는가

슬롯 하나를 쓰려면 네 가지 자세가 모두 깨끗해야 한다.

| 검사 | 상태 | 받침 예외 |
|---|---|---|
| `place_held` | 놓기 높이, 자재를 든 채 | 자재↔벨트 허용(막 놓는 중) |
| `approach_held` | 접근 높이, 자재를 든 채 | 없음 |
| `retreat_open` | 접근 높이, 빈손·열림 | 없음 |
| `grasp_probe` | 파지 높이, 자재가 슬롯에 놓인 탐침 | 자재↔벨트 허용 |

그리고 **이웃이 찬 상태**로 한 번 더 묻는다(`neighbours_occupied`). 혼자일 때
깨끗한 슬롯이 옆자리가 차면 막힐 수 있다 — 다중 슬롯에서 실제로 문제가 되는
것은 이쪽이다. 이웃은 공유 planning scene을 바꾸지 않도록 **탐침**으로 넣는다.

## 무엇을 하지 않는가

로봇을 움직이지 않는다. IK와 MoveIt 질의만 한다. 유효한 후보가 없으면 값을
만들지 않고 `status=blocked`로 적는다.

실행:

    source /opt/ros/lyrical/setup.bash
    ROS_DOMAIN_ID=44 GZ_PARTITION=forstick2_fr3_workcell \\
      /usr/bin/python3 scripts/derive_conveyor_slots.py

결과:
 - 보고서 `reports/workcell/conveyor_slots.json` (후보별 전 검사 기록)
 - 채택분만 `config/workcell/fr3_2f85_workcell_grasp.json`의
   **별도 키** `conveyor_slots`에 쓴다. 기존 키는 건드리지 않는다.
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
from scripts.derive_grasp_poses import (  # noqa: E402
    CONVEYOR_RESOURCE,
    OUT,
    POSES,
    _joints_with,
    _model_of,
    _object_offset_in_tcp,
    build_client,
    classify,
    client_close,
    support_contacts,
)
from scripts.derive_workcell_poses import (  # noqa: E402
    GRIPPER_OPEN_RAD,
    IK_ORIENTATION_TOLERANCE_RAD,
    IK_POSITION_TOLERANCE_M,
    WORKCELL,
    load_urdf,
    solve_ik,
)

REPORT = ROOT / "reports/workcell/conveyor_slots.json"

#: 슬롯 수. 자재가 A/B/C 셋이므로 셋을 넘겨 만들지 않는다.
SLOT_COUNT = 3
#: 후보 pitch를 훑는 간격(m). 셀 설정의 `sweep_step_m`과 같은 눈금을 쓴다.
PITCH_STEP_M = 0.005


def conveyor_geometry(data: dict) -> dict:
    """벨트 배치 가능 구역. **선언된 치수에서만** 나온다."""
    model = _model_of(data, CONVEYOR_RESOURCE)
    conveyor = data["models"][model]
    belt = next(p for p in conveyor["parts"] if p["name"] == "belt")
    rails = [p for p in conveyor["parts"] if p["name"].startswith("frame_")]
    legs = [p for p in conveyor["parts"] if p["name"].startswith("leg_")]
    frames = data["frames"]
    top = np.asarray(frames[conveyor["frame"]]["xyz_m"], dtype=float)
    rail_inner = (min(abs(p["center_xyz_m"][1]) - p["size_m"][1] / 2 for p in rails)
                  if rails else belt["size_m"][1] / 2)
    return {
        "model": model,
        "belt_size_m": [float(v) for v in belt["size_m"]],
        "top_center_world_m": [float(v) for v in top],
        "rail_inner_half_y_m": float(rail_inner),
        "leg_center_x_m": sorted(float(p["center_xyz_m"][0]) for p in legs),
        "usable_half_x_m": float(belt["size_m"][0] / 2),
        "usable_half_y_m": float(min(belt["size_m"][1] / 2, rail_inner)),
    }


def candidate_pitches(geometry: dict, object_size_m, clearance_m: float,
                      open_aperture_m: float) -> dict:
    """후보 pitch 목록과 그 하한·상한 근거.

    하한 — 이웃 자재 반폭 + 그리퍼 개구 반폭 + 선언 여유. 그리퍼를 연 채 슬롯에
    들어갈 때 옆 자재를 건드리지 않으려면 최소 이만큼 떨어져야 한다.
    상한 — 자재 중심이 벨트 안에 있어야 한다(기존 `placement_zone` 규칙과 같다).
    실제 채택은 여기서 정하지 않는다. MoveIt이 정한다.
    """
    half_x = float(object_size_m[0]) / 2
    lower = half_x + open_aperture_m / 2 + clearance_m
    upper = geometry["usable_half_x_m"] - half_x
    steps = int(np.floor((upper - lower) / PITCH_STEP_M)) + 1
    pitches = [round(lower + i * PITCH_STEP_M, 6) for i in range(max(steps, 0))]
    return {
        "lower_bound_m": round(lower, 6),
        "upper_bound_m": round(upper, 6),
        "step_m": PITCH_STEP_M,
        "basis": {
            "object_half_x_m": half_x,
            "gripper_open_aperture_m": open_aperture_m,
            "environment_clearance_min_m": clearance_m,
            "lower": "이웃 자재 반폭 + 개구 반폭 + 선언 여유",
            "upper": "자재 중심이 벨트 안 (usable_half_x - 자재 반폭)",
        },
        "candidates_m": pitches,
    }


def slot_centers(geometry: dict, pitch_m: float, object_height_m: float):
    """슬롯 중심(world). `slot_1`은 언제나 벨트 상면 중심이다."""
    top = np.asarray(geometry["top_center_world_m"], dtype=float)
    z = top[2] + object_height_m / 2
    return {
        "slot_1": np.array([top[0], top[1], z]),
        "slot_2": np.array([top[0] - pitch_m, top[1], z]),
        "slot_3": np.array([top[0] + pitch_m, top[1], z]),
    }


def _probe(model: str, name: str, center, size, base_rotation, base_world):
    """공유 planning scene을 바꾸지 않는 탐침. 닿아도 되는 링크가 없다."""
    return AttachedObject(
        object_id=f"{model}__{name}_probe", link="base_link",
        size_m=tuple(float(v) for v in size),
        offset_m=tuple(float(v) for v in base_rotation.T @ (np.asarray(center) - base_world)),
        touch_links=(), source="config/workcell/fr3_2f85_workcell.json")


def derive(out_path: Path, report_path: Path, *, model_filter=None,
           forced_pitch: float | None = None) -> int:
    data = json.loads(WORKCELL.read_text(encoding="utf-8"))
    derived = json.loads(POSES.read_text(encoding="utf-8"))
    if not out_path.is_file():
        raise SystemExit(f"파지 측정 결과가 없다: {out_path}")
    grasp_file = json.loads(out_path.read_text(encoding="utf-8"))

    spec = data["grasp"]
    pad_links = tuple(spec["pad_links"])
    tcp_link = spec["tcp_link"]
    tool_down = data["tcp_targets"]["tool_down_rpy_rad"]
    clearance = float(data["collision_margins_m"]["environment_clearance_min_m"])
    gripper = grasp_file.get("gripper") or {}
    if gripper.get("status") != "measured":
        raise SystemExit("측정된 파지 개구가 없다 — 값을 만들지 않고 멈춘다")
    grasp_joint = float(gripper["grasp_joint_rad"])
    open_aperture = float(
        (gripper.get("grasp_aperture_evidence") or {}).get("aperture_target_m") or 0.05)
    # 연 상태의 실제 개구는 gripper_control 관측에서 온다(open_source 문구의 값).
    open_aperture = float(gripper.get("open_aperture_m") or 0.084996)

    geometry = conveyor_geometry(data)
    conveyor_model = geometry["model"]
    verified = {name: pose["joint_rad"] for name, pose in derived["poses"].items()
                if pose.get("status") == "verified"}
    approach_name = f"{conveyor_model}_approach"
    place_name = f"{conveyor_model}_place"
    for name in (approach_name, place_name):
        if name not in verified:
            raise SystemExit(f"검증된 {name} 자세가 없다")
    approach_offset = float(
        derived["poses"][approach_name]["target_offset_m"][2])
    place_offset = float(derived["poses"][place_name]["target_offset_m"][2])
    # 컨베이어 위 자재 파지 높이(자재 중심 기준 dz). **측정된 값만 쓴다.**
    slots_grasp = grasp_file.get("conveyor_grasp_poses") or {}
    verified_grasps = [row for row in slots_grasp.values()
                       if row.get("status") == "verified"]
    if not verified_grasps:
        raise SystemExit("검증된 컨베이어 파지 자세가 없다 —"
                         " 먼저 scripts/derive_grasp_poses.py --conveyor")
    grasp_dz_values = {round(float(row["target_offset_m"][2]), 6)
                       for row in verified_grasps}
    if len(grasp_dz_values) != 1:
        raise SystemExit(f"자재마다 파지 dz가 다르다: {sorted(grasp_dz_values)}"
                         " — 슬롯 공통 높이를 만들지 않는다")
    grasp_dz = grasp_dz_values.pop()

    materials = [(row["resource_id"], row["gazebo_model"])
                 for row in sorted(data["resource_map"], key=lambda r: r["resource_id"])
                 if (data["models"].get(row["gazebo_model"]) or {}).get("kind") == "material"]
    if model_filter:
        materials = [m for m in materials if m[1] in model_filter]
    if not materials:
        raise SystemExit("자재가 없다")

    joints, _links, mimics, limits = load_urdf()
    gripper_limits = {"robotiq_85_left_knuckle_joint": {"lower": 0.0, "upper": 0.8}}
    client, node = build_client({**limits, **gripper_limits})
    before = client.snapshot()
    base_rotation, base_world = link_transforms(
        joints, _joints_with(verified[approach_name], 0.0, mimics, spec))["base_link"]

    size = [float(v) for v in data["models"][materials[0][1]]["size_m"]]
    pitch_plan = candidate_pitches(geometry, size, clearance, open_aperture)

    report = {
        "schema": "forstick2.workcell_conveyor_slots/1",
        "derived_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "is_simulated": True,
        "method": "declared_geometry_candidates_plus_moveit_validation",
        "support_resource_id": CONVEYOR_RESOURCE,
        "slot_count": SLOT_COUNT,
        "geometry": geometry,
        "pitch_plan": pitch_plan,
        "grasp_dz_m": grasp_dz,
        "grasp_dz_source": "config/workcell/fr3_2f85_workcell_grasp.json"
                           " conveyor_grasp_poses[*].target_offset_m[2] (측정값)",
        "scene": {"content_hash": before.content_hash,
                  "world_object_count": before.summary.get("world_object_count")},
        "probe_note": "슬롯 위 자재와 이웃 자재는 질의 로봇 상태에만 붙인 탐침이다."
                      " 공유 planning scene은 바꾸지 않았다",
        "pitches": [],
        "adopted": None,
    }

    def ask(arm, gripper_rad, *, pads, attached, object_id, world_instance_id=None,
            allow_support=True):
        state = client.check_state(_joints_with(arm, gripper_rad, mimics, spec),
                                   attached=list(attached))
        supports = support_contacts(state, conveyor_model) if allow_support else ()
        return classify(state, object_id=object_id, pad_links=pads,
                        world_instance_id=world_instance_id, support_ids=supports)

    top = np.asarray(geometry["top_center_world_m"], dtype=float)
    obj_z = top[2] + size[2] / 2

    def center_at(offset):
        return np.array([top[0] + offset, top[1], obj_z])

    def recheck(centers: dict) -> dict:
        """고른 조합을 **이웃이 찬 상태로** 다시 묻는다(7종 전부)."""
        combo = {"offsets_m": [round(float(c[0]) - top[0], 6) for c in centers.values()],
                 "slots": {}, "ok": True}
        for name, center in centers.items():
            neighbours = [c for n, c in centers.items() if n != name]
            row = validate_slot(
                client=client, ask=ask, joints=joints, mimics=mimics, spec=spec,
                limits=limits, tool_down=tool_down, pad_links=pad_links,
                tcp_link=tcp_link, grasp_joint=grasp_joint, size=size,
                center=center, neighbours=neighbours,
                approach_seed=verified[approach_name], place_seed=verified[place_name],
                approach_offset=approach_offset, place_offset=place_offset,
                grasp_dz=grasp_dz, base_rotation=base_rotation,
                base_world=base_world, model=materials[0][1])
            combo["slots"][name] = row
            combo["ok"] = combo["ok"] and row["ok"]
        return combo

    # ── 지정 pitch 게이트 ───────────────────────────────────────────
    # `--pitch`를 주면 훑지 않고 **그 배치만** 같은 7종으로 확인한다.
    # 통과하지 못하면 **설정을 건드리지 않는다** — 이미 검증된 배치를 지우지
    # 않기 위해서다(호출자가 이전 채택값을 그대로 쓴다).
    if forced_pitch is not None:
        picked = [0.0, round(-forced_pitch, 6), round(-2 * forced_pitch, 6)]
        centers = {f"slot_{i + 1}": center_at(o) for i, o in enumerate(picked)}
        combo = recheck(centers)
        report["forced_pitch_m"] = forced_pitch
        report["occupied_recheck"] = combo
        report["pitch_plan"]["candidates_m"] = picked
        after = client.snapshot()
        report["scene"]["content_hash_after"] = after.content_hash
        report["scene"]["stable_during_measurement"] = (
            after.content_hash == before.content_hash)
        if not combo["ok"]:
            report["status"] = "blocked"
            report["reason_code"] = "geometry.collision"
            report["detail"] = (f"지정 pitch {forced_pitch} m가 7종 검증을 통과하지"
                                " 못했다 — 설정을 바꾸지 않는다")
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            client_close(node)
            print(f"[슬롯] pitch {forced_pitch} m 채택 안 함 — 검증 실패")
            for name, row in combo["slots"].items():
                print(f"        {name}: ok={row['ok']} reason={row.get('reason_code')}"
                      f" failed={row.get('failed_checks')}")
            print(f"[슬롯] 보고서 {report_path.relative_to(ROOT)}")
            return 1
        adopted = (forced_pitch, centers, combo)
        return _write(adopted, report, report_path, out_path, grasp_file,
                      geometry, pitch_plan, node)

    # ── 1단계: 위치별 단독 검증 ─────────────────────────────────────
    # 대칭 ±pitch로 고정하지 않는다. 벨트 위 후보 위치를 모두 훑어 **어디가
    # 되는지 먼저 본다** — 실측에서 +x 쪽은 접근 높이 IK가 수렴하지 않았다.
    half_x = size[0] / 2
    span = geometry["usable_half_x_m"] - half_x
    offsets = [round(-span + i * PITCH_STEP_M, 6)
               for i in range(int(np.floor(2 * span / PITCH_STEP_M)) + 1)]
    if 0.0 not in offsets:
        offsets.append(0.0)
        offsets.sort()
    solo: dict[float, dict] = {}
    for offset in offsets:
        row = validate_slot(
            client=client, ask=ask, joints=joints, mimics=mimics, spec=spec,
            limits=limits, tool_down=tool_down, pad_links=pad_links,
            tcp_link=tcp_link, grasp_joint=grasp_joint, size=size,
            center=center_at(offset), neighbours=[],
            approach_seed=verified[approach_name], place_seed=verified[place_name],
            approach_offset=approach_offset, place_offset=place_offset,
            grasp_dz=grasp_dz, base_rotation=base_rotation, base_world=base_world,
            model=materials[0][1])
        row["x_offset_m"] = offset
        solo[offset] = row
    report["solo_sweep"] = [
        {k: v for k, v in row.items() if k != "checks"} | {
            "failed_checks": row.get("failed_checks"),
        }
        for row in (solo[o] for o in offsets)
    ]
    usable = [o for o in offsets if solo[o]["ok"]]
    report["usable_offsets_m"] = usable
    report["usable_count"] = len(usable)

    min_pitch = pitch_plan["lower_bound_m"]
    adopted = None
    if 0.0 in usable:
        # slot_1은 기존 `conveyor_place`와 같은 자리(offset 0)다 — 호환 유지.
        # 나머지 둘은 최소 pitch를 지키면서 **가장 촘촘한** 조합을 고른다.
        others = sorted((o for o in usable if abs(o) >= min_pitch - 1e-9),
                        key=lambda o: abs(o))
        chosen = []
        for cand in others:
            if all(abs(cand - c) >= min_pitch - 1e-9 for c in chosen):
                chosen.append(cand)
            if len(chosen) == SLOT_COUNT - 1:
                break
        if len(chosen) == SLOT_COUNT - 1:
            # 번호는 **slot_1에서 가까운 순**이다. slot_1은 offset 0(기존
            # `conveyor_place`와 같은 자리)이고, 2번·3번이 차례로 멀어진다 —
            # 사용자에게 "컨베이어 N번 위치"로 보이므로 순서가 뒤집히면 안 된다.
            picked = [0.0, *sorted(chosen, key=abs)]
            centers = {f"slot_{i + 1}": center_at(o) for i, o in enumerate(picked)}
            # ── 2단계: 이웃이 찬 상태로 재검증 ───────────────────────
            combo = recheck(centers)
            report["occupied_recheck"] = combo
            if combo["ok"]:
                pitch = min(abs(picked[i + 1] - picked[i])
                            for i in range(len(picked) - 1))
                adopted = (pitch, centers, combo)

    after = client.snapshot()
    report["scene"]["content_hash_after"] = after.content_hash
    report["scene"]["stable_during_measurement"] = (
        after.content_hash == before.content_hash)

    if adopted is None:
        report["status"] = "blocked"
        report["reason_code"] = "geometry.collision"
        report["detail"] = ("모든 pitch 후보가 검사에 걸렸다 — 값을 만들지 않는다."
                            " 슬롯을 늘리지 않고 기존 단일 배치를 유지한다")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        client_close(node)
        print(f"[슬롯] 채택 없음 — {report['detail']}")
        print(f"[슬롯] 보고서 {report_path.relative_to(ROOT)}")
        return 1

    return _write(adopted, report, report_path, out_path, grasp_file,
                  geometry, pitch_plan, node)


def _write(adopted, report, report_path: Path, out_path: Path, grasp_file: dict,
           geometry: dict, pitch_plan: dict, node) -> int:
    """채택된 배치를 보고서와 설정에 쓴다. **통과했을 때만 부른다.**"""
    pitch, centers, combo = adopted
    top_x = geometry["top_center_world_m"][0]
    report["status"] = "verified"
    report["adopted"] = {
        "pitch_m": pitch,
        "slots": {name: {"object_world_center_m": [round(float(v), 6) for v in c],
                         "x_offset_m": round(float(c[0]) - top_x, 6)}
                  for name, c in centers.items()},
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    grasp_file["conveyor_slots"] = {
        "status": "verified",
        "derived_at": report["derived_at"],
        "is_simulated": True,
        "method": report["method"],
        "support_resource_id": CONVEYOR_RESOURCE,
        "pitch_m": pitch,
        "pitch_basis": pitch_plan["basis"],
        "report": str(report_path.relative_to(ROOT)),
        "slots": {
            name: {
                "object_world_center_m": [round(float(v), 6) for v in c],
                "x_offset_m": round(float(c[0]) - top_x, 6),
                "checks": combo["slots"][name]["checks"],
                "place_joint_rad": combo["slots"][name]["place_joint_rad"],
                "approach_joint_rad": combo["slots"][name]["approach_joint_rad"],
                "grasp_joint_rad_arm": combo["slots"][name]["grasp_joint_rad_arm"],
            }
            for name, c in centers.items()
        },
    }
    out_path.write_text(json.dumps(grasp_file, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    client_close(node)
    print(f"[슬롯] 채택 pitch {pitch:.3f} m")
    for name, c in centers.items():
        print(f"        {name}: world ({c[0]:.4f}, {c[1]:.4f}, {c[2]:.4f})")
    print(f"[슬롯] 보고서 {report_path.relative_to(ROOT)}")
    print(f"[슬롯] 설정  {out_path.relative_to(ROOT)} · 키 conveyor_slots")
    return 0


def validate_slot(*, client, ask, joints, mimics, spec, limits, tool_down,
                  pad_links, tcp_link, grasp_joint, size, center, neighbours,
                  approach_seed, place_seed, approach_offset, place_offset,
                  grasp_dz, base_rotation, base_world, model) -> dict:
    """슬롯 하나에 대해 IK·충돌·attached-object를 모두 묻는다."""
    top_z = float(center[2]) - size[2] / 2
    place_target = np.array([center[0], center[1], top_z + place_offset])
    approach_target = np.array([center[0], center[1], top_z + approach_offset])

    row: dict = {"object_world_center_m": [round(float(v), 6) for v in center],
                 "checks": {}, "ok": False}

    place_arm, p_dist, p_ang = solve_ik(joints, mimics, limits, place_target,
                                        tool_down, dict(place_seed))
    appr_arm, a_dist, a_ang = solve_ik(joints, mimics, limits, approach_target,
                                       tool_down, dict(approach_seed))
    row["ik"] = {
        "place": {"position_error_m": round(p_dist, 6),
                  "orientation_error_rad": round(p_ang, 6),
                  "converged": bool(p_dist <= IK_POSITION_TOLERANCE_M
                                    and p_ang <= IK_ORIENTATION_TOLERANCE_RAD)},
        "approach": {"position_error_m": round(a_dist, 6),
                     "orientation_error_rad": round(a_ang, 6),
                     "converged": bool(a_dist <= IK_POSITION_TOLERANCE_M
                                       and a_ang <= IK_ORIENTATION_TOLERANCE_RAD)},
    }
    if not (row["ik"]["place"]["converged"] and row["ik"]["approach"]["converged"]):
        row["reason_code"] = "geometry.workspace_violation"
        return row

    # 든 자재 — place 자세 기준으로 TCP 좌표계 오프셋을 잡는다.
    offset, _tcp = _object_offset_in_tcp(joints, mimics, spec, place_arm, center, tcp_link)
    held = AttachedObject(
        object_id=f"{model}__held", link=tcp_link, size_m=tuple(size),
        offset_m=tuple(float(v) for v in offset), touch_links=pad_links,
        source="config/workcell/fr3_2f85_workcell.json models[*].size_m")
    # 슬롯에 놓인 자재(복귀 방향 파지용 탐침)와 이웃 슬롯 탐침.
    here = _probe(model, "on_slot", center, size, base_rotation, base_world)
    occupied = [_probe(model, f"neighbour_{i}", c, size, base_rotation, base_world)
                for i, c in enumerate(neighbours)]

    # 파지 높이는 **측정값**이다 — `conveyor_grasp_poses`의 검증된 dz를 그대로
    # 쓴다. place 높이에서 유도하면 어긋난다(실측: 0.025 m 차이로 패드가 빗나가
    # grasp_probe_closed가 slot_1에서도 실패했다).
    grasp_target = np.array([center[0], center[1], float(center[2]) + grasp_dz])
    grasp_arm, g_dist, g_ang = solve_ik(joints, mimics, limits, grasp_target,
                                        tool_down, dict(place_seed))
    grasp_ok = (g_dist <= IK_POSITION_TOLERANCE_M
                and g_ang <= IK_ORIENTATION_TOLERANCE_RAD)
    row["ik"]["grasp"] = {"position_error_m": round(g_dist, 6),
                          "orientation_error_rad": round(g_ang, 6),
                          "converged": bool(grasp_ok)}

    checks = {
        # 놓기: 자재를 든 채 놓기 높이. 자재↔벨트는 받침 예외(막 놓는 중).
        "place_held": ask(place_arm, grasp_joint, pads=pad_links, attached=(held,),
                          object_id=held.object_id, world_instance_id=model),
        # 접근: 자재를 든 채 접근 높이. 받침 예외 없음.
        "approach_held": ask(appr_arm, grasp_joint, pads=pad_links, attached=(held,),
                             object_id=held.object_id, world_instance_id=model,
                             allow_support=False),
        # 후퇴: 빈손·열림. 받침 예외 없음.
        "retreat_open": ask(appr_arm, GRIPPER_OPEN_RAD, pads=(), attached=(),
                            object_id="", allow_support=False),
    }
    if grasp_ok:
        # 복귀 방향 파지: 자재가 슬롯에 놓여 있다(탐침).
        checks["grasp_probe_open"] = ask(grasp_arm, GRIPPER_OPEN_RAD, pads=(),
                                         attached=(here,), object_id=here.object_id)
        checks["grasp_probe_closed"] = ask(grasp_arm, grasp_joint, pads=pad_links,
                                           attached=(here,), object_id=here.object_id)
    # 이웃이 찬 상태 — 다중 슬롯에서 실제로 문제가 되는 검사다.
    if occupied:
        checks["neighbours_occupied_place"] = ask(
            place_arm, grasp_joint, pads=pad_links,
            attached=(held, *occupied), object_id=held.object_id,
            world_instance_id=model)
        checks["neighbours_occupied_approach"] = ask(
            appr_arm, grasp_joint, pads=pad_links,
            attached=(held, *occupied), object_id=held.object_id,
            world_instance_id=model, allow_support=False)

    row["checks"] = checks
    closed = checks.get("grasp_probe_closed")
    row["ok"] = bool(
        grasp_ok
        and all(c["clean"] for c in checks.values())
        and closed is not None and closed["expected_contacts"])
    if not row["ok"]:
        dirty = [name for name, c in checks.items() if not c["clean"]]
        row["reason_code"] = ("geometry.workspace_violation" if not grasp_ok
                              else "geometry.collision")
        row["failed_checks"] = dirty
    row["place_joint_rad"] = {k: round(v, 6) for k, v in place_arm.items()}
    row["approach_joint_rad"] = {k: round(v, 6) for k, v in appr_arm.items()}
    row["grasp_joint_rad_arm"] = ({k: round(v, 6) for k, v in grasp_arm.items()}
                                  if grasp_ok else None)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--report", default=str(REPORT))
    parser.add_argument("--models", nargs="+", default=None,
                        help="검사에 쓸 자재 모델(기본: 첫 자재 하나로 대표 검사)")
    parser.add_argument("--pitch", type=float, default=None,
                        help="이 pitch 배치만 7종으로 확인한다(통과해야 채택)")
    args = parser.parse_args()
    try:
        return derive(Path(args.out), Path(args.report),
                      model_filter=args.models, forced_pitch=args.pitch)
    finally:
        import rclpy

        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
