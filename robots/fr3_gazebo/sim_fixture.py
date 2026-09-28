"""Gazebo 물체 고정 장치 — **시뮬레이션 전용** (8-11).

이것은 **파지가 아니다.** 마찰·파지력·물체 감지를 모사하지 않는다. 시뮬레이터
안에서 물체를 도구에 붙여 함께 움직이게 하는 장치이며, 그 사실이 모든 기록에
남는다(`kind: "simulation_fixture"`).

## 어떻게 붙이는가

Gazebo의 `set_pose`는 pose만 바꾼다 — 동적 물체는 다음 순간 중력에 끌려
떨어진다(실측: 1.05 m에 놓은 0.2 kg 자재가 0.21 s 만에 팔레트로 되돌아왔다).
그래서 pose를 반복해서 덮어쓰는 방식을 쓰지 않는다. 속도가 계속 쌓여 처짐이
커지기 때문이다.

대신 **동적 물체를 지우고 같은 이름의 정적 물체를 만든다.** 정적 물체는 중력에
끌리지 않으므로 `set_pose`만으로 도구를 따라간다. 떼면 정적 물체를 지우고
동적 물체를 그 자리에 다시 만들어, 실제로 떨어져 컨베이어에 안착한다.

따라서 "붙어 있는 동안"의 물체는 **물리적으로 정적**이다. 파지력이 부족해
미끄러지는 일은 시뮬레이터에서 일어나지 않는다 — 그것이 이 장치가 실기 파지
근거가 될 수 없는 이유다.

## 2026-09-26: 관절로 붙이기(기본, `FORSTICK2_FIXTURE_MODE=joint`)

위 방식(정적 교체 + `set_pose` 추종)은 실측으로 두 문제가 있었다.
① 붙이고 뗄 때 모델을 **지우고 다시 만들어** Gazebo 장면에서 자재가 0.4~0.5 s 사라졌다
(엔티티 id가 바뀐다). ② 운반 중 `set_pose` 추종이 팔을 늦게 따라가 손목 좌표계에서
최대 102 mm 벗어났다(`reports/workcell/grasp_follow_before_*.json`).

그래서 기본은 Gazebo `DetachableJoint` 시스템으로 **그리퍼 링크와 자재 링크를 고정
관절로 잇는다.** 자재는 동적 모델 그대로이고 물리가 함께 움직인다(순간 이동 없음).
시스템은 처음 붙일 때 로봇 모델에 런타임으로 넣는다(`entity/system/add` — 넣는 순간
붙는다). 그 뒤로는 자재별 attach/detach 토픽으로 붙이고 뗀다. 상태는 시스템이 알리는
`attached`/`detached`로 확인한다. 이전 방식은 `FORSTICK2_FIXTURE_MODE=static`으로 남긴다.

## 요청 성공을 결과로 쓰지 않는다

`create`/`remove`/`set_pose` 응답은 믿지 않는다(실측: 요청이 성공했는데 응답이
제때 오지 않는 경우가 있다). **pose를 되읽어** 확인하고, 확인되지 않으면
`exec.sim_fixture_failed`로 돌려준다.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from core.reason_codes import ReasonCode

#: 기록에 남는 장치 종류. 실기 파지와 구분되는 유일한 표시다.
FIXTURE_KIND = "simulation_fixture"

#: pose 확인 허용치(m). set_pose 직후 되읽은 값과의 차이 상한.
POSE_VERIFY_TOLERANCE_M = 0.005
#: 붙이는 방식. joint(기본) = DetachableJoint 고정 관절 · static = 정적 교체 + set_pose 추종.
FIXTURE_MODES = ("joint", "static")
#: 관절로 붙일 때의 로봇 모델·그리퍼 링크(Gazebo가 고정 관절을 합친 뒤 남는 손목 링크 —
#: 그리퍼 베이스가 여기에 합쳐져 있다). 자재 모델의 링크 이름은 `body`다(SDF 선언).
from robots.fr3_gazebo.view_source import ROBOT_MODEL as JOINT_PARENT_MODEL  # noqa: E402

JOINT_PARENT_LINK = "wrist3_Link"
JOINT_CHILD_LINK = "body"
#: 붙임 상태 알림을 기다리는 시간(s).
JOINT_STATE_TIMEOUT_SEC = 2.0
#: 붙임/뗌 알림이 이 시간 안에 없으면 같은 요청을 다시 보낸다(`_send_until`).
RESEND_SEC = 0.4


def joint_record_path():
    """시스템 알림으로 **확인된** 붙임 상태 기록(프로세스 사이 — 재개·복원 확인용)."""
    import os
    from pathlib import Path

    from core.paths import workcell_log_dir
    base = workcell_log_dir()
    return base / "fixture_joints.json"


def read_joint_record() -> dict:
    import json

    try:
        return json.loads(joint_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_joint_record(model: str, state: str, detail: str) -> None:
    import json
    import os

    data = read_joint_record()
    data[model] = {"state": state, "at": time.time(), "confirmed_by": detail}
    path = joint_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class FixtureError(Exception):
    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


@dataclass(frozen=True)
class ObjectDeclaration:
    """붙일 물체의 **선언**. 치수·질량·마찰을 여기서 만들지 않는다."""

    model: str
    size_m: tuple[float, float, float]
    mass_kg: float
    friction: float
    color_rgba: tuple[float, float, float, float]
    home_pose_m: tuple[float, float, float]
    source: str = ""


@dataclass
class FixtureEvent:
    """장치가 한 일. 시각과 되읽은 pose를 함께 남긴다."""

    kind: str
    model: str
    at: float
    pose_m: tuple[float, float, float] | None
    verified: bool
    detail: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "fixture_kind": FIXTURE_KIND,
            "model": self.model,
            "at": self.at,
            "pose_m": None if self.pose_m is None else list(self.pose_m),
            "verified": self.verified,
            "detail": self.detail,
            **({"extra": dict(self.extra)} if self.extra else {}),
        }


def dynamic_sdf(item: ObjectDeclaration) -> str:
    """동적(원래) 물체 SDF. 선언된 치수·질량·마찰을 그대로 쓴다."""
    sx, sy, sz = item.size_m
    r, g, b, a = item.color_rgba
    mass = item.mass_kg
    ixx = mass * (sy * sy + sz * sz) / 12.0
    iyy = mass * (sx * sx + sz * sz) / 12.0
    izz = mass * (sx * sx + sy * sy) / 12.0
    return f"""<?xml version="1.0"?>
