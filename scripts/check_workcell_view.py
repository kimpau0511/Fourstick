#!/usr/bin/env python3
"""GUI 초기 시야 검증 (8-09 우선 작업 3).

"GUI에 다 보이는가"를 **렌더러에 의존하지 않고** 확인한다. 스크린샷은 부하가
높으면 슬롯을 얻지 못한다(서비스 타임아웃). 그때 "안 보인다"고 결론 내리는
것은 틀린 판정이다 — 창은 떠 있고 렌더링도 돌고 있다.

그래서 두 가지를 따로 확인한다.

1. **씬에 있는가**: Gazebo에 모델이 존재하고 자세가 설정과 일치하는가
   (`gz model --list`, `/world/*/pose/info`).
2. **시야에 들어오는가**: world SDF가 정한 카메라 자세에서 각 모델의 경계상자
   8개 꼭짓점이 카메라 절두체 안에 있는가. 기하 계산이므로 부하와 무관하다.

스크린샷은 **부가 확인**으로만 시도하고, 실패를 이 검증의 실패로 세지 않는다.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
WORKCELL = ROOT / "config/workcell/fr3_2f85_workcell.json"
WORLD_SDF = ROOT / "config/gazebo/fr3_2f85_workcell.sdf"
OUT = ROOT / "reports/workcell/gui_view.json"
ROBOT_MODEL = "fr3wms_2f85_workcell"
#: 로봇을 지지하는 구조물. 로봇 링크를 가린 것으로 세지 않는다 — 로봇이 그
#: 위에 놓여 있으므로 시선이 스치는 것이 정상이다(간극 판정과 같은 근거).
ROBOT_SUPPORT_MODELS = ("robot_pedestal",)
#: gz-gui 기본 카메라 수평 화각(rad). MinimalScene이 다른 값을 선언하면 그것을 쓴다.
DEFAULT_HFOV_RAD = 1.047
#: 창 가로/세로 비. 캡처한 화면(777×950)에서 확인한 값이다.
DEFAULT_ASPECT = 777.0 / 950.0
#: 절두체 판정에 쓰는 여유(rad). 화면 가장자리에 딱 걸치는 것을 "보인다"로
#: 세지 않기 위해 화각을 이만큼 좁혀서 본다.
FOV_MARGIN_RAD = 0.02


def run(command: list[str], timeout: float = 40.0) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout)
        return done.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def camera_from_world() -> tuple[np.ndarray, np.ndarray, float]:
    """world SDF의 카메라 자세와 화각. 값을 만들지 않는다."""
    root = ET.parse(WORLD_SDF).getroot()
    node = root.find(".//gui//camera_pose")
    if node is None or not (node.text or "").strip():
        raise SystemExit("world SDF에 camera_pose가 없다")
    values = [float(v) for v in node.text.split()]
    position = np.asarray(values[:3])
    roll, pitch, yaw = values[3:6]
    # gz 카메라는 자기 +X를 바라본다. rpy로 회전행렬을 만든다.
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    rotation = np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ])
    hfov = DEFAULT_HFOV_RAD
    fov_node = root.find(".//gui//horizontal_fov")
    if fov_node is not None and (fov_node.text or "").strip():
        hfov = float(fov_node.text)
    return position, rotation, hfov


def resolve(frames: dict, name: str) -> np.ndarray:
    out = np.zeros(3)
    while name is not None:
        out = out + np.asarray(frames[name]["xyz_m"])
        name = frames[name]["parent"]
    return out


def model_boxes(data: dict) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """모델별 world 기준 경계상자(min, max). 설정에서만 만든다."""
    frames = data["frames"]
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for name, model in data["models"].items():
        if model["kind"] == "ground_plane":
            continue
        origin = resolve(frames, model["frame"])
        lows, highs = [], []
        if model["kind"] == "material":
            size = np.asarray(model["size_m"])
            lows.append(origin - size / 2)
            highs.append(origin + size / 2)
        else:
            for part in model["parts"]:
                center = origin + np.asarray(part["center_xyz_m"])
                size = np.asarray(part["size_m"])
                lows.append(center - size / 2)
                highs.append(center + size / 2)
        out[name] = (np.min(lows, axis=0), np.max(highs, axis=0))
    return out


def corners(low: np.ndarray, high: np.ndarray) -> np.ndarray:
    return np.array([[x, y, z] for x in (low[0], high[0])
                     for y in (low[1], high[1]) for z in (low[2], high[2])])


def in_view(points: np.ndarray, position: np.ndarray, rotation: np.ndarray,
            hfov: float, aspect: float) -> dict:
    """점들이 카메라 절두체 안에 있는지. 각도로 판정한다."""
    local = (rotation.T @ (points - position).T).T   # 카메라 좌표계
    forward = local[:, 0]
    left = local[:, 1]
    up = local[:, 2]
    half_h = hfov / 2 - FOV_MARGIN_RAD
    half_v = math.atan(math.tan(hfov / 2) * aspect) - FOV_MARGIN_RAD
    behind = forward <= 0.01
    yaw_angle = np.arctan2(np.abs(left), np.maximum(forward, 1e-6))
    pitch_angle = np.arctan2(np.abs(up), np.maximum(forward, 1e-6))
    inside = (~behind) & (yaw_angle <= half_h) & (pitch_angle <= half_v)
    return {
        "corners": int(len(points)),
        "corners_in_view": int(inside.sum()),
        "all_in_view": bool(inside.all()),
        "any_in_view": bool(inside.any()),
        "max_yaw_deg": round(float(np.degrees(yaw_angle.max())), 2),
        "max_pitch_deg": round(float(np.degrees(pitch_angle.max())), 2),
        "half_hfov_deg": round(math.degrees(half_h), 2),
        "half_vfov_deg": round(math.degrees(half_v), 2),
        "nearest_m": round(float(forward.min()), 4),
        "farthest_m": round(float(forward.max()), 4),
    }


def ray_hits_box(origin: np.ndarray, target: np.ndarray,
                 low: np.ndarray, high: np.ndarray) -> bool:
    """카메라에서 목표점으로 가는 선분이 상자를 통과하는가(슬랩 방식).

    상자 안에서 끝나는 경우(목표가 상자 내부)는 가림으로 세지 않는다.
    """
    direction = target - origin
    length = float(np.linalg.norm(direction))
    if length < 1e-9:
        return False
    direction = direction / length
    t_min, t_max = 0.0, length
    for axis in range(3):
        if abs(direction[axis]) < 1e-12:
            if origin[axis] < low[axis] or origin[axis] > high[axis]:
                return False
            continue
        t1 = (low[axis] - origin[axis]) / direction[axis]
        t2 = (high[axis] - origin[axis]) / direction[axis]
        t_min = max(t_min, min(t1, t2))
        t_max = min(t_max, max(t1, t2))
        if t_min > t_max:
            return False
    # 목표점보다 **앞에서** 상자에 들어가야 가림이다.
    return t_min < length - 1e-6 and t_max > 1e-6


def occlusion(points: np.ndarray, camera: np.ndarray,
              boxes: dict[str, tuple[np.ndarray, np.ndarray]],
              *, ignore: tuple[str, ...] = ()) -> dict:
    """점들이 다른 모델에 가려지는가. **화면 안에 있는 것과 다른 문제다.**"""
    blocked: dict[str, int] = {}
    visible = 0
    for point in points:
        hit_by = [name for name, (low, high) in boxes.items()
                  if name not in ignore
                  and ray_hits_box(camera, point, low, high)]
        if hit_by:
            for name in hit_by:
                blocked[name] = blocked.get(name, 0) + 1
        else:
            visible += 1
    return {
        "samples": int(len(points)),
        "unoccluded": visible,
        "all_visible": visible == len(points),
        "blocked_by": dict(sorted(blocked.items(), key=lambda kv: -kv[1])),
    }


def gazebo_model_poses(world: str) -> dict[str, list[float]]:
    raw = run(["gz", "topic", "-e", "-t", f"/world/{world}/pose/info", "-n", "1",
               "--json-output"], timeout=40)
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw.strip().splitlines()[-1])
    except json.JSONDecodeError:
        return {}
    out = {}
    for entry in payload.get("pose", []):
        position = entry.get("position", {})
        out[entry.get("name", "")] = [
            round(position.get("x", 0.0), 4), round(position.get("y", 0.0), 4),
            round(position.get("z", 0.0), 4)]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--screenshot-dir", default=None,
                        help="부가 확인용 스크린샷 디렉터리(실패는 무시한다)")
    args = parser.parse_args()

    data = json.loads(WORKCELL.read_text(encoding="utf-8"))
    world = data["world_name"]
    position, rotation, hfov = camera_from_world()
    boxes = model_boxes(data)

    # 설정의 프레임 부모 관계로 "무엇이 무엇 위에 놓였는가"를 만든다.
    frames = data["frames"]
    frame_owner = {model["frame"]: name
                   for name, model in data["models"].items()
                   if model["kind"] != "ground_plane"}
    stacked_on: dict[str, tuple[str, ...]] = {}
    for name, model in data["models"].items():
        parent_frame = frames.get(model.get("frame", ""), {}).get("parent")
        owner = frame_owner.get(parent_frame or "")
        if owner and owner != name:
            stacked_on.setdefault(owner, ())
            stacked_on[owner] = (*stacked_on[owner], name)

    models_listed = [line.strip("- ").strip()
                     for line in run(["gz", "model", "--list"]).splitlines()
                     if line.strip().startswith("-")]
    poses = gazebo_model_poses(world)

    # 로봇은 설정에 형상이 없다(외부 URDF). Gazebo가 보고하는 링크 자세로
    # 경계상자를 만든다 — 값을 만들지 않는다.
    # 링크를 **두 묶음으로 나눈다.** 팔·그리퍼는 동작을 보는 부분이므로 모두
    # 보여야 한다. base는 셀 구조물과 같은 높이에 있어 시점에 따라 가려질 수
    # 있다 — 사실로 기록하되 요구하지는 않는다.
    ARM_LINKS = ("shoulder_Link", "upperarm_Link", "forearm_Link",
                 "wrist1_Link", "wrist2_Link", "wrist3_Link",
                 "robotiq_85_left_finger_tip_link",
                 "robotiq_85_right_finger_tip_link")
    BASE_LINKS = ("base_link",)
    arm_named = {name: np.asarray(xyz) for name, xyz in poses.items()
                 if name in ARM_LINKS}
    base_named = {name: np.asarray(xyz) for name, xyz in poses.items()
                  if name in BASE_LINKS}
    robot_points = np.array(list(arm_named.values())) if arm_named else np.empty((0, 3))

    checks: dict[str, dict] = {}
    required = ["robot_pedestal", "workbench", "pallet_1", "pallet_2", "pallet_3",
                "conveyor", "material_a", "material_b", "material_c"]
    for name in required:
        low, high = boxes[name]
        view = in_view(corners(low, high), position, rotation, hfov,
                       DEFAULT_ASPECT)
        present = name in models_listed
        # 모델 중심 위쪽 면을 표본으로 가림을 본다.
        # 자기 자신과 **자기 위에 놓인 것**은 제외한다 — 자재가 팔레트 위에
        # 있으니 팔레트 상면을 가리는 것이 정상이다(설정의 프레임 부모 관계).
        top = np.array([[(low[0] + high[0]) / 2, (low[1] + high[1]) / 2, high[2]]])
        hidden = occlusion(top, position, boxes,
                           ignore=(name, *stacked_on.get(name, ())))
        checks[name] = {
            "in_scene": present,
            "gazebo_pose": poses.get(name),
            "box_min_m": [round(float(v), 4) for v in low],
            "box_max_m": [round(float(v), 4) for v in high],
            "view": view,
            "occlusion": hidden,
            "passed": bool(present and view["all_in_view"]
                           and hidden["all_visible"]),
        }

    if len(robot_points):
        view = in_view(robot_points, position, rotation, hfov, DEFAULT_ASPECT)
        # **화면 안에 있는 것과 가려지지 않는 것은 다른 문제다.**
        # 작업대가 로봇을 가려 "로봇이 안 보인다"가 된 사례를 실측했다.
        hidden = occlusion(robot_points, position, boxes,
                           ignore=ROBOT_SUPPORT_MODELS)
        per_link = {}
        for name, point in {**arm_named, **base_named}.items():
            one = np.array([point])
            per_link[name] = {
                "in_view": in_view(one, position, rotation, hfov,
                                   DEFAULT_ASPECT)["all_in_view"],
                "unoccluded": occlusion(one, position, boxes,
                                        ignore=ROBOT_SUPPORT_MODELS
                                        )["all_visible"],
            }
        base_hidden = (occlusion(np.array(list(base_named.values())), position,
                                 boxes, ignore=ROBOT_SUPPORT_MODELS)
                       if base_named else {"all_visible": None})
        checks["fr3_base"] = {
            "in_scene": ROBOT_MODEL in models_listed,
            "links": list(base_named),
            "occlusion": base_hidden,
            "required": False,
            "note": "base는 셀 구조물과 같은 높이에 있어 시점에 따라 가려질 수"
                    " 있다. 사실로 기록하되 시연 가시성 조건으로 요구하지 않는다",
            # 씬에 있으면 통과다. 가림은 기록만 한다.
            "passed": ROBOT_MODEL in models_listed,
        }
        checks["fr3_arm_and_gripper"] = {
            "in_scene": ROBOT_MODEL in models_listed,
            "links": list(arm_named),
            "link_samples": int(len(robot_points)),
            "source": f"/world/{world}/pose/info (Gazebo 실측 링크 자세)",
            "view": view,
            "occlusion": hidden,
            "per_link": per_link,
            "occlusion_ignored": list(ROBOT_SUPPORT_MODELS),
            "occlusion_ignored_reason": "로봇을 지지하는 구조물이다."
                                        " 로봇이 그 위에 놓여 있으므로 시선이"
                                        " 스치는 것이 정상이다",
            "passed": bool(ROBOT_MODEL in models_listed and view["all_in_view"]
                           and hidden["all_visible"]),
        }
    else:
        checks["fr3_arm_and_gripper"] = {
            "in_scene": ROBOT_MODEL in models_listed,
            "passed": False,
            "detail": "Gazebo가 링크 자세를 주지 않았다 — 시야를 계산할 수 없다",
            "reason_code": "exec.unverifiable",
        }

    # 부가 확인: 스크린샷. 실패를 이 검증의 실패로 세지 않는다.
    screenshot = {"attempted": False}
    if args.screenshot_dir:
        target = Path(args.screenshot_dir)
        target.mkdir(parents=True, exist_ok=True)
        before = set(target.glob("*.png"))
        out = run(["gz", "service", "-s", "/gui/screenshot",
                   "--reqtype", "gz.msgs.StringMsg",
                   "--reptype", "gz.msgs.Boolean", "--timeout", "60000",
                   "--req", f'data: "{target}"'], timeout=90)
        deadline = time.monotonic() + 40
        saved = None
        while time.monotonic() < deadline:
            new = set(target.glob("*.png")) - before
            if new:
                saved = str(sorted(new)[0])
                break
            time.sleep(2)
        screenshot = {
            "attempted": True, "service_reply": out.strip()[:80],
            "saved": saved,
            "note": "부가 확인이다. 부하가 높으면 GUI가 캡처 슬롯을 주지 못한다"
                    " — 그 실패를 '안 보인다'로 세지 않는다",
        }

    passed = sum(1 for entry in checks.values() if entry["passed"])
    report = {
        "schema": "forstick2.workcell_gui_view/1",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "world": world,
        "camera": {
            "position_m": [round(float(v), 4) for v in position],
            "source": str(WORLD_SDF.relative_to(ROOT)),
            "horizontal_fov_rad": hfov,
            "aspect": round(DEFAULT_ASPECT, 4),
            "fov_margin_rad": FOV_MARGIN_RAD,
        },
        "method": "설정의 경계상자 8꼭짓점과 Gazebo 실측 링크 자세를 카메라"
                  " 절두체에 투영해 판정한다. 렌더러에 의존하지 않는다",
        "models_in_scene": models_listed,
        "checks": checks,
        "screenshot": screenshot,
        "passed_count": passed,
        "total_count": len(checks),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    for name, entry in checks.items():
        view = entry.get("view") or {}
        print(f"  [{'통과' if entry['passed'] else '실패'}] {name:22}"
              f" 씬={entry.get('in_scene')}"
              f" 시야={view.get('corners_in_view')}/{view.get('corners')}"
              f" (yaw {view.get('max_yaw_deg')}° ≤ {view.get('half_hfov_deg')}°,"
              f" pitch {view.get('max_pitch_deg')}° ≤ {view.get('half_vfov_deg')}°)"
              f" 비가림={entry.get('occlusion', {}).get('unoccluded')}"
              f"/{entry.get('occlusion', {}).get('samples')}")
        hidden = entry.get("occlusion") or {}
        if hidden and not hidden.get("all_visible", True):
            print(f"        가림: {hidden.get('blocked_by')}")
    print(f"  {out_path.relative_to(ROOT)} 기록 — {passed}/{len(checks)} 통과")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
