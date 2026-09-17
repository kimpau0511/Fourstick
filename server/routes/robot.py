"""robot 라우트 (md/개발플랜.md 3-04).

Registry 목록·연결 상태·Capability·지원 스킬. **전역 fixture를 두지 않는다** —
값은 `RobotRegistry`와 `CapabilityProfile`에서만 온다(7-01·7-02·7-03).

실제 로봇 Profile이 등록돼 있지 않으면 `configured: false`로 알린다. 수치를
만들어 채우지 않는다.
"""

from __future__ import annotations

from server.routes.common import RouteContext, Response, json_response

PATHS: tuple[str, ...] = ("/v1/robots",)


async def handle(
    ctx: RouteContext, method: str, path: str, receive, query: dict[str, str],
) -> Response | None:
    if method != "GET" or path != "/v1/robots":
        return None
    runtime = ctx.runtime
    return json_response({
        "configured": runtime.robot_configured,
        "robots": runtime.registry.catalog(),
        # 선언됐지만 아직 실행 대상이 아닌 구성도 보여준다(8-02).
        # 무엇이 막혀 있는지 화면에서 알 수 있어야 한다.
        "declared": runtime.declared_robots(),
        "assets": runtime.asset_status_payload(),
        # FR3 Gazebo 작업 셀 연결 상태(8-08). 붙지 않았으면 이유를 담는다.
        # **실제 하드웨어가 아니다** — 화면이 그 사실을 보여줄 수 있어야 한다.
        "workcell": runtime.workcell,
        # 실기 전환 준비 상태(8-12). **Gazebo 시연 결과와 다른 축이다** —
        # 화면이 두 값을 나란히 두되 하나의 배지로 합치지 않는다.
        "hardware_readiness": runtime.hardware_readiness,
        # Gazebo 이송 시연 요약(8-11). 위 실기 준비와 **별도 키**로 둔다.
        "simulation_e2e": runtime.simulation_e2e,
        # 시뮬레이터 검증 기록(목표 vs 관측). 실제 로봇 실행과 섞지 않는다.
        "verifications": [
            record.to_dict()
            for record in ctx.repository.sim_verifications(limit=12)
        ],
    })
