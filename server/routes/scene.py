"""scene 라우트 (md/개발플랜.md 8-09 우선 작업 3).

작업 셀 장면 영상. **Gazebo 서버가 렌더링한 프레임**을 PNG로 돌려준다.

이 경로가 있는 이유: 이 환경의 Gazebo GUI는 소프트웨어 렌더링만 가능하고
부하가 오르면 프레임을 완성하지 못한다. 같은 장면을 서버는 훨씬 적은 CPU로
렌더링하므로, **사용자가 셀을 보는 경로를 웹 화면에 둔다.**

프레임을 못 받으면 **빈 그림을 만들지 않는다** — 이유 코드와 함께 거절한다.
"""

from __future__ import annotations

from server.routes.common import (
    Response,
    RouteContext,
    json_response,
)

PATHS: tuple[str, ...] = ("/v1/scene.png", "/v1/scene.raw", "/v1/scene")

#: 스트림이 새 프레임을 확인하는 주기(Hz). 카메라 발행 주기보다 약간 빠르게
#: 둬서 프레임을 흘리지 않는다. 같은 프레임은 다시 보내지 않는다(seq 비교).
SCENE_POLL_HZ = 10.0


async def handle(
    ctx: RouteContext, method: str, path: str, receive, query: dict[str, str],
) -> Response | None:
    if method != "GET" or path not in PATHS:
        return None
    viewer = ctx.runtime.scene_viewer
    if viewer is None:
        payload = {
            "available": False,
            "detail": "작업 셀 장면 카메라가 구성되지 않았다",
            "reason_code": "config.missing",
        }
        # 이미지·원본 경로는 거절한다(200으로 성공처럼 보이지 않게).
        image_paths = ("/v1/scene.png", "/v1/scene.raw")
        return json_response(payload, status=503 if path in image_paths else 200)

    if path == "/v1/scene":
        ok, detail = viewer.available()
        live = viewer.raw() is not None
        return json_response({
            "available": ok,
            "detail": detail or "서버가 렌더링한 작업 셀 장면",
            "image_path": "/v1/scene.png",
            # 실시간 경로가 살아 있는가. 없으면 화면이 느린 경로를 쓴다.
            "live": live,
            "live_detail": viewer.live_detail(),
            "raw_path": "/v1/scene.raw",
            "note": "Gazebo GUI와 무관하다. 서버 카메라 센서 프레임이다",
        })

    if path == "/v1/scene.raw":
        # **인코딩하지 않는다.** 원본 RGB를 그대로 보내고 브라우저가 canvas에
        # 그린다 — 실시간 표시에서 PNG 인코딩 비용을 쓰지 않기 위해서다.
        live = viewer.raw()
        if live is None:
            return json_response({
                "available": False,
                "detail": viewer.live_detail() or "실시간 프레임을 받지 못했다",
                "reason_code": "exec.unverifiable",
            }, status=503)
        data, width, height, channels, captured_at, seq = live
        return (
            200,
            [
                (b"content-type", b"application/octet-stream"),
                (b"cache-control", b"no-store"),
                (b"x-scene-width", str(width).encode()),
                (b"x-scene-height", str(height).encode()),
                (b"x-scene-channels", str(channels).encode()),
                (b"x-scene-seq", str(seq).encode()),
                (b"x-scene-captured-at", f"{captured_at:.3f}".encode()),
            ],
            data,
        )

    frame = viewer.frame()
    if not frame.ok:
        return json_response({
            "available": False,
            "detail": frame.detail,
            "reason_code": frame.reason_code,
        }, status=503)
    return (
        200,
        [
            (b"content-type", b"image/png"),
            # 브라우저가 오래된 프레임을 재사용하지 않게 한다.
            (b"cache-control", b"no-store"),
            (b"x-scene-width", str(frame.width).encode()),
            (b"x-scene-height", str(frame.height).encode()),
        ],
        frame.png,
    )


