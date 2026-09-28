"""MoveIt planning scene ROS client (8-07).

**이 모듈만 rclpy를 import한다.** 웹 서버(.venv)는 이 파일을 import하지 않는다.
`robots/moveit/__init__.py`도 이 파일을 올리지 않는다.

여기서 하는 일:
- `/get_planning_scene`으로 scene을 읽어 식별자와 hash를 만든다(본문은 남기지
  않는다).
- `/check_state_validity`로 한 자세의 유효성을 묻는다.
- 관절 제한 위반은 **우리가 URDF 제한으로 판정한다**. MoveIt은 제한을 벗어난
  자세를 valid=false로만 돌려주고 이유를 쌍으로 주지 않기 때문에, 충돌과
  제한 위반을 구분하려면 제한을 따로 봐야 한다.
"""

from __future__ import annotations

import hashlib
import time
from typing import Mapping, Sequence

import rclpy
from geometry_msgs.msg import Pose
from moveit_msgs.msg import (
    AttachedCollisionObject,
    CollisionObject,
    PlanningSceneComponents,
    RobotState,
)
from moveit_msgs.srv import GetPlanningScene, GetStateValidity
from rclpy.node import Node
from sensor_msgs.msg import JointState
from shape_msgs.msg import SolidPrimitive

from robots.moveit.scene import (
    AttachedObject,
    SceneSnapshot,
    StateValidity,
    scene_snapshot_from_parts,
)

#: 요청할 scene 구성요소. octomap은 요청하지 않는다(센서가 없다).
COMPONENTS = (
    PlanningSceneComponents.SCENE_SETTINGS
    | PlanningSceneComponents.ROBOT_STATE
    | PlanningSceneComponents.WORLD_OBJECT_NAMES
    | PlanningSceneComponents.WORLD_OBJECT_GEOMETRY
    | PlanningSceneComponents.ALLOWED_COLLISION_MATRIX
    | PlanningSceneComponents.TRANSFORMS
)
SOLID_PRIMITIVE_TYPE = {1: "box", 2: "sphere", 3: "cylinder", 4: "cone"}


def robot_model_hash(urdf: str, srdf: str) -> str:
    """로봇 모델 지문. URDF·SRDF가 바뀌면 값이 달라진다."""
    digest = hashlib.sha256()
    digest.update(urdf.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(srdf.encode("utf-8"))
    return digest.hexdigest()


class RosPlanningSceneClient:
    """`PlanningSceneClient` 계약의 ROS 구현체."""

    def __init__(
        self, node: Node, *, group_name: str, frame_id: str,
        joint_limits: Mapping[str, tuple[float, float]],
        model_hash: str, ttl_sec: float, source: str,
        service_timeout_sec: float = 10.0,
    ):
        self._node = node
        self._group = group_name
        self._frame = frame_id
        self._limits = dict(joint_limits)
        self._model_hash = model_hash
        self._ttl = ttl_sec
        self._source = source
        self._timeout = service_timeout_sec
        self._scene = node.create_client(GetPlanningScene, "/get_planning_scene")
        self._validity = node.create_client(GetStateValidity, "/check_state_validity")

    def wait(self, timeout_sec: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout_sec
        for client in (self._scene, self._validity):
            while not client.service_is_ready():
                if time.monotonic() > deadline:
                    return False
                rclpy.spin_once(self._node, timeout_sec=0.2)
        return True

    def _call(self, client, request):
        future = client.call_async(request)
        deadline = time.monotonic() + self._timeout
        while not future.done():
            if time.monotonic() > deadline:
                raise TimeoutError(f"{client.srv_name} 응답 없음")
            rclpy.spin_once(self._node, timeout_sec=0.1)
        return future.result()

    def snapshot(self) -> SceneSnapshot:
        request = GetPlanningScene.Request()
        request.components.components = COMPONENTS
        # **관측 시각은 벽시계 UTC다.** 만료 판정을 앱과 같은 시계로 한다.
        captured_at = time.time()
        response = self._call(self._scene, request)
        scene = response.scene
        objects = []
        for obj in scene.world.collision_objects:
            shapes = [
                {"type": SOLID_PRIMITIVE_TYPE.get(p.type, str(p.type)),
                 "dimensions": tuple(float(v) for v in p.dimensions)}
                for p in obj.primitives
            ]
            shapes += [{"type": "mesh", "dimensions": (float(len(m.triangles)),)}
                       for m in obj.meshes]
            pose = obj.pose
            objects.append({
                "id": obj.id,
                "shapes": shapes,
                "pose": (pose.position.x, pose.position.y, pose.position.z,
                         pose.orientation.x, pose.orientation.y,
                         pose.orientation.z, pose.orientation.w),
            })
        acm = scene.allowed_collision_matrix
        disabled: list[tuple[str, str]] = []
        for i, name in enumerate(acm.entry_names):
            values = acm.entry_values[i].enabled if i < len(acm.entry_values) else []
            for j, allowed in enumerate(values):
                if allowed and j > i:
                    disabled.append((name, acm.entry_names[j]))
        return scene_snapshot_from_parts(
            scene_name=scene.name or scene.robot_model_name or "(unnamed)",
            robot_model_hash=self._model_hash,
            world_objects=objects,
            acm_disabled_pairs=disabled,
            # PlanningScene은 planning frame 이름을 담지 않는다. 좌표계는
            # 우리가 Profile frames에서 주입한 값을 쓴다(추정하지 않는다).
            frame_id=self._frame,
            captured_at=captured_at,
            ttl_sec=self._ttl,
            source=self._source,
        )

    def _attached_message(self, item: AttachedObject) -> AttachedCollisionObject:
        """선언된 치수 그대로 붙는 물체 메시지를 만든다. 치수를 만들지 않는다."""
        box = SolidPrimitive()
        box.type = SolidPrimitive.BOX
        box.dimensions = [float(v) for v in item.size_m]
        pose = Pose()
        pose.position.x, pose.position.y, pose.position.z = (
            float(v) for v in item.offset_m
        )
        pose.orientation.w = 1.0
        obj = CollisionObject()
        obj.header.frame_id = item.link
        obj.id = item.object_id
        obj.primitives = [box]
        obj.primitive_poses = [pose]
        obj.operation = CollisionObject.ADD
        attached = AttachedCollisionObject()
        attached.link_name = item.link
        attached.object = obj
        # **선언된 패드만** 닿아도 되는 링크로 넘긴다. 범위를 넓히지 않는다.
        attached.touch_links = list(item.touch_links)
        return attached

    def check_state(
        self, joints: Mapping[str, float], *,
        attached: Sequence[AttachedObject] = (),
    ) -> StateValidity:
        out_of_bounds = tuple(
            name for name, value in joints.items()
            if name in self._limits
            and not (self._limits[name][0] <= value <= self._limits[name][1])
        )
        request = GetStateValidity.Request()
        state = RobotState()
        state.joint_state = JointState()
        state.joint_state.name = list(joints)
        state.joint_state.position = [float(v) for v in joints.values()]
        state.is_diff = False
        # 적재 상태는 **호출자가 선언한 것만** 붙인다. 스스로 붙이지 않는다.
        state.attached_collision_objects = [
            self._attached_message(item) for item in attached
        ]
        request.robot_state = state
        request.group_name = self._group
        response = self._call(self._validity, request)
        contacts = tuple(
            (c.contact_body_1, c.contact_body_2) for c in response.contacts
        )
        return StateValidity(
            valid=bool(response.valid) and not out_of_bounds,
            contacts=contacts,
            out_of_bounds=out_of_bounds,
            detail="" if response.valid else "MoveIt valid=false",
        )
