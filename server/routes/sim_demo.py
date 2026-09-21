"""시뮬레이션 시연 라우트 — 웹의 시연 이송·복귀·resume·복구·정지.

`server/sim_demo_jobs.py`가 검증된 시연 스크립트를 별도 프로세스로 띄운다.
**일반 `/v1/plan`·`/v1/execute`의 pick/place 차단과 무관하다** — 이 경로는
명시적 시뮬레이션 시연 경로이고, 서버 설정(`FORSTICK2_SIM_DEMO_WEB=1`)과
시뮬레이션 작업 셀일 때만 열린다. 상태 조회는 로봇 명령을 보내지 않는다.

| 경로 | 뜻 |
|---|---|
| `GET /v1/sim-demo` | 사용 가능 여부 · 시연 상태 · 자재별 가능 동작(안내) · 실행 중 작업 |
| `POST /v1/sim-demo/jobs` | `{action, material, checkpoint_id?}` 작업 시작 |
| `GET /v1/sim-demo/jobs/<id>` | 진행 단계 · 콘솔 끝부분 · 결과 보고서 |
| `POST /v1/sim-demo/stop` | 실행 중 작업에 정지 요청 |
| `POST /v1/sim-demo/command` | `{mode:"simulation_demo", utterance, source}` 텍스트·STT final 발화 |

`/v1/sim-demo/command`는 텍스트와 STT **final**이 함께 쓰는 하나의 입구다.
LLM·계획 생성을 거치지 않는다(`server/sim_demo_commands.py`). partial은 받지
않는다. 모호하거나 지금 할 수 없으면 ASK/BLOCK만 돌려주고 작업을 만들지 않는다.
시뮬레이션 명령이 아닌 발화는 `PASS_THROUGH`다 — 화면은 그때만 기존 계획
생성으로 간다. 시뮬레이션 셀이 아니면 403 BLOCK이고, 화면은 기존 경로를 쓴다.
"""

from __future__ import annotations

from core.reason_codes import ReasonCode
from server.api import ApiError
from server.routes.common import RouteContext, Response, body_field, json_response

PATHS: tuple[str, ...] = ("/v1/sim-demo",)


def _jobs(ctx: RouteContext):
    jobs = getattr(ctx.runtime, "sim_demo_jobs", None)
    if jobs is None:
        raise ApiError(403, ReasonCode.EXEC_PERMIT_DENIED,
                       "시뮬레이션 시연을 웹에서 쓸 수 없다: "
                       + str(getattr(ctx.runtime, "sim_demo_disabled_reason", "")))
    return jobs


async def handle(
    ctx: RouteContext, method: str, path: str, receive, query: dict[str, str],
) -> Response | None:
    if not path.startswith("/v1/sim-demo"):
        return None
    from server.sim_demo_jobs import SimDemoJobError

    if method == "GET" and path == "/v1/sim-demo":
        jobs = getattr(ctx.runtime, "sim_demo_jobs", None)
        if jobs is None:
            return json_response({
                "enabled": False, "is_simulated": True,
                "reason": getattr(ctx.runtime, "sim_demo_disabled_reason", None),
                "state": ctx.runtime.simulation_demo_status()})
        return json_response({"enabled": True, **jobs.status()})

    if method == "POST" and path == "/v1/sim-demo/command":
        payload = await ctx.read_body(receive)
        return command_response(ctx, payload)

    try:
        if method == "POST" and path == "/v1/sim-demo/jobs":
            payload = await ctx.read_body(receive)
            job = _jobs(ctx).start(
                body_field(payload, "action"), body_field(payload, "material"),
                checkpoint_id=payload.get("checkpoint_id") or None)
            return json_response(job, 202)
        if method == "GET" and path.startswith("/v1/sim-demo/jobs/"):
            return json_response(_jobs(ctx).job(path[len("/v1/sim-demo/jobs/"):]))
        if method == "POST" and path == "/v1/sim-demo/stop":
            return json_response(_jobs(ctx).request_stop(reason="sim_demo_stop"))
    except SimDemoJobError as exc:
        reason = (ReasonCode.EXEC_PERMIT_DENIED if exc.status == 409
                  else ReasonCode.PLAN_ARG_UNKNOWN if exc.status == 400
                  else ReasonCode.CONFIG_MISSING)
        raise ApiError(exc.status, reason, str(exc)) from exc
    return None


def command_response(ctx: RouteContext, payload: dict) -> Response:
    """발화 하나 → RUN(작업 생성) / STOP(정지 요청) / ASK / BLOCK."""
    from server.sim_demo_commands import (
        ASK,
        BLOCK,
        PASS_THROUGH,
        RUN,
        SIMULATION_NOTICE,
        SOURCES,
        STOP,
        decide,
        parse_command,
    )
    from server.sim_demo_jobs import SimDemoJobError

    utterance = str(payload.get("utterance") or "")
    source = str(payload.get("source") or "")
    base = {"is_simulated": True, "real_hardware_ready": False,
            "simulation_notice": SIMULATION_NOTICE, "utterance": utterance,
            "source": source, "job": None, "job_spec": None, "stop": None}

    def answer(status: int, **fields) -> Response:
        return json_response({**base, **fields}, status)

    if payload.get("mode") != "simulation_demo":
        return answer(400, decision=BLOCK,
                      reason="시뮬레이션 시연 모드가 아니다 — 일반 명령은 계획 생성으로 간다")
    if source not in SOURCES:
        return answer(400, decision=BLOCK,
                      reason=f"작업을 만들 수 없는 입력 출처다: {source or '없음'}"
                             " (STT partial은 표시만 한다)")
    jobs = getattr(ctx.runtime, "sim_demo_jobs", None)
    if jobs is None:
        return answer(403, decision=BLOCK,
                      reason="시뮬레이션 시연을 쓸 수 없다: "
                             + str(getattr(ctx.runtime, "sim_demo_disabled_reason", "")))
    workcell = getattr(jobs, "workcell", None) or {}
    parsed = parse_command(utterance, workcell)
    if parsed.get("decision") == STOP:
        # 계획·LLM을 거치지 않고 즉시 기존 시연 정지로 보낸다.
        return answer(200, decision=STOP, intent="stop",
                      stop=jobs.request_stop(reason="sim_demo_command_stop"))
    decision = decide(parsed, jobs.status(), jobs.materials)
    fields = {k: decision.get(k) for k in ("decision", "intent", "material", "reason",
                                           "job_spec")}
    if decision.get("decision") != RUN:
        # PASS_THROUGH는 "시뮬레이션 명령이 아니다" — 화면이 기존 계획 생성으로 간다.
        return answer(200 if decision.get("decision") in (ASK, PASS_THROUGH) else 409,
                      **fields)
    spec = decision["job_spec"]
    try:
        job = jobs.start(spec["action"], spec["material"],
                         checkpoint_id=spec.get("checkpoint_id"))
    except SimDemoJobError as exc:
        return answer(409, **{**fields, "decision": BLOCK, "reason": str(exc)})
    return answer(202, **fields, job=job)
