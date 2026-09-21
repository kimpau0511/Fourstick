"""FR3 Gazebo 작업 셀 전송 계층 ROS 구현 (8-08 우선순위 6).

`WorkcellTransport` 계약의 실제 구현체다. rclpy·gz CLI를 **여기서만** 쓴다 —
Adapter는 이 모듈을 몰라도 동작한다(테스트는 스텁을 넣는다).

관측 규칙:
- world 존재는 `gz service -l`로 확인한다(해당 파티션에서만 보인다).
- 컨트롤러 상태는 `/controller_manager/list_controllers` 서비스로 읽는다.
- 관절은 `/joint_states` 구독으로 읽고 **수신 시각**을 관측 시각으로 쓴다.
- planning scene 검사는 `/check_state_validity`로 한다. 서비스가 없으면
  `available=False`로 남기고 안전으로 보지 않는다.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Mapping

from robots.fr3_gazebo.transport import (
    GoalOutcome,
    JointObservation,
    SceneCheck,
    WorldStatus,
)

ARM_JOINTS = ("j1", "j2", "j3", "j4", "j5", "j6")
GRIPPER_JOINT = "robotiq_85_left_knuckle_joint"
ARM_ACTION = "/arm_trajectory_controller/follow_joint_trajectory"
#: `should_stop` 때문에 결과를 기다리지 않고 돌아왔다는 표시(GoalOutcome.detail).
STOP_REQUESTED_DETAIL = "정지 요청으로 결과 대기를 멈췄다"
#: `action_msgs/GoalStatus`의 **terminal** 상태(SUCCEEDED 4, CANCELED 5, ABORTED 6).
#: 값은 ROS 액션 규약이 고정한다. UNKNOWN(0)·ACCEPTED(1)·EXECUTING(2)·
#: CANCELING(3)은 terminal이 아니다 — 추적을 풀지 않는다.
TERMINAL_GOAL_STATUSES = frozenset({4, 5, 6})
GRIPPER_ACTION = "/gripper_trajectory_controller/follow_joint_trajectory"


class RosWorkcellTransport:
    """rclpy 기반 구현. 노드 하나를 만들고 백그라운드로 spin한다."""

    def __init__(self, *, world_name: str, gz_partition: str, ros_domain_id: int,
                 group_name: str = "fr3wms_arm", node_name: str = "forstick2_fr3_gazebo"):
        self._world_name = world_name
        self._partition = gz_partition
        self._domain = ros_domain_id
        self._group = group_name
        self._node_name = node_name
        self._node = None
        self._executor = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest: JointObservation | None = None
        self._arm_client = None
        self._gripper_client = None
        self._controllers_client = None
        self._validity_client = None
        self._scene_client = None
        #: 추적 중인 goal handle. **그 goal의 결과가 terminal 상태로 올 때만**
        #: 뺀다(`_on_goal_result`). 정지 래치 해제가 이 목록을 센다.
        self._handles: list = []
        self._goal_lock = threading.Lock()
        self._started_rclpy = False

    # ── 수명 ────────────────────────────────────────────────────────────
    def _ensure_node(self) -> None:
        if self._node is not None:
            return
        os.environ.setdefault("GZ_PARTITION", self._partition)
        os.environ["ROS_DOMAIN_ID"] = str(self._domain)
        import rclpy
        from rclpy.action import ActionClient
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.node import Node
        from sensor_msgs.msg import JointState
        from control_msgs.action import FollowJointTrajectory
        from controller_manager_msgs.srv import ListControllers
        from moveit_msgs.srv import GetPlanningScene, GetStateValidity

        if not rclpy.ok():
            rclpy.init()
            self._started_rclpy = True
        node = Node(self._node_name)
        self._node = node

        def on_state(message: JointState) -> None:
            with self._lock:
                self._latest = JointObservation(
                    positions=dict(zip(message.name, message.position)),
                    velocities=dict(zip(message.name, message.velocity)),
                    observed_at=time.time(),
                    valid=True,
                )

        node.create_subscription(JointState, "/joint_states", on_state, 50)
        self._arm_client = ActionClient(node, FollowJointTrajectory, ARM_ACTION)
        self._gripper_client = ActionClient(node, FollowJointTrajectory,
                                            GRIPPER_ACTION)
        self._controllers_client = node.create_client(
            ListControllers, "/controller_manager/list_controllers")
        self._validity_client = node.create_client(
            GetStateValidity, "/check_state_validity")
        self._scene_client = node.create_client(
            GetPlanningScene, "/get_planning_scene")

        self._executor = SingleThreadedExecutor()
        self._executor.add_node(node)
        self._thread = threading.Thread(target=self._executor.spin, daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        if self._executor is not None:
            self._executor.shutdown()
        if self._node is not None:
            self._node.destroy_node()
        self._node = None
        self._executor = None
        if self._started_rclpy:
            import rclpy
            if rclpy.ok():
                rclpy.shutdown()
            self._started_rclpy = False

    # ── world·컨트롤러 ──────────────────────────────────────────────────
    def _world_present(self, timeout_sec: float) -> tuple[bool, str]:
        env = dict(os.environ, GZ_PARTITION=self._partition)
        try:
            result = subprocess.run(
                ["gz", "service", "-l"], capture_output=True, text=True,
                timeout=max(10.0, timeout_sec), env=env)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return False, f"gz service 조회 실패: {exc}"
        needle = f"/world/{self._world_name}/"
        if needle in result.stdout:
            return True, ""
        return False, f"{needle} 서비스가 보이지 않는다(파티션 {self._partition})"

    def _controller_states(self, timeout_sec: float) -> tuple[dict[str, str], str]:
        self._ensure_node()
        if not self._controllers_client.wait_for_service(timeout_sec=timeout_sec):
            return {}, "/controller_manager/list_controllers 서비스가 없다"
        from controller_manager_msgs.srv import ListControllers
        future = self._controllers_client.call_async(ListControllers.Request())
        deadline = time.monotonic() + timeout_sec
        while not future.done() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not future.done() or future.result() is None:
            return {}, "컨트롤러 목록 응답이 없다"
        return {c.name: c.state for c in future.result().controller}, ""

    def _models(self, timeout_sec: float) -> tuple[str, ...]:
        env = dict(os.environ, GZ_PARTITION=self._partition)
        try:
            result = subprocess.run(
                ["gz", "model", "--list"], capture_output=True, text=True,
                timeout=max(10.0, timeout_sec), env=env)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ()
        return tuple(line.strip("- ").strip() for line in result.stdout.splitlines()
                     if line.strip().startswith("-"))

    def connect(self, timeout_sec: float) -> WorldStatus:
        present, detail = self._world_present(timeout_sec)
        if not present:
            return WorldStatus(world_present=False, world_name=self._world_name,
                               gz_partition=self._partition,
                               ros_domain_id=self._domain, detail=detail)
        self._ensure_node()
        controllers, controller_detail = self._controller_states(timeout_sec)
        return WorldStatus(
            world_present=True, world_name=self._world_name,
            gz_partition=self._partition, ros_domain_id=self._domain,
            controllers=controllers, models=self._models(timeout_sec),
            detail=controller_detail)

    def status(self, timeout_sec: float) -> WorldStatus:
        present, detail = self._world_present(timeout_sec)
        if not present:
            return WorldStatus(world_present=False, world_name=self._world_name,
                               gz_partition=self._partition,
                               ros_domain_id=self._domain, detail=detail)
        controllers, controller_detail = self._controller_states(timeout_sec)
        return WorldStatus(
            world_present=True, world_name=self._world_name,
            gz_partition=self._partition, ros_domain_id=self._domain,
            controllers=controllers, detail=controller_detail)

    # ── 관측 ────────────────────────────────────────────────────────────
    def joint_observation(self, timeout_sec: float,
                          *, after: float | None = None) -> JointObservation:
        """관절 관측 한 장.

        `after`를 주면 **그보다 새로운 표본**을 기다린다. 정지 확인은 서로 다른
        표본 여러 장을 봐야 한다 — 같은 표본을 반복해서 세면 "연속으로 멈춰
        있었다"는 근거가 되지 않는다.
        """
        self._ensure_node()
        deadline = time.monotonic() + timeout_sec
        while True:
            with self._lock:
                latest = self._latest
            if latest is not None and (after is None or latest.observed_at > after):
                return latest
            if time.monotonic() > deadline:
                detail = ("/joint_states를 받지 못했다" if latest is None
                          else "새 표본이 오지 않았다")
                return JointObservation(
                    positions={}, velocities={}, observed_at=0.0, valid=False,
                    detail=detail)
            time.sleep(0.005)

    # ── 명령 ────────────────────────────────────────────────────────────
    def _send(self, client, names, values, seconds: float,
              timeout_sec: float, should_stop=None) -> GoalOutcome:
        """goal을 보내고 결과까지 기다린다.

        `should_stop`(선택)이 참을 돌려주면 **결과를 기다리지 않고** 돌아온다.
        goal은 추적 목록에 남아 호출자가 `cancel_all`로 취소한다 — 여기서 취소하지
        않는다. 없으면 기존 동작과 같다.
        """
        self._ensure_node()
        from builtin_interfaces.msg import Duration
        from control_msgs.action import FollowJointTrajectory
        from trajectory_msgs.msg import JointTrajectoryPoint

        if not client.wait_for_server(timeout_sec=timeout_sec):
            return GoalOutcome(accepted=False, detail="액션 서버가 없다")
        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = list(names)
        point = JointTrajectoryPoint()
        point.positions = [float(v) for v in values]
        point.velocities = [0.0] * len(values)
        point.time_from_start = Duration(
            sec=int(seconds), nanosec=int((seconds % 1) * 1e9))
        goal.trajectory.points = [point]
        send_future = client.send_goal_async(goal)
        deadline = time.monotonic() + timeout_sec
        while not send_future.done() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not send_future.done():
            return GoalOutcome(accepted=False, detail="goal 전송 응답 없음")
        handle = send_future.result()
        if not handle.accepted:
            return GoalOutcome(accepted=False, detail="goal 거부")
        result_future = self._track(handle)
        result_deadline = time.monotonic() + seconds + timeout_sec
        while not result_future.done() and time.monotonic() < result_deadline:
            if should_stop is not None and should_stop():
                return GoalOutcome(accepted=True, result_received=False,
                                   detail=STOP_REQUESTED_DETAIL)
            time.sleep(0.02)
        if not result_future.done():
            return GoalOutcome(accepted=True, result_received=False,
                               detail="결과 대기 초과")
        result = result_future.result()
        return GoalOutcome(accepted=True, result_received=True,
                           error_code=int(result.result.error_code))

    def _send_async(self, client, names, values, seconds: float,
                    timeout_sec: float) -> GoalOutcome:
        """goal을 보내고 **수락까지만** 기다린다. 결과를 기다리지 않는다.

        이동 중에 정지를 시험하려면 명령이 진행되는 동안 제어가 돌아와야 한다.
        handle은 그대로 추적 목록에 남아 `cancel_all`이 취소할 수 있다 —
        추적을 잃으면 무엇을 취소했는지 말할 수 없다.
        """
        self._ensure_node()
        from builtin_interfaces.msg import Duration
        from control_msgs.action import FollowJointTrajectory
        from trajectory_msgs.msg import JointTrajectoryPoint

        if not client.wait_for_server(timeout_sec=timeout_sec):
            return GoalOutcome(accepted=False, detail="액션 서버가 없다")
        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = list(names)
        point = JointTrajectoryPoint()
        point.positions = [float(v) for v in values]
        point.velocities = [0.0] * len(values)
        point.time_from_start = Duration(
            sec=int(seconds), nanosec=int((seconds % 1) * 1e9))
        goal.trajectory.points = [point]
        send_future = client.send_goal_async(goal)
        deadline = time.monotonic() + timeout_sec
        while not send_future.done() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not send_future.done():
            return GoalOutcome(accepted=False, detail="goal 전송 응답 없음")
        handle = send_future.result()
        if not handle.accepted:
            return GoalOutcome(accepted=False, detail="goal 거부")
        self._track(handle)
        return GoalOutcome(accepted=True, result_received=False,
                           detail="결과를 기다리지 않았다")

    def send_arm_async(self, joints: Mapping[str, float], seconds: float,
                       timeout_sec: float) -> GoalOutcome:
        names = [n for n in ARM_JOINTS if n in joints]
        return self._send_async(self._arm_client, names,
                                [joints[n] for n in names], seconds, timeout_sec)

    def live_goals(self) -> int:
        """추적 중인 goal 수. 정지 래치 해제 조건에 쓴다."""
        with self._goal_lock:
            return len([handle for handle in self._handles if handle is not None])

    # ── goal 추적 ───────────────────────────────────────────────────────
    @staticmethod
    def _goal_key(handle) -> bytes | None:
        goal_id = getattr(handle, "goal_id", None)
        uuid = getattr(goal_id, "uuid", None)
        return None if uuid is None else bytes(uuid)

    def _track(self, handle):
        """수락된 goal을 추적 목록에 넣고, 결과가 오면 `_on_goal_result`로 넘긴다.

        목록에 먼저 넣고 콜백을 건다 — 결과가 이미 와 있어도 빼는 쪽이 뒤에 온다.
        """
        with self._goal_lock:
            self._handles.append(handle)
        result_future = handle.get_result_async()
        result_future.add_done_callback(
            lambda future, h=handle: self._on_goal_result(h, future))
        return result_future

    def _on_goal_result(self, handle, future) -> bool:
        """그 goal의 결과가 terminal이면 **그 goal만** 목록에서 한 번 뺀다.

        - 상태를 읽지 못하거나 terminal이 아니면 남긴다(활성·취소 진행 중).
        - 다른 goal의 결과로 현재 goal을 빼지 않는다 — goal_id로 맞춘다.
        - 같은 goal의 terminal 결과가 두 번 와도 두 번째는 아무것도 하지 않는다.

        돌려주는 값: 이번 호출로 뺐는가.
        """
        try:
            response = future.result()
        except Exception:  # noqa: BLE001 — 상태를 모르면 terminal로 보지 않는다
            return False
        status = getattr(response, "status", None)
        if status not in TERMINAL_GOAL_STATUSES:
            return False
        key = self._goal_key(handle)
        with self._goal_lock:
            for index, tracked in enumerate(self._handles):
                if tracked is None:
                    continue
                same = (tracked is handle if key is None
                        else self._goal_key(tracked) == key)
                if same:
                    del self._handles[index]
                    return True
        return False

    def send_arm(self, joints: Mapping[str, float], seconds: float,
                 timeout_sec: float, *, should_stop=None) -> GoalOutcome:
        names = [n for n in ARM_JOINTS if n in joints]
        return self._send(self._arm_client, names, [joints[n] for n in names],
                          seconds, timeout_sec, should_stop=should_stop)

    def send_gripper(self, value: float, seconds: float,
                     timeout_sec: float, *, should_stop=None) -> GoalOutcome:
        return self._send(self._gripper_client, [GRIPPER_JOINT], [value],
                          seconds, timeout_sec, should_stop=should_stop)

    def cancel_all(self, timeout_sec: float) -> GoalOutcome:
        """추적 중인 goal을 모두 취소한다. **handle을 버려 추적을 잃지 않는다.**

        취소 ACK는 terminal이 아니다(CANCELING). 목록에서 빼는 것은 각 goal의
        terminal 결과(`_on_goal_result`)다.
        """
        self._ensure_node()
        with self._goal_lock:
            pending = [h for h in self._handles if h is not None]
        if not pending:
            return GoalOutcome(accepted=True, cancel_ack=None, goals_canceling=0,
                               detail="취소할 goal이 없다")
        futures = [h.cancel_goal_async() for h in pending]
        deadline = time.monotonic() + timeout_sec
        while not all(f.done() for f in futures) and time.monotonic() < deadline:
            time.sleep(0.02)
        acked = [f for f in futures if f.done() and f.result() is not None]
        canceling = sum(len(f.result().goals_canceling) for f in acked)
        if len(acked) != len(futures):
            return GoalOutcome(accepted=True, cancel_ack=False,
                               goals_canceling=canceling,
                               detail="일부 goal의 취소 ACK를 받지 못했다")
        return GoalOutcome(accepted=True, cancel_ack=True,
                           goals_canceling=canceling)

    # ── planning scene ──────────────────────────────────────────────────
    def check_state(self, joints: Mapping[str, float],
                    timeout_sec: float) -> SceneCheck:
        self._ensure_node()
        if not self._validity_client.wait_for_service(timeout_sec=timeout_sec):
            return SceneCheck(available=False,
                              detail="/check_state_validity 서비스가 없다")
        from moveit_msgs.msg import RobotState
        from moveit_msgs.srv import GetPlanningScene, GetStateValidity
        from sensor_msgs.msg import JointState

        request = GetStateValidity.Request()
        state = RobotState()
        state.joint_state = JointState()
        state.joint_state.name = list(joints)
        state.joint_state.position = [float(v) for v in joints.values()]
        state.is_diff = False
        request.robot_state = state
        request.group_name = self._group
        future = self._validity_client.call_async(request)
        deadline = time.monotonic() + timeout_sec
        while not future.done() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not future.done() or future.result() is None:
            return SceneCheck(available=False, detail="상태 유효성 응답이 없다")
        response = future.result()

        snapshot_id, content_hash = "", ""
        if self._scene_client.wait_for_service(timeout_sec=2.0):
            scene_request = GetPlanningScene.Request()
            scene_request.components.components = (
                scene_request.components.WORLD_OBJECT_NAMES)
            scene_future = self._scene_client.call_async(scene_request)
            scene_deadline = time.monotonic() + timeout_sec
            while not scene_future.done() and time.monotonic() < scene_deadline:
                time.sleep(0.02)
            if scene_future.done() and scene_future.result() is not None:
                import hashlib
                ids = sorted(o.id for o in
                             scene_future.result().scene.world.collision_objects)
                snapshot_id = scene_future.result().scene.name or "(unnamed)"
                content_hash = hashlib.sha256(
                    "|".join(ids).encode("utf-8")).hexdigest()[:16]
        return SceneCheck(
            available=True,
            valid=bool(response.valid),
            contacts=tuple((c.contact_body_1, c.contact_body_2)
                           for c in response.contacts),
            snapshot_id=snapshot_id,
            content_hash=content_hash,
        )
