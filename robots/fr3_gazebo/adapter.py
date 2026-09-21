"""FR3 Gazebo 작업 셀 Adapter (md/개발플랜.md 8-08 우선순위 6).

`텍스트/음성 명령 → 계획 → 안전 판단 → 사용자 실행 시작` 흐름의 마지막 칸이다.
Fake Adapter를 대신해 **실제 Gazebo 작업 셀**에 명령을 보낸다.

지키는 것:
- 실행 직전에 world·컨트롤러·관측 신선도·planning scene을 **다시** 확인한다.
  하나라도 어긋나면 이유 코드를 붙여 거부한다.
- 자세 값을 만들지 않는다. `config/workcell/fr3_2f85_workcell_poses.json`의
  **status=verified** 자세만 쓴다.
- pick·place는 실행하지 않는다. 자원·자세 해석까지 하고
  `capability.profile_incomplete`로 거부한다.
- stop()은 요청 접수만으로 성공을 주장하지 않는다. confirm_stopped()가
  관측 속도로 확인한 뒤에만 STOPPED가 된다.
- 실제 하드웨어가 아니다. 모든 근거에 `is_simulated=True`를 담는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from core.capability_profile import CapabilityProfile
from core.execution_result import ExecutionResult, rejected, success, unverifiable
from core.execution_state import ExecutionState
from core.reason_codes import ReasonCode
from robots.base.robot_adapter import RobotAdapter, RobotStateSnapshot
from robots.fr3_gazebo.transport import (
    GoalOutcome,
    JointObservation,
    SceneCheck,
    WorkcellTransport,
    WorldStatus,
)

ARM_JOINTS = ("j1", "j2", "j3", "j4", "j5", "j6")
GRIPPER_JOINT = "robotiq_85_left_knuckle_joint"
#: 실행 직전에 활성이어야 하는 컨트롤러. 하나라도 비활성이면 실행을 막는다.
REQUIRED_CONTROLLERS = (
    "joint_state_broadcaster",
    "arm_trajectory_controller",
    "gripper_trajectory_controller",
)
#: 관절 도달 판정 허용치(rad). 컨트롤러 goal_tolerance와 같은 근거를 쓴다.
ARM_TOLERANCE_RAD = 0.05
#: 정지 판정 변위 허용치(rad). 측정된 정지 상태 잡음 바닥 49.8 µrad의 20배이고
#: 컨트롤러 goal_tolerance(0.02 rad)보다 20배 엄격하다.
#: 근거: reports/workcell/stop_observation.json
STOP_DISPLACEMENT_RAD = 0.001
#: 속도 허용치 초과를 운동으로 볼 최소 연속 표본 수. 고립 1표본 스파이크는
#: 위치가 정지한 상태에서도 나온다(같은 근거 파일).
CONSECUTIVE_EXCEED_FOR_MOTION = 2


class WorkcellConfigError(Exception):
    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


@dataclass(frozen=True)
class WorkcellResources:
    """자원 id → Gazebo 모델·프레임·자세 이름의 대조표.

    발화에 등장한 자원 id를 **실제 씬의 무엇**으로 옮기는 유일한 경로다.
    여기 없는 자원은 만들지 않는다.
    """

    workcell_id: str
    workcell_version: str
    world_name: str
    gz_partition: str
    ros_domain_id: int
    #: resource_id -> {"gazebo_model", "frame", "korean"}
    resources: Mapping[str, Mapping[str, str]]
    #: resource_id -> 이동용 자세 이름(접근 자세)
    move_pose: Mapping[str, str]
    #: resource_id -> pick 자세 이름(있으면)
    pick_pose: Mapping[str, str]
    #: resource_id -> place 자세 이름(있으면)
    place_pose: Mapping[str, str]
    #: 자세 이름 -> {관절명: rad}. status=verified인 자세만 담는다.
    poses: Mapping[str, Mapping[str, float]]
    #: 자세 이름 -> 유도·검증 근거 요약.
    pose_evidence: Mapping[str, Mapping[str, object]]
    safe_home_pose: str
    #: 관측 신선도 상한(초).
    state_max_age_sec: float
    #: 그리퍼 명령 관절값 -> 개구(m) 모델. 없으면 **개구를 주장하지 않는다.**
    aperture_model: object | None = None
    #: 물체 resource_id -> 측정된 파지 자세 이름(8-10). 없으면 비어 있다.
    grasp_pose: Mapping[str, str] = field(default_factory=dict)
    #: 물체 resource_id -> **선언된** 받침 위치 resource_id(프레임 부모 관계).
    object_support: Mapping[str, str] = field(default_factory=dict)
    #: pick/place 계획 검증에 필요한 파지 선언. 근거 파일이 없으면 비어 있다.
    grasp_declaration: Mapping[str, object] = field(default_factory=dict)

    def scene_mapping(self, resource_id: str) -> Mapping[str, str] | None:
        return self.resources.get(resource_id)


def load_workcell_resources(
    workcell_path: Path, poses_path: Path, *, state_max_age_sec: float,
    mounting_path: Path | None = None, grasp_path: Path | None = None,
) -> WorkcellResources:
    """설정과 유도 결과에서 대조표를 만든다. **값을 보충하지 않는다.**"""
    workcell = json.loads(Path(workcell_path).read_text(encoding="utf-8"))
    derived = json.loads(Path(poses_path).read_text(encoding="utf-8"))

    home = derived.get("safe_home", {})
    if home.get("status") != "verified":
        raise WorkcellConfigError(
            ReasonCode.GEOMETRY_COLLISION,
            f"안전 home이 verified가 아니다({home.get('status')}) —"
            " 검증되지 않은 자세를 실행 경로에 두지 않는다")

    poses: dict[str, Mapping[str, float]] = {
        "workcell_safe_home": dict(home["joint_rad"])}
    evidence: dict[str, Mapping[str, object]] = {
        "workcell_safe_home": {
            "status": "verified",
            "floor_clearance_m": home.get("floor_clearance_m"),
            "environment_clearance_min_m": home.get("environment_clearance_min_m"),
            "measurement_method": home.get("measurement_method"),
            "source": home.get("source"),
        }
    }
    for name, pose in derived.get("poses", {}).items():
        if pose.get("status") != "verified":
            continue
        poses[name] = dict(pose["joint_rad"])
        evidence[name] = {
            "status": "verified",
            "target_frame": pose.get("target_frame"),
            "target_world_xyz_m": pose.get("target_world_xyz_m"),
            "position_error_m": pose.get("position_error_m"),
            "orientation_error_rad": pose.get("orientation_error_rad"),
            "floor_clearance_m": pose.get("floor_clearance_m"),
            "environment_clearance_min_m": pose.get("environment_clearance_min_m"),
            "measurement_method": pose.get("measurement_method"),
        }

    resources: dict[str, dict[str, str]] = {}
    for row in workcell["resource_map"]:
        resources[row["resource_id"]] = {
            "korean": row["korean"],
            "gazebo_model": row["gazebo_model"],
            "frame": row["frame"],
        }

    # 자원 → 자세는 **이름 규칙이 아니라 존재하는 자세로만** 잇는다.
    move_pose: dict[str, str] = {}
    pick_pose: dict[str, str] = {}
    place_pose: dict[str, str] = {}
    for resource_id, mapping in resources.items():
        model = mapping["gazebo_model"]
        for suffix, table in (("_approach", move_pose), ("_pick", pick_pose),
                              ("_place", place_pose)):
            candidate = f"{model}{suffix}"
            if candidate in poses:
                table[resource_id] = candidate

    # 개구 모델은 **장착 Profile의 공식 FK 표**에서만 온다. 없으면 담지 않는다.
    aperture_model = None
    if mounting_path is not None and Path(mounting_path).is_file():
        from core.aperture_model import ApertureModel

        mounting = json.loads(Path(mounting_path).read_text(encoding="utf-8"))
        table = mounting.get("transform_derivation", {}).get("aperture_table")
        if table:
            aperture_model = ApertureModel.from_rows(
                table,
                source=f"{mounting['mounting_profile_id']}"
                       f" {mounting['mounting_profile_version']}")

    # 파지 자세는 **측정 결과 파일**에서만 온다(scripts/derive_grasp_poses.py).
    # 파일이 없으면 담지 않는다 — pick/place 계획을 펼칠 자세가 없다는 뜻이다.
    grasp_pose: dict[str, str] = {}
    object_support: dict[str, str] = {}
    grasp_declaration: dict[str, object] = {}
    if grasp_path is not None and Path(grasp_path).is_file():
        grasp = json.loads(Path(grasp_path).read_text(encoding="utf-8"))
        object_support = dict(grasp.get("object_support") or {})
        declared = dict(grasp.get("declared") or {})
        gripper = dict(grasp.get("gripper") or {})
        attached: dict[str, dict] = {}
        for name, pose in (grasp.get("poses") or {}).items():
            if pose.get("status") != "verified":
                continue
            rid = pose.get("object_resource_id")
            if not rid:
                continue
            poses[name] = dict(pose["joint_rad"])
            evidence[name] = {
                "status": "verified",
                "kind": pose.get("kind"),
                "target_frame": pose.get("target_frame"),
                "target_world_xyz_m": pose.get("target_world_xyz_m"),
                "position_error_m": pose.get("position_error_m"),
                "orientation_error_rad": pose.get("orientation_error_rad"),
                "usable_dz_range_m": pose.get("usable_dz_range_m"),
                "measurement_method": pose.get("measurement_method"),
            }
            grasp_pose[rid] = name
            spec = dict(pose.get("attached_object") or {})
            if spec:
                spec["source"] = str(grasp_path)
                attached[rid] = spec
        grasp_declaration = {
            "schema": grasp.get("schema"),
            "derived_at": grasp.get("derived_at"),
            "method": grasp.get("method"),
            "gripper_joint": declared.get("gripper_command_joint"),
            "gripper_mimic": {name: tuple(values) for name, values
                              in (declared.get("gripper_mimic") or {}).items()},
            "pad_links": tuple(declared.get("pad_links") or ()),
            "pad_links_source": declared.get("pad_links_source"),
            "tcp_link": declared.get("tcp_link"),
            "gripper_open_rad": gripper.get("open_joint_rad"),
            "gripper_grasp_rad": gripper.get("grasp_joint_rad"),
            "gripper_open_source": gripper.get("open_source"),
            "gripper_grasp_evidence": gripper.get("grasp_aperture_evidence"),
            "attached": attached,
            "limitations": tuple(grasp.get("limitations") or ()),
            "source": str(grasp_path),
        }

    return WorkcellResources(
        workcell_id=workcell["workcell_id"],
        workcell_version=workcell["workcell_version"],
        world_name=workcell["world_name"],
        gz_partition=workcell["gz_partition"],
        ros_domain_id=int(workcell["ros_domain_id"]),
        resources=resources,
        move_pose=move_pose,
        pick_pose=pick_pose,
        place_pose=place_pose,
        poses=poses,
        pose_evidence=evidence,
        safe_home_pose="workcell_safe_home",
        state_max_age_sec=state_max_age_sec,
        aperture_model=aperture_model,
        grasp_pose=grasp_pose,
        object_support=object_support,
        grasp_declaration=grasp_declaration,
    )


def transport_live_goals(transport) -> int:
    """전송 계층이 추적 중인 goal 수. 셀 수 없으면 0으로 본다."""
    handles = getattr(transport, "_handles", None)
    if handles is None:
        return 0
    return len([handle for handle in handles if handle is not None])


class StopLatch:
    """정지 래치. `core/stop_contract.GoalTracker`의 해제 규칙을 따른다.

    규칙(같은 계약의 `reset_for_new_plan`):
    - 새 계획을 수락할 때만 푼다.
    - **추적 중인 goal이 남아 있으면 해제를 거부한다** — 이전 goal이 살아 있는데
      새 계획을 시작하지 않는다.

    공통 코드(`server/runtime.reset_stop_latch`)는 `adapter.tracker
    .reset_for_new_plan()`을 부른다. 그 계약을 그대로 노출한다.
    """

    def __init__(self, live_goals: Callable[[], int]):
        self._live_goals = live_goals
        self.stopped = False
        #: 래치를 건 전역 STOP이 멈춘 실행. 호출자가 알려 주지 않으면 None이다.
        self.execution_id: str | None = None

    def latch(self, execution_id: str | None = None) -> None:
        self.stopped = True
        self.execution_id = execution_id

    def reset_for_new_plan(self) -> None:
        live = self._live_goals()
        if live:
            raise RuntimeError(
                f"추적 중인 goal이 {live}개 남아 있어 정지 래치를 풀지 않는다")
        self.stopped = False
        self.execution_id = None

    def diagnostics(self) -> dict:
        """읽기 전용 진단값. 메모리 값만 읽는다 — 해제·취소·외부 호출이 없다."""
        return {
            "tracked_goal_count": self._live_goals(),
            "stop_latch_active": self.stopped,
            "stop_latch_execution_id": self.execution_id,
        }


class Fr3GazeboAdapter(RobotAdapter):
    """FR3 Gazebo 작업 셀 Adapter."""

    #: 어댑터 종류. 개발용 Fake가 아니고, 실하드웨어도 아니다.
    adapter_kind = "gazebo_sim"
    #: 이 어댑터는 **항상 시뮬레이션**이다. 연결이 확인돼도 그것은 시뮬레이터
    #: 와의 연결이다 — real로 기록하지 않는다.
    is_simulated_adapter = True

    def __init__(
        self,
        robot_id: str,
        profile: CapabilityProfile,
        *,
        transport: WorkcellTransport,
        resources: WorkcellResources,
        now: Callable[[], float],
        move_seconds: float = 6.0,
        stop_velocity_rad_s: float = 0.01,
        stop_samples: int = 10,
        max_sample_gap_sec: float = 0.2,
    ):
        super().__init__(robot_id, profile)
        self._transport = transport
        self._resources = resources
        self._now = now
        self._move_seconds = move_seconds
        self._stop_velocity = stop_velocity_rad_s
        self._stop_samples = stop_samples
        self._max_sample_gap_sec = max_sample_gap_sec
        self._connected = False
        self._world: WorldStatus | None = None
        # 정지 래치는 계약(core/stop_contract)의 해제 규칙을 따른다.
        self.tracker = StopLatch(lambda: transport_live_goals(transport))
        #: 마지막 취소 ACK를 받은 시각(`now` 시계). 정지 확인 기록에 남긴다.
        self._cancel_ack_at: float | None = None
        #: 마지막으로 성공한 재검증의 snapshot. 실행 사이에 씬이 바뀌면 막는다.
        self._last_snapshot: tuple[str, str] | None = None

    # ── 연결 ────────────────────────────────────────────────────────────
    def connect(self, timeout_sec: float) -> ExecutionResult:
        world = self._transport.connect(timeout_sec)
        self._world = world
        if not world.world_present:
            self._connected = False
            return rejected(ReasonCode.ROBOT_CONNECTION_LOST, {
                "world": world.world_name, "detail": world.detail,
                "is_simulated": True,
            })
        if world.world_name != self._resources.world_name:
            self._connected = False
            return rejected(ReasonCode.CONFIG_VERSION_MISMATCH, {
                "expected_world": self._resources.world_name,
                "actual_world": world.world_name,
                "detail": "다른 world에 붙었다 — 격리가 깨졌다",
            })
        self._connected = True
        return ExecutionResult(
            state=ExecutionState.IDLE, request_accepted=True,
            evidence={
                "connected": True, "is_simulated": True,
                "world": world.world_name,
                "gz_partition": world.gz_partition,
                "ros_domain_id": world.ros_domain_id,
                "controllers": dict(world.controllers),
                "models": list(world.models),
            })

    def disconnect(self) -> None:
        self._connected = False
        self._transport.disconnect()

    def stop_diagnostics(self) -> dict:
        """추적 goal 수와 정지 래치 상태. **읽기 전용**이다.

        전송 계층의 추적 목록과 래치의 메모리 값만 읽는다. 관절 관측·Gazebo
        서비스 조회·goal 취소·래치 해제를 하지 않는다.
        """
        return {**self.tracker.diagnostics(),
                "is_simulated": True, "real_hardware": False}

    # ── 상태 ────────────────────────────────────────────────────────────
    def state(self) -> RobotStateSnapshot:
        observation = self._transport.joint_observation(2.0)
        return RobotStateSnapshot(
            state=(ExecutionState.STOPPED if self.tracker.stopped
                   else ExecutionState.IDLE),
            observed_at=observation.observed_at,
            joint_positions=dict(observation.positions),
            joint_velocities=dict(observation.velocities),
            valid=observation.valid,
            # 파지 관측 수단이 없다. "쥔 것이 없다"고 주장하지 않는다.
            held_object=None,
            hold_observed=False,
        )


    def _gripper_observation(self, observation: JointObservation) -> dict:
        """그리퍼 관절값과 개구. **관측·모델이 없으면 값을 만들지 않는다.**"""
        value = observation.positions.get(GRIPPER_JOINT)
        if value is None:
            return {"observed": False,
                    "detail": f"{GRIPPER_JOINT}가 관측되지 않는다",
                    "reason_code": str(ReasonCode.EXEC_UNVERIFIABLE)}
        out: dict = {"observed": True, "joint_rad": round(value, 6)}
        model = self._resources.aperture_model
        if model is None:
            out["aperture_m"] = None
            out["aperture_detail"] = "개구 모델이 없다 — 개구를 주장하지 않는다"
            return out
        reading = model.observe(value, edge_tolerance_rad=0.02)
        out["aperture_m"] = reading["aperture_m"]
        out["aperture_at_table_edge"] = reading.get("at_table_edge")
        out["aperture_source"] = model.source
        if reading["aperture_m"] is None:
            out["aperture_detail"] = "개구 표 범위를 벗어났다"
            out["reason_code"] = str(ReasonCode.EXEC_UNVERIFIABLE)
        return out

    # ── 사전 점검 ───────────────────────────────────────────────────────
    def check(self) -> ExecutionResult:
        """실행 직전 점검. 순서대로 world → 컨트롤러 → 관측 신선도를 본다."""
        if not self._connected:
            return rejected(ReasonCode.ROBOT_NOT_CONNECTED, {"is_simulated": True})
        world = self._transport.status(5.0)
        self._world = world
        if not world.world_present:
            return rejected(ReasonCode.ROBOT_CONNECTION_LOST, {
                "world": self._resources.world_name, "detail": world.detail,
                "is_simulated": True})
        inactive = world.inactive(REQUIRED_CONTROLLERS)
        if inactive:
            return rejected(ReasonCode.ROBOT_STATE_UNAVAILABLE, {
                "inactive_controllers": list(inactive),
                "required": list(REQUIRED_CONTROLLERS),
                "controllers": dict(world.controllers),
                "detail": "컨트롤러가 활성 상태가 아니다 — 실행하지 않는다",
                "is_simulated": True})
        observation = self._transport.joint_observation(3.0)
        if not observation.valid:
            return rejected(ReasonCode.ROBOT_STATE_UNAVAILABLE, {
                "detail": observation.detail or "관절 상태를 관측할 수 없다",
                "is_simulated": True})
        age = self._now() - observation.observed_at
        if age > self._resources.state_max_age_sec:
            return rejected(ReasonCode.ROBOT_STATE_STALE, {
                "age_sec": round(age, 4),
                "max_age_sec": self._resources.state_max_age_sec,
                "is_simulated": True})
        missing_joints = [name for name in ARM_JOINTS
                          if name not in observation.positions]
        if missing_joints:
            return rejected(ReasonCode.CAPABILITY_UNKNOWN_JOINT, {
                "missing_joints": missing_joints, "is_simulated": True})
        return ExecutionResult(
            state=ExecutionState.IDLE, request_accepted=True,
            evidence={
                "observed_at": observation.observed_at,
                "age_sec": round(age, 4),
                "controllers": dict(world.controllers),
                "required_controllers": list(REQUIRED_CONTROLLERS),
                "gripper": self._gripper_observation(observation),
                "world": world.world_name,
                "gz_partition": world.gz_partition,
                "ros_domain_id": world.ros_domain_id,
                "is_simulated": True,
            })

    # ── 자원·자세 해석 ──────────────────────────────────────────────────
    def resolve_move(self, target: str) -> tuple[str | None, dict]:
        """자원 id → 자세 이름. 없으면 이유를 담아 돌려준다."""
        mapping = self._resources.scene_mapping(target)
        if mapping is None:
            return None, {
                "resource_id": target,
                "known_resources": sorted(self._resources.resources),
                "detail": "작업 셀 대조표에 없는 자원이다",
                "reason_code": str(ReasonCode.PLAN_UNKNOWN_RESOURCE),
            }
        pose_name = self._resources.move_pose.get(target)
        if pose_name is None:
            return None, {
                "resource_id": target, **mapping,
                "detail": "이 자원에 검증된 접근 자세가 없다",
                "reason_code": str(ReasonCode.GEOMETRY_WORKSPACE_VIOLATION),
            }
        return pose_name, {
            "resource_id": target, **mapping, "pose": pose_name,
            "pose_evidence": dict(self._resources.pose_evidence.get(pose_name, {})),
        }

    # ── 스킬 ────────────────────────────────────────────────────────────
    def home(self, timeout_sec: float) -> ExecutionResult:
        return self._go(self._resources.safe_home_pose, timeout_sec,
                        extra={"skill": "home"})

    def move(self, target: str, timeout_sec: float) -> ExecutionResult:
        pose_name, detail = self.resolve_move(target)
        if pose_name is None:
            code = detail.pop("reason_code")
            reason = (ReasonCode.PLAN_UNKNOWN_RESOURCE
                      if code == str(ReasonCode.PLAN_UNKNOWN_RESOURCE)
                      else ReasonCode.GEOMETRY_WORKSPACE_VIOLATION)
            return rejected(reason, {**detail, "is_simulated": True})
        return self._go(pose_name, timeout_sec,
                        extra={"skill": "move", "target": target, **detail})

    def pick(self, obj: str, source: str, timeout_sec: float) -> ExecutionResult:
        return self._pick_place_blocked("pick", obj, source)

    def place(self, obj: str, destination: str,
              timeout_sec: float) -> ExecutionResult:
        return self._pick_place_blocked("place", obj, destination)

    def _pick_place_blocked(self, skill: str, obj: str,
                            location: str) -> ExecutionResult:
        """자원·자세 해석까지 하고 **실행은 막는다.**"""
        table = (self._resources.pick_pose if skill == "pick"
                 else self._resources.place_pose)
        return rejected(ReasonCode.CAPABILITY_PROFILE_INCOMPLETE, {
            "skill": skill,
            "object": obj,
            "location": location,
            "object_scene": dict(self._resources.scene_mapping(obj) or {}),
            "location_scene": dict(self._resources.scene_mapping(location) or {}),
            "pose": table.get(location),
            "message": "2F-85 장착 근거, 그리퍼 close 안정성, 파지 관측,"
                       " pick/place 재검증이 완료되지 않았습니다.",
            "unmet": [
                "장착 transform과 yaw 근거",
                "커플링 질량·관성 근거",
                "파지 상태 관측 수단",
                "pick 접근·파지·후퇴·place 계획",
                "실행 직전 재검증",
            ],
            "is_simulated": True,
        })

    def _go(self, pose_name: str, timeout_sec: float,
            *, extra: Mapping[str, object]) -> ExecutionResult:
        joints = self._resources.poses.get(pose_name)
        if joints is None:
            return rejected(ReasonCode.GEOMETRY_FRAME_UNKNOWN, {
                "pose": pose_name,
                "known_poses": sorted(self._resources.poses),
                "detail": "검증된 자세가 아니다 — 값을 만들지 않는다",
                "is_simulated": True, **extra})

        precheck = self.check()
        if not precheck.request_accepted:
            return rejected(precheck.reason or ReasonCode.EXEC_PERMIT_DENIED,
                            {**dict(precheck.evidence), **extra,
                             "stage": "precheck"})

        # 실행 직전 재검증: planning scene에서 목표 상태의 충돌을 본다.
        scene = self._transport.check_state(joints, 10.0)
        if not scene.available:
            return rejected(ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE, {
                "pose": pose_name, "detail": scene.detail,
                "is_simulated": True, **extra})
        if not scene.valid:
            return rejected(ReasonCode.GEOMETRY_COLLISION, {
                "pose": pose_name,
                "contacts": [list(pair) for pair in scene.contacts],
                "out_of_bounds": list(scene.out_of_bounds),
                "snapshot_id": scene.snapshot_id,
                "detail": "목표 자세가 planning scene에서 충돌한다",
                "is_simulated": True, **extra})
        current = (scene.snapshot_id, scene.content_hash)
        snapshot_changed = (self._last_snapshot is not None
                            and self._last_snapshot != current)
        self._last_snapshot = current

        outcome = self._transport.send_arm(joints, self._move_seconds, timeout_sec)
        evidence = {
            "pose": pose_name,
            "target_joint_rad": {k: round(v, 6) for k, v in joints.items()},
            "snapshot_id": scene.snapshot_id,
            "snapshot_changed_since_last_execution": snapshot_changed,
            "accepted": outcome.accepted,
            "result_received": outcome.result_received,
            "error_code": outcome.error_code,
            "is_simulated": True,
            "real_hardware_verified": False,
            **extra,
        }
        if not outcome.accepted:
            return rejected(ReasonCode.EXEC_GOAL_REJECTED,
                            {**evidence, "detail": outcome.detail})
        if not outcome.result_received:
            return unverifiable(ReasonCode.EXEC_RESULT_TIMEOUT, evidence)

        observation = self._transport.joint_observation(3.0)
        if not observation.valid:
            return unverifiable(ReasonCode.ROBOT_STATE_UNAVAILABLE, evidence)
        errors = {name: abs(observation.positions.get(name, 0.0) - joints[name])
                  for name in joints if name in ARM_JOINTS}
        worst = max(errors.values()) if errors else None
        evidence["observed_joint_rad"] = {
            k: round(observation.positions.get(k, 0.0), 6) for k in joints}
        evidence["gripper"] = self._gripper_observation(observation)
        evidence["controllers"] = dict(
            (self._world.controllers if self._world else {}))
        evidence["planning_scene"] = {
            "checked": True,
            "valid": scene.valid,
            "contacts": [list(pair) for pair in scene.contacts],
            "snapshot_id": scene.snapshot_id,
            "content_hash": scene.content_hash,
        }
        evidence["max_error_rad"] = None if worst is None else round(worst, 6)
        evidence["tolerance_rad"] = ARM_TOLERANCE_RAD
        if worst is None:
            return unverifiable(ReasonCode.ROBOT_STATE_UNAVAILABLE, evidence)
        if worst > ARM_TOLERANCE_RAD:
            return ExecutionResult(
                state=ExecutionState.FAILED, request_accepted=True,
                task_succeeded=False, verified=True,
                reason=ReasonCode.EXEC_GOAL_NOT_REACHED, evidence=evidence)
        return success(evidence)

    # ── 정지 ────────────────────────────────────────────────────────────
    def stop(self, timeout_sec: float,
             execution_id: str | None = None) -> ExecutionResult:
        self.tracker.latch(execution_id)
        outcome = self._transport.cancel_all(timeout_sec)
        self._cancel_ack_at = self._now() if outcome.cancel_ack is True else None
        # **요청 접수만으로 정지를 주장하지 않는다.**
        return ExecutionResult(
            state=ExecutionState.STOPPING,
            request_accepted=True,
            evidence={
                "cancel_ack": outcome.cancel_ack,
                "goals_canceling": outcome.goals_canceling,
                "detail": outcome.detail,
                "is_simulated": True,
                "note": "정지는 confirm_stopped()가 관측 속도로 확인한다",
            })

    def cancel(self, timeout_sec: float) -> ExecutionResult:
        outcome = self._transport.cancel_all(timeout_sec)
        self._cancel_ack_at = self._now() if outcome.cancel_ack is True else None
        if outcome.cancel_ack is not True:
            return unverifiable(ReasonCode.EXEC_STOP_UNCONFIRMED, {
                "cancel_ack": outcome.cancel_ack,
                "detail": outcome.detail or "취소 ACK를 받지 못했다",
                "is_simulated": True})
        return ExecutionResult(
            state=ExecutionState.STOPPING, request_accepted=True,
            evidence={"cancel_ack": True,
                      "goals_canceling": outcome.goals_canceling,
                      "is_simulated": True})

    def _stop_channel(self, samples: list[JointObservation],
                      names: tuple[str, ...]) -> dict | None:
        """채널 하나의 정지 근거. **속도와 변위를 함께 본다.**

        속도 채널에는 위치가 정지한 상태에서도 **고립 1표본 스파이크**가
        섞인다(측정 근거: reports/workcell/stop_observation.json — 2767표본
        동안 관절 이동 49.8 µrad인데 0.01 rad/s 초과가 115표본, 모두 길이 1).
        그래서 허용치 초과를 **연속 2표본 이상**일 때만 운동으로 보고,
        변위 조건을 하나 더 요구한다. 허용치를 늘리지 않는다.
        """
        present = [name for name in names
                   if all(name in s.velocities for s in samples)]
        if not present:
            return None
        peaks, runs, displacement = 0.0, 0, 0.0
        for name in present:
            values = [abs(s.velocities[name]) for s in samples]
            peaks = max(peaks, max(values))
            run = best = 0
            for value in values:
                run = run + 1 if value >= self._stop_velocity else 0
                best = max(best, run)
            runs = max(runs, best)
            positions = [s.positions.get(name) for s in samples
                         if s.positions.get(name) is not None]
            if positions:
                displacement = max(displacement, max(positions) - min(positions))
        return {
            "joints": present,
            "peak_rad_s": round(peaks, 6),
            "longest_consecutive_exceed_samples": runs,
            "displacement_rad": round(displacement, 8),
            "velocity_moving": runs >= CONSECUTIVE_EXCEED_FOR_MOTION,
            "displacement_moving": displacement > STOP_DISPLACEMENT_RAD,
            "moving": runs >= CONSECUTIVE_EXCEED_FOR_MOTION
                      or displacement > STOP_DISPLACEMENT_RAD,
        }

    @staticmethod
    def _window_reject_reasons(arm: dict | None, gripper: dict | None) -> list[str]:
        """창이 안정 창이 아닌 사유. 비어 있으면 안정 창이다."""
        reasons: list[str] = []
        for label, channel in (("arm", arm), ("gripper", gripper)):
            if channel is None:
                reasons.append(f"{label}_unobserved")
                continue
            if channel["velocity_moving"]:
                reasons.append(f"{label}_velocity_consecutive_exceed")
            if channel["displacement_moving"]:
                reasons.append(f"{label}_displacement_exceed")
        return reasons

    def confirm_stopped(self, timeout_sec: float) -> ExecutionResult:
        """관측으로 정지를 확인한다. **안정 창이 나올 때까지 timeout 안에서 기다린다.**

        취소 ACK 직후의 첫 표본 10개만으로 결론 내리지 않는다. controller는
        취소 ACK 시점에 goal을 끝내지만 물리적 정착은 그 뒤에 온다
        (2026-09-17 domain 44 실측: 취소 뒤 1초 넘게 0.01 rad/s 초과).

        - 서로 다른 fresh 표본만 창에 넣는다. 이전 표본보다 새롭지 않은 표본은
          버리고, 표본 간격이 `max_sample_gap_sec`를 넘으면 창을 비운다.
        - 연속 표본 `stop_samples`개 창을 한 표본씩 밀며, 속도·변위 기준을 모두
          만족하는 **첫 창**에서 확인한다. 기준값·표본 수·timeout은 바꾸지 않는다.
        - timeout까지 안정 창이 없을 때만 `exec.stop_unconfirmed`다.
        - controller 완료 신호로 확인하지 않는다. 관측 창만 근거다.
        """
        started_at = self._now()
        deadline = started_at + timeout_sec
        window: list[JointObservation] = []
        last_at: float | None = None
        fresh_count = 0
        stale_count = 0
        invalid_count = 0
        # 탈락 창 사유. 같은 사유가 이어지면 한 구간으로 묶는다(창 하나도 빠뜨리지 않는다).
        rejections: list[dict] = []
        rejected_windows = 0
        last_arm: dict | None = None
        last_gripper: dict | None = None
        last_window: list[JointObservation] = []

        def reject(reason: str, at: float) -> None:
            nonlocal rejected_windows
            rejected_windows += 1
            if rejections and rejections[-1]["reason"] == reason:
                rejections[-1]["last_at"] = round(at, 6)
                rejections[-1]["windows"] += 1
            else:
                rejections.append({"reason": reason, "first_at": round(at, 6),
                                   "last_at": round(at, 6), "windows": 1})

        stable: list[JointObservation] | None = None
        while self._now() < deadline:
            observation = self._transport.joint_observation(1.0, after=last_at)
            if not observation.valid:
                # 관측이 끊겼다. 창을 이어 붙이지 않는다.
                invalid_count += 1
                if window:
                    window = []
                continue
            if last_at is not None and observation.observed_at <= last_at:
                # **새 표본이 아니다.** 같은 표본을 반복해서 세지 않는다.
                stale_count += 1
                continue
            if last_at is not None and observation.observed_at - last_at > self._max_sample_gap_sec:
                reject("sample_gap_exceeded", observation.observed_at)
                window = []
            fresh_count += 1
            window.append(observation)
            last_at = observation.observed_at
            if len(window) > self._stop_samples:
                window.pop(0)
            if len(window) < self._stop_samples:
                continue
            arm = self._stop_channel(window, ARM_JOINTS)
            gripper = self._stop_channel(window, (GRIPPER_JOINT,))
            last_arm, last_gripper, last_window = arm, gripper, list(window)
            reasons = self._window_reject_reasons(arm, gripper)
            if reasons:
                reject("+".join(reasons), window[-1].observed_at)
                continue
            stable = list(window)
            break

        base = {
            "tolerance_rad_s": self._stop_velocity,
            "displacement_tolerance_rad": STOP_DISPLACEMENT_RAD,
            "consecutive_exceed_for_motion": CONSECUTIVE_EXCEED_FOR_MOTION,
            "required": self._stop_samples,
            "criterion": "속도 허용치 초과가 연속 2표본 이상이거나 변위가"
                         " 허용치를 넘으면 운동으로 본다"
                         " (근거: reports/workcell/stop_observation.json)",
            "max_sample_gap_sec": self._max_sample_gap_sec,
            "timeout_sec": timeout_sec,
            "search": "timeout 안에서 연속 표본 창을 한 표본씩 밀며 첫 안정 창을 찾는다",
            "cancel_ack_at": self._cancel_ack_at,
            "search_started_at": round(started_at, 6),
            "fresh_samples": fresh_count,
            "stale_samples_skipped": stale_count,
            "invalid_observations": invalid_count,
            "rejected_windows": rejected_windows,
            "rejected_window_runs": rejections,
            "unobservable_mimic_joints": 5,
            "unobservable_detail": "mimic 관절은 state_interface가 없어 관측할 수"
                                   " 없다 — 따라 멈췄는지 확인하지 못했다",
            "is_simulated": True,
        }
        shown = stable if stable is not None else last_window
        arm = self._stop_channel(shown, ARM_JOINTS) if shown else None
        gripper = self._stop_channel(shown, (GRIPPER_JOINT,)) if shown else None
        evidence = {
            **base,
            "samples": len(shown) if shown else len(window),
            "observation_span_sec": (round(shown[-1].observed_at - shown[0].observed_at, 4)
                                     if shown else None),
            "arm": arm,
            "gripper_channel": gripper,
            "arm_peak_rad_s": None if arm is None else arm["peak_rad_s"],
            "gripper_peak_rad_s": None if gripper is None else gripper["peak_rad_s"],
            "gripper_observed": gripper is not None,
            "gripper": self._gripper_observation(shown[-1]) if shown else None,
        }

        if stable is None:
            if not shown:
                detail = "서로 다른 표본이 부족해 정지를 확인할 수 없다"
            elif last_arm is None:
                detail = "팔 속도를 관측하지 못했다 — 정지 미확인"
            elif last_gripper is None:
                # 그리퍼 정지를 관측하지 못했다. **정지로 주장하지 않는다.**
                detail = "그리퍼 속도를 관측하지 못했다 — 정지 미확인"
            else:
                detail = "정지 미확인 — timeout까지 안정 창이 없었다(관측값이 움직이고 있다)"
            return unverifiable(ReasonCode.EXEC_STOP_UNCONFIRMED, {
                **evidence, "stop_verdict": "stop_unconfirmed",
                "searched_until": round(self._now(), 6), "detail": detail})

        window_start, window_end = stable[0].observed_at, stable[-1].observed_at
        reference = self._cancel_ack_at if self._cancel_ack_at is not None else started_at
        return ExecutionResult(
            state=ExecutionState.STOPPED, request_accepted=True,
            task_succeeded=None, verified=True, evidence={
                **evidence,
                "stop_verdict": "stop_confirmed",
                "stable_window_start_at": round(window_start, 6),
                "stable_window_end_at": round(window_end, 6),
                "seconds_to_confirm_from": ("cancel_ack" if self._cancel_ack_at is not None
                                            else "search_start"),
                "seconds_to_confirm": round(window_end - reference, 4),
            })
