"""Three.js 작업 셀 화면의 **읽기 전용** 데이터 (모델 · 관측 상태).

Gazebo·MoveIt이 실행과 관측을 맡는다. 이 모듈은 화면이 그릴 것만 준다.

- 모델: Gazebo에 올라간 조립 URDF(로봇 어댑터가 위치를 준다)에서 **시각 메시만**
  남기고, 메시 경로를 이 서버의 허용 목록 경로(`/v1/sim-view/mesh/<n>/<파일>`)로
  바꾼다. 셀 상자(받침대·작업대·팔레트·컨베이어)와 자재 크기·색은 셀 설정에서 온다.
- 상태: 관절·그리퍼는 `/joint_states`(ros2_control), 자재 pose는 Gazebo
  `pose/info`에서 **관측한 값만** 준다. 받은 시각(서버 벽시계)과 나이를 함께 준다.
  낡았으면 `stale=true` — 화면이 움직이는 것처럼 꾸미지 않게 한다.
- 로봇에 명령을 보내지 않는다. 구독만 한다.

gz 구독은 원본 바이트만 저장하고(콜백 비용 최소 — 실행기 추종 지연 실측 참고),
화면이 요청할 때만 해석한다. 전송 계층에서 초당 개수를 줄인다(`msgs_per_sec`).
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
#: 이 나이(초)를 넘은 관측은 낡았다. 화면은 멈추고 표시한다.
STALE_AFTER_SEC = 0.5
#: gz pose/info 구독 상한(초당 메시지). 화면 갱신(30 Hz)보다 여유 있게.
POSE_MSGS_PER_SEC = 60.0
def load_robot_model(urdf_path: Path, resolve_mesh,
                     url_prefix: str = "/v1/sim-view/mesh") -> tuple[str, dict[str, Path], list[str]]:
    """(화면용 URDF 문자열, 메시 번호 → 파일, 파일이 없어 뺀 메시).

    충돌 형상·플러그인은 뺀다. 메시 파일이 없는 시각 요소는 **빼고 알린다**
    (Gazebo도 그 메시를 그리지 못한다). `resolve_mesh(파일명)`은 로봇 어댑터가 준다.
    """
    text = Path(urdf_path).read_text(encoding="utf-8")
    text = re.sub(r"<collision\b.*?</collision>", "", text, flags=re.S)
    text = re.sub(r"<gazebo\b.*?</gazebo>", "", text, flags=re.S)
    text = re.sub(r"<ros2_control\b.*?</ros2_control>", "", text, flags=re.S)
    meshes: dict[str, Path] = {}
    missing: list[str] = []
    resolve = resolve_mesh

    def visual(block: re.Match) -> str:
        body = block.group(0)
        found = re.search(r'filename="([^"]+)"', body)
        if not found:
            return body                       # 기본 도형(box 등)은 그대로
        target = resolve(found.group(1))
        if target is None or not target.is_file():
            missing.append(found.group(1))
            return ""
        key = str(len(meshes))
        meshes[key] = target
        return body.replace(found.group(0),
                            f'filename="{url_prefix}/{key}/{target.name}"')

    text = re.sub(r"<visual\b.*?</visual>", visual, text, flags=re.S)
    return text, meshes, missing


def cell_boxes(workcell: Mapping[str, Any]) -> dict:
    """셀 설정 → 화면 상자(월드 좌표, 회전 없음). 자재는 크기·색·선언 자리."""
    frames = workcell["frames"]

    def resolve(name: str) -> list[float]:
        out = [0.0, 0.0, 0.0]
        while name:
            frame = frames[name]
            out = [a + b for a, b in zip(out, frame.get("xyz_m") or (0, 0, 0))]
            name = frame.get("parent")
        return out

    fixed, materials = [], []
    # 자재 이름·자원 id는 셀 설정의 resource_map에서(서버 자재 정보와 같은 출처).
    named = {row.get("gazebo_model"): row for row in workcell.get("resource_map") or ()}
    for model, spec in (workcell.get("models") or {}).items():
        kind = spec.get("kind")
        if kind == "ground_plane":
            continue
        origin = resolve(spec["frame"])
        if kind == "material":
            # shape: Gazebo world 생성기(scripts/build_workcell_world.py)와 같은 값 — 외곽 size_m 안의
            # box | cylinder(지름 = 짧은 변) | triangle_prism(한 변 = 짧은 변, 꼭짓점 +x). 없으면 box.
            row = named.get(model) or {}
            materials.append({"model": model, "size_m": list(spec["size_m"]),
                              "shape": spec.get("shape", "box"),
                              "resource_id": spec.get("resource_id") or row.get("resource_id"),
                              "name": row.get("korean"),
                              "color_rgba": list(spec.get("color_rgba") or (0.7, 0.7, 0.7, 1)),
                              "home_xyz_m": origin})
            continue
        for part in spec.get("parts") or ():
            fixed.append({"id": f"{model}__{part['name']}", "kind": kind,
                          "size_m": list(part["size_m"]),
                          "center_xyz_m": [a + b for a, b in zip(origin, part["center_xyz_m"])],
                          "color_rgba": list(part.get("color_rgba") or (0.6, 0.6, 0.6, 1))})
    return {"fixed": fixed, "materials": materials}


class SimViewState:
    """관절(`/joint_states`)·자재 pose(gz `pose/info`) 최신값. 구독만 한다."""

    def __init__(self, *, world: str, partition: str, robot_model: str,
                 materials: list[str], joints: tuple[str, ...] = (), clock=time.time):
        self.world, self.partition = world, partition
        self.joint_names = tuple(joints)
        self.robot_model = robot_model
        self.materials = list(materials)
        self._clock = clock
        self._lock = threading.Lock()
        self._joints: tuple[dict, float, int] | None = None     # (값, 받은 시각, seq)
        self._pose_raw: tuple[bytes, float, int] | None = None
        self._joint_seq = 0
        self._pose_seq = 0
        self._gz_node = None
        self._ros = None
        self.detail = "시작 전"

    # ── 구독 ───────────────────────────────────────────────────────
    def start(self) -> bool:
        ok_joint = self._start_joints()
        ok_pose = self._start_poses()
        return ok_joint and ok_pose

    def _start_joints(self) -> bool:
        try:
            import rclpy
            from rclpy.executors import SingleThreadedExecutor
            from rclpy.node import Node
            from rclpy.qos import qos_profile_sensor_data
            from sensor_msgs.msg import JointState
        except Exception as exc:  # noqa: BLE001
            self.detail = f"rclpy를 쓸 수 없다: {exc}"[:200]
            return False
        try:
            if not rclpy.ok():
                rclpy.init()
            node = Node("forstick2_sim_view")
            wanted = set(self.joint_names)

            def on_joints(message) -> None:
                values = {n: float(p) for n, p in zip(message.name, message.position)
                          if n in wanted}
                if not values:
                    return
                with self._lock:
                    self._joint_seq += 1
                    self._joints = (values, self._clock(), self._joint_seq)

            node.create_subscription(JointState, "/joint_states", on_joints,
                                     qos_profile_sensor_data)
            executor = SingleThreadedExecutor()
            executor.add_node(node)
            thread = threading.Thread(target=executor.spin, daemon=True,
                                      name="sim-view-joints")
            thread.start()
            self._ros = (node, executor, thread)
            return True
        except Exception as exc:  # noqa: BLE001
            self.detail = f"관절 구독 실패: {exc}"[:200]
            return False

    def _start_poses(self) -> bool:
        try:
            os.environ["GZ_PARTITION"] = self.partition
            from gz.transport import Node, SubscribeOptions
        except Exception as exc:  # noqa: BLE001
            self.detail = f"gz transport를 쓸 수 없다: {exc}"[:200]
            return False
        node = Node()
        options = SubscribeOptions()
        options.msgs_per_sec = int(POSE_MSGS_PER_SEC)

        def on_raw(data: bytes, _info) -> None:
            # 해석하지 않는다 — 원본만 둔다(콜백을 가볍게).
            with self._lock:
                self._pose_seq += 1
                self._pose_raw = (bytes(data), self._clock(), self._pose_seq)

        if not node.subscribe_raw(f"/world/{self.world}/pose/info", on_raw,
                                  "gz.msgs.Pose_V", options):
            self.detail = "pose/info 구독 실패"
            return False
        self._gz_node = node
        self.detail = "관절·자재 pose 구독 중"
        return True

    def attached_models(self) -> tuple[set[str] | None, str]:
        """지금 그리퍼에 붙어 있는 자재(월드 상태의 붙임 관절, 읽기 전용). 모르면 (None, 이유)."""
        from robots.fr3_gazebo.joint_observation import observe_attached_isolated

        if self._gz_node is None:
            return None, "Gazebo 구독이 없다"
        # 이 프로세스는 pose를 구독 중이라 여기서 서비스 요청을 하면 응답을 받지 못한다 — 새 프로세스에서 조회한다.
        return observe_attached_isolated(self.world, self.partition)

    def close(self) -> None:
        """구독을 끊는다. 서버 종료 때 부른다(인터프리터 정리 중 gz 콜백 방지)."""
        if self._gz_node is not None:
            try:
                self._gz_node.unsubscribe(f"/world/{self.world}/pose/info")
            except Exception:  # noqa: BLE001
                pass
            self._gz_node = None
            time.sleep(0.2)
        if self._ros is not None:
            node, executor, _thread = self._ros
            try:
                executor.shutdown(timeout_sec=1.0)
                node.destroy_node()
            except Exception:  # noqa: BLE001
                pass
            self._ros = None

    # ── 표본 ───────────────────────────────────────────────────────
    def sample(self) -> dict:
        """화면에 보낼 최신 관측. 없거나 낡았으면 그렇다고 적는다."""
        with self._lock:
            joints, pose_raw = self._joints, self._pose_raw
        now = self._clock()
        out: dict[str, Any] = {"server_time": now, "stale_after_sec": STALE_AFTER_SEC,
                               "joints": None, "joint_time": None, "joint_seq": None,
                               "materials": {}, "robot_pose": None, "pose_time": None,
                               "pose_seq": None, "pose_sim_time": None}
        if joints is not None:
            out.update(joints=joints[0], joint_time=joints[1], joint_seq=joints[2])
        if pose_raw is not None:
            decoded = self._decode(pose_raw[0])
            if decoded is not None:
                out.update(materials=decoded["materials"], robot_pose=decoded["robot"],
                           pose_time=pose_raw[1], pose_seq=pose_raw[2],
                           pose_sim_time=decoded["sim_time"])
        ages = [now - t for t in (out["joint_time"], out["pose_time"]) if t is not None]
        out["joint_age_sec"] = None if out["joint_time"] is None else now - out["joint_time"]
        out["pose_age_sec"] = None if out["pose_time"] is None else now - out["pose_time"]
        out["stale"] = (len(ages) < 2 or max(ages) > STALE_AFTER_SEC
                        or len(out["materials"]) < len(self.materials))
        return out

    def _decode(self, data: bytes) -> dict | None:
        try:
            from gz.msgs.pose_v_pb2 import Pose_V
        except Exception:  # noqa: BLE001
            return None
        message = Pose_V()
        try:
            message.ParseFromString(data)
        except Exception:  # noqa: BLE001
            return None
        wanted = set(self.materials)
        materials, robot = {}, None
        for pose in message.pose:
            if pose.name in wanted or pose.name == self.robot_model:
                p, q = pose.position, pose.orientation
                values = [p.x, p.y, p.z, q.x, q.y, q.z, q.w]
                if pose.name == self.robot_model:
                    robot = values
                else:
                    materials[pose.name] = values
        stamp = message.header.stamp
        return {"materials": materials, "robot": robot,
                "sim_time": stamp.sec + stamp.nsec * 1e-9}


class SimView:
    """런타임에 붙는 묶음: 화면 모델(한 번 읽어 둠) + 관측 상태 구독.

    `source`는 로봇 어댑터의 `build_view_source()` 결과다(URDF 경로 · 메시 해석 ·
    관절 이름 · Gazebo 모델 이름). 공통 코드는 로봇 이름을 모른다.
    """

    #: 화면으로 보내는 상태 주기 상한(Hz).
    STREAM_HZ = 30.0

    def __init__(self, workcell_path: Path, source: Mapping[str, Any]):
        import json

        self.workcell = json.loads(Path(workcell_path).read_text(encoding="utf-8"))
        self.source = dict(source)
        self._model: dict | None = None
        self._meshes: dict[str, Path] = {}
        self.model_error = ""
        materials = [m["model"] for m in cell_boxes(self.workcell)["materials"]]
        joints = tuple(self.source["arm_joints"]) + (self.source["gripper_joint"],)
        self.state = SimViewState(
            world=str(self.workcell.get("world_name") or ""),
            partition=str(self.workcell.get("gz_partition") or ""),
            robot_model=str(self.source["robot_model"]), materials=materials,
            joints=joints)

    def start(self) -> bool:
        return self.state.start()

    def close(self) -> None:
        self.state.close()

    def model(self) -> dict:
        if self._model is None:
            try:
                urdf, meshes, missing = load_robot_model(self.source["urdf_path"],
                                                         self.source["resolve_mesh"])
            except OSError as exc:
                self.model_error = f"조립 URDF를 읽지 못했다: {exc}"[:200]
                return {"available": False, "detail": self.model_error}
            self._meshes = meshes
            self._model = {
                "available": True, "urdf": urdf, "cell": cell_boxes(self.workcell),
                "arm_joints": list(self.source["arm_joints"]),
                "gripper_joint": self.source["gripper_joint"],
                "missing_meshes": missing, "stale_after_sec": STALE_AFTER_SEC,
                "stream_hz": self.STREAM_HZ, "is_simulated": True,
                "note": "읽기 전용 화면이다. 관측(Gazebo)만 그린다 — 명령을 보내지 않는다",
            }
        return self._model

    def mesh(self, key: str, name: str) -> Path | None:
        """허용 목록에 있는 메시 파일만. 이름까지 맞아야 한다."""
        self.model()
        path = self._meshes.get(key)
        if path is None or path.name != name or not path.is_file():
            return None
        return path
