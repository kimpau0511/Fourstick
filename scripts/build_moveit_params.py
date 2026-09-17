#!/usr/bin/env python3
"""move_group 파라미터 파일을 우리 설정 조각으로 조립한다 (8-07).

MoveIt Setup Assistant로 만든 설정 패키지를 저장소에 만들지 않는다. 대신
`config/moveit/*.yaml`(우리 파일)과 **외부 경로의 공식 URDF에서 생성한**
URDF·SRDF를 합쳐 하나의 파라미터 파일을 만든다.

- URDF·메시는 복사하지 않는다. 생성물은 로그 디렉터리에만 둔다.
- 여기서 수치를 새로 만들지 않는다. 각 값의 출처는 조각 파일에 적혀 있다.

사용법: build_moveit_params.py <urdf> <srdf> <config/moveit 디렉터리> <출력>
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml


def joint_limit_params(urdf_path: Path, policy: dict) -> dict:
    """URDF 제한 + 정책으로 MoveIt joint_limits 파라미터를 만든다.

    속도는 URDF 값을 그대로 옮긴다(수치를 여기서 만들지 않는다). 가속도는
    정책의 파생 규칙으로 계산하고, 그 값이 **공식 규격이 아니라 시뮬레이션
    설정값**임은 config/moveit/joint_limits.yaml에 적혀 있다.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from robots.moveit.kinematics import parse_urdf, revolute_limits

    joints, _ = parse_urdf(urdf_path)
    accel = policy["acceleration_limits"]
    if accel["rule"] != "urdf_velocity_limit_times_factor":
        raise SystemExit(f"모르는 가속도 파생 규칙: {accel['rule']}")
    factor = float(accel["factor_per_sec"])
    out: dict[str, dict] = {}
    for name, limit in revolute_limits(joints).items():
        velocity = limit.get("velocity")
        if velocity is None:
            raise SystemExit(f"URDF에 {name} 속도 제한이 없다 — 값을 만들지 않는다")
        out[name] = {
            "has_position_limits": True,
            "min_position": limit["lower"],
            "max_position": limit["upper"],
            "has_velocity_limits": True,
            "max_velocity": velocity,
            "has_acceleration_limits": True,
            "max_acceleration": round(velocity * factor, 6),
            "has_jerk_limits": bool(policy["jerk_limits"]["enabled"]),
        }
    return out


def main() -> int:
    urdf_path, srdf_path, config_dir, out_path = (Path(p) for p in sys.argv[1:5])
    load = lambda name: yaml.safe_load((config_dir / name).read_text(encoding="utf-8"))

    kinematics = load("kinematics.yaml")
    policy = load("joint_limits.yaml")
    limits = joint_limit_params(urdf_path, policy)
    controllers = load("moveit_controllers.yaml")
    ompl = load("ompl_planning.yaml")
    scene = load("planning_scene.yaml")

    params: dict = {
        "use_sim_time": True,
        "robot_description": urdf_path.read_text(encoding="utf-8"),
        "robot_description_semantic": srdf_path.read_text(encoding="utf-8"),
        "robot_description_kinematics": kinematics,
        "robot_description_planning": {
            "joint_limits": limits,
            "default_velocity_scaling_factor":
                policy["default_velocity_scaling_factor"],
            "default_acceleration_scaling_factor":
                policy["default_acceleration_scaling_factor"],
        },
        "planning_pipelines": ["ompl"],
        "default_planning_pipeline": "ompl",
        "ompl": ompl,
        # 실행 경로: 8-03에서 검증한 ros2_control 컨트롤러에 그대로 보낸다.
        "allow_trajectory_execution": True,
        "moveit_manage_controllers": False,
        "trajectory_execution": {
            # 컨트롤러 결과를 성공 근거로 쓰지 않는다. 관측 판정은 검증기가 한다.
            "allowed_execution_duration_scaling": 2.0,
            "allowed_goal_duration_margin": 1.0,
            "allowed_start_tolerance": 0.05,
            "execution_duration_monitoring": True,
        },
        "capabilities": "",
        "disable_capabilities": "",
        # planning scene monitor
        "monitor_dynamics": False,
        "publish_robot_description": True,
        "publish_robot_description_semantic": True,
    }
    params.update(controllers)
    params.update({k: v for k, v in scene.items()
                   if not k.startswith("robot_description_planning_scene")})
    # 관측 신선도 기준은 우리 설정 파일 값을 쓴다(브라우저·코드에 숫자 중복 금지).
    params["planning_scene_monitor_state_max_age"] = scene[
        "robot_description_planning_scene_monitor_state_max_age"]

    out_path.write_text(
        yaml.safe_dump({"move_group": {"ros__parameters": params}},
                       allow_unicode=True, default_flow_style=False, width=10000),
        encoding="utf-8",
    )
    print(f"[build_moveit_params] {out_path} ({out_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