async def scene_socket(ctx: RouteContext, receive, send) -> None:
    """`/v1/scene/stream` — 장면 프레임을 **서버가 밀어 보낸다.**

    폴링이 아니다. 구독이 새 프레임을 받을 때마다 바이너리로 보낸다.
    WebRTC를 쓰지 않는 이유: 미디어 인코더·ICE 스택이 필요하고, 이 저장소는
    표준 라이브러리 밖의 의존성을 늘리지 않는다. 같은 화면을 WebSocket
    바이너리 프레임으로 보내면 인코딩 비용이 0이다(원본 RGB를 그대로 보낸다).

    프로토콜:
      1. 서버 → 클라이언트: JSON 한 줄 `{type:"scene_header", width, height,
         channels, update_rate_hz}`
      2. 서버 → 클라이언트: 새 프레임마다 바이너리(RGB/RGBA 원본)
      3. 프레임이 끊기면 JSON `{type:"scene_stalled", detail}` 한 번

    프레임을 만들지 않는다. 받지 못하면 그 사실을 보낸다.
    """
    import asyncio
    import json as _json

    message = await receive()
    if message["type"] != "websocket.connect":
        return
    await send({"type": "websocket.accept"})

    viewer = ctx.runtime.scene_viewer
    if viewer is None:
        await send({"type": "websocket.send", "text": _json.dumps({
            "type": "scene_unavailable",
            "detail": "작업 셀 장면 카메라가 구성되지 않았다",
            "reason_code": "config.missing",
        }, ensure_ascii=False)})
        await send({"type": "websocket.close", "code": 4404})
        return

    first = viewer.raw()
    if first is None:
        await send({"type": "websocket.send", "text": _json.dumps({
            "type": "scene_unavailable",
            "detail": viewer.live_detail() or "실시간 프레임을 받지 못했다",
            "reason_code": "exec.unverifiable",
        }, ensure_ascii=False)})
        await send({"type": "websocket.close", "code": 4503})
        return

    _, width, height, channels, _, last_seq = first
    await send({"type": "websocket.send", "text": _json.dumps({
        "type": "scene_header", "width": width, "height": height,
        "channels": channels,
        "update_rate_hz": SCENE_POLL_HZ,
        "detail": "Gazebo 서버가 렌더링한 장면이다. GUI 창과 무관하다",
    }, ensure_ascii=False)})

    # 클라이언트가 끊는 것을 감지하려면 receive를 함께 기다려야 한다.
    closing = asyncio.ensure_future(receive())
    stalled_sent = False
    try:
        while True:
            if closing.done():
                event = closing.result()
                if event["type"] == "websocket.disconnect":
                    return
                closing = asyncio.ensure_future(receive())
            live = viewer.raw()
            if live is None:
                if not stalled_sent:
                    await send({"type": "websocket.send", "text": _json.dumps({
                        "type": "scene_stalled",
                        "detail": viewer.live_detail() or "프레임이 끊겼다",
                    }, ensure_ascii=False)})
                    stalled_sent = True
                await asyncio.sleep(1.0)
                continue
            data, frame_width, frame_height, frame_channels, _, seq = live
            if (frame_width, frame_height, frame_channels) != (
                    width, height, channels):
                # 해상도가 바뀌면 헤더를 다시 보낸다(추측하지 않는다).
                width, height, channels = (frame_width, frame_height,
                                           frame_channels)
                await send({"type": "websocket.send", "text": _json.dumps({
                    "type": "scene_header", "width": width, "height": height,
                    "channels": channels, "update_rate_hz": SCENE_POLL_HZ,
                }, ensure_ascii=False)})
            if seq != last_seq:
                last_seq = seq
                stalled_sent = False
                await send({"type": "websocket.send", "bytes": data})
            await asyncio.sleep(1.0 / SCENE_POLL_HZ)
    except (ConnectionError, RuntimeError):
        return
    finally:
        closing.cancel()
