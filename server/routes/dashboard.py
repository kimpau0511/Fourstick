"""관제 대시보드 읽기 API.

대시보드는 DB에 직접 접근하지 않는다. Repository의 append-only 기록과 이미
구성된 Runtime 상태만 읽으며, 조회 때문에 로봇 어댑터를 새로 연결하지 않는다.
"""

from __future__ import annotations

from server.routes.common import RouteContext, Response, json_response

PATHS: tuple[str, ...] = ("/v1/dashboard",)


async def handle(
    ctx: RouteContext, method: str, path: str, receive, query: dict[str, str],
) -> Response | None:
    if method != "GET" or path != "/v1/dashboard":
        return None

    runtime = ctx.runtime
    snapshot = dict(ctx.repository.dashboard_snapshot(limit=12))
    snapshot.update({
        "robot": {
            "configured": runtime.robot_configured,
            "robot_id": runtime.robot_id,
            "kind": runtime.adapter_kind,
            "is_simulated": runtime.is_simulated,
        },
        "robots": runtime.registry.catalog(),
        "workcell": runtime.workcell,
        "simulation_demo": runtime.simulation_demo_status(),
        "verifications": [
            record.to_dict()
            for record in ctx.repository.sim_verifications(limit=12)
        ],
    })
    return json_response(snapshot)
