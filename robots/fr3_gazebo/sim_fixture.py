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
        self._held: set[str] = set()

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
                if pose.name in self.objects:
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

    # ── 붙이고 떼기 ─────────────────────────────────────────────────────
    def attach(self, model: str, *, pose_m: Sequence[float]) -> FixtureEvent:
        """물체를 정적으로 바꿔 도구에 붙인다. **실제 파지가 아니다.**"""
        item = self._declaration(model)
        event = self._replace(model, static_sdf(item), pose_m, kind="attach")
        if not event.verified:
            raise FixtureError(
                ReasonCode.EXEC_SIM_FIXTURE_FAILED,
                f"{model}을 붙이지 못했다 — {event.detail}")
        self._held.add(model)
        return event

    def follow(self, model: str, pose_m: Sequence[float]) -> None:
        """붙어 있는 물체를 도구 위치로 옮긴다. 기록은 남기지 않는다(고빈도)."""
        if model not in self._held:
            raise FixtureError(
                ReasonCode.EXEC_SIM_FIXTURE_FAILED,
                f"{model}은 붙어 있지 않다 — 따라가게 할 수 없다")
        self._set_pose(model, pose_m)

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

        event = self._replace(model, dynamic_sdf(item), pose_m, kind="place",
                              accept=accept)
        self._held.discard(model)
        return event

    def restore(self, model: str) -> FixtureEvent:
        """물체를 **선언된 원래 자리**의 동적 물체로 되돌린다(시연 정리)."""
        item = self._declaration(model)
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
                      f"/world/{self.world}/dynamic_pose/info"):
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