<sdf version="1.9">
  <model name="{item.model}">
    <link name="body">
      <inertial>
        <mass>{mass}</mass>
        <inertia><ixx>{ixx}</ixx><iyy>{iyy}</iyy><izz>{izz}</izz>
          <ixy>0</ixy><ixz>0</ixz><iyz>0</iyz></inertia>
      </inertial>
      <collision name="collision">
        <geometry><box><size>{sx} {sy} {sz}</size></box></geometry>
        <surface>
          <friction><ode><mu>{item.friction}</mu><mu2>{item.friction}</mu2></ode></friction>
          <contact><collide_bitmask>1</collide_bitmask></contact>
        </surface>
      </collision>
      <visual name="visual">
        <geometry><box><size>{sx} {sy} {sz}</size></box></geometry>
        <material><ambient>{r * 0.6} {g * 0.6} {b * 0.6} {a}</ambient>
          <diffuse>{r} {g} {b} {a}</diffuse></material>
      </visual>
    </link>
  </model>
</sdf>"""


def static_sdf(item: ObjectDeclaration) -> str:
    """붙어 있는 동안의 정적 물체 SDF. 치수·색은 같고 **충돌 형상이 없다.**

    왜 충돌 형상을 빼는가. 붙어 있는 동안 물체는 정적(=불가동)이고 그리퍼
    손가락이 그 안에 들어가 있다. 충돌 형상을 그대로 두면 정적 물체와
    손가락 사이에 접촉력이 생겨 **팔이 궤적을 따라가지 못한다**
    (실측: lift 0.020 rad, place 접근 0.067 rad로 허용치 0.05를 넘겼다).

    그래서 붙어 있는 동안의 충돌 판정은 Gazebo가 아니라 **MoveIt의
    AttachedCollisionObject**가 한다(`validation/pick_place_plan.py`). 거기서는
    물체가 로봇에 붙은 것으로 모델링되므로 손가락과의 접촉이 선언된 예외이고,
    다른 물체·구조물과의 접촉만 충돌로 본다.

    이것도 이 장치가 실기 파지 근거가 될 수 없는 이유의 일부다 — 실제로는
    물체가 주변에 부딪히면 힘이 발생하고 미끄러진다.
    """
    sx, sy, sz = item.size_m
    r, g, b, a = item.color_rgba
    return f"""<?xml version="1.0"?>
