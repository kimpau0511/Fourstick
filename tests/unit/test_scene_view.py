"""작업 셀 장면 영상 (md/개발플랜.md 8-09 우선 작업 3).

확인하는 것:

- PNG 인코딩이 표준 라이브러리만으로 올바른 바이트를 만든다
- 프레임을 받지 못하면 **빈 그림을 만들지 않는다** — 이유와 함께 실패한다
- 장면 카메라가 꺼져 있으면 그 사실을 알린다
- 라우트가 공급자 없이도 안전하게 거절한다
- 낡은 프레임을 실시간이라고 하지 않는다
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import time
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.routes import scene as scene_route
from server.routes.common import EventHub, RouteContext
from server.scene_view import RosFrameSource, SceneViewer, encode_png


class TestPngEncoding(unittest.TestCase):
    def test_rgb_frame_encodes_to_a_valid_png(self):
        width, height = 3, 2
        raw = bytes([
            255, 0, 0, 0, 255, 0, 0, 0, 255,
            10, 20, 30, 40, 50, 60, 70, 80, 90,
        ])
        png = encode_png(raw, width, height, 3)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        # IHDR을 되읽어 크기·색 타입을 확인한다.
        length = struct.unpack(">I", png[8:12])[0]
        self.assertEqual(png[12:16], b"IHDR")
        header = png[16:16 + length]
        got_width, got_height, depth, colour = struct.unpack(">IIBB", header[:10])
        self.assertEqual((got_width, got_height, depth, colour),
                         (width, height, 8, 2))
        # IDAT을 풀어 원본 스캔라인을 되찾는다(필터 0 + 픽셀).
        offset = 16 + length + 4
        idat_length = struct.unpack(">I", png[offset:offset + 4])[0]
        self.assertEqual(png[offset + 4:offset + 8], b"IDAT")
        data = zlib.decompress(png[offset + 8:offset + 8 + idat_length])
        expected = b"".join(b"\x00" + raw[y * width * 3:(y + 1) * width * 3]
                            for y in range(height))
        self.assertEqual(data, expected)

    def test_rgba_uses_colour_type_six(self):
        png = encode_png(bytes(4), 1, 1, 4)
        colour = png[25]
        self.assertEqual(colour, 6)


class TestSceneViewerFailures(unittest.TestCase):
    def test_missing_config_reports_unavailable(self):
        viewer = SceneViewer(ROOT / "config/workcell/does_not_exist.json")
        ok, detail = viewer.available()
        self.assertFalse(ok)
        self.assertIn("읽을 수 없다", detail)
        frame = viewer.frame()
        # **빈 그림을 만들지 않는다.**
        self.assertIsNone(frame.png)
        self.assertFalse(frame.ok)
        self.assertEqual(frame.reason_code, "config.missing")

    def test_disabled_camera_is_reported(self):
        path = Path("/tmp/forstick2_test_workcell_camera_off.json")
        path.write_text(json.dumps({
            "world_name": "w", "scene_camera": {"enabled": False},
        }), encoding="utf-8")
        viewer = SceneViewer(path)
        ok, detail = viewer.available()
        self.assertFalse(ok)
        self.assertIn("꺼져 있다", detail)

    def test_raw_is_none_without_a_live_source(self):
        path = Path("/tmp/forstick2_test_workcell_no_topic.json")
        path.write_text(json.dumps({
            "world_name": "w", "scene_camera": {"enabled": True},
        }), encoding="utf-8")
        viewer = SceneViewer(path)
        self.assertIsNone(viewer.raw())
        self.assertEqual(viewer.live_detail(), "")


class TestRosFrameSourceFreshness(unittest.TestCase):
    def test_stale_frames_are_not_served_as_live(self):
        source = RosFrameSource("/nope")
        # 구독 없이 내부 상태만 채워 신선도 판정을 본다.
        source._latest = (b"\x00" * 3, 1, 1, 3, time.time() - 10.0)
        source._seq = 1
        self.assertIsNone(source.latest(max_age_sec=3.0))
        source._latest = (b"\x00" * 3, 1, 1, 3, time.time())
        self.assertIsNotNone(source.latest(max_age_sec=3.0))

    def test_start_without_a_bridge_fails_quietly(self):
        source = RosFrameSource("/definitely/not/bridged")
        # rclpy가 없으면 False, 있으면 구독은 만들어지되 프레임은 오지 않는다.
        started = source.start()
        if started:
            self.assertIsNone(source.latest(max_age_sec=0.5))
        else:
            self.assertTrue(source.detail)


class FakeRuntime:
    def __init__(self, viewer=None):
        self.scene_viewer = viewer


class TestSceneRoute(unittest.IsolatedAsyncioTestCase):
    def _ctx(self, viewer=None) -> RouteContext:
        async def read_body(receive):
            return {}

        return RouteContext(api=None, runtime=FakeRuntime(viewer), config=None,
                            hub=EventHub(), read_body=read_body)

    async def test_foreign_path_returns_none(self):
        result = await scene_route.handle(self._ctx(), "GET", "/nope", None, {})
        self.assertIsNone(result)

    async def test_without_viewer_png_is_refused(self):
        status, headers, body = await scene_route.handle(
            self._ctx(), "GET", "/v1/scene.png", None, {})
        self.assertEqual(status, 503)
        payload = json.loads(body)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason_code"], "config.missing")

    async def test_without_viewer_raw_is_refused(self):
        status, _, body = await scene_route.handle(
            self._ctx(), "GET", "/v1/scene.raw", None, {})
        self.assertEqual(status, 503)

    async def test_info_path_reports_unavailable(self):
        status, _, body = await scene_route.handle(
            self._ctx(), "GET", "/v1/scene", None, {})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["available"])

    async def test_raw_serves_dimensions_in_headers(self):
        class Viewer:
            def available(self):
                return True, ""

            def live_detail(self):
                return "test"

            def raw(self):
                return (b"\x01\x02\x03", 1, 1, 3, 123.0, 7)

        status, headers, body = await scene_route.handle(
            self._ctx(Viewer()), "GET", "/v1/scene.raw", None, {})
        self.assertEqual(status, 200)
        table = {key.decode(): value.decode() for key, value in headers}
        self.assertEqual(table["content-type"], "application/octet-stream")
        self.assertEqual(table["x-scene-width"], "1")
        self.assertEqual(table["x-scene-height"], "1")
        self.assertEqual(table["x-scene-channels"], "3")
        self.assertEqual(table["x-scene-seq"], "7")
        # **인코딩하지 않는다** — 원본 바이트를 그대로 보낸다.
        self.assertEqual(body, b"\x01\x02\x03")


class TestSceneSocket(unittest.IsolatedAsyncioTestCase):
    """스트림은 프레임을 만들지 않는다. 없으면 없다고 보낸다."""

    def _ctx(self, viewer=None) -> RouteContext:
        async def read_body(receive):
            return {}

        return RouteContext(api=None, runtime=FakeRuntime(viewer), config=None,
                            hub=EventHub(), read_body=read_body)

    async def test_socket_refuses_without_viewer(self):
        sent: list[dict] = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            sent.append(message)

        await scene_route.scene_socket(self._ctx(), receive, send)
        kinds = [m["type"] for m in sent]
        self.assertIn("websocket.accept", kinds)
        self.assertIn("websocket.close", kinds)
        text = next(m for m in sent if m.get("text"))
        self.assertEqual(json.loads(text["text"])["type"], "scene_unavailable")

    async def test_socket_sends_header_then_frames(self):
        now = time.time()
        frames = [
            (b"\x01" * 3, 1, 1, 3, now, 1),   # 헤더용 첫 프레임
            (b"\x02" * 3, 1, 1, 3, now, 2),
            (b"\x03" * 3, 1, 1, 3, now, 3),
        ]

        class Viewer:
            def __init__(self):
                self.index = 0

            def available(self):
                return True, ""

            def live_detail(self):
                return ""

            def raw(self):
                frame = frames[self.index]
                self.index = min(self.index + 1, len(frames) - 1)
                return frame

        sent: list[dict] = []
        calls = {"receive": 0}

        async def receive():
            calls["receive"] += 1
            if calls["receive"] == 1:
                return {"type": "websocket.connect"}
            # 프레임 두 장이 나간 뒤 연결이 끊긴 것으로 만든다.
            while len([m for m in sent if m.get("bytes")]) < 2:
                await asyncio.sleep(0.01)
            return {"type": "websocket.disconnect"}

        async def send(message):
            sent.append(message)

        original = scene_route.SCENE_POLL_HZ
        scene_route.SCENE_POLL_HZ = 200.0   # 테스트를 빠르게 돌린다
        try:
            await asyncio.wait_for(
                scene_route.scene_socket(self._ctx(Viewer()), receive, send),
                timeout=10.0)
        finally:
            scene_route.SCENE_POLL_HZ = original
        texts = [json.loads(m["text"]) for m in sent if m.get("text")]
        self.assertEqual(texts[0]["type"], "scene_header")
        self.assertEqual(texts[0]["width"], 1)
        self.assertEqual(texts[0]["channels"], 3)
        binaries = [m["bytes"] for m in sent if m.get("bytes")]
        self.assertGreaterEqual(len(binaries), 2)
        # **같은 프레임을 다시 보내지 않는다**(seq 비교).
        self.assertEqual(binaries[0], b"\x02" * 3)
        self.assertEqual(binaries[1], b"\x03" * 3)

    async def test_socket_reports_stall_without_making_a_frame(self):
        class Viewer:
            def __init__(self):
                self.calls = 0

            def available(self):
                return True, ""

            def live_detail(self):
                return "토픽이 끊겼다"

            def raw(self):
                self.calls += 1
                # 첫 호출만 프레임을 주고(헤더용) 그 뒤에는 끊긴다.
                if self.calls == 1:
                    return (b"\x01" * 3, 1, 1, 3, time.time(), 1)
                return None

        sent: list[dict] = []
        calls = {"receive": 0}

        async def receive():
            calls["receive"] += 1
            if calls["receive"] == 1:
                return {"type": "websocket.connect"}
            while not any(json.loads(m["text"]).get("type") == "scene_stalled"
                          for m in sent if m.get("text")):
                await asyncio.sleep(0.01)
            return {"type": "websocket.disconnect"}

        async def send(message):
            sent.append(message)

        await asyncio.wait_for(
            scene_route.scene_socket(self._ctx(Viewer()), receive, send),
            timeout=10.0)
        texts = [json.loads(m["text"]) for m in sent if m.get("text")]
        kinds = [entry["type"] for entry in texts]
        self.assertIn("scene_stalled", kinds)
        stalled = next(e for e in texts if e["type"] == "scene_stalled")
        self.assertIn("끊겼다", stalled["detail"])
        # 프레임을 만들어 보내지 않는다.
        self.assertEqual([m for m in sent if m.get("bytes")], [])


if __name__ == "__main__":
    unittest.main()
