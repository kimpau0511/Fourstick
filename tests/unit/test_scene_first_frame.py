"""첫 장면 프레임 대기 단위 검증.

재시작 직후 첫 요청이 구독만 만들고 빈 버퍼를 읽어 503/4503이 되던 문제를
막는다. 확인하는 것:

- 프레임이 이미 있으면 즉시 돌려준다.
- 늦게 도착한 첫 프레임은 raw·stream 모두 정상으로 돌려준다.
- 상한(3초) 안에 오지 않으면 기존처럼 raw=503, stream=4503이다.
- 구독 시작은 호출자를 막지 않는다.

**ROS 없이** 돈다. 구독 대신 내부 버퍼를 직접 채운다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import server.scene_view as scene_view
from server.routes import scene as scene_route
from server.routes.common import EventHub, RouteContext
from server.scene_view import FIRST_FRAME_WAIT_SEC, RosFrameSource, SceneViewer

CONFIG = Path("/tmp/forstick2_test_scene_first_frame.json")


def viewer_with_source() -> tuple[SceneViewer, RosFrameSource]:
    """구독이 이미 시작된 것처럼 만든 뷰어. 버퍼는 비어 있다."""
    CONFIG.write_text(json.dumps({
        "world_name": "w",
        "scene_camera": {"enabled": True, "ros_topic": "/scene_camera/image"},
    }), encoding="utf-8")
    viewer = SceneViewer(CONFIG)
    source = RosFrameSource("/scene_camera/image")
    source._started = True
    viewer._source = source
    return viewer, source


def put_frame(source: RosFrameSource, fill: int = 7) -> None:
    with source._lock:
        source._seq += 1
        source._latest = (bytes([fill]) * 3 * 4, 2, 2, 3, time.time())


class FakeRuntime:
    def __init__(self, viewer):
        self.scene_viewer = viewer


def ctx(viewer) -> RouteContext:
    async def read_body(receive):
        return {}

    return RouteContext(api=None, runtime=FakeRuntime(viewer), config=None,
                        hub=EventHub(), read_body=read_body)


async def first_socket_messages(viewer) -> list[dict]:
    """스트림 첫 연결. 첫 바이너리 프레임이나 닫힘까지 받는다."""
    sent: list[dict] = []
    calls = {"n": 0}

    async def receive():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"type": "websocket.connect"}
        while not any(m.get("bytes") for m in sent):
            await asyncio.sleep(0.01)
        return {"type": "websocket.disconnect"}

    async def send(message):
        sent.append(message)

    original = scene_route.SCENE_POLL_HZ
    scene_route.SCENE_POLL_HZ = 100.0
    try:
        await asyncio.wait_for(
            scene_route.scene_socket(ctx(viewer), receive, send), timeout=10.0)
    finally:
        scene_route.SCENE_POLL_HZ = original
    return sent


class WaitBoundTest(unittest.TestCase):
    def test_wait_bound_is_three_seconds(self):
        self.assertEqual(FIRST_FRAME_WAIT_SEC, 3.0)


class FrameAlreadyPresentTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_immediately(self):
        viewer, source = viewer_with_source()
        put_frame(source)
        started = time.monotonic()
        frame = await viewer.wait_raw()
        self.assertIsNotNone(frame)
        self.assertLess(time.monotonic() - started, 0.05)

    async def test_raw_route_returns_immediately(self):
        viewer, source = viewer_with_source()
        put_frame(source)
        started = time.monotonic()
        status, headers, body = await scene_route.handle(
            ctx(viewer), "GET", "/v1/scene.raw", None, {})
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 12)
        self.assertLess(time.monotonic() - started, 0.05)


class LateFirstFrameTest(unittest.IsolatedAsyncioTestCase):
    async def test_raw_waits_for_late_first_frame(self):
        viewer, source = viewer_with_source()
        threading.Timer(0.5, put_frame, args=(source,)).start()
        started = time.monotonic()
        status, headers, body = await scene_route.handle(
            ctx(viewer), "GET", "/v1/scene.raw", None, {})
        waited = time.monotonic() - started
        self.assertEqual(status, 200)
        table = {k.decode(): v.decode() for k, v in headers}
        self.assertEqual(table["x-scene-width"], "2")
        self.assertEqual(len(body), 2 * 2 * 3)
        self.assertGreaterEqual(waited, 0.45)
        self.assertLess(waited, FIRST_FRAME_WAIT_SEC)

    async def test_stream_first_connection_gets_late_first_frame(self):
        viewer, source = viewer_with_source()
        threading.Timer(0.5, put_frame, args=(source,)).start()
        # 헤더 뒤 새 프레임이 와야 바이너리가 나간다(같은 seq는 다시 보내지 않는다).
        threading.Timer(0.8, put_frame, args=(source, 9)).start()
        sent = await first_socket_messages(viewer)
        texts = [json.loads(m["text"]) for m in sent if m.get("text")]
        self.assertEqual(texts[0]["type"], "scene_header")
        self.assertNotIn(4503, [m.get("code") for m in sent])
        self.assertTrue(any(m.get("bytes") for m in sent))

    async def test_wait_does_not_block_the_event_loop(self):
        viewer, source = viewer_with_source()
        threading.Timer(0.5, put_frame, args=(source,)).start()
        ticks = []

        async def ticker():
            while len(ticks) < 20:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        await asyncio.gather(viewer.wait_raw(), ticker())
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        self.assertLess(max(gaps), 0.1)


class NoFrameWithinBoundTest(unittest.IsolatedAsyncioTestCase):
    async def test_raw_keeps_503(self):
        viewer, _ = viewer_with_source()
        started = time.monotonic()
        status, _, body = await scene_route.handle(
            ctx(viewer), "GET", "/v1/scene.raw", None, {})
        waited = time.monotonic() - started
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["reason_code"], "exec.unverifiable")
        self.assertGreaterEqual(waited, FIRST_FRAME_WAIT_SEC - 0.05)
        self.assertLess(waited, FIRST_FRAME_WAIT_SEC + 0.5)

    async def test_stream_keeps_4503(self):
        viewer, _ = viewer_with_source()
        sent = await first_socket_messages(viewer)
        texts = [json.loads(m["text"]) for m in sent if m.get("text")]
        self.assertEqual(texts[0]["type"], "scene_unavailable")
        closes = [m for m in sent if m["type"] == "websocket.close"]
        self.assertEqual(closes[0]["code"], 4503)
        self.assertFalse(any(m.get("bytes") for m in sent))


class StartLiveTest(unittest.TestCase):
    def test_start_live_does_not_block_and_raw_does_not_wait(self):
        CONFIG.write_text(json.dumps({
            "world_name": "w",
            "scene_camera": {"enabled": True, "ros_topic": "/scene_camera/image"},
        }), encoding="utf-8")
        released = threading.Event()

        class SlowSource(RosFrameSource):
            def start(self):
                released.wait(2.0)          # 느린 구독 생성
                self._started = True
                return True

        original = scene_view.RosFrameSource
        scene_view.RosFrameSource = SlowSource
        try:
            viewer = SceneViewer(CONFIG)
            started = time.monotonic()
            self.assertTrue(viewer.start_live())
            self.assertLess(time.monotonic() - started, 0.1)
            # 시작 중에는 기다리지 않고 None이다.
            started = time.monotonic()
            self.assertIsNone(viewer.raw())
            self.assertLess(time.monotonic() - started, 0.1)
            # 두 번 불러도 구독을 하나만 만든다.
            source = viewer._source
            self.assertTrue(viewer.start_live())
            self.assertIs(viewer._source, source)
        finally:
            released.set()
            scene_view.RosFrameSource = original

    def test_start_live_without_topic_is_false(self):
        CONFIG.write_text(json.dumps({
            "world_name": "w", "scene_camera": {"enabled": True},
        }), encoding="utf-8")
        viewer = SceneViewer(CONFIG)
        self.assertFalse(viewer.start_live())
        self.assertIsNone(viewer._source)


if __name__ == "__main__":
    unittest.main()
