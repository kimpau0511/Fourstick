#!/usr/bin/env python3
"""FR3-WMS Gazebo 동작 재생 (md/개발플랜.md 8-07 · 사용자 확인용).

순서대로 다시 돌릴 수 있는 재생 스크립트다. 한 단계씩 실행하고, 각 단계의
판정을 **관측값**으로 남긴다. 아직 근거가 없는 단계는 실행하지 않고 이유를
분명히 말하며 거부한다 — 되는 척하지 않는다.

    ./scripts/replay_fr3_demo.sh                 # 전체 순서
    ./scripts/replay_fr3_demo.sh home move stop  # 고른 단계만

단계:
  camera        GUI 카메라를 FR3·시험 물체가 함께 보이는 위치로 옮긴다
  home          home 자세로 계획·실행하고 관측으로 도달을 확인한다
  move          안전 자세로 계획·실행하고 관측으로 도달을 확인한다
  stop          이동 중 STOP → 관측 속도로 정지를 확인한다(도달하지 않아야 한다)
  gripper_open  2F-85 열기 — **장착 근거 미확보로 거부**
  gripper_close 2F-85 닫기 — **장착 근거 미확보로 거부**
  pick          시험 물체 집기 — 진입 조건 미충족으로 거부
  lift          들어올리기 — 진입 조건 미충족으로 거부
  place         내려놓기 — 진입 조건 미충족으로 거부

전제: scripts/run_gazebo_fr3.sh와 scripts/run_moveit_fr3.sh가 이미 돌고 있다.
"""

from __future__ import annotations

import importlib.util
import json
import math
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.loader import load_composite_profile, load_mounting_profile  # noqa: E402
from core.reason_codes import ReasonCode  # noqa: E402

CONFIG = ROOT / "config"
WORLD = "forstick2_fr3_cell"
#: GUI 카메라 위치. FR3(원점)와 시험 물체(앞쪽 0.45 m)가 함께 보이는 각도다.
CAMERA_XYZ = (1.7, -1.7, 1.3)
CAMERA_TARGET = (0.3, 0.0, 0.3)
STEPS = ("camera", "home", "move", "stop", "gripper_open", "gripper_close",
         "pick", "lift", "place")