<sdf version="1.9">
  <model name="{item.model}">
    <static>true</static>
    <link name="body">
      <visual name="visual">
        <geometry><box><size>{sx} {sy} {sz}</size></box></geometry>
        <material><ambient>{r * 0.6} {g * 0.6} {b * 0.6} {a}</ambient>
          <diffuse>{r} {g} {b} {a}</diffuse></material>
      </visual>
    </link>
  </model>
</sdf>"""


class GazeboObjectFixture:
    """Gazebo 물체를 정적으로 바꿔 도구를 따라가게 하는 시뮬레이션 장치."""

    def __init__(self, *, world_name: str, gz_partition: str,
                 objects: Mapping[str, ObjectDeclaration],
                 settle_sec: float = 1.5):
        self.world = world_name
        self.partition = gz_partition
        self.objects = dict(objects)
        self.settle_sec = settle_sec
        self.events: list[FixtureEvent] = []
        self._node = None
        self._poses: dict[str, tuple[float, float, float]] = {}
        self._orientations: dict[str, tuple[float, float, float, float]] = {}
        self._want_orientation = False
        self._held: set[str] = set()
        import os as _os

        mode = _os.environ.get("FORSTICK2_FIXTURE_MODE", "joint")
        self.mode = mode if mode in FIXTURE_MODES else "joint"
        #: 자재 → (마지막 붙임 상태 "attached"/"detached", 받은 시각). 시스템 알림에서만.
        self._joint_state: dict[str, tuple[str, float]] = {}
        self._joint_subscribed: set[str] = set()

    # ── 연결 ────────────────────────────────────────────────────────────
    def _ensure(self) -> None:
        if self._node is not None:
            return
        import os

        os.environ["GZ_PARTITION"] = self.partition
        from gz.msgs.pose_v_pb2 import Pose_V
        from gz.transport import Node

        self._node = Node()

        def on_pose(msg) -> None:
            for pose in msg.pose:
                if pose.name == JOINT_PARENT_MODEL:
                    # 시스템 추가 서비스는 엔티티를 **id**로 찾는다(이름만 주면 거절된다).
                    self._parent_entity_id = pose.id
                if pose.name in self.objects:
                    # 방향은 **요청할 때만** 받는다(`pose7_of`). 실행 중 콜백 일을 늘리지
                    # 않는다. 방향을 먼저 넣는다 — 새 위치를 본 읽기는 같은 메시지의 방향을 본다.
                    if self._want_orientation:
                        self._orientations[pose.name] = (
                            pose.orientation.x, pose.orientation.y,
                            pose.orientation.z, pose.orientation.w)
                    self._poses[pose.name] = (pose.position.x, pose.position.y,
                                              pose.position.z)

        for topic in (f"/world/{self.world}/pose/info",
                      f"/world/{self.world}/dynamic_pose/info"):
            self._node.subscribe(Pose_V, topic, on_pose)
        # 첫 pose가 올 때까지 기다린다. 오지 않으면 관측이 없는 것이다.
        deadline = time.monotonic() + 5.0
        while not self._poses and time.monotonic() < deadline:
            time.sleep(0.05)

    # ── 관측 ────────────────────────────────────────────────────────────
    def pose_of(self, model: str, *, timeout_sec: float = 3.0,
                fresh: bool = False) -> tuple[float, float, float] | None:
        """물체 pose를 **관측해서** 돌려준다. 없으면 None이다."""
        self._ensure()
        if fresh:
            self._poses.pop(model, None)
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if model in self._poses:
                return self._poses[model]
            time.sleep(0.05)
        return self._poses.get(model)

    def pose7_of(self, model: str, *, timeout_sec: float = 3.0,
                 fresh: bool = False) -> tuple[float, ...] | None:
        """관측 위치 + 방향(x, y, z, qx, qy, qz, qw). 방향을 못 받았으면 None이다.

        방향은 이 호출 동안만 받는다 — 새 위치와 같은 메시지의 방향이다.
        """
        self._ensure()
        self._want_orientation = True
        try:
            self._orientations.pop(model, None)
            position = self.pose_of(model, timeout_sec=timeout_sec, fresh=True)
            deadline = time.monotonic() + timeout_sec
            while model not in self._orientations and time.monotonic() < deadline:
                time.sleep(0.02)
            orientation = self._orientations.get(model)
        finally:
            self._want_orientation = False
        if position is None or orientation is None:
            return None
        return tuple(position) + tuple(orientation)

    def _wait_for_pose(self, model: str, target: Sequence[float],
                       *, timeout_sec: float,
                       accept=None) -> tuple[bool, tuple | None]:
        """되읽은 pose가 조건을 만족하길 기다린다.

        기본 조건은 "목표와 허용치 안"이다. 떼는 순간에는 다른 조건을 쓴다 —
        동적으로 되돌린 물체는 **떨어지는 것이 정상**이므로 목표 높이에 그대로
        있기를 요구하면 정상 동작을 실패로 읽는다(실측).
        """
        if accept is None:
            def accept(observed):  # noqa: E306 — 기본 조건
                return _distance(observed, target) <= POSE_VERIFY_TOLERANCE_M

        deadline = time.monotonic() + timeout_sec
        last = None
        while time.monotonic() < deadline:
            self._poses.pop(model, None)
            last = self.pose_of(model, timeout_sec=0.5)
            if last is not None and accept(last):
                return True, last
            time.sleep(0.1)
        return False, last

    # ── 서비스 (응답을 결과로 쓰지 않는다) ──────────────────────────────
    def _remove(self, model: str) -> None:
        self._ensure()
        from gz.msgs.boolean_pb2 import Boolean
        from gz.msgs.entity_pb2 import Entity

        request = Entity()
        request.name = model
        request.type = Entity.MODEL
        self._node.request(f"/world/{self.world}/remove", request,
                           Entity, Boolean, 3000)

    def _create(self, sdf: str, pose: Sequence[float]) -> None:
        self._ensure()
        from gz.msgs.boolean_pb2 import Boolean
        from gz.msgs.entity_factory_pb2 import EntityFactory

        request = EntityFactory()
        request.sdf = sdf
        request.pose.position.x = float(pose[0])
        request.pose.position.y = float(pose[1])
        request.pose.position.z = float(pose[2])
        request.pose.orientation.w = 1.0
        self._node.request(f"/world/{self.world}/create", request,
                           EntityFactory, Boolean, 3000)

    def _set_pose(self, model: str, pose: Sequence[float]) -> None:
        self._ensure()
        from gz.msgs.boolean_pb2 import Boolean
        from gz.msgs.pose_pb2 import Pose

        request = Pose()
        request.name = model
        request.position.x = float(pose[0])
        request.position.y = float(pose[1])
        request.position.z = float(pose[2])
        request.orientation.w = 1.0
        # 짧은 제한시간을 쓴다. 따라가기는 고빈도로 돌아야 하고, 응답을
        # 기다리는 시간이 곧 물체가 뒤처지는 거리다.
        self._node.request(f"/world/{self.world}/set_pose", request,
                           Pose, Boolean, 300)

    def _replace(self, model: str, sdf: str, pose: Sequence[float],
                 *, kind: str, timeout_sec: float = 8.0,
                 accept=None) -> FixtureEvent:
        """물체를 지우고 다시 만든다. **되읽어 확인**하고 기록한다."""
        self._remove(model)
        time.sleep(0.4)
        self._create(sdf, pose)
        ok, observed = self._wait_for_pose(model, pose, timeout_sec=timeout_sec,
                                           accept=accept)
        event = FixtureEvent(
            kind=kind, model=model, at=time.time(), pose_m=observed,
            verified=ok,
            detail=("" if ok else
                    f"pose 확인 실패: 목표 {tuple(round(v, 4) for v in pose)}"
                    f" 관측 {observed}"))
        self.events.append(event)
        return event

    # ── 관절로 붙이기(DetachableJoint) ───────────────────────────────────
    @staticmethod
    def joint_topics(model: str) -> dict[str, str]:
        base = f"/forstick2/fixture/{model}"
        return {"attach": f"{base}/attach", "detach": f"{base}/detach",
                "state": f"{base}/state"}

    def _subscribe_joint_state(self, model: str) -> None:
        self._ensure()
        if model in self._joint_subscribed:
            return
        from gz.msgs.stringmsg_pb2 import StringMsg

        def on_state(msg) -> None:
            self._joint_state[model] = (str(msg.data), time.time())

        self._node.subscribe(StringMsg, self.joint_topics(model)["state"], on_state)
        self._joint_subscribed.add(model)

    def joint_system_loaded(self, model: str) -> bool:
        """그 자재의 붙임 시스템이 로봇 모델에 있는가(상태 토픽이 알려져 있는가)."""
        self._ensure()
        try:
            return self.joint_topics(model)["state"] in set(self._node.topic_list())
        except Exception:  # noqa: BLE001
            return False

    def _add_joint_system(self, model: str) -> None:
        """로봇 모델에 DetachableJoint 시스템을 넣는다 — **넣는 순간 붙는다.**"""
        from gz.msgs.boolean_pb2 import Boolean
        from gz.msgs.entity_plugin_v_pb2 import EntityPlugin_V
        from gz.msgs.entity_pb2 import Entity

        topics = self.joint_topics(model)
        deadline = time.monotonic() + 3.0
        while getattr(self, "_parent_entity_id", None) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if getattr(self, "_parent_entity_id", None) is None:
            raise FixtureError(ReasonCode.EXEC_SIM_FIXTURE_FAILED,
                               f"{JOINT_PARENT_MODEL}의 엔티티 id를 관측하지 못했다")
        request = EntityPlugin_V()
        request.entity.id = int(self._parent_entity_id)
        request.entity.name = JOINT_PARENT_MODEL
        request.entity.type = Entity.MODEL
        plugin = request.plugins.add()
        plugin.name = "gz::sim::systems::DetachableJoint"
        plugin.filename = "gz-sim-detachable-joint-system"
        plugin.innerxml = (
            f"<parent_link>{JOINT_PARENT_LINK}</parent_link>"
            f"<child_model>{model}</child_model><child_link>{JOINT_CHILD_LINK}</child_link>"
            f"<attach_topic>{topics['attach']}</attach_topic>"
            f"<detach_topic>{topics['detach']}</detach_topic>"
            f"<output_topic>{topics['state']}</output_topic>")
        self._node.request(f"/world/{self.world}/entity/system/add", request,
                           EntityPlugin_V, Boolean, 3000)

    def _publish_empty(self, topic: str) -> None:
        from gz.msgs.empty_pb2 import Empty

        if not hasattr(self, "_publishers"):
            self._publishers = {}
        publisher = self._publishers.get(topic)
        if publisher is None:
            publisher = self._node.advertise(topic, Empty)
            self._publishers[topic] = publisher
        # 구독자가 붙을 시간을 준다(새 publisher는 발견까지 수십 ms가 걸린다).
        started = time.monotonic()
        deadline = started + 1.0
        while not publisher.has_connections() and time.monotonic() < deadline:
            time.sleep(0.02)
        connected = publisher.has_connections()
        publisher.publish(Empty())
        # 진단: 보낼 때 구독자가 붙어 있었는지와 기다린 시간(알림이 빠지는 원인을 가르려고 남긴다).
        self.last_publish = {"topic": topic, "connected": bool(connected),
                             "waited_s": round(time.monotonic() - started, 3)}
        print(f"[fixture-diag] publish {topic} connected={connected} "
              f"waited={self.last_publish['waited_s']}s", flush=True)

    def _send_until(self, model: str, kind: str, want: str, since: float,
                    timeout_sec: float = JOINT_STATE_TIMEOUT_SEC) -> bool:
        """붙임/뗌 요청을 보내고 알림이 올 때까지 짧은 간격으로 **다시 보낸다**.

        새 프로세스가 gz-sim에 처음 보내는 메시지는 광고 직후 바로 보내면 빠질 수 있다 — 구독자가 이미
        알려져 `has_connections()`는 참인데 실제 전송 연결이 아직 없는 경우다(실측 2026-09-28: 세계를 새로
        띄운 뒤 첫 작업에서 두 번 모두, 붙임은 시스템 추가로 하고 뗌 요청이 그 프로세스의 첫 발행이었다 —
        알림 없음, 2 s 뒤 다시 보낸 뗌은 즉시 알림). 같은 요청을 다시 보내도 이미 그 상태면 시스템이 무시한다.
        """
        deadline = time.time() + timeout_sec
        while True:
            self._publish_empty(self.joint_topics(model)[kind])
            if self._wait_joint_state(model, want, since,
                                      timeout_sec=max(0.05, min(RESEND_SEC, deadline - time.time()))):
                return True
            if time.time() >= deadline:
                return False

    def _wait_joint_state(self, model: str, want: str, since: float,
                          timeout_sec: float = JOINT_STATE_TIMEOUT_SEC) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            state = self._joint_state.get(model)
            if state and state[0] == want and state[1] >= since:
                return True
            time.sleep(0.02)
        return False

    def _joint_attach(self, model: str) -> tuple[bool, str]:
        """관절로 붙인다. 시스템의 `attached` 알림으로 확인한다.

        시스템이 이미 있는데 알림이 오지 않으면 **이미 붙어 있다고 믿고 있는** 낡은 상태일
        수 있다(다른 실행기가 떼지 못하고 끝났을 때). 그러면 한 번 떼고 다시 붙인다.
        """
        self._subscribe_joint_state(model)
        started = time.time()
        ok, detail = self._joint_attach_inner(model, started)
        _write_joint_record(model, "attached" if ok else "unknown", detail)
        return ok, detail

    def _joint_attach_inner(self, model: str, started: float) -> tuple[bool, str]:
        if not self.joint_system_loaded(model):
            self._add_joint_system(model)
            if self._wait_joint_state(model, "attached", started):
                return True, "붙임 시스템을 넣었다(넣는 순간 붙음)"
            # 알림 토픽 발견이 늦어 첫 알림을 놓쳤을 수 있다 — 떼었다 다시 붙여 확인한다.
        if self._send_until(model, "attach", "attached", started):
            return True, "attach 토픽으로 붙였다"
        again = time.time()
        self._publish_empty(self.joint_topics(model)["detach"])
        self._wait_joint_state(model, "detached", again)
        again = time.time()
        self._publish_empty(self.joint_topics(model)["attach"])
        if self._wait_joint_state(model, "attached", again):
            return True, "낡은 붙임 상태를 떼고 다시 붙였다"
        return False, "붙임 시스템이 attached를 알리지 않았다"

    def _joint_detach(self, model: str) -> tuple[bool, str]:
        self._subscribe_joint_state(model)
        if not self.joint_system_loaded(model):
            return True, "붙임 시스템이 없다(붙어 있지 않다)"
        started = time.time()
        if self._send_until(model, "detach", "detached", started):
            _write_joint_record(model, "detached", "detach 토픽 → detached 알림")
            return True, "detach 토픽으로 뗐다"
        _write_joint_record(model, "unknown", "detach 뒤 알림 없음")
        return False, ("붙임 시스템이 detached를 알리지 않았다(이미 떨어져 있었을 수 있다)"
                       f" · 보냄 {getattr(self, 'last_publish', None)}"
                       f" · 마지막 알림 {self._joint_state.get(model)}")

    def release_joint_if_any(self, model: str) -> None:
        """복원·배치 전에 관절이 남아 있으면 뗀다(지운 모델을 가리키는 관절을 남기지 않는다)."""
        if self.mode == "joint" and self.joint_system_loaded(model):
            self._joint_detach(model)
            self._held.discard(model)

    # ── 붙이고 떼기 ─────────────────────────────────────────────────────
    def attach(self, model: str, *, pose_m: Sequence[float]) -> FixtureEvent:
        """물체를 도구에 붙인다. **실제 파지가 아니다.**

        joint: 지금 자리 그대로 그리퍼 링크에 고정 관절로 잇는다(모델을 바꾸지 않는다).
        static: 정적 모델로 바꿔 `set_pose`로 따라가게 한다(이전 방식).
        """
        item = self._declaration(model)
        if self.mode == "joint":
            ok, detail = self._joint_attach(model)
            time.sleep(0.2)
            observed = self.pose_of(model, timeout_sec=2.0, fresh=True)
            moved = (None if observed is None else _distance(observed, pose_m))
            # 붙이는 동안 자재가 밀리지 않았는가(손가락 안에 그대로 있는가).
            ok = ok and moved is not None and moved <= POSE_VERIFY_TOLERANCE_M * 2
            event = FixtureEvent(kind="attach", model=model, at=time.time(),
                                 pose_m=observed, verified=ok,
                                 detail=detail if ok else f"{detail} · 이동 {moved}")
            event.extra = {"mode": "joint", "parent_link": JOINT_PARENT_LINK}
            self.events.append(event)
            if not ok:
                raise FixtureError(ReasonCode.EXEC_SIM_FIXTURE_FAILED,
                                   f"{model}을 붙이지 못했다 — {event.detail}")
            self._held.add(model)
            return event
        event = self._replace(model, static_sdf(item), pose_m, kind="attach")
        if not event.verified:
            raise FixtureError(
                ReasonCode.EXEC_SIM_FIXTURE_FAILED,
                f"{model}을 붙이지 못했다 — {event.detail}")
        self._held.add(model)
        return event

    def follow(self, model: str, pose_m: Sequence[float]) -> None:
        """붙어 있는 물체를 도구 위치로 옮긴다. 기록은 남기지 않는다(고빈도).

        joint 방식에서는 **아무것도 하지 않는다** — 물리가 관절로 함께 움직인다.
        """
        if model not in self._held:
            raise FixtureError(
                ReasonCode.EXEC_SIM_FIXTURE_FAILED,
                f"{model}은 붙어 있지 않다 — 따라가게 할 수 없다")
        if self.mode == "joint":
            return
        self._set_pose(model, pose_m)

    def rebind(self, model: str, *, pose_m: Sequence[float]) -> FixtureEvent:
        """재개할 때 이미 든 자재를 이 실행기에 다시 묶는다.

        joint: 관절은 Gazebo에 남아 있다 — 시스템 상태를 확인하고 이 프로세스의 붙임
        목록에만 넣는다(다시 붙이지 않는다). static: 이전과 같이 다시 붙인다.
        """
        if self.mode != "joint":
            return self.attach(model, pose_m=pose_m)
        loaded = self.joint_system_loaded(model)
        observed = self.pose_of(model, timeout_sec=2.0, fresh=True)
        ok = loaded and observed is not None and _distance(observed, pose_m) <= 0.01
        event = FixtureEvent(kind="rebind", model=model, at=time.time(), pose_m=observed,
                             verified=ok,
                             detail="" if ok else f"붙임 시스템 {loaded} · 관측 {observed}")
        event.extra = {"mode": "joint"}
        self.events.append(event)
        if not ok:
            raise FixtureError(ReasonCode.EXEC_SIM_FIXTURE_FAILED,
                               f"{model}을 다시 묶지 못했다 — {event.detail}")
        self._held.add(model)
        return event

    def detach(self, model: str, *, pose_m: Sequence[float]) -> FixtureEvent:
        """물체를 동적으로 되돌린다. 이 뒤에는 **실제로 떨어져 안착한다.**

        확인 조건은 "같은 수평 위치에 있고, 높이가 해제 높이보다 높지 않다"다.
        떨어지는 것이 정상이므로 해제 높이를 그대로 요구하지 않는다. 실제로
        어디에 안착했는지는 `settle()`과 배치 구역 판정이 확인한다.
        """
        item = self._declaration(model)
        target_z = float(pose_m[2])

        def accept(observed) -> bool:
            horizontal = _distance((observed[0], observed[1], 0.0),
                                   (pose_m[0], pose_m[1], 0.0))
            return (horizontal <= POSE_VERIFY_TOLERANCE_M
                    and observed[2] <= target_z + POSE_VERIFY_TOLERANCE_M)

        if self.mode == "joint":
            ok, detail = self._joint_detach(model)
            passed, observed = self._wait_for_pose(model, pose_m, timeout_sec=4.0,
                                                   accept=accept)
            event = FixtureEvent(kind="detach", model=model, at=time.time(),
                                 pose_m=observed, verified=ok and passed,
                                 detail="" if ok and passed else
                                 f"{detail} · 관측 {observed}")
            event.extra = {"mode": "joint"}
            self.events.append(event)
        else:
            event = self._replace(model, dynamic_sdf(item), pose_m, kind="detach",
                                  accept=accept)
        if event.pose_m is not None:
            event.extra = {
                "released_at_z_m": round(target_z, 6),
                "observed_z_m": round(float(event.pose_m[2]), 6),
                "dropped_m": round(target_z - float(event.pose_m[2]), 6),
                "accept_rule": "수평 위치 유지 · 높이는 해제 높이 이하"
                               " (동적으로 되돌린 물체는 떨어지는 것이 정상)",
            }
        self._held.discard(model)
        if not event.verified:
            raise FixtureError(
                ReasonCode.EXEC_SIM_FIXTURE_FAILED,
                f"{model}을 떼지 못했다 — {event.detail}")
        return event

    def settle(self, model: str) -> tuple[float, float, float] | None:
        """떨어진 물체가 멈출 때까지 기다린 뒤 pose를 관측한다."""
        time.sleep(self.settle_sec)
        previous = self.pose_of(model, timeout_sec=2.0, fresh=True)
        for _ in range(20):
            time.sleep(0.2)
            current = self.pose_of(model, timeout_sec=1.0, fresh=True)
            if current is None or previous is None:
                previous = current
                continue
            if _distance(current, previous) < 0.0005:
                return current
            previous = current
        return previous

    def place(self, model: str, pose_m: Sequence[float]) -> FixtureEvent:
        """동적 물체를 지정한 자리에 다시 만든다(검증 준비·정리용).

        시연 경로가 아니다. 검증에서 "물체가 선언된 자리에 없다"나 "배치 구역
        밖" 같은 상황을 **실제로** 만들 때 쓴다.
        """
        item = self._declaration(model)
        target_z = float(pose_m[2])

        def accept(observed) -> bool:
            horizontal = _distance((observed[0], observed[1], 0.0),
                                   (pose_m[0], pose_m[1], 0.0))
            return (horizontal <= 0.05
                    and observed[2] <= target_z + POSE_VERIFY_TOLERANCE_M)

        self.release_joint_if_any(model)
        event = self._replace(model, dynamic_sdf(item), pose_m, kind="place",
                              accept=accept)
        self._held.discard(model)
        return event

    def restore(self, model: str) -> FixtureEvent:
        """물체를 **선언된 원래 자리**의 동적 물체로 되돌린다(시연 정리)."""
        item = self._declaration(model)
        self.release_joint_if_any(model)
        event = self._replace(model, dynamic_sdf(item), item.home_pose_m,
                              kind="restore")
        self._held.discard(model)
        return event

    def held(self) -> tuple[str, ...]:
        return tuple(sorted(self._held))

    def close(self) -> None:
        """구독을 끊고 노드를 버린다.

        **끝내기 전에 반드시 부른다.** gz 구독 콜백은 C++ 스레드에서 파이썬을
        호출하는데, 인터프리터가 정리되는 중에 콜백이 들어오면 프로세스가
        abort한다(실측: `cb_deserialize`에서 `TypeError` → `terminate called`).
        """
        if self._node is None:
            return
        for topic in (f"/world/{self.world}/pose/info",
                      f"/world/{self.world}/dynamic_pose/info",
                      *(self.joint_topics(m)["state"] for m in self._joint_subscribed)):
            try:
                self._node.unsubscribe(topic)
            except Exception:  # noqa: BLE001 — 끊지 못해도 진행한다
                pass
        time.sleep(0.3)
        self._node = None

    def _declaration(self, model: str) -> ObjectDeclaration:
        item = self.objects.get(model)
        if item is None:
            raise FixtureError(
                ReasonCode.EXEC_SIM_FIXTURE_FAILED,
                f"선언되지 않은 물체다: {model}")
        return item

    def event_dicts(self) -> tuple[dict, ...]:
        return tuple(event.to_dict() for event in self.events)


def _distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


def declarations_from_config(config: Mapping[str, Any], frames: Mapping[str, Any],
                             source: str = "") -> dict[str, ObjectDeclaration]:
    """작업 셀 설정에서 물체 선언을 만든다. **값을 보충하지 않는다.**"""

    def resolve(name: str | None) -> tuple[float, float, float]:
        out = [0.0, 0.0, 0.0]
        while name is not None:
            frame = frames[name]
            out = [a + b for a, b in zip(out, frame["xyz_m"])]
            name = frame["parent"]
        return (out[0], out[1], out[2])

    out: dict[str, ObjectDeclaration] = {}
    for name, model in config.items():
        if model.get("kind") != "material":
            continue
        out[name] = ObjectDeclaration(
            model=name,
            size_m=tuple(float(v) for v in model["size_m"]),
            mass_kg=float(model["mass_kg"]),
            friction=float(model["friction"]),
            color_rgba=tuple(float(v) for v in model["color_rgba"]),
            home_pose_m=resolve(model["frame"]),
            source=source,
        )
    return out
