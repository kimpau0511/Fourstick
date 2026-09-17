#!/usr/bin/env python3
"""FR3-WMS arm-only MoveIt2 검증 (md/개발플랜.md 8-07).

**명령 전송 성공과 목표 도달을 구분한다.** MoveIt이 SUCCEEDED를 돌려줘도
Gazebo 관측값이 목표 허용치 안에 들어오지 않으면 성공으로 보지 않는다.

확인 항목(요청 목록 그대로):
 1. 현재 상태 수신          2. home 계획
 3. 안전 자세 계획          4. 궤적 실행
 5. 목표 vs 관측 자세 비교   6. 관절 제한 위반 차단
 7. 자기충돌 차단           8. 바닥·환경 충돌 차단
 9. 계획 제한시간           10. 계획 중 snapshot 변경
11. 이동 중 STOP            12. STOP 후 새 계획 재검증

기존 GeometryValidator(`robots/moveit`)를 MoveIt planning scene에 연결해,
검사에 쓴 scene snapshot id·hash·시각을 판정에 남긴다.

ROS 환경을 source한 셸에서 실행한다. 결과는 JSON으로 출력한다.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rclpy
from control_msgs.action import FollowJointTrajectory
from geometry_msgs.msg import Pose
from moveit_msgs.action import ExecuteTrajectory
from moveit_msgs.msg import (
    AllowedCollisionMatrix,
    AttachedCollisionObject,
    CollisionObject,
    Constraints,
    JointConstraint,
    MotionPlanRequest,
    PlanningScene,
    PlanningSceneComponents,
    RobotState,
    WorkspaceParameters,
)
from moveit_msgs.srv import ApplyPlanningScene, GetMotionPlan, GetPlanningScene
from rclpy.action import ActionClient
from rclpy.node import Node
from sensor_msgs.msg import JointState
from shape_msgs.msg import SolidPrimitive
from std_msgs.msg import String
from trajectory_msgs.msg import JointTrajectoryPoint

from core.geometry import GeometryDecision, GeometryRequest
from core.reason_codes import ReasonCode
from core.task_plan import TaskPlan, TaskStep
from robots.moveit.kinematics import link_transforms, parse_urdf, revolute_limits
from robots.moveit.ros_client import RosPlanningSceneClient, robot_model_hash
from robots.moveit.validator import MoveItGeometryValidator, joint_motion
from validation.geometry_check import check_geometry

JOINTS = ["j1", "j2", "j3", "j4", "j5", "j6"]
GROUP = "fr3wms_arm"
LOG_DIR = Path("/tmp/forstick2_gazebo")
#: 목표 도달 판정 허용치(rad) — 8-04·8-05 검증과 같은 값을 쓴다.
POSITION_TOLERANCE = 0.05
#: 정지 판정: 관측 속도가 이 값 아래로 연속 표본 수만큼 유지돼야 한다.
STOP_VELOCITY_TOLERANCE = 0.01
STOP_SAMPLES = 10
#: 관측 신선도 기준(초). config/moveit/planning_scene.yaml과 같은 값이다.
STATE_MAX_AGE_SEC = 0.1
SNAPSHOT_TTL_SEC = 30.0

HOME = dict.fromkeys(JOINTS, 0.0)
SAFE_POSE = dict(zip(JOINTS, [0.8, -0.6, 0.9, -0.5, 0.7, 0.4]))
#: 관절 제한을 벗어난 목표. j1 제한은 ±3.0543 rad이다(URDF).
OUT_OF_LIMIT = dict(HOME, j1=3.5)
#: 바닥면 아래로 손목이 내려가는 자세. **눈대중 값이 아니라** URDF 제한 안에서
#: FK로 찾은 값이다(`floor_dip_pose`). 제한 경계는 피한다(계획 거부 방지).
FLOOR_DIP_MARGIN_RAD = 0.15
#: 검증용 바닥판 윗면 높이(m). base_link 바닥면 바로 아래에 둔다.
FLOOR_TOP_Z_M = -0.005
FLOOR_THICKNESS_M = 0.02
#: 손목을 감싸는 검증용 상자 한 변(m).
BOX_SIZE_M = 0.12


def urdf_limits(urdf_path: Path) -> dict[str, tuple[float, float]]:
    joints, _ = parse_urdf(urdf_path)
    return {name: (limit["lower"], limit["upper"])
            for name, limit in revolute_limits(joints).items()}


def floor_dip_pose(urdf_path: Path, *, grid: int = 7) -> tuple[dict, float]:
    """손목이 가장 낮아지는 자세를 URDF 제한 안에서 찾는다(경계는 피한다)."""
    import itertools

    joints, _ = parse_urdf(urdf_path)
    limits = revolute_limits(joints)
    swept = ["j2", "j3", "j4", "j5"]
    grids = []
    for name in swept:
        low = limits[name]["lower"] + FLOOR_DIP_MARGIN_RAD
        high = limits[name]["upper"] - FLOOR_DIP_MARGIN_RAD
        grids.append([low + (high - low) * i / (grid - 1) for i in range(grid)])
    best = (math.inf, None)
    wrists = ["wrist1_Link", "wrist2_Link", "wrist3_Link"]
    for combo in itertools.product(*grids):
        angles = dict(zip(swept, combo))
        transforms = link_transforms(joints, angles)
        lowest = min(float(transforms[link][1][2]) for link in wrists)
        if lowest < best[0]:
            best = (lowest, dict(HOME, **angles))
    return best[1], round(best[0], 5)


def link_position(urdf_path: Path, angles: dict, link: str) -> tuple[float, ...]:
    joints, _ = parse_urdf(urdf_path)
    transforms = link_transforms(joints, angles)
    return tuple(round(float(v), 6) for v in transforms[link][1])


class MoveItVerifier(Node):
    def __init__(self) -> None:
        super().__init__("forstick2_fr3_moveit_verifier")
        self.states: list[dict] = []
        self.create_subscription(JointState, "/joint_states", self._on_state, 20)
        self.plan_srv = self.create_client(GetMotionPlan, "/plan_kinematic_path")
        self.scene_srv = self.create_client(GetPlanningScene, "/get_planning_scene")
        self.apply_srv = self.create_client(ApplyPlanningScene,
                                            "/apply_planning_scene")
        self.execute = ActionClient(self, ExecuteTrajectory, "/execute_trajectory")
        self.traj = ActionClient(
            self, FollowJointTrajectory,
            "/arm_trajectory_controller/follow_joint_trajectory")
        # MoveIt의 실행 중지 통로. MoveGroupInterface::stop()과 같은 방법이다.
        self.stop_pub = self.create_publisher(String, "/trajectory_execution_event", 10)

    def publish_stop(self) -> float:
        message = String()
        message.data = "stop"
        self.stop_pub.publish(message)
        return time.time()

    def _on_state(self, message: JointState) -> None:
        index = {name: i for i, name in enumerate(message.name)}
        if not all(joint in index for joint in JOINTS):
            return
        self.states.append({
            "sim_at": message.header.stamp.sec + message.header.stamp.nanosec * 1e-9,
            "wall": time.time(),
            "position": {j: message.position[index[j]] for j in JOINTS},
            "velocity": {j: (message.velocity[index[j]]
                             if index[j] < len(message.velocity) else 0.0)
                         for j in JOINTS},
        })

    def spin(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.05)

    def latest(self) -> dict | None:
        return self.states[-1] if self.states else None

    def call(self, client, request, timeout: float = 20.0):
        future = client.call_async(request)
        deadline = time.monotonic() + timeout
        while not future.done():
            if time.monotonic() > deadline:
                raise TimeoutError(f"{client.srv_name} 응답 없음")
            rclpy.spin_once(self, timeout_sec=0.05)
        return future.result()

    # ---- 계획 ----
    def plan_to(self, goal: dict, *, allowed_time: float = 5.0,
                attempts: int = 1, planner_id: str = "",
                velocity_scaling: float = 0.2) -> dict:
        request = MotionPlanRequest()
        request.group_name = GROUP
        request.planner_id = planner_id
        # 시작 상태를 관측값으로 명시한다. 비워 두면 move_group이
        # "Found empty JointState message"를 남기고 현재 상태를 추정한다.
        sample = self.latest()
        if sample is not None:
            start = RobotState()
            start.joint_state.name = list(JOINTS)
            start.joint_state.position = [float(sample["position"][j])
                                          for j in JOINTS]
            start.is_diff = True
            request.start_state = start
        request.allowed_planning_time = allowed_time
        request.num_planning_attempts = attempts
        request.max_velocity_scaling_factor = velocity_scaling
        request.max_acceleration_scaling_factor = velocity_scaling
        workspace = WorkspaceParameters()
        workspace.header.frame_id = "base_link"
        workspace.min_corner.x = workspace.min_corner.y = -2.0
        workspace.min_corner.z = -2.0
        workspace.max_corner.x = workspace.max_corner.y = 2.0
        workspace.max_corner.z = 2.0
        request.workspace_parameters = workspace
        constraints = Constraints()
        for name, value in goal.items():
            jc = JointConstraint()
            jc.joint_name = name
            jc.position = float(value)
            jc.tolerance_above = jc.tolerance_below = 0.001
            jc.weight = 1.0
            constraints.joint_constraints.append(jc)
        request.goal_constraints.append(constraints)
        srv = GetMotionPlan.Request()
        srv.motion_plan_request = request
        started = time.time()
        response = self.call(self.plan_srv, srv, timeout=max(30.0, allowed_time * 4))
        result = response.motion_plan_response
        points = result.trajectory.joint_trajectory.points
        return {
            "error_code": int(result.error_code.val),
            "planning_time_sec": round(float(result.planning_time), 4),
            "wall_sec": round(time.time() - started, 4),
            "point_count": len(points),
            "trajectory": result.trajectory,
            "final_point": (dict(zip(result.trajectory.joint_trajectory.joint_names,
                                     points[-1].positions)) if points else None),
        }

    def execute_plan(self, trajectory, *, cancel_after: float | None = None,
                     stop_after: float | None = None) -> dict:
        if not self.execute.wait_for_server(timeout_sec=20.0):
            return {"accepted": False, "detail": "execute_trajectory 서버 없음"}
        goal = ExecuteTrajectory.Goal()
        goal.trajectory = trajectory
        send = self.execute.send_goal_async(goal)
        deadline = time.monotonic() + 20.0
        while not send.done():
            if time.monotonic() > deadline:
                return {"accepted": False, "detail": "goal 응답 없음"}
            rclpy.spin_once(self, timeout_sec=0.05)
        handle = send.result()
        if not handle.accepted:
            return {"accepted": False, "detail": "goal 거부"}
        cancel_at = None if cancel_after is None else time.monotonic() + cancel_after
        stop_at = None if stop_after is None else time.monotonic() + stop_after
        result_future = handle.get_result_async()
        cancel_ack = None
        stop_sent_at = None
        deadline = time.monotonic() + 60.0
        while not result_future.done():
            if stop_at is not None and time.monotonic() >= stop_at:
                stop_at = None
                stop_sent_at = self.publish_stop()
            if cancel_at is not None and time.monotonic() >= cancel_at:
                cancel_at = None
                cancel_future = handle.cancel_goal_async()
                cancel_sent = time.monotonic()
                while not cancel_future.done():
                    rclpy.spin_once(self, timeout_sec=0.02)
                cancel_ack = {
                    "ack_sec": round(time.monotonic() - cancel_sent, 4),
                    "goals_canceling": len(cancel_future.result().goals_canceling),
                }
            if time.monotonic() > deadline:
                return {"accepted": True, "detail": "결과 대기 초과",
                        "cancel_ack": cancel_ack, "stop_sent_at": stop_sent_at}
            rclpy.spin_once(self, timeout_sec=0.05)
        result = result_future.result()
        return {
            "accepted": True,
            "status": int(result.status),
            "error_code": int(result.result.error_code.val),
            "cancel_ack": cancel_ack,
            "stop_sent_at": stop_sent_at,
            "result_at": time.time(),
        }

    # ---- scene 조작 ----
    def apply_scene_diff(self, scene: PlanningScene) -> bool:
        request = ApplyPlanningScene.Request()
        request.scene = scene
        response = self.call(self.apply_srv, request, timeout=20.0)
        return bool(response.success)

    def add_floor(self, object_id: str, *, z: float, size=(1.2, 1.2, 0.02)) -> bool:
        obj = CollisionObject()
        obj.header.frame_id = "base_link"
        obj.id = object_id
        primitive = SolidPrimitive()
        primitive.type = SolidPrimitive.BOX
        primitive.dimensions = list(size)
        obj.primitives.append(primitive)
        pose = Pose()
        pose.position.z = z
        pose.orientation.w = 1.0
        obj.primitive_poses.append(pose)
        obj.operation = CollisionObject.ADD
        scene = PlanningScene()
        scene.is_diff = True
        scene.world.collision_objects.append(obj)
        return self.apply_scene_diff(scene)

    def add_box(self, object_id: str, *, xyz, size) -> bool:
        obj = CollisionObject()
        obj.header.frame_id = "base_link"
        obj.id = object_id
        primitive = SolidPrimitive()
        primitive.type = SolidPrimitive.BOX
        primitive.dimensions = list(size)
        obj.primitives.append(primitive)
        pose = Pose()
        pose.position.x, pose.position.y, pose.position.z = (float(v) for v in xyz)
        pose.orientation.w = 1.0
        obj.primitive_poses.append(pose)
        obj.operation = CollisionObject.ADD
        scene = PlanningScene()
        scene.is_diff = True
        scene.world.collision_objects.append(obj)
        return self.apply_scene_diff(scene)

    def remove_object(self, object_id: str) -> bool:
        obj = CollisionObject()
        obj.header.frame_id = "base_link"
        obj.id = object_id
        obj.operation = CollisionObject.REMOVE
        scene = PlanningScene()
        scene.is_diff = True
        scene.world.collision_objects.append(obj)
        return self.apply_scene_diff(scene)

    def acm(self) -> AllowedCollisionMatrix:
        request = GetPlanningScene.Request()
        request.components.components = (
            PlanningSceneComponents.ALLOWED_COLLISION_MATRIX)
        return self.call(self.scene_srv, request).scene.allowed_collision_matrix

    def apply_acm(self, matrix: AllowedCollisionMatrix) -> bool:
        scene = PlanningScene()
        scene.is_diff = True
        scene.allowed_collision_matrix = matrix
        return self.apply_scene_diff(scene)


def observed_error(verifier: MoveItVerifier, target: dict) -> dict:
    sample = verifier.latest()
    if sample is None:
        return {"observed": None, "reached": False,
                "detail": "joint_states 표본이 없다"}
    errors = {j: abs(sample["position"][j] - target[j]) for j in target}
    age = time.time() - sample["wall"]
    return {
        "observed": {j: round(sample["position"][j], 5) for j in JOINTS},
        "max_error_rad": round(max(errors.values()), 5),
        "per_joint_error_rad": {j: round(v, 5) for j, v in errors.items()},
        "observation_age_sec": round(age, 4),
        "fresh": age <= max(STATE_MAX_AGE_SEC, 1.0),
        # 관측값이 허용치 안에 들어와야 도달이다. 컨트롤러 결과는 근거가 아니다.
        "reached": max(errors.values()) <= POSITION_TOLERANCE,
    }


def stop_confirmed(verifier: MoveItVerifier, samples: int = STOP_SAMPLES) -> dict:
    """관측 속도로 정지를 확인한다. 명령을 보냈다는 사실로 판정하지 않는다."""
    verifier.states.clear()
    verifier.spin(2.0)
    recent = verifier.states[-samples:]
    if len(recent) < samples:
        return {"stopped": False, "samples": len(recent),
                "detail": "표본 부족 — 정지를 확인할 수 없다"}
    peak = max(max(abs(v) for v in s["velocity"].values()) for s in recent)
    return {
        "stopped": peak < STOP_VELOCITY_TOLERANCE,
        "samples": len(recent),
        "max_abs_velocity_rad_s": round(peak, 6),
        "tolerance_rad_s": STOP_VELOCITY_TOLERANCE,
    }


def geometry_plan(steps: tuple[str, ...]) -> TaskPlan:
    return TaskPlan(
        "plan_moveit_verify", "fr3wms_arm", "fr3wms_arm_urdf", "0.3.0-urdf-reach-defined",
        tuple(TaskStep("move", {"to": name}) for name in steps),
        created_at=time.time() - 1.0, ttl_sec=600.0,
    )


def geometry_check(validator, client, targets: dict[str, dict],
                   *, snapshot=None) -> dict:
    """GeometryValidator를 MoveIt scene에 붙여 판정을 받는다."""
    plan = geometry_plan(tuple(targets))
    environment = (snapshot or client.snapshot()).to_environment()
    request = GeometryRequest(
        plan=plan, plan_hash=plan.plan_hash(), robot_id="fr3wms_arm",
        profile_id="fr3wms_arm_urdf", profile_version="0.3.0-urdf-reach-defined",
        frame_id=environment.frame_id, checked_at=time.time(),
        snapshot=environment, timeout_sec=20.0,
        resolved_motion={i: joint_motion(joints)
                         for i, joints in enumerate(targets.values(), start=1)},
    )
    verdict = check_geometry(validator, request, now=request.checked_at)
    return {
        "decision": verdict.decision.value,
        "reason_codes": [r.value for r in verdict.reason_codes()],
        "reason_details": [r.detail for r in verdict.reasons],
        "input_complete": verdict.input_complete,
        "snapshot_id": verdict.snapshot_id,
        "snapshot_version": verdict.snapshot_version,
        "snapshot_hash": verdict.snapshot_hash,
        "frame_id": verdict.frame_id,
        "checked_at": request.checked_at,
        "evidence": dict(verdict.evidence),
        "validator_id": verdict.validator_id,
        "validator_version": verdict.validator_version,
    }


def main() -> int:
    urdf_path = LOG_DIR / "fr3wms_arm.moveit.urdf"
    srdf_path = LOG_DIR / "fr3wms_arm.srdf"
    if not urdf_path.is_file() or not srdf_path.is_file():
        print("생성물이 없다. scripts/run_moveit_fr3.sh를 먼저 실행한다.",
              file=sys.stderr)
        return 2
    model_hash = robot_model_hash(urdf_path.read_text(encoding="utf-8"),
                                  srdf_path.read_text(encoding="utf-8"))
    limits = urdf_limits(urdf_path)

    rclpy.init()
    verifier = MoveItVerifier()
    results: dict[str, dict] = {}
    try:
        client = RosPlanningSceneClient(
            verifier, group_name=GROUP, frame_id="base_link", joint_limits=limits,
            model_hash=model_hash, ttl_sec=SNAPSHOT_TTL_SEC,
            source="moveit planning scene (Gazebo 시뮬레이션)",
        )
        validator = MoveItGeometryValidator(client)
        if not client.wait(45.0):
            print("move_group 서비스가 없다", file=sys.stderr)
            return 3
        verifier.spin(3.0)

        # 1) 현재 상태 수신
        sample = verifier.latest()
        snapshot = client.snapshot()
        results["01_current_state"] = {
            "joint_states_received": len(verifier.states),
            "latest": None if sample is None else {
                "position": {j: round(sample["position"][j], 5) for j in JOINTS},
                "age_sec": round(time.time() - sample["wall"], 4),
            },
            "scene_snapshot": snapshot.to_dict(),
            "state_max_age_sec": STATE_MAX_AGE_SEC,
            "passed": sample is not None and bool(snapshot.content_hash),
        }

        # 2) home 계획 + 3) 안전 자세 계획
        home_plan = verifier.plan_to(HOME)
        results["02_plan_home"] = {
            k: v for k, v in home_plan.items() if k != "trajectory"}
        results["02_plan_home"]["passed"] = home_plan["error_code"] == 1

        # home으로 먼저 이동해 알려진 시작점에서 검증한다.
        if home_plan["error_code"] == 1 and home_plan["point_count"]:
            verifier.execute_plan(home_plan["trajectory"])
            verifier.spin(2.0)
        safe_plan = verifier.plan_to(SAFE_POSE)
        results["03_plan_safe_pose"] = {
            k: v for k, v in safe_plan.items() if k != "trajectory"}
        results["03_plan_safe_pose"]["passed"] = safe_plan["error_code"] == 1

        # 4) 궤적 실행 + 5) 목표 vs 관측 비교
        execution = verifier.execute_plan(safe_plan["trajectory"])
        verifier.spin(2.0)
        observation = observed_error(verifier, SAFE_POSE)
        results["04_execute_trajectory"] = {
            "target": {j: round(v, 5) for j, v in SAFE_POSE.items()},
            "command_accepted": execution.get("accepted", False),
            "moveit_result": {k: v for k, v in execution.items()
                              if k != "cancel_ack"},
            "passed": execution.get("accepted", False),
            "note": "명령 수락까지의 판정이다. 도달은 05에서 관측으로 본다",
        }
        results["05_target_vs_observed"] = dict(
            observation, target={j: round(v, 5) for j, v in SAFE_POSE.items()},
            tolerance_rad=POSITION_TOLERANCE, passed=observation["reached"])

        # 6) 관절 제한 위반 차단
        #    **MoveIt은 제한 밖 목표를 거부하지 않고 제한값으로 잘라서 계획한다**
        #    (실측: j1=3.5 요청 → 최종점 3.0543). 그래서 차단은 우리 게이트가
        #    한다. 입력을 범위 안으로 잘라 통과시키는 동작에 의존하지 않는다.
        limit_plan = verifier.plan_to(OUT_OF_LIMIT)
        limit_geometry = geometry_check(validator, client,
                                       {"out_of_limit": OUT_OF_LIMIT})
        clamped = None
        if limit_plan["final_point"]:
            clamped = {
                name: round(float(value), 6)
                for name, value in limit_plan["final_point"].items()
            }
        results["06_joint_limit_blocked"] = {
            "goal": OUT_OF_LIMIT, "urdf_limit_j1": limits["j1"],
            "moveit_plan_error_code": limit_plan["error_code"],
            "moveit_accepted_goal": limit_plan["error_code"] == 1,
            "moveit_final_point": clamped,
            "moveit_clamped_goal": bool(
                clamped and abs(clamped["j1"] - limits["j1"][1]) < 1e-6
                and abs(OUT_OF_LIMIT["j1"] - limits["j1"][1]) > 1e-6),
            "geometry": limit_geometry,
            # 판정 기준은 **우리 게이트가 차단했는가**다.
            "passed": (limit_geometry["decision"] == "block"
                       and ReasonCode.GEOMETRY_WORKSPACE_VIOLATION.value
                       in limit_geometry["reason_codes"]),
            "finding": ("MoveIt이 제한 밖 목표를 거부하지 않고 제한값으로"
                        " 잘라(clamp) 계획했다. 계획 성공을 제한 준수의 근거로"
                        " 쓸 수 없다 — GeometryValidator가 차단해야 한다"),
        }

        # 7) 자기충돌 차단
        #    독립 검토(reports/moveit/self_collision_review.json)가 찾은
        #    **실제 근접 자세**로 검사한다. collisions_updater가 "Never"로
        #    비활성화한 쌍 중 12쌍이 0.2 mm 안까지 접근한다 — 그래서
        #    prune_self_collision_srdf.py가 그 쌍을 검사 대상으로 되살린다.
        #    **계획을 통과시키려고 충돌 링크를 제외하지 않는다.**
        review_path = ROOT / "reports/moveit/self_collision_review.json"
        prune_path = LOG_DIR / "srdf_prune.json"
        review = (json.loads(review_path.read_text(encoding="utf-8"))
                  if review_path.is_file() else None)
        prune = (json.loads(prune_path.read_text(encoding="utf-8"))
                 if prune_path.is_file() else None)
        matrix = verifier.acm()
        disabled_now = set()
        names = list(matrix.entry_names)
        for i, name in enumerate(names):
            values = (matrix.entry_values[i].enabled
                      if i < len(matrix.entry_values) else [])
            for j, allowed in enumerate(values):
                if allowed and j > i:
                    disabled_now.add(frozenset((name, names[j])))
        close_pairs = [] if review is None else review["pairs_within_threshold"]
        collision_code = ReasonCode.GEOMETRY_COLLISION.value
        checks = []
        first_verdict = None
        for entry in close_pairs[:3]:
            pose = dict(HOME, **{k: v for k, v in entry["angles_rad"].items()
                                 if k in HOME})
            verdict = geometry_check(validator, client,
                                     {"self_collision": pose})
            first_verdict = first_verdict or verdict
            validity = client.check_state(pose)
            checks.append({
                "pair": entry["pair"],
                "reviewed_min_distance_m": entry["min_distance_m"],
                "pair_still_disabled_in_acm":
                    frozenset(entry["pair"]) in disabled_now,
                "pose_rad": {j: round(v, 4) for j, v in pose.items()},
                "moveit_state_valid": validity.valid,
                "contacts": [list(c) for c in validity.contacts],
                "geometry_decision": verdict["decision"],
                "geometry_reason_codes": verdict["reason_codes"],
                "blocked": (verdict["decision"] == "block"
                            and collision_code in verdict["reason_codes"]),
            })
        results["07_self_collision"] = {
            "review_report": "reports/moveit/self_collision_review.json",
            "review_conclusion": None if review is None else review["conclusion"],
            "matrix_generated_pairs": (
                None if prune is None else len(prune["kept_disabled"])
                + len(prune["restored_to_checked"])),
            "matrix_kept_disabled": None if prune is None else [
                k["pair"] for k in prune["kept_disabled"]],
            "matrix_restored_to_checked": None if prune is None else [
                k["pair"] for k in prune["restored_to_checked"]],
            "close_pose_checks": checks,
            # 기록용: 첫 근접 자세의 기하 판정 전체(scene 식별자 포함).
            "geometry": first_verdict,
            "passed": bool(checks) and all(c["blocked"] for c in checks),
            "finding": ("collisions_updater가 'Never'로 비활성화한 15쌍 중"
                        " 12쌍이 관절 제한 안에서 0.2 mm 안까지 접근한다"
                        " (표본 10만 개가 놓쳤다). 자동 생성 행렬을 그대로"
                        " 쓰면 실제 자기충돌을 검사하지 않게 된다"),
        }

        # 8) 바닥·환경 충돌 차단
        #    (a) 바닥판: home에서는 충돌이 없어야 하고, FK로 찾은 침범 자세에서는
        #        충돌해야 한다. (b) 손목 위치에 놓은 상자: 그 자세만 충돌해야 한다.
        dip_pose, dip_z = floor_dip_pose(urdf_path)
        floor_id = "verify_floor_plate"
        box_id = "verify_wrist_box"
        for name in (floor_id, box_id):
            verifier.remove_object(name)
        verifier.spin(0.5)
        before_fixture = geometry_check(validator, client, {"floor_dip": dip_pose})
        floor_added = verifier.add_floor(
            floor_id, z=FLOOR_TOP_Z_M - FLOOR_THICKNESS_M / 2,
            size=(1.2, 1.2, FLOOR_THICKNESS_M))
        verifier.spin(1.0)
        floor_dip_check = geometry_check(validator, client, {"floor_dip": dip_pose})
        floor_home_check = geometry_check(validator, client, {"home": HOME})
        dip_plan = verifier.plan_to(dip_pose, allowed_time=3.0)
        verifier.remove_object(floor_id)
        verifier.spin(0.5)

        wrist_xyz = link_position(urdf_path, SAFE_POSE, "wrist3_Link")
        box_added = verifier.add_box(box_id, xyz=wrist_xyz,
                                     size=(BOX_SIZE_M,) * 3)
        verifier.spin(1.0)
        box_safe_check = geometry_check(validator, client, {"safe": SAFE_POSE})
        box_home_check = geometry_check(validator, client, {"home": HOME})
        verifier.remove_object(box_id)
        verifier.spin(0.5)
        collision_code = ReasonCode.GEOMETRY_COLLISION.value
        results["08_environment_collision_blocked"] = {
            "fixtures": [
                {"id": floor_id, "shape": "box",
                 "size_m": [1.2, 1.2, FLOOR_THICKNESS_M],
                 "top_z_m": FLOOR_TOP_Z_M, "added": floor_added,
                 "purpose": "바닥면. 검증 전용 world fixture다"},
                {"id": box_id, "shape": "box", "size_m": [BOX_SIZE_M] * 3,
                 "center_xyz_m": list(wrist_xyz), "added": box_added,
                 "purpose": "안전 자세의 wrist3 위치(FK 계산)에 놓은 장애물"},
            ],
            "floor_dip_pose": {j: round(v, 4) for j, v in dip_pose.items()},
            "floor_dip_lowest_wrist_z_m": dip_z,
            "geometry_dip_without_floor": before_fixture,
            "geometry_dip_with_floor": floor_dip_check,
            "geometry_home_with_floor": floor_home_check,
            "moveit_plan_error_code_dip_with_floor": dip_plan["error_code"],
            "geometry_safe_with_box": box_safe_check,
            "geometry_home_with_box": box_home_check,
            "passed": (
                floor_added and box_added
                and floor_dip_check["decision"] == "block"
                and collision_code in floor_dip_check["reason_codes"]
                and floor_home_check["decision"] == "allow"
                and box_safe_check["decision"] == "block"
                and collision_code in box_safe_check["reason_codes"]
                and box_home_check["decision"] == "allow"),
            "note": ("거짓 양성도 확인한다 — 같은 fixture에서 home은 통과해야"
                     " 한다. 충돌이 나는 링크를 제외해 통과시키지 않는다"),
        }

        # 9) 계획 제한시간
        #    쉬운 문제는 0.001초 예산에서도 성공한다(실측). 예산을 더 줄이면
        #    OMPL이 TIMED_OUT으로 실패한다. **서비스 응답의 error_code는
        #    99999(FAILURE)로 뭉개져 오므로**, 구분된 이유는 move_group 로그
        #    관측으로 남긴다. 판정 기준은 "궤적 없이 실패했는가"다 —
        #    제한시간 초과가 조용히 성공으로 바뀌지 않아야 한다.
        log_path = LOG_DIR / "move_group.log"
        offset = log_path.stat().st_size if log_path.is_file() else 0
        budget_results = []
        for budget in (1e-5, 1e-4, 1e-3):
            attempt = verifier.plan_to(SAFE_POSE, allowed_time=budget)
            budget_results.append({
                "allowed_planning_time_sec": budget,
                "error_code": attempt["error_code"],
                "planning_time_sec": attempt["planning_time_sec"],
                "point_count": attempt["point_count"],
            })
        appended = ""
        if log_path.is_file():
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                appended = handle.read()
        timed_out_lines = [line for line in appended.splitlines()
                           if "TIMED_OUT" in line]
        failed_without_trajectory = [
            r for r in budget_results
            if r["error_code"] != 1 and r["point_count"] == 0]
        results["09_planning_timeout"] = {
            "attempts": budget_results,
            "moveit_log_timed_out_lines": timed_out_lines[:5],
            "service_error_code_is_generic_failure": all(
                r["error_code"] == 99999 for r in failed_without_trajectory),
            "passed": (len(failed_without_trajectory) == len(budget_results)
                       and bool(timed_out_lines)),
            "finding": ("MoveIt 서비스 응답은 제한시간 초과를 TIMED_OUT(-6)이"
                        " 아니라 99999(FAILURE)로 돌려준다. 구분된 이유는"
                        " planner 로그에만 있다 — 응답 코드로 원인을 판단하지"
                        " 않고, 궤적 부재를 실패 근거로 쓴다"),
        }

        # 10) 계획 중 snapshot 변경 → 판정이 환경에 붙어 있어야 한다
        snapshot_before = client.snapshot()
        change_id = "verify_midplan_fixture"
        holder: dict[str, object] = {}

        def change_scene() -> None:
            time.sleep(0.2)
            holder["added"] = verifier.add_floor(change_id, z=0.9,
                                                 size=(0.1, 0.1, 0.1))

        thread = threading.Thread(target=change_scene)
        thread.start()
        mid_plan = verifier.plan_to(SAFE_POSE, allowed_time=3.0)
        thread.join()
        verifier.spin(1.0)
        stale = geometry_check(validator, client, {"safe": SAFE_POSE},
                               snapshot=snapshot_before)
        snapshot_after = client.snapshot()
        verifier.remove_object(change_id)
        verifier.spin(0.5)
        results["10_snapshot_change_during_planning"] = {
            "fixture_added_midplan": holder.get("added"),
            "snapshot_before": snapshot_before.to_dict(),
            "snapshot_after": snapshot_after.to_dict(),
            "hash_changed": snapshot_before.content_hash != snapshot_after.content_hash,
            "plan_error_code": mid_plan["error_code"],
            "geometry_with_stale_snapshot": stale,
            "passed": (snapshot_before.content_hash != snapshot_after.content_hash
                       and stale["decision"] == "ask"
                       and ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE.value
                       in stale["reason_codes"]),
        }

        # 11) 이동 중 STOP
        #    MoveIt의 실행 중지는 `/trajectory_execution_event`에 "stop"을
        #    발행하는 경로다(MoveGroupInterface::stop()과 같다). 액션 취소
        #    (cancel_goal)는 실행이 끝날 때까지 응답이 오지 않았다(실측:
        #    ack 31초, goals_canceling 0) — 그 관측도 함께 남긴다.
        home_again = verifier.plan_to(HOME)
        if home_again["error_code"] == 1 and home_again["point_count"]:
            verifier.execute_plan(home_again["trajectory"])
            verifier.spin(3.0)
        long_plan = verifier.plan_to(SAFE_POSE, velocity_scaling=0.02)
        duration = None
        if long_plan["point_count"]:
            last = long_plan["trajectory"].joint_trajectory.points[-1]
            duration = round(last.time_from_start.sec
                             + last.time_from_start.nanosec * 1e-9, 3)
        stopped = verifier.execute_plan(long_plan["trajectory"], stop_after=2.0)
        stop_check = stop_confirmed(verifier)
        stop_sample = verifier.latest()
        not_reached = (
            None if stop_sample is None else
            max(abs(stop_sample["position"][j] - SAFE_POSE[j]) for j in JOINTS)
            > POSITION_TOLERANCE)
        results["11_stop_during_motion"] = {
            "trajectory_duration_sec": duration,
            "velocity_scaling": 0.02,
            "stop_after_sec": 2.0,
            "stop_channel": "/trajectory_execution_event (data=stop)",
            "moveit_result": {k: v for k, v in stopped.items()
                              if k != "cancel_ack"},
            "stop_latency_sec": (
                None if not stopped.get("stop_sent_at")
                or not stopped.get("result_at") else
                round(stopped["result_at"] - stopped["stop_sent_at"], 3)),
            "preempted": stopped.get("error_code") == -7,
            "stop_observation": stop_check,
            "position_at_stop": (None if stop_sample is None else
                                 {j: round(stop_sample["position"][j], 5)
                                  for j in JOINTS}),
            "target_not_reached": not_reached,
            # 관측 속도가 0이고 **목표에 도달하지 않았어야** 이동 중 정지다.
            "passed": (stopped.get("error_code") == -7
                       and stop_check.get("stopped", False)
                       and bool(not_reached)),
        }

        # 12) STOP 후 새 계획 재검증
        after_stop_snapshot = client.snapshot()
        replan = verifier.plan_to(HOME)
        revalidation = geometry_check(validator, client, {"home": HOME})
        start_state = verifier.latest()
        results["12_revalidation_after_stop"] = {
            "replan_error_code": replan["error_code"],
            "replan_point_count": replan["point_count"],
            "start_from_observed_state": (
                None if start_state is None or not replan["point_count"] else
                max(abs(replan["trajectory"].joint_trajectory.points[0].positions[i]
                        - start_state["position"][name])
                    for i, name in enumerate(
                        replan["trajectory"].joint_trajectory.joint_names)) <= 0.05),
            "snapshot_after_stop": after_stop_snapshot.to_dict(),
            "geometry": revalidation,
            "passed": (replan["error_code"] == 1
                       and revalidation["decision"] == "allow"),
        }
        if replan["error_code"] == 1 and replan["point_count"]:
            verifier.execute_plan(replan["trajectory"])
            verifier.spin(2.0)

        report = {
            "recorded_at": time.time(),
            "environment": {
                "group": GROUP, "joints": JOINTS,
                "urdf": str(urdf_path), "srdf": str(srdf_path),
                "urdf_sha256": hashlib.sha256(urdf_path.read_bytes()).hexdigest(),
                "srdf_sha256": hashlib.sha256(srdf_path.read_bytes()).hexdigest(),
                "robot_model_hash": model_hash,
                "moveit_config_dir": "config/moveit",
                "is_simulated": True, "arm_only": True,
            },
            "checks": results,
            "passed_count": sum(1 for v in results.values() if v.get("passed")),
            "total_count": len(results),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        verifier.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
