"""sim-view 라우트 — Three.js 작업 셀 화면(읽기 전용).

| 경로 | 내용 |
|---|---|
| GET `/v1/sim-view/model` | 화면용 URDF(시각 메시만) · 셀 상자 · 자재 크기·색 · 관절 허용 범위(Profile, 읽기 전용) |
| GET `/v1/sim-view/mesh/<n>/<파일>` | 허용 목록의 메시 파일 |
| GET `/v1/sim-view/state` | 최신 관측(관절·그리퍼·자재 pose) + 나이 · stale |
| WS `/v1/sim-view/stream` | 같은 관측을 새 값이 올 때마다(최대 30 Hz) |

로봇에 명령을 보내지 않는다. 관측이 없거나 낡으면 그렇다고 보낸다 — 화면이
움직이는 것처럼 꾸미지 않게 한다.
"""

from __future__ import annotations

import asyncio
import json

from server.routes.common import Response, RouteContext, json_response

PREFIX = "/v1/sim-view"
STREAM_PATH = f"{PREFIX}/stream"
_MESH_TYPES = {".dae": "model/vnd.collada+xml", ".stl": "model/stl"}


def _view(ctx: RouteContext):
    return getattr(ctx.runtime, "sim_view", None)


def _joint_limits(runtime) -> dict:
    """현재 로봇 Profile의 관절 한계(제어·검증 — 장면 관절 한계 검사·Capability 사전 검사가 쓰는 값).

    `declared`(실기 준비용 선언)는 쓰지 않는다. Profile이 없으면 값을 만들지 않고 없다고 알린다.
    """
    profile = getattr(runtime, "profile", None)
    if profile is None:
        return {"available": False, "detail": "로봇 Profile이 없다", "joints": {}}
    return {
        "available": True,
        "source": f"capability profile {profile.profile_id} {profile.profile_version}",
        "joints": {j.name: {"lower": j.lower, "upper": j.upper, "unit": j.unit, "kind": j.kind}
                   for j in profile.joint_limits},
    }


async def handle(ctx: RouteContext, method: str, path: str, receive,
                 query: dict[str, str]) -> Response | None:
    if method != "GET" or not path.startswith(PREFIX + "/"):
        return None
    view = _view(ctx)
    if view is None:
        return json_response({"available": False,
                              "detail": "작업 셀 3D 화면이 구성되지 않았다"}, status=503)
    if path == f"{PREFIX}/model":
        model = view.model()
        if model.get("available"):
            # 관절 상태 패널의 허용 범위(2026-10-07). 지금 로봇의 제어·검증이 쓰는 Profile 값 그대로(읽기 전용).
            model = {**model, "joint_limits": _joint_limits(ctx.runtime)}
        return json_response(model, status=200 if model.get("available") else 503)
    if path == f"{PREFIX}/state":
        return json_response(view.state.sample())
    if path.startswith(f"{PREFIX}/mesh/"):
        parts = path[len(f"{PREFIX}/mesh/"):].split("/")
        target = view.mesh(parts[0], parts[1]) if len(parts) == 2 else None
        if target is None:
            return json_response({"error": "허용되지 않은 메시"}, status=404)
        kind = _MESH_TYPES.get(target.suffix.lower(), "application/octet-stream")
        return 200, [(b"content-type", kind.encode()),
                     (b"cache-control", b"max-age=3600")], target.read_bytes()
    return None


async def stream_socket(ctx: RouteContext, receive, send) -> None:
    """새 관측이 올 때마다 JSON 한 건. 낡은 동안은 2 Hz로 stale 상태를 알린다."""
    message = await receive()
    if message["type"] != "websocket.connect":
        return
    await send({"type": "websocket.accept"})
    view = _view(ctx)
    if view is None:
        await send({"type": "websocket.send", "text": json.dumps(
            {"type": "unavailable", "detail": "작업 셀 3D 화면이 구성되지 않았다"},
            ensure_ascii=False)})
        await send({"type": "websocket.close", "code": 4404})
        return
    closing = asyncio.ensure_future(receive())
    period = 1.0 / view.STREAM_HZ
    last_key = None
    last_sent = 0.0
    loop = asyncio.get_running_loop()
    try:
        while True:
            if closing.done():
                if closing.result()["type"] == "websocket.disconnect":
                    return
                closing = asyncio.ensure_future(receive())
            sample = view.state.sample()
            key = (sample.get("joint_seq"), sample.get("pose_seq"))
            now = loop.time()
            fresh = key != last_key and not sample.get("stale")
            if fresh or now - last_sent >= 0.5:
                sample["type"] = "state"
                await send({"type": "websocket.send", "text": json.dumps(sample)})
                last_key, last_sent = key, now
            await asyncio.sleep(period)
    finally:
        closing.cancel()