def load_verifier():
    """검증 스크립트를 모듈로 불러온다(같은 판정 규칙을 쓴다)."""
    spec = importlib.util.spec_from_file_location(
        "verify_fr3_moveit", ROOT / "scripts/verify_fr3_moveit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def gz(service: str, reqtype: str, request: str, timeout_ms: int = 20000) -> bool:
    result = subprocess.run(
        ["gz", "service", "-s", service, "--reqtype", reqtype,
         "--reptype", "gz.msgs.Boolean", "--timeout", str(timeout_ms),
         "--req", request],
        capture_output=True, text=True, timeout=timeout_ms / 1000 + 20,
    )
    return "data: true" in result.stdout


def camera_quaternion() -> tuple[float, float, float, float]:
    delta = [t - c for t, c in zip(CAMERA_TARGET, CAMERA_XYZ)]
    yaw = math.atan2(delta[1], delta[0])
    pitch = -math.atan2(delta[2], math.hypot(delta[0], delta[1]))
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    return (-sp * sy, sp * cy, cp * sy, cp * cy)


def step_camera() -> dict:
    fixture = json.loads((CONFIG / "gazebo/test_object.json").read_text("utf-8"))
    models = subprocess.run(["gz", "model", "--list"], capture_output=True,
                            text=True, timeout=60).stdout
    spawned = fixture["object_id"] in models
    if not spawned:
        spawned = gz(f"/world/{WORLD}/create", "gz.msgs.EntityFactory",
                     f'sdf_filename: "{CONFIG}/gazebo/test_object.sdf",'
                     f' name: "{fixture["object_id"]}", allow_renaming: false')
    x, y, z, w = camera_quaternion()
    moved = gz("/gui/move_to/pose", "gz.msgs.GUICamera",
               f"pose: {{position: {{x: {CAMERA_XYZ[0]}, y: {CAMERA_XYZ[1]},"
               f" z: {CAMERA_XYZ[2]}}}, orientation: {{x: {x:.6f}, y: {y:.6f},"
               f" z: {z:.6f}, w: {w:.6f}}}}}")
    pose = subprocess.run(
        ["gz", "topic", "-e", "-t", "/gui/camera/pose", "-n", "1"],
        capture_output=True, text=True, timeout=60).stdout.strip()
    return {
        "step": "camera", "executed": True,
        "test_object": {"id": fixture["object_id"],
                        "sim_fixture_version": fixture["sim_fixture_version"],
                        "test_only": fixture["test_only"],
                        "spawned": spawned,
                        "start_pose": fixture["start_pose"]},
        "camera_moved": moved,
        "camera_pose_observed": pose.replace("\n", " ")[:200],
        "note": ("2F-85는 장면에 없다 — 커플링 근거가 확인되기 전에는 결합하지"
                 " 않는다. 카메라는 FR3와 시험 물체가 함께 보이는 각도다"),
    }


def observed_step(module, verifier, label: str, target: dict) -> dict:
    plan = verifier.plan_to(target)
    if plan["error_code"] != 1 or not plan["point_count"]:
        return {"step": label, "executed": False,
                "plan_error_code": plan["error_code"],
                "detail": "계획이 만들어지지 않았다"}
    execution = verifier.execute_plan(plan["trajectory"])
    verifier.spin(2.5)
    observation = module.observed_error(verifier, target)
    return {
        "step": label, "executed": True,
        "target_rad": {j: round(v, 4) for j, v in target.items()},
        "command_accepted": execution.get("accepted"),
        "moveit_error_code": execution.get("error_code"),
        "observed_rad": observation["observed"],
        "max_error_rad": observation["max_error_rad"],
        "tolerance_rad": module.POSITION_TOLERANCE,
        # 명령 수락과 도달을 합치지 않는다. 도달은 관측으로만 판정한다.
        "target_reached": observation["reached"],
    }


def step_stop(module, verifier) -> dict:
    home = verifier.plan_to(module.HOME)
    if home["error_code"] == 1 and home["point_count"]:
        verifier.execute_plan(home["trajectory"])
        verifier.spin(3.0)
    slow = verifier.plan_to(module.SAFE_POSE, velocity_scaling=0.02)
    if slow["error_code"] != 1:
        return {"step": "stop", "executed": False,
                "detail": "계획이 만들어지지 않았다"}
    result = verifier.execute_plan(slow["trajectory"], stop_after=2.0)
    check = module.stop_confirmed(verifier)
    sample = verifier.latest()
    error = (None if sample is None else
             round(max(abs(sample["position"][j] - module.SAFE_POSE[j])
                       for j in module.JOINTS), 5))
    return {
        "step": "stop", "executed": True,
        "stop_channel": "/trajectory_execution_event (data=stop)",
        "moveit_error_code": result.get("error_code"),
        "preempted": result.get("error_code") == -7,
        "observed_stop": check,
        "error_to_target_rad": error,
        "target_not_reached": (None if error is None
                              else error > module.POSITION_TOLERANCE),
    }


def refusal(step: str, reason: ReasonCode, detail: str, blocked: dict) -> dict:
    return {"step": step, "executed": False, "reason_code": reason.value,
            "detail": detail, "unmet_conditions": blocked}


def gripper_refusal(step: str, mounting) -> dict:
    return refusal(
        step, ReasonCode.CAPABILITY_PROFILE_INCOMPLETE,
        "2F-85가 결합되지 않았다. 장착 변환의 공식 근거가 없어 그리퍼 명령을"
        " 만들지 않는다(근거 없는 좌표를 쓰지 않는다).",
        {"mounting_profile_version": mounting.mounting_profile_version,
         "conclusion": mounting.comparison["conclusion"],
         "missing": list(mounting.unverified_items)},
    )


def task_refusal(step: str, mounting, composite) -> dict:
    return refusal(
        step, ReasonCode.EXEC_UNVERIFIABLE,
        f"{step}의 진입 조건이 충족되지 않았다. 시뮬레이션에서도 실행하지 않는다.",
        {
            "1_moveit_home_move_collision": "충족 — reports/moveit/verify_moveit.json",
            "2_mounting_transform_verified": (
                f"미충족 — {mounting.mounting_profile_version}"),
            "3_gripper_open_close_observed": "미충족 — 그리퍼 미결합",
            "4_contact_observation": "미충족 — 패드 접촉 정보 없음",
            "5_grasp_verifier": "미충족 — 파지 확인기 입력 없음",
            "6_resource_consistency": "미충족 — 물체 id·카탈로그 대조 전",
            "7_profile_policy_snapshot_revalidation": (
                f"미충족 — composite {composite.composite_profile_version}"
                " 미검증"),
            "supported_skills": list(composite.supported_skills),
        },
    )


def main() -> int:
    wanted = [s for s in sys.argv[1:] if not s.startswith("-")] or list(STEPS)
    unknown = [s for s in wanted if s not in STEPS]
    if unknown:
        print(f"모르는 단계: {unknown}. 가능한 단계: {list(STEPS)}", file=sys.stderr)
        return 2

    load = lambda name: json.loads(  # noqa: E731
        (CONFIG / "profiles" / name).read_text(encoding="utf-8"))
    mounting = load_mounting_profile(
        load("fr3wms_to_robotiq_2f85_mounting.json"))
    from config.loader import load_arm_profile, load_gripper_profile

    arm = load_arm_profile(load("fr3wms_arm.json"))
    gripper = load_gripper_profile(load("robotiq_2f85_gripper.json"))
    composite = load_composite_profile(
        load("composite_fr3wms_2f85.json"),
        arms={arm.arm_profile_id: arm},
        grippers={gripper.gripper_profile_id: gripper},
        mountings={mounting.mounting_profile_id: mounting},
    )

    results = []
    needs_ros = [s for s in wanted if s in ("home", "move", "stop")]
    module = verifier = None
    if needs_ros:
        import rclpy

        module = load_verifier()
        rclpy.init()
        verifier = module.MoveItVerifier()
        verifier.spin(3.0)
    try:
        for step in wanted:
            if step == "camera":
                results.append(step_camera())
            elif step == "home":
                results.append(observed_step(module, verifier, "home", module.HOME))
            elif step == "move":
                results.append(
                    observed_step(module, verifier, "move", module.SAFE_POSE))
            elif step == "stop":
                results.append(step_stop(module, verifier))
            elif step in ("gripper_open", "gripper_close"):
                results.append(gripper_refusal(step, mounting))
            else:
                results.append(task_refusal(step, mounting, composite))
    finally:
        if verifier is not None:
            import rclpy

            verifier.destroy_node()
            rclpy.shutdown()

    report = {
        "recorded_at": time.time(),
        "world": WORLD,
        "is_simulated": True,
        "arm_only": True,
        "mounting_profile_version": mounting.mounting_profile_version,
        "composite_profile_version": composite.composite_profile_version,
        "steps": results,
        "executed": [r["step"] for r in results if r.get("executed")],
        "refused": [r["step"] for r in results if not r.get("executed")],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
