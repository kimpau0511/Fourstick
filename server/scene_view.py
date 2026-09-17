"""작업 셀 장면 영상 (md/개발플랜.md 8-09 우선 작업 3).

Gazebo **서버가 렌더링하는** 장면 카메라 프레임을 받아 PNG로 돌려준다.

왜 이것이 필요한가: 이 환경의 Xwayland는 DRI3를 노출하지 않아 Gazebo GUI가
소프트웨어 렌더링만 할 수 있고, 부하가 오르면 GUI가 프레임을 완성하지 못한다
(실측: GUI 클라이언트 CPU 755%, 모든 GUI 서비스 무응답). 같은 장면을 서버는
81% CPU로 렌더링한다. 그래서 **사용자가 셀을 보는 경로를 웹 화면에 둔다.**

- 표준 라이브러리만 쓴다(PNG 인코딩 포함).
- 프레임을 못 받으면 **빈 그림을 만들지 않는다.** 이유를 담아 실패로 돌려준다.
- 카메라가 꺼져 있으면 그 사실을 알린다.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import subprocess
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class SceneFrame:
    """한 프레임. `png`가 None이면 받지 못했다는 뜻이다."""

    png: bytes | None
    width: int = 0
    height: int = 0
    captured_at: float = 0.0
    detail: str = ""
    reason_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.png is not None


def encode_png(raw: bytes, width: int, height: int, channels: int) -> bytes:
    """RGB8/RGBA8 바이트를 PNG로. 외부 의존성을 쓰지 않는다."""
    stride = width * channels
    rows = b"".join(b"\x00" + raw[y * stride:(y + 1) * stride]
                    for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    colour_type = 2 if channels == 3 else 6
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8,
                                         colour_type, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6))
            + chunk(b"IEND", b""))


class RosFrameSource:
    """ROS로 브리지된 장면 이미지를 **계속 구독해** 최신 프레임을 들고 있다.

    실시간 표시를 위해서다. 요청마다 `gz topic`을 새로 띄우면 프레임이 1~4초
    낡는다(토픽 탐색 + 다음 발행 대기). 구독을 유지하면 마지막 프레임이 항상
    최신이다 — forstick 8090 데모와 같은 방식이다.

    rclpy를 쓸 수 없거나 브리지가 없으면 **조용히 실패한다.** 호출자가 느린
    경로로 떨어지고, 그 사실을 화면에 적는다.
    """

    def __init__(self, topic: str, node_name: str = "forstick2_scene_view"):
        self._topic = topic
        self._node_name = node_name
        self._lock = threading.Lock()
        self._latest: tuple[bytes, int, int, int, float] | None = None
        self._seq = 0
        self._started = False
        self._detail = ""

    @property
    def detail(self) -> str:
        return self._detail

    def start(self) -> bool:
        if self._started:
            return True
        try:
            import rclpy
            from rclpy.executors import SingleThreadedExecutor
            from rclpy.node import Node
            from rclpy.qos import qos_profile_sensor_data
            from sensor_msgs.msg import Image
        except Exception as exc:  # noqa: BLE001 — rclpy가 없으면 느린 경로다
            self._detail = f"rclpy를 쓸 수 없다: {exc}"[:200]
            return False
        try:
            if not rclpy.ok():
                rclpy.init()
            node = Node(self._node_name)

            def on_image(message) -> None:
                channels = {"rgb8": 3, "rgba8": 4, "bgr8": 3}.get(
                    message.encoding, 0)
                if channels == 0:
                    self._detail = f"모르는 인코딩: {message.encoding}"
                    return
                data = bytes(message.data)
                if message.encoding == "bgr8":
                    # BGR을 RGB로 바꾼다(브라우저 canvas는 RGB를 기대한다).
                    view = bytearray(data)
                    view[0::3], view[2::3] = data[2::3], data[0::3]
                    data = bytes(view)
                with self._lock:
                    self._seq += 1
                    self._latest = (data, int(message.width), int(message.height),
                                    channels, time.time())

            node.create_subscription(Image, self._topic, on_image,
                                     qos_profile_sensor_data)
            executor = SingleThreadedExecutor()
            executor.add_node(node)
            thread = threading.Thread(target=executor.spin, daemon=True)
            thread.start()
            self._node, self._executor, self._thread = node, executor, thread
            self._started = True
            self._detail = f"{self._topic} 구독 중"
            return True
        except Exception as exc:  # noqa: BLE001
            self._detail = f"구독을 만들 수 없다: {exc}"[:200]
            return False

    def latest(self, max_age_sec: float) -> tuple | None:
        """최신 프레임. 너무 낡았으면 None이다(낡은 그림을 실시간이라 하지 않는다)."""
        with self._lock:
            frame = self._latest
            seq = self._seq
        if frame is None:
            return None
        data, width, height, channels, captured_at = frame
        if time.time() - captured_at > max_age_sec:
            return None
        return data, width, height, channels, captured_at, seq


class SceneViewer:
    """장면 카메라 프레임 공급자.

    빠른 경로: ROS로 브리지된 토픽을 계속 구독한다(실시간).
    느린 경로: 구독을 못 만들면 요청마다 `gz topic`을 한 번 호출한다.
    """

    def __init__(self, workcell_config: Path, *, cache_sec: float = 1.0,
                 timeout_sec: float = 20.0, max_age_sec: float = 3.0):
        self._config_path = Path(workcell_config)
        self._cache_sec = cache_sec
        self._timeout = timeout_sec
        self._max_age = max_age_sec
        self._cached: SceneFrame | None = None
        self._source: RosFrameSource | None = None

    def _live_source(self) -> RosFrameSource | None:
        if self._source is not None:
            return self._source if self._source._started else None
        data = self._config()
        camera = (data or {}).get("scene_camera") or {}
        topic = camera.get("ros_topic")
        if not topic:
            return None
        source = RosFrameSource(topic)
        self._source = source
        return source if source.start() else None

    def raw(self) -> tuple | None:
        """실시간 표시용 원본 프레임. 인코딩하지 않는다(CPU를 쓰지 않는다)."""
        source = self._live_source()
        if source is None:
            return None
        return source.latest(self._max_age)

    def live_detail(self) -> str:
        source = self._source
        return "" if source is None else source.detail

    def _config(self) -> dict | None:
        if not self._config_path.is_file():
            return None
        try:
            return json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def available(self) -> tuple[bool, str]:
        data = self._config()
        if data is None:
            return False, "작업 셀 설정을 읽을 수 없다"
        camera = data.get("scene_camera") or {}
        if not camera.get("enabled"):
            return False, "장면 카메라가 꺼져 있다(config/workcell scene_camera)"
        return True, ""

    def frame(self) -> SceneFrame:
        now = time.time()
        # 구독 중이면 최신 프레임을 바로 인코딩한다(느린 캡처를 하지 않는다).
        live = self.raw()
        if live is not None:
            data, width, height, channels, captured_at, _ = live
            return SceneFrame(png=encode_png(data, width, height, channels),
                              width=width, height=height,
                              captured_at=captured_at,
                              detail="ROS 브리지 구독 프레임")
        if (self._cached is not None and self._cached.ok
                and now - self._cached.captured_at < self._cache_sec):
            return self._cached
        ok, detail = self.available()
        if not ok:
            return SceneFrame(None, detail=detail, reason_code="config.missing",
                              captured_at=now)
        data = self._config()
        world = data["world_name"]
        camera = data["scene_camera"]
        topic = f"/world/{world}/model/scene_camera/link/link/sensor/camera/image"
        env = dict(os.environ)
        env.setdefault("GZ_PARTITION", data.get("gz_partition", ""))
        try:
            done = subprocess.run(
                ["gz", "topic", "-e", "-t", topic, "-n", "1", "--json-output"],
                capture_output=True, text=True, timeout=self._timeout, env=env)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return SceneFrame(None, captured_at=now,
                              detail=f"프레임을 받지 못했다: {exc}"[:200],
                              reason_code="exec.unverifiable")
        lines = done.stdout.strip().splitlines()
        if not lines:
            return SceneFrame(
                None, captured_at=now,
                detail=f"프레임이 비었다 ({topic})", reason_code="exec.unverifiable")
        try:
            payload = json.loads(lines[-1])
            width, height = int(payload["width"]), int(payload["height"])
            pixel_format = payload.get("pixelFormatType", "")
            channels = 4 if "RGBA" in pixel_format else 3
            raw = base64.b64decode(payload["data"])
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            return SceneFrame(None, captured_at=now,
                              detail=f"프레임을 해석할 수 없다: {exc}"[:200],
                              reason_code="exec.unverifiable")
        if len(raw) != width * height * channels:
            return SceneFrame(
                None, captured_at=now,
                detail=f"바이트 수가 맞지 않는다: {len(raw)}"
                       f" != {width * height * channels}",
                reason_code="exec.unverifiable")
        frame = SceneFrame(
            png=encode_png(raw, width, height, channels),
            width=width, height=height, captured_at=now,
            detail=f"카메라 자세 {camera.get('pose')}")
        self._cached = frame
        return frame
