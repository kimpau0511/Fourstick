#!/usr/bin/env python3
"""작업 셀 관절 자세 유도·검증 (md/개발플랜.md 8-08 우선순위 3·4).

작업 셀 설정의 **TCP 목표(Cartesian)** 를 관절값으로 푼다. 관절값을 손으로
적지 않는다 — 수치 IK로 풀고, 같은 FK로 되돌려 확인하고, 충돌 메시 점군으로
환경 간극까지 재고 나서야 기록한다.

기록하는 것:
- `joint_rad`: 수치 IK 해
- `fk_tcp_xyz_m` / `position_error_m`: 같은 FK로 되돌린 값과 목표의 차
- `clearance`: 바닥·받침대·작업대·팔레트·컨베이어에 대한 최소 간극
- `status`: 위 셋이 모두 허용치 안일 때만 `verified`

해를 못 찾거나 간극이 부족하면 `blocked`으로 남긴다. **그 값을 실행 경로에
넣지 않는다.**

안전 home도 같은 방식으로 정한다. 후보 자세를 훑어 간극이 가장 큰 것을
고르되, 바닥을 띄우거나 floor 높이를 바꿔서 통과시키지 않는다.
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
sys.path.insert(0, str(ROOT / "scripts"))

from robots.moveit.kinematics import link_transforms, parse_urdf, rpy_matrix  # noqa: E402
from scripts_support_stl import read_stl_points  # noqa: E402

WORKCELL = ROOT / "config/workcell/fr3_2f85_workcell.json"
OUT = ROOT / "config/workcell/fr3_2f85_workcell_poses.json"
URDF = Path("/tmp/forstick2_gazebo/workcell/fr3wms_with_2f85.moveit.urdf")
OVERLAY = Path("/tmp/forstick2_gazebo/overlay/share/robotiq_description")
ARM_JOINTS = ["j1", "j2", "j3", "j4", "j5", "j6"]
GRIPPER_JOINT = "robotiq_85_left_knuckle_joint"
TCP_LINK = "robotiq_85_tcp"

#: IK 허용치. 컨트롤러 관절 허용치(0.05 rad)보다 훨씬 작게 잡는다.
IK_POSITION_TOLERANCE_M = 0.002
IK_ORIENTATION_TOLERANCE_RAD = 0.05
#: 메시 점군 표본 수. 간극 계산의 분해능을 정한다.
SAMPLE_POINTS = 2500
#: home 후보를 훑을 때 쓰는 그리퍼 관절값(열림).
GRIPPER_OPEN_RAD = 0.0


def load_urdf():
    if not URDF.is_file():
        raise SystemExit(
            f"작업 셀 URDF가 없다: {URDF}\n"
            "먼저 실행한다: FORSTICK2_ASSEMBLY_YAW_RAD=0"
            " ./scripts/run_gazebo_fr3_2f85_workcell_gui.sh --urdf-only"
        )
    joints, links = parse_urdf(URDF)
    import xml.etree.ElementTree as ET

    mimics = {}
    for joint in ET.parse(URDF).getroot().findall("joint"):
        mimic = joint.find("mimic")
        if mimic is not None and mimic.get("joint") == GRIPPER_JOINT:
            mimics[joint.get("name")] = (float(mimic.get("multiplier", "1")),
                                         float(mimic.get("offset", "0")))
    limits = {j["name"]: j["limit"] for j in joints
              if j["name"] in ARM_JOINTS and j.get("limit")}
    return joints, links, mimics, limits


def mesh_clouds(links: dict) -> dict:
    clouds = {}
    for name, info in links.items():
        mesh = info.get("mesh")
        if not mesh:
            continue
        path = Path(mesh.replace("file://", "").replace(
            "package://robotiq_description", str(OVERLAY)))
        if not path.is_file():
            raise SystemExit(f"충돌 메시를 찾지 못했다: {path}")
        points = read_stl_points(path, SAMPLE_POINTS) * np.asarray(info["scale"])
        clouds[name] = (rpy_matrix(*info["rpy"]) @ points.T).T + np.asarray(info["xyz"])
    if not clouds:
        raise SystemExit("충돌 메시가 하나도 없다")
    return clouds


def angles_for(arm: dict, gripper: float, mimics: dict) -> dict:
    out = dict(arm)
    out[GRIPPER_JOINT] = gripper
    for name, (multiplier, offset) in mimics.items():
        out[name] = gripper * multiplier + offset
    return out


def tcp_pose(joints, arm: dict, gripper: float, mimics: dict):
    transforms = link_transforms(joints, angles_for(arm, gripper, mimics))
    rotation, position = transforms[TCP_LINK]
    return rotation, position, transforms


def obstacle_boxes(data: dict) -> list[dict]:
    """환경 장애물을 (world 기준) 축정렬 박스 목록으로 만든다."""
    frames = data["frames"]

    def resolve(name):
        out = np.zeros(3)
        while name is not None:
            frame = frames[name]
            out = out + np.asarray(frame["xyz_m"])
            name = frame["parent"]
        return out

    boxes = []
    for model_name, model in data["models"].items():
        if model["kind"] == "ground_plane":
            continue
        origin = resolve(model["frame"])
        if model["kind"] == "material":
            size = np.asarray(model["size_m"])
            boxes.append({"model": model_name, "part": "body",
                          "min": origin - size / 2, "max": origin + size / 2})
            continue
        for part in model["parts"]:
            center = origin + np.asarray(part["center_xyz_m"])
            size = np.asarray(part["size_m"])
            boxes.append({"model": model_name, "part": part["name"],
                          "min": center - size / 2, "max": center + size / 2})
    return boxes


def clearances(joints, clouds, arm: dict, gripper: float, mimics: dict,
               boxes: list[dict], *, robot_models=("robot_pedestal",)) -> dict:
    """바닥과 각 장애물 박스에 대한 최소 간극(음수면 침범)."""
    transforms = link_transforms(joints, angles_for(arm, gripper, mimics))
    world = {}
    for name, points in clouds.items():
        rotation, position = transforms[name]
        world[name] = (rotation @ points.T).T + position
    everything = np.vstack(list(world.values()))

    floor_min = float(everything[:, 2].min())
    floor_link = min(world, key=lambda n: world[n][:, 2].min())

    worst = {"floor": {"clearance_m": round(floor_min, 6), "link": floor_link}}
    per_box = []
    for box in boxes:
        # 받침대는 로봇을 지지하는 구조물이다. base_link가 그 위에 놓이므로
        # 접촉이 정상이다 — 간극 판정에서 제외하고 그 사실을 기록한다.
        if box["model"] in robot_models:
            continue
        gap_best = None
        gap_link = None
        for name, points in world.items():
            # 박스 밖으로 나간 거리(축별 최대). 박스 안이면 음수.
            outside = np.maximum(box["min"] - points, points - box["max"])
            distance = np.max(outside, axis=1).min()
            if gap_best is None or distance < gap_best:
                gap_best, gap_link = float(distance), name
        per_box.append({"model": box["model"], "part": box["part"],
                        "clearance_m": round(gap_best, 6), "link": gap_link})
    per_box.sort(key=lambda item: item["clearance_m"])
    worst["environment"] = per_box[:5]
    worst["environment_min_m"] = per_box[0]["clearance_m"] if per_box else None
    return worst


def _ik_once(joints, mimics, limits, target_xyz, target_rpy, seed,
             *, iterations: int, orientation_weight: float):
    """감쇠 최소제곱 1회 실행. 우리 FK만 쓴다(외부 IK 라이브러리 없음)."""
    names = list(ARM_JOINTS)
    q = np.array([seed[n] for n in names], dtype=float)
    lower = np.array([limits[n]["lower"] for n in names])
    upper = np.array([limits[n]["upper"] for n in names])
    target_position = np.asarray(target_xyz, dtype=float)
    target_rotation = rpy_matrix(*target_rpy)
    step = 1e-5

    def error_of(values):
        rotation, position, _ = tcp_pose(
            joints, dict(zip(names, values)), GRIPPER_OPEN_RAD, mimics)
        position_error = target_position - position
        relative = target_rotation @ rotation.T
        angle = float(np.arccos(np.clip((np.trace(relative) - 1) / 2, -1, 1)))
        axis = np.array([relative[2, 1] - relative[1, 2],
                         relative[0, 2] - relative[2, 0],
                         relative[1, 0] - relative[0, 1]])
        norm = np.linalg.norm(axis)
        rotation_error = np.zeros(3) if norm < 1e-9 else axis / norm * angle
        return position_error, rotation_error, float(np.linalg.norm(position_error)), angle

    damping = 0.08
    best = (q.copy(), *error_of(q)[2:])
    for _ in range(iterations):
        position_error, rotation_error, distance, angle = error_of(q)
        if distance < best[1] + 1e-12 and (
                distance < best[1] or angle < best[2]):
            best = (q.copy(), distance, angle)
        if distance <= IK_POSITION_TOLERANCE_M and angle <= IK_ORIENTATION_TOLERANCE_RAD:
            return dict(zip(names, [float(v) for v in q])), distance, angle
        jacobian = np.zeros((6, len(names)))
        for index in range(len(names)):
            perturbed = q.copy()
            perturbed[index] += step
            p_err, r_err, _, _ = error_of(perturbed)
            jacobian[:3, index] = -(p_err - position_error) / step
            jacobian[3:, index] = -(r_err - rotation_error) / step * orientation_weight
        error = np.concatenate([position_error, rotation_error * orientation_weight])
        delta = jacobian.T @ np.linalg.solve(
            jacobian @ jacobian.T + damping ** 2 * np.eye(6), error)
        candidate = np.clip(q + np.clip(delta, -0.35, 0.35), lower, upper)
        _, _, new_distance, new_angle = error_of(candidate)
        if new_distance + new_angle * 0.1 < distance + angle * 0.1:
            q = candidate
            damping = max(0.01, damping * 0.9)   # 잘 줄어들면 감쇠를 낮춘다
        else:
            damping = min(1.0, damping * 2.0)    # 발산하면 감쇠를 올린다
            if damping >= 1.0:
                break
    q, distance, angle = best
    return dict(zip(names, [float(v) for v in q])), distance, angle


def solve_ik(joints, mimics, limits, target_xyz, target_rpy, seed: dict,
             *, iterations: int = 300) -> tuple[dict, float, float]:
    """다중 시작점 IK. 지역 최소에 갇히는 것을 막는다.

    1단계는 위치만 맞추고(orientation_weight 작게), 2단계에서 자세까지 맞춘다.
    한 시작점이 실패하면 다음 시작점으로 넘어가고, **끝까지 허용치에 들지
    못하면 그 사실을 그대로 돌려준다** — 값을 만들지 않는다.
    """
    rng = np.random.default_rng(20260916)
    lower = np.array([limits[n]["lower"] for n in ARM_JOINTS])
    upper = np.array([limits[n]["upper"] for n in ARM_JOINTS])
    # 목표 방향을 향한 j1을 시작점에 넣는다(base z축 회전이라 방향을 바로 준다).
    yaw_to_target = float(np.arctan2(target_xyz[1], target_xyz[0]))
    seeds = [dict(seed)]
    for j2, j3, j4, j5 in ((-1.2, 1.2, -1.5, -1.5708), (-0.8, 1.6, -2.4, -1.5708),
                           (-1.6, 0.9, -0.9, -1.5708), (-0.5, 1.9, -2.9, -1.5708),
                           (-1.0, 1.4, -1.9, 1.5708)):
        seeds.append({"j1": yaw_to_target, "j2": j2, "j3": j3,
                      "j4": j4, "j5": j5, "j6": 0.0})
    for _ in range(12):
        values = rng.uniform(np.maximum(lower, -3.0), np.minimum(upper, 3.0))
        candidate = dict(zip(ARM_JOINTS, [float(v) for v in values]))
        candidate["j1"] = yaw_to_target
        seeds.append(candidate)

    best = (dict(seed), float("inf"), float("inf"))
    for candidate_seed in seeds:
        clipped = {n: float(np.clip(candidate_seed[n], limits[n]["lower"],
                                    limits[n]["upper"])) for n in ARM_JOINTS}
        rough, _, _ = _ik_once(joints, mimics, limits, target_xyz, target_rpy,
                               clipped, iterations=iterations // 2,
                               orientation_weight=0.15)
        arm, distance, angle = _ik_once(joints, mimics, limits, target_xyz,
                                        target_rpy, rough, iterations=iterations,
                                        orientation_weight=1.0)
        if distance + angle * 0.05 < best[1] + best[2] * 0.05:
            best = (arm, distance, angle)
        if (distance <= IK_POSITION_TOLERANCE_M
                and angle <= IK_ORIENTATION_TOLERANCE_RAD):
            return arm, distance, angle
    return best


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    data = json.loads(WORKCELL.read_text(encoding="utf-8"))
    joints, links, mimics, limits = load_urdf()
    clouds = mesh_clouds(links)
    boxes = obstacle_boxes(data)
    margins = data["collision_margins_m"]
    floor_min = margins["floor_clearance_min_m"]
    env_min = margins["environment_clearance_min_m"]

    frames = data["frames"]

    def resolve(name):
        out = np.zeros(3)
        while name is not None:
            frame = frames[name]
            out = out + np.asarray(frame["xyz_m"])
            name = frame["parent"]
        return out

    # ── 1. 안전 home 후보 탐색 ─────────────────────────────────────────
    # 팔을 접어 세운 자세들을 훑는다. **로봇을 띄우거나 바닥을 내리지 않는다.**
    candidates = []
    for j2 in (-1.9, -1.7, -1.5, -1.3, -1.1):
        for j3 in (1.2, 1.5, 1.8, 2.1):
            for j4 in (-1.9, -1.6, -1.3, -1.0):
                for j5 in (-1.5708, 0.0, 1.5708):
                    candidates.append({"j1": 0.0, "j2": j2, "j3": j3,
                                       "j4": j4, "j5": j5, "j6": 0.0})
    # 전 관절 0 자세를 **비교 기준**으로 함께 잰다. 받침대가 바닥 간극을
    # 비구속 조건으로 만들기 때문에, 무엇이 실제로 구속하는지 드러내야 한다.
    zero_arm = dict.fromkeys(ARM_JOINTS, 0.0)
    zero_gaps = clearances(joints, clouds, zero_arm, GRIPPER_OPEN_RAD, mimics, boxes)
    _, zero_position, _ = tcp_pose(joints, zero_arm, GRIPPER_OPEN_RAD, mimics)
    zero_pose = {
        "joint_rad": zero_arm,
        "floor_clearance_m": zero_gaps["floor"]["clearance_m"],
        "environment_clearance_min_m": zero_gaps["environment_min_m"],
        "worst_pairs": zero_gaps["environment"],
        "tcp_world_m": [round(float(v), 6) for v in zero_position],
        "passes_floor": zero_gaps["floor"]["clearance_m"] >= floor_min,
        "passes_environment": (zero_gaps["environment_min_m"] is not None
                               and zero_gaps["environment_min_m"] >= env_min),
        "note": "실행용 home으로 쓰지 않는다. 받침대 없는 단순 셀에서는 이 자세의"
                " 손목이 바닥을 2 mm 파고들었다"
                " (reports/gripper/floor_clearance.json)",
    }

    scored = []
    for arm in candidates:
        if any(not (limits[n]["lower"] <= v <= limits[n]["upper"])
               for n, v in arm.items()):
            continue
        gaps = clearances(joints, clouds, arm, GRIPPER_OPEN_RAD, mimics, boxes)
        if gaps["floor"]["clearance_m"] < floor_min:
            continue
        if gaps["environment_min_m"] is None or gaps["environment_min_m"] < env_min:
            continue
        _, position, _ = tcp_pose(joints, arm, GRIPPER_OPEN_RAD, mimics)
        scored.append({
            "arm": arm,
            "floor_clearance_m": gaps["floor"]["clearance_m"],
            "environment_min_m": gaps["environment_min_m"],
            "tcp_world_m": [round(float(v), 6) for v in position],
            "worst": gaps,
            # 가장 여유가 큰 자세를 고른다(바닥·환경 중 작은 쪽 기준).
            "score": min(gaps["floor"]["clearance_m"], gaps["environment_min_m"]),
        })
    scored.sort(key=lambda item: item["score"], reverse=True)

    if not scored:
        safe_home = {
            "status": "blocked",
            "reason": "후보 자세 중 바닥·환경 간극 조건을 만족하는 것이 없다",
            "reason_code": "geometry.collision_detected",
            "candidates_tried": len(candidates),
        }
    else:
        best = scored[0]
        safe_home = {
            "status": "verified",
            "joint_rad": {k: round(v, 6) for k, v in best["arm"].items()},
            "gripper_joint_rad": GRIPPER_OPEN_RAD,
            "floor_clearance_m": best["floor_clearance_m"],
            "environment_clearance_min_m": best["environment_min_m"],
            "tcp_world_m": best["tcp_world_m"],
            "worst_pairs": best["worst"]["environment"],
            "lowest_link": best["worst"]["floor"]["link"],
            "candidates_tried": len(candidates),
            "candidates_passing": len(scored),
            "source": "scripts/derive_workcell_poses.py 후보 탐색",
            "measurement_method": "fk_plus_collision_mesh_point_cloud",
            "verified": True,
            "captured_at": time.strftime("%Y-%m-%d"),
            "note": "받침대는 로봇을 지지하는 구조물이므로 간극 판정에서 제외한다."
                    " 바닥 높이를 바꾸거나 로봇을 띄워 통과시키지 않았다",
        }

    # ── 2. TCP 목표 → 관절값 ───────────────────────────────────────────
    spec = data["tcp_targets"]
    tool_down = spec["tool_down_rpy_rad"]
    seed = (safe_home.get("joint_rad") or {"j1": 0.0, "j2": -1.5, "j3": 1.5,
                                           "j4": -1.5, "j5": -1.5708, "j6": 0.0})
    poses = {}
    for name, target in spec["targets"].items():
        origin = resolve(target["frame"]) + np.asarray(target["xyz_m"])
        arm, distance, angle = solve_ik(joints, mimics, limits, origin, tool_down,
                                        dict(seed))
        gaps = clearances(joints, clouds, arm, GRIPPER_OPEN_RAD, mimics, boxes)
        _, position, _ = tcp_pose(joints, arm, GRIPPER_OPEN_RAD, mimics)
        reachable = (distance <= IK_POSITION_TOLERANCE_M
                     and angle <= IK_ORIENTATION_TOLERANCE_RAD)
        # pick/place 목표는 물체·팔레트에 닿는 자세라 환경 간극이 음수가 정상이다.
        # 그래서 접근(approach) 자세만 간극 조건을 적용한다.
        is_approach = name.endswith("_approach")
        clear = (gaps["floor"]["clearance_m"] >= floor_min
                 and (not is_approach
                      or (gaps["environment_min_m"] is not None
                          and gaps["environment_min_m"] >= env_min)))
        poses[name] = {
            "status": "verified" if (reachable and clear) else "blocked",
            "target_frame": target["frame"],
            "target_offset_m": target["xyz_m"],
            "target_world_xyz_m": [round(float(v), 6) for v in origin],
            "target_rpy_rad": tool_down,
            "joint_rad": {k: round(v, 6) for k, v in arm.items()},
            "fk_tcp_world_m": [round(float(v), 6) for v in position],
            "position_error_m": round(distance, 6),
            "orientation_error_rad": round(angle, 6),
            "position_tolerance_m": IK_POSITION_TOLERANCE_M,
            "orientation_tolerance_rad": IK_ORIENTATION_TOLERANCE_RAD,
            "floor_clearance_m": gaps["floor"]["clearance_m"],
            "environment_clearance_min_m": gaps["environment_min_m"],
            "worst_pairs": gaps["environment"],
            "clearance_checked": is_approach,
            "clearance_check_note": (
                "접근 자세만 환경 간극을 요구한다. pick/place 자세는 물체에"
                " 닿는 자세라 간극이 음수인 것이 정상이다 — 그 판정은"
                " pick/place 관문이 별도로 한다"),
            "source": "config/workcell/fr3_2f85_workcell.json tcp_targets",
            "measurement_method": "numerical_ik_verified_by_fk_and_collision_mesh",
            "verified": bool(reachable and clear),
            "captured_at": time.strftime("%Y-%m-%d"),
        }
        if not reachable:
            poses[name]["reason_code"] = "geometry.unreachable"
        elif not clear:
            poses[name]["reason_code"] = "geometry.collision_detected"

    report = {
        "schema": "forstick2.workcell_poses/1",
        "workcell_id": data["workcell_id"],
        "workcell_version": data["workcell_version"],
        "derived_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "is_simulated": True,
        "real_hardware_verified": False,
        "source_urdf": str(URDF),
        "arm_joints": ARM_JOINTS,
        "tcp_link": TCP_LINK,
        "sample_points_per_link": SAMPLE_POINTS,
        "margins_m": {"floor": floor_min, "environment": env_min},
        "safe_home": safe_home,
        "zero_pose_for_comparison": zero_pose,
        "binding_constraint": (
            "받침대(상판 0.73 m) 때문에 바닥 간극은 이 셀에서 구속 조건이 아니다."
            " 실제로 구속하는 것은 작업대·팔레트·컨베이어에 대한 환경 간극이다."
            " 두 값을 따로 기록해 무엇이 판정했는지 드러낸다"),
        "poses": poses,
        "note": "관절값은 수치 IK 해이고, 같은 FK로 되돌려 확인했다."
                " MoveIt planning scene 검증은 scripts/verify_workcell.py가"
                " 별도로 수행한다 — 이 파일만으로 충돌 없음을 주장하지 않는다",
    }
    out = Path(args.out)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")

    print(f"{out.relative_to(ROOT)} 기록")
    if safe_home["status"] == "verified":
        print(f"  안전 home: 바닥 간극 {safe_home['floor_clearance_m']:+.4f} m"
              f" · 환경 간극 {safe_home['environment_clearance_min_m']:+.4f} m"
              f" (후보 {safe_home['candidates_tried']}개 중 {safe_home['candidates_passing']}개 통과)")
        print(f"    관절 {safe_home['joint_rad']}")
    else:
        print(f"  안전 home: **{safe_home['status']}** — {safe_home['reason']}")
    for name, pose in poses.items():
        mark = "통과" if pose["status"] == "verified" else "차단"
        print(f"  {mark} {name:20} 오차 {pose['position_error_m']:.4f} m"
              f" / {pose['orientation_error_rad']:.4f} rad"
              f" · 바닥 {pose['floor_clearance_m']:+.4f} m"
              f" · 환경 {pose['environment_clearance_min_m']:+.4f} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
