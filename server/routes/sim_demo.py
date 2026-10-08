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
| `POST /v1/sim-demo/reconcile` | 기록↔관측 정합(읽기 전용) 작업 시작 |
| `POST /v1/sim-demo/goals` | 목표 계획(동작 없음): `return_all_to_origin`, 또는 `arrange` + `spec`(목표 배치·제약) |
| `POST /v1/sim-demo/goals/<id>/confirm` | 계획 확인 또는 취소 |
| `GET /v1/sim-demo/goals/<id>` | 목표 계획·현재 단계·진행·최종 결과 |
| `POST /v1/sim-demo/goals/<id>/stop` | 목표와 현재 복귀 작업 STOP |
| `POST /v1/sim-demo/command` | `{mode:"simulation_demo", utterance, source}` 텍스트·STT final 발화 |
| `POST /v1/sim-demo/confirm` | `{token, action:"confirm" 또는 "cancel"}` 확인 카드의 버튼 |
| `GET/POST /v1/sim-demo/motion` | 이동 속도 설정(`{speed_percent}`). 다음 작업부터 적용 |

`/v1/sim-demo/command`는 텍스트와 STT **final**이 함께 쓰는 하나의 입구다.
LLM·계획 생성을 거치지 않는다(`server/sim_demo_commands.py`). partial은 받지
않는다. 모호하거나 지금 할 수 없으면 ASK/BLOCK만 돌려주고 작업을 만들지 않는다.
규칙으로 정확히 해석된 명령도 **바로 실행하지 않는다** — 확인 대기를 만들고
`decision:"CONFIRM"`으로 돌려준다.
시뮬레이션 명령이 아닌 발화는 `PASS_THROUGH`다 — 화면은 그때만 기존 계획
생성으로 간다. 시뮬레이션 셀이 아니면 403 BLOCK이고, 화면은 기존 경로를 쓴다.

규칙이 ASK/PASS_THROUGH로 끝났고 발화가 **자재 작업처럼 보이면** Qwen 분류기
(`server/sim_demo_intent.py`)가 한 번 본다. 분류기는 좁은 JSON만 내고, 서버가
스키마·후보·시연 상태를 다시 검증한다. 통과해도 **작업을 만들지 않는다** —
확인 대기(`server/sim_demo_confirm.py`) 한 건을 만들고 `decision:"CONFIRM"`으로
돌려준다. `/v1/sim-demo/confirm`의 `confirm`을 받은 뒤에만 작업이 생긴다.
만료(기본 60초)·상태 변경·취소·낮은 confidence·JSON 오류는 모두 **작업 0건**이다.
"""

from __future__ import annotations

import json
import logging
import re
import time

from core.reason_codes import ReasonCode
from validation.conveyor_slots import slot_label
from server.api import ApiError
from server.routes.common import RouteContext, Response, body_field, json_response

PATHS: tuple[str, ...] = ("/v1/sim-demo",)


def _goal_confirmation(goal: dict, utterance: str = "") -> dict:
    return {
        "kind": "goal", "goal_id": goal["goal_id"],
        "action": "confirm", "summary": goal["summary"],
        "plan": goal["plan"], "utterance": utterance,
        "goal": goal.get("goal"), "reasoning": goal.get("reasoning"),
        "request": goal.get("request"),
        "is_simulated": True,
    }


#: 움직이지 않는 작업(관측·사전 검사만). 세션만 확인하고 바로 시작한다.
READ_ONLY_ACTIONS = ("reconcile", "resume_preflight")
#: 직접 API로 **확인 카드**를 만들 수 있는 동작 — 서버의 자재별 가능 동작(actions)으로 판정되는 것만.
#: 칸·경로·표면 자리를 고르는 동작(move·route·spot)은 명령 경로가 자리 검증과 함께 만든다.
DIRECT_CONFIRMABLE = ("transfer", "return", "resume", "restore")


def _confirm_store(ctx: RouteContext):
    store = getattr(ctx.runtime, "sim_demo_confirm", None)
    if store is None:
        from server.sim_demo_confirm import ConfirmStore

        store = ConfirmStore()
        ctx.runtime.sim_demo_confirm = store
    return store


def _session_of(ctx: RouteContext, payload: dict) -> str:
    """직접 실행 API의 세션(2026-10-08 리뷰 2번). 없거나 서버가 발급한 쓸 수 있는 세션이 아니면 거절한다."""
    session_id = body_field(payload, "session_id")
    api = getattr(ctx, "api", None)
    if api is not None and hasattr(api, "require_session"):
        api.require_session(session_id)
    return session_id


def _require_served_session(ctx: RouteContext, payload: dict) -> None:
    """명령·확인도 서버가 발급한 세션이 있어야 한다(리뷰 2번 — 세션 없는 명령이 만든 카드는 아무 세션이나 확인할 수
    있었다). 실제 Api가 붙은 서버에서만 본다(Api 없는 단위 시험 대역·미리보기 샌드박스는 그대로)."""
    api = getattr(ctx, "api", None)
    check = getattr(type(api), "require_session", None)
    if callable(check):
        check(api, body_field(payload, "session_id"))


def _stop_latched(ctx: RouteContext) -> bool:
    api = getattr(ctx, "api", None)
    check = getattr(type(api), "stop_latched", None)       # 대역(__getattr__)을 건드리지 않고 메서드가 있을 때만
    return bool(check(api)) if callable(check) else False


def _refuse_if_stopped(ctx: RouteContext) -> None:
    """전체 정지가 걸려 있으면 움직이는 작업을 만들거나 시작하지 않는다(명시적인 정지 해제 전까지)."""
    if _stop_latched(ctx):
        raise ApiError(409, ReasonCode.EXEC_STOPPED,
                       "전체 정지가 걸려 있다 — 정지 해제 뒤 다시 요청해 주세요")


def _material_check(ctx: RouteContext, jobs, model: str, action: str) -> dict | None:
    """이송·복귀 시작 전 기록·관측 확인(2026-10-08 리뷰 11번). 다른 동작은 None(각자의 사전 검사가 있다)."""
    from server.material_check import check_for_action

    # workcell_log_dir: 미리보기·평가 샌드박스가 부착 기록 폴더를 정할 때만 둔다(없으면 운영 기본 폴더).
    return check_for_action(jobs, getattr(ctx.runtime, "sim_view", None), model, action,
                            log_dir=getattr(ctx.runtime, "workcell_log_dir", None))


def _material_refusal(ctx: RouteContext, jobs, spec: dict) -> tuple[dict, int] | None:
    """확인 카드를 만들기 전 기록·관측 확인. 통과면 None, 아니면 (응답 본문, 상태 코드) — 카드를 만들지 않는다."""
    check = _material_check(ctx, jobs, spec["material"], spec["action"])
    if check is None or check["kind"] == "ok":
        return None
    base = {"intent": spec["action"], "material": spec["material"], "job_spec": None, "material_check": check}
    if check["kind"] == "noop":
        return {**base, "decision": "NOOP", "reason": check["detail"]}, 200
    return {**base, "decision": "BLOCK", "reason": f"{check['detail']} — {check['guidance']}"}, 409


def _direct_job_request(ctx: RouteContext, payload: dict) -> Response:
    """POST /v1/sim-demo/jobs 본문(리뷰 2번·11번). 이벤트 루프 밖(`_off_loop`)에서 돈다."""
    from server.sim_demo_jobs import SimDemoJobError

    jobs = _jobs(ctx)                         # 셀이 없으면 먼저 그 사실(403)을 알린다
    session_id = _session_of(ctx, payload)
    action = body_field(payload, "action")
    if action in READ_ONLY_ACTIONS:
        # Slot is intentionally not read from the request.
        job = jobs.start(action, payload.get("material") or None,
                         checkpoint_id=payload.get("checkpoint_id") or None)
        return json_response(job, 202)
    _refuse_if_stopped(ctx)
    if action not in DIRECT_CONFIRMABLE:
        raise SimDemoJobError(409, f"'{action}'은(는) 직접 요청할 수 없다 — 명령으로 요청해 주세요(자리 검증을 함께 한다)")
    material = body_field(payload, "material")
    status = jobs.status()
    if (status.get("running_job") or {}).get("job_id"):
        raise SimDemoJobError(409, "다른 시연 작업이 실행 중이다")
    row = next((m for m in status.get("materials") or () if m.get("model") == material), None)
    if row is None:
        raise SimDemoJobError(400, f"셀 선언의 자재가 아니다: {material}")
    if not (row.get("actions") or {}).get(action):
        raise SimDemoJobError(409, f"지금 {row.get('korean') or material}에 '{action}'을(를) 할 수 없다(서버 판정)")
    spec = {"action": action, "material": material}
    refusal = _material_refusal(ctx, jobs, spec)
    if refusal is not None:
        return json_response({**refusal[0], "job": None, "session_id": session_id}, refusal[1])
    if action == "resume":
        checkpoint = (status.get("state") or {}).get("checkpoint") or {}
        wanted = payload.get("checkpoint_id") or None
        if checkpoint.get("model") != material or not checkpoint.get("checkpoint_id") \
                or (wanted is not None and wanted != checkpoint.get("checkpoint_id")):
            raise SimDemoJobError(409, "이어서 할 정지 지점(체크포인트)이 맞지 않는다")
        spec["checkpoint_id"] = checkpoint["checkpoint_id"]
    store = _confirm_store(ctx)
    offer, code = offer_confirmation(jobs, store, spec, utterance=f"[직접 요청] {action} {material}",
                                     source="direct")
    token = (offer.get("confirmation") or {}).get("token")
    if token:
        store.bind_session(token, session_id)
    return json_response({**offer, "job": None, "session_id": session_id}, code)


#: 시연 명령·확인·직접 요청을 처리하는 **한 줄짜리** 작업 스레드. 모델(Qwen) 호출·Gazebo 관측 같은 블로킹 일을
#: 이벤트 루프 밖에서 하되(그동안에도 /v1/stop이 바로 처리된다 — 2026-10-09 리뷰 4번 보완), 이 요청들끼리는
#: 전처럼 한 번에 하나씩 처리한다(대화 맥락·확인 카드 순서를 바꾸지 않는다).
_SERIAL = None


async def _off_loop(fn, *args):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    global _SERIAL
    if _SERIAL is None:
        _SERIAL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sim-demo-command")
    return await asyncio.get_running_loop().run_in_executor(_SERIAL, lambda: fn(*args))


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
        pending = getattr(ctx.runtime, "sim_demo_confirm", None)
        current = pending.current() if pending is not None else None
        goals = getattr(ctx.runtime, "sim_demo_goals", None)
        goal = None if goals is None else goals.current()
        pending_json = None if current is None else current.to_json(time.time())
        if pending_json is None and goal is not None and goal.get("status") == "planned":
            pending_json = _goal_confirmation(goal)
        return json_response({
            "enabled": True, **jobs.status(),
            "intent_available": getattr(ctx.runtime, "sim_demo_intent", None) is not None,
            "intent_reason": getattr(ctx.runtime, "sim_demo_intent_disabled_reason", None),
            "pending_confirmation": pending_json,
            "goal": goal,
            "environment": None if goals is None else goals.environment(),
        })

    if path == "/v1/sim-demo/environment":
        goals = getattr(ctx.runtime, "sim_demo_goals", None)
        if goals is None:
            raise ApiError(503, None, "시연 목표 실행기를 쓸 수 없습니다")
        if method == "GET":
            return json_response(goals.environment())
        if method == "POST":
            payload = await ctx.read_body(receive)
            try:
                return json_response(goals.set_environment(payload))
            except Exception as exc:
                from server.sim_demo_goals import SimDemoGoalError
                if not isinstance(exc, SimDemoGoalError):
                    raise
                raise ApiError(exc.status, None, str(exc)) from exc

    if path == "/v1/sim-demo/motion":
        motion = getattr(_jobs(ctx), "motion", None)
        if motion is None:
            raise ApiError(503, None, "이동 속도 설정을 쓸 수 없습니다: "
                           + str(getattr(ctx.runtime, "sim_demo_motion_disabled_reason", "")))
        from core.policy import PolicyError
        try:
            if method == "GET":
                return json_response(motion.current())
            if method == "POST":
                payload = await ctx.read_body(receive)
                return json_response(motion.set_percent(payload.get("speed_percent")))
        except PolicyError as exc:
            raise ApiError(400 if method == "POST" else 503, exc.reason, str(exc)) from exc
        except OSError as exc:
            raise ApiError(503, None, "이동 속도를 저장하지 못했습니다") from exc

    if method == "POST" and path == "/v1/sim-demo/command":
        payload = await ctx.read_body(receive)
        _require_served_session(ctx, payload)
        return await _off_loop(command_response, ctx, payload)

    if method == "POST" and path == "/v1/sim-demo/interpret":
        # 해석 미리보기 — 샌드박스에서 같은 해석을 돌린다. 작업·확인·환경을 만들지 않는다.
        import asyncio
        from server.sim_demo_preview import preview
        payload = await ctx.read_body(receive)
        status, result = await asyncio.to_thread(preview, ctx.runtime, payload)
        return json_response(result, status)

    if method == "POST" and path == "/v1/sim-demo/confirm":
        payload = await ctx.read_body(receive)
        _require_served_session(ctx, payload)
        return await _off_loop(confirm_response, ctx, payload)

    try:
        if method == "POST" and path == "/v1/sim-demo/goals":
            # 목표 = 계획(여기) → 확인(/confirm, 같은 세션만). 세션 필수, 정지 중에는 만들지 않는다(리뷰 2번).
            payload = await ctx.read_body(receive)
            session_id = _session_of(ctx, payload)
            _refuse_if_stopped(ctx)
            goals = getattr(ctx.runtime, "sim_demo_goals", None)
            if goals is None:
                raise SimDemoJobError(503, "시연 목표 실행기를 쓸 수 없다")
            created = goals.create(body_field(payload, "goal"), payload.get("spec"))
            goals.bind_session(created["goal_id"], session_id)
            return json_response({**created, "session_id": session_id}, 201)
        if path.startswith("/v1/sim-demo/goals/"):
            rest = path[len("/v1/sim-demo/goals/"):]
            parts = rest.split("/")
            goal_id = parts[0]
            goals = getattr(ctx.runtime, "sim_demo_goals", None)
            if goals is None:
                raise SimDemoJobError(503, "시연 목표 실행기를 쓸 수 없다")
            if method == "GET" and len(parts) == 1:
                return json_response(goals.get(goal_id))
            if method == "POST" and len(parts) == 2 and parts[1] == "confirm":
                payload = await ctx.read_body(receive)
                action = body_field(payload, "action")
                if action == "confirm":
                    _refuse_if_stopped(ctx)
                return json_response(goals.confirm(
                    goal_id, action, session_id=str(payload.get("session_id") or "")), 202)
            if method == "POST" and len(parts) == 2 and parts[1] == "stop":
                return json_response(goals.request_stop(goal_id))
        if method == "POST" and path == "/v1/sim-demo/jobs":
            # 2026-10-08 리뷰 2번: 직접 실행 API도 세션·안전 검증·승인을 거친다. 움직이는 동작은 **확인 카드만** 만들고
            # (작업 0건), 작업은 /v1/sim-demo/confirm(같은 세션·한 번·만료·상태 변경·정지 재검사)으로만 시작한다.
            payload = await ctx.read_body(receive)
            return await _off_loop(_direct_job_request, ctx, payload)
        if method == "GET" and path.startswith("/v1/sim-demo/jobs/"):
            return json_response(_jobs(ctx).job(path[len("/v1/sim-demo/jobs/"):]))
        if method == "POST" and path == "/v1/sim-demo/stop":
            goals = getattr(ctx.runtime, "sim_demo_goals", None)
            goal_stop = goals.request_stop() if goals is not None else None
            if goal_stop and goal_stop.get("requested"):
                return json_response(goal_stop)
            return json_response(_jobs(ctx).request_stop(reason="sim_demo_stop"))
        if method == "POST" and path == "/v1/sim-demo/reconcile":
            # 기록↔관측 정합. 스크립트가 관측만 하고 로봇 명령을 보내지 않는다.
            return json_response(_jobs(ctx).start("reconcile"), 202)
    except (SimDemoJobError,) as exc:
        reason = (ReasonCode.EXEC_PERMIT_DENIED if exc.status == 409
                  else ReasonCode.PLAN_ARG_UNKNOWN if exc.status == 400
                  else ReasonCode.CONFIG_MISSING)
        raise ApiError(exc.status, reason, str(exc)) from exc
    except Exception as exc:
        from server.sim_demo_goals import SimDemoGoalError
        if not isinstance(exc, SimDemoGoalError):
            raise
        reason = (ReasonCode.PLAN_ARG_UNKNOWN if exc.status == 400
                  else ReasonCode.CONFIG_MISSING if exc.status == 404
                  else ReasonCode.EXEC_PERMIT_DENIED)
        raise ApiError(exc.status, reason, str(exc)) from exc
    return None


#: 지원 밖의 자유 집기·놓기에 **할 수 있는 길**을 알려 주는 한 문장.
#: 화면의 안내와 같은 말을 쓴다(`html/static/js/render.js`의 SIM_PICK_PLACE_HINT).
SIM_TARGET_HINT = ("시뮬레이션 작업 명령에서 A/B/C 자재와 대상 위치를 지정하세요"
                   " — 지원하는 작업은 팔레트↔컨베이어 이송·복귀입니다")

#: 해석이 통과했지만 **아직 작업이 아니다.** 사람이 확인을 눌러야 실행된다.
CONFIRM = "CONFIRM"
#: 고정 목표를 만들었고, 목표 계획 확인을 기다린다.
CONFIRM_GOAL = "CONFIRM_GOAL"
#: 확인 카드를 사용자가 취소했다.
CANCELLED = "CANCELLED"

#: 확인 문장. 동작마다 한 문장이고, 화면이 그대로 읽는다.
_SUMMARY = {
    "transfer": "{material}를 {slot}로 옮기겠습니다.",
    "return": "{material}를 {slot}에서 원래 자리로 돌려놓겠습니다.",
    "resume": "{material} 작업을 체크포인트에서 이어서 하겠습니다({slot}).",
}
#: 슬롯이 없을 때(설정 미적용)의 문장. 기존 단일 배치와 같은 말이다.
_SUMMARY_NO_SLOT = {
    "transfer": "{material}를 컨베이어로 옮기겠습니다.",
    "return": "{material}를 원래 자리로 돌려놓겠습니다.",
    "resume": "{material} 작업을 체크포인트에서 이어서 하겠습니다.",
}


def _material_korean(jobs, model: str | None) -> str:
    return str(((jobs.materials.get(model) or {}) if model else {}).get("korean")
               or model or "자재")


def _confirm_summary(jobs, action: str, model: str | None,
                     slot: str | None = None) -> str:
    """확인 카드가 읽는 한 문장. **서버가 고른 슬롯을 사용자에게 보인다.**"""
    korean = _material_korean(jobs, model)
    table = _SUMMARY if slot else _SUMMARY_NO_SLOT
    template = table.get(action)
    if template is None:
        return f"{korean}에 {action} 동작을 하겠습니다."
    return template.format(material=korean, slot=slot_label(slot))


def _candidates(jobs) -> list[dict]:
    """분류기에 줄 자재 후보. 셀 설정이 선언한 것만 간다."""
    return [{"model": model, "korean": spec.get("korean"),
             "korean_colors": list(spec.get("korean_colors") or ())}
            for model, spec in jobs.materials.items()]


def _color_materials(registry, words) -> dict[str, list[str]]:
    """발화에 남은 색 낱말 → 그 색으로 **등록된** 자재들. 등록되지 않은 색은 빈 목록."""
    from server.material_colors import canonical_color

    by_color = getattr(registry, "by_color", None) or {}
    return {word: list(by_color.get(canonical_color(word) or "", ())) for word in words}


def _material_locations(jobs) -> dict[str, str | None]:
    """자재 → 지금 기록된 자리 id(팔레트·컨베이어 칸·표면 빈 위치). 모르면 None."""
    try:
        world = jobs.transfer_world()
    except Exception:  # noqa: BLE001 — 위치를 모르면 모른다고 둔다(서버 검사가 막는다)
        world = None
    location_of = getattr(world, "location_of", None) or {}
    return {model: location_of.get(model) for model in jobs.materials}


def _classify(ctx: RouteContext, jobs, utterance: str, source: str,
              rule_decision: str, rule_reason: str | None, *,
              said: str | None = None, registry=None, dialogue=None,
              original: str | None = None):
    """규칙이 끝내지 못한 자재 작업 발화 → Qwen → 서버 검증 → 확인 대기 한 건.

    돌려주는 것은 `(fields, status)`다. **작업을 만들지 않는다.** Qwen 결과는 그대로
    실행하지 않고 아래를 모두 다시 본다. 하나라도 어긋나면 되묻는다(ASK)·막는다(BLOCK).

    - 자재·자리 id가 이 셀의 목록에 있는가(`sim_demo_intent.validate`)
    - 발화의 이름·색이 고른 자재와 맞는가, 둘 다 없으면 대화 맥락의 자재인가
    - 말한 출발지가 자재의 지금 위치와 맞는가
    - 지원하는 동작인가 · confidence가 기준 이상인가
    - 지금 시연 상태에서 할 수 있는가(`decide`: 목적지 점유·체크포인트·실행 중 작업 등)

    `said`는 색·맥락 치환을 거친 발화(색 낱말이 남아 있을 수 있다), `utterance`는
    정규화한 발화다.
    """
    from server.sim_demo_commands import (
        ASK,
        BLOCK,
        RUN,
        decide,
        material_aliases,
        mentions_conveyor,
        named_pallets,
        spoken_places,
    )
    from server.sim_demo_intent import EXECUTABLE_INTENTS

    classifier = getattr(ctx.runtime, "sim_demo_intent", None)
    store = getattr(ctx.runtime, "sim_demo_confirm", None)

    def fallback(reason: str, intent_info: dict | None = None,
                 decision: str | None = None):
        """분류에 실패했다. **실행하지 않는다 — 언제나 ASK다.**

        여기까지 온 발화는 이미 `looks_like_material_work`를 지난 자재 작업
        발화다. 일반 home/move 발화는 분류기를 부르지도 않으므로(그 경로는
        규칙의 PASS_THROUGH 그대로 계획 생성으로 간다) 여기서 ASK로 끝내도
        기존 경로가 바뀌지 않는다.
        """
        return ({"decision": decision or ASK, "intent": None, "material": None,
                 "reason": reason, "job_spec": None,
                 "intent_result": intent_info}, 200)

    if classifier is None:
        return fallback("자재 작업 해석기를 쓸 수 없습니다: "
                        + str(getattr(ctx.runtime, "sim_demo_intent_disabled_reason",
                                      "") or "이유 미기록"))
    places = getattr(jobs, "places", ()) or ()
    from server.sim_demo_places import place_label

    locations = _material_locations(jobs)
    candidates = [{**row, "location": locations.get(row["model"]),
                   "location_label": (place_label(places, locations.get(row["model"]))
                                      if locations.get(row["model"]) else None)}
                  for row in _candidates(jobs)]
    focus = list(dialogue.current_focus()) if dialogue is not None else []

    def choices(models=None) -> str:
        """되물을 때 보여 줄 자재 후보: 이름(색, 지금 위치)."""
        rows = []
        for row in candidates:
            if models is not None and row["model"] not in models:
                continue
            color = "·".join(row.get("korean_colors") or ())
            where = row.get("location_label") or "위치 확인 안 됨"
            rows.append(f"{row.get('korean') or row['model']}({color + ', ' if color else ''}{where})")
        return " / ".join(rows)

    def which(reason: str, models=None) -> str:
        return f"{reason} — 어느 자재인지 말해 주세요: {choices(models)}"

    result = classifier.classify(utterance, candidates, places, context={"focus": focus},
                                 original=original)
    info = {"intent": result.intent, "material_id": result.material_id,
            "source_resource": result.source_resource,
            "destination_resource": result.destination_resource,
            "confidence": result.confidence, "ok": result.ok,
            "failure": result.failure, "reason": result.reason,
            "model_reason": getattr(result, "model_reason", ""),
            "model_id": result.model_id, "raw": result.raw,
            "latency_sec": result.latency_sec,
            "min_confidence": classifier.min_confidence}
    if not result.ok:
        # 호출 실패·형식 오류·시간 초과·낮은 확신·unknown — 실행하지 않고 되묻는다.
        return fallback(which(result.reason), info)
    # 모델은 오인식 보정 후보를 낼 수 있지만, 발화에 명시된 자재와 충돌하면
    # 어느 쪽이 맞는지 사람이 다시 말해야 한다. 확인 카드도 만들지 않는다.
    workcell = getattr(jobs, "workcell", None) or {}
    compact = "".join(utterance.lower().split())
    spoken_materials = {model for model, aliases in material_aliases(workcell).items()
                        if any(alias in compact for alias in aliases)}
    if spoken_materials and (len(spoken_materials) != 1
                             or result.material_id not in spoken_materials):
        return fallback("제가 이렇게 들었습니다: " + utterance
                        + ". 해석한 자재가 발화와 달라 다시 확인해 주세요", info)
    # 색으로 가리켰으면 그 색으로 **등록된** 자재여야 한다. 같은 색이 여럿이면 고르지 않는다.
    from server.material_colors import stray_color_words
    colored: set[str] = set()
    for word, models in _color_materials(registry, stray_color_words(said or utterance)).items():
        if not models:
            return fallback(f"'{word}'이 어느 자재인지 정할 수 없습니다 — 자재 이름이나 등록된"
                            " 색으로 말해 주세요", info)
        colored.update(models)
    if not spoken_materials and colored:
        names = ", ".join(str((jobs.materials.get(m) or {}).get("korean") or m)
                          for m in sorted(colored))
        if len(colored) != 1:
            return fallback(which(f"말한 색에 맞는 자재가 여럿입니다({names})", colored), info)
        if result.material_id not in colored:
            return fallback(f"해석한 자재가 말한 색과 다릅니다 — 말한 색의 자재는 {names}입니다."
                            " 다시 말해 주세요", info)
    # 이름도 색도 없이 고른 자재는 대화 맥락(직전에 말한 자재 하나)일 때만 받는다.
    grounded = bool(spoken_materials or colored)
    if (not grounded and result.material_id is not None
            and focus != [result.material_id]):
        return fallback(which("발화에 자재 이름·색이 없고 대화 맥락으로도 정할 수 없습니다"), info)
    if not grounded and result.material_id is None and result.intent in ("transfer", "move_slot"):
        return fallback(which("옮길 자재를 정할 수 없습니다"), info)
    # 말한 출발지가 지금 위치와 다르면 옮기지 않는다(관측·기록이 기준이다).
    current = locations.get(result.material_id) if result.material_id else None
    if result.source_resource and current and result.source_resource != current:
        return fallback(f"말한 출발지({place_label(places, result.source_resource)})와 자재의 지금"
                        f" 위치({place_label(places, current)})가 다릅니다 — 위치를 확인해 다시"
                        " 말해 주세요", info)
    if source == "stt_final" and result.intent in ("transfer", "return", "move_slot") \
            and not grounded:
        return fallback("제가 이렇게 들었습니다: " + utterance
                        + ". 어느 자재인지 다시 말해 주세요", info)
    if result.intent == "transfer" and result.material_id is None:
        return fallback("어느 자재를 옮길지 알 수 없습니다 — A·B·C 자재 중 하나를"
                        " 말해 주세요", info, decision=ASK)
    from server.sim_demo_places import (
        CONVEYOR,
        CONVEYOR_SLOT,
        PALLET,
        SURFACE,
        resolve as resolve_place,
    )
    target = resolve_place(places, result.destination_resource)
    intent = result.intent
    if intent == "move_slot":
        # 빈자리 이동은 기존 동작으로만 바꾼다 — 새 실행 경로를 만들지 않는다.
        here = resolve_place(places, current)
        if target is None or target.kind not in (SURFACE, CONVEYOR_SLOT, CONVEYOR):
            return fallback("어느 빈자리로 옮길지 알 수 없습니다 — 컨베이어 몇 번 칸이나"
                            " 작업대 빈 곳처럼 말해 주세요", info)
        if target.kind != SURFACE and (here is None or here.kind != PALLET):
            return fallback("컨베이어 칸 사이 이동은 'C자재를 컨베이어 3번 칸으로 옮겨줘'처럼"
                            " 자재 이름과 칸을 말해 주세요", info)
        intent = "transfer"
    if intent == "transfer" and target is not None and target.kind == SURFACE:
        # 분류기가 정한 것은 자재·표면뿐이다. 빈 위치는 서버가 계산·검증한다.
        # 발화가 그 표면을 **말했을 때만** 받는다 — "바닥 빈 곳"을 작업대로 바꿔 읽은
        # 실측(2026-10-02)이 있다. 말하지 않은 표면을 모델이 골라 주지 않는다.
        from server.sim_free_spot import surface_aliases, surfaces_of
        names = surface_aliases(surfaces_of(jobs)).get(target.id, ())
        if not any(name.lower() in compact for name in names):
            declared = ", ".join(str(spec.get("korean") or sid) for sid, spec in
                                 (surfaces_of(jobs).get("surfaces") or {}).items()
                                 if spec.get("placement") == "free_spot")
            return fallback(f"말한 표면은 놓을 수 있는 표면으로 선언되지 않았습니다 — 놓을 수 있는"
                            f" 표면: {declared or '없음'}", info, decision=ASK)
        return _free_spot_answer(ctx, jobs, utterance=utterance, source=source,
                                 detected={"surface_id": target.id, "surfaces": [target.id],
                                           "material": result.material_id,
                                           "materials": [result.material_id],
                                           "stated_source": result.source_resource,
                                           "via": "classifier"},
                                 classifier=info)
    # 분류기는 **자재만** 정한다. 목적지는 intent가 정해져 있다
    # (transfer=컨베이어, return=원래 팔레트). 그래서 발화가 컨베이어를 말하지
    # 않았는데 transfer로 올리면 **목적지를 대신 골라 준 것**이 된다.
    # 실측: "작업대에 올려줘"가 "컨베이어 1번 위치로 옮기겠습니다"로 바뀌었다.
    # 목적지를 **모델이 말했거나 발화가 말했을 때만** 이송으로 올린다. 둘 다
    # 없으면 목적지를 서버가 대신 골라 준 것이 된다(실측: "작업대에 올려줘"가
    # "컨베이어 1번 위치로 옮기겠습니다"로 바뀌었다).
    if intent == "transfer" \
            and result.destination_resource is None \
            and not mentions_conveyor(utterance, workcell):
        return fallback(SIM_TARGET_HINT, info, decision=ASK)
    if source == "stt_final" and intent == "transfer":
        spoken = spoken_places(utterance, workcell, jobs.slots)
        if not spoken["conveyor"] and not spoken["slot"]:
            return fallback("제가 이렇게 들었습니다: " + utterance
                            + ". 어디로 옮길지 다시 말해 주세요", info)
    # 해석 결과를 **지금 시연 상태**로 다시 본다. 여기서 BLOCK이면 실행 후보가
    # 아니고, 확인 카드도 만들지 않는다.
    # 발화가 부른 팔레트를 그대로 넘긴다 — 규칙 경로와 같은 "엉뚱한 팔레트"
    # 검사를 분류기 경로에서도 받게 한다(빈 dict를 넘기면 그 검사가 꺼진다).
    checked = decide({"intent": intent, "decision": RUN,
                      "material": result.material_id,
                      "source": result.source_resource,
                      "destination": result.destination_resource,
                      "mentioned_pallets": named_pallets(utterance, workcell)},
                     jobs.status(), jobs.materials, jobs.slots, places)
    if checked.get("decision") != RUN:
        return ({"decision": checked.get("decision", BLOCK), "intent": intent,
                 "material": checked.get("material") or result.material_id,
                 "reason": checked.get("reason"), "job_spec": None,
                 "intent_result": info},
                200 if checked.get("decision") == ASK else 409)
    if store is None:
        return fallback("확인 대기함을 쓸 수 없습니다", info, decision=ASK)
    spec = checked["job_spec"]
    if spec["action"] not in EXECUTABLE_INTENTS:
        return fallback(f"발화 해석으로는 실행하지 않는 동작입니다: {spec['action']}",
                        info, decision=ASK)
    refusal = _material_refusal(ctx, jobs, spec)
    if refusal is not None:
        return {**refusal[0], "intent_result": info}, refusal[1]
    return offer_confirmation(jobs, store, spec, utterance=utterance,
                              source=source, rule_decision=rule_decision,
                              rule_reason=rule_reason, classifier=info)


def offer_confirmation(jobs, store, spec: dict, *, utterance: str, source: str,
                       rule_decision: str | None = None,
                       rule_reason: str | None = None,
                       classifier: dict | None = None):
    """확인 대기 한 건을 세운다. **여기서 작업을 만들지 않는다.**

    분류기 경로와 규칙 경로가 **같은 자리**를 쓴다 — 확인 카드가 담는 근거가
    어느 경로로 왔는지에 따라 달라지지 않게 한다.
    """
    status = jobs.status()
    pending = store.create(
        job_spec=spec, status=status,
        summary=_confirm_summary(jobs, spec["action"], spec["material"],
                                 spec.get("slot")),
        evidence={
            "slot": spec.get("slot"),
            "slot_label": slot_label(spec["slot"]) if spec.get("slot") else None,
            "conveyor": status.get("conveyor"),
            "rule_decision": rule_decision, "rule_reason": rule_reason,
            "classifier": classifier,
            "material_korean": _material_korean(jobs, spec["material"]),
            "state": _state_brief(jobs, status),
            "readiness": _readiness(jobs, spec, status),
        },
        utterance=utterance, source=source,
    )
    return ({"decision": CONFIRM, "intent": spec["action"],
             "material": spec["material"], "job_spec": spec, "reason": None,
             "slot": spec.get("slot"),
             "slot_label": slot_label(spec["slot"]) if spec.get("slot") else None,
             "intent_result": classifier,
             "confirmation": pending.to_json(time.time())}, 200)


#: 동작별 출발→도착. 화면이 문구를 만들지 않게 서버가 정한다.
_ROUTE = {
    "transfer": ("원래 팔레트", "컨베이어"),
    "return": ("컨베이어", "원래 팔레트"),
    "resume": ("정지 지점", "컨베이어"),
}


def _readiness(jobs, spec: dict, status: dict) -> dict:
    """"안전 판단 및 실행" 카드가 읽는 **실행 준비 근거**.

    **확인된 것과 실행 직전에 다시 볼 것을 구분한다.** 지금 통과한 검사만
    통과로 적고, 나머지는 `recheck`로 남긴다 — 검사하지 않은 것을 통과로
    보여 주면 사용자가 근거 없이 확인을 누르게 된다.
    """
    slot = spec.get("slot")
    entry = next((s for s in jobs.slots if s.name == slot), None)
    checks = []

    # ① geometry — 슬롯 자세는 MoveIt(IK·충돌·attached-object·이웃 점유)으로
    #    미리 검증된 것만 설정에 들어온다. 지금 다시 물은 것이 아니라
    #    **검증된 값을 쓴다**는 뜻이다.
    # **검사 사유에 슬롯 이름을 되풀이하지 않는다.** 카드에 "배정 슬롯" 행과
    # "출발 → 도착" 행이 이미 그 이름을 갖고 있어, 사유마다 다시 적으면 같은
    # 말이 카드에서 네 번 보인다(실측).
    if entry is not None:
        checks.append({"key": "geometry", "label": "geometry (슬롯 자세)",
                       "ok": True, "recheck": False,
                       "detail": "배정된 자리의 자세가 MoveIt 검증을 통과한 값"})
    else:
        checks.append({"key": "geometry", "label": "geometry (슬롯 자세)",
                       "ok": True, "recheck": False,
                       "detail": "기존 단일 배치 위치(검증된 conveyor_place)"})

    # ② 점유 — 서버가 방금 기록으로 확인했다(빈 첫 슬롯 배정 / 배정받은 슬롯).
    conveyor = status.get("conveyor") or {}
    if spec.get("action") == "transfer":
        detail = "배정된 자리가 비어 있음을 기록으로 확인" if slot else "컨베이어가 비어 있다"
    else:
        detail = "배정된 자리의 자재를 대상으로 확인" if slot else "컨베이어 기록 확인"
    checks.append({"key": "occupancy", "label": "슬롯 점유", "ok": True,
                   "recheck": True, "detail": detail})

    # ③ scene·충돌·pose — **아직 보지 않았다.** 실행 직전에 관측으로 본다.
    checks.append({"key": "scene", "label": "scene hash · 충돌 · 자재 pose",
                   "ok": None, "recheck": True,
                   "detail": "작업 시작 직후 관측으로 다시 검사한다"})

    # 출발·도착은 **해석된 자리 이름**을 먼저 쓴다. 발화나 모델이 자리를 말했으면
    # 그 자리가 카드에 그대로 보여야 한다 — 서버가 동작 이름으로 뭉뚱그리지
    # 않는다. 말하지 않았으면 동작이 정한 기본 경로를 적는다.
    from server.sim_demo_places import resolve

    places = getattr(jobs, "places", ()) or ()
    origin, target = _ROUTE.get(spec.get("action") or "", ("—", "—"))
    if spec.get("action") == "transfer":
        target = slot_label(slot) if slot else target
    elif spec.get("action") == "return":
        origin = slot_label(slot) if slot else origin
    spoken_source = resolve(places, spec.get("source"))
    spoken_target = resolve(places, spec.get("destination"))
    if spoken_source is not None:
        origin = spoken_source.label
    if spoken_target is not None:
        # 자리 미지정 컨베이어는 서버가 고른 자리 이름이 더 정확하다. 그 밖에는
        # 말한 자리를 그대로 적는다.
        target = (slot_label(slot) if (spoken_target.kind == "conveyor" and slot)
                  else spoken_target.label)
    return {
        "action": spec.get("action"),
        "material": spec.get("material"),
        "material_korean": _material_korean(jobs, spec.get("material")),
        "origin": origin,
        "target": target,
        "slot": slot,
        "slot_label": slot_label(slot) if slot else None,
        # 해석에 쓰인 **선언된 자리 id**. 화면이 이름을 지어내지 않게 함께 준다.
        "source_resource": spec.get("source"),
        "destination_resource": spec.get("destination"),
        "checks": checks,
        # 확인을 누르면 시작할 수 있다는 뜻이지, 이미 시작했다는 뜻이 아니다.
        "status": "ready",
        "status_label": "실행 준비됨",
        "conveyor": conveyor,
    }


def _state_brief(jobs, status: dict) -> list[dict]:
    """확인 카드에 함께 보이는 **현재 자재 상태.** 관측 기록 그대로다."""
    state = status.get("state") or {}
    objects = state.get("objects") or {}
    rows = []
    for model, spec in jobs.materials.items():
        record = objects.get(model) or None
        rows.append({"model": model, "korean": spec.get("korean"),
                     "state": (record or {}).get("state"),
                     "has_checkpoint": model in (state.get("checkpoints") or [])})
    return rows


ENVIRONMENT = "ENVIRONMENT"
#: 이미 목표 상태다 — 목표·작업을 만들지 않았다(움직이지 않는다).
NOOP = "NOOP"
#: 검사·확인을 건너뛰라는 표현. 이런 요청은 실행 후보로 만들지 않는다.
SAFETY_BYPASS_WORDS = ("검증건너", "검사건너", "확인건너", "검증없이", "검사없이",
                       "확인없이", "승인없이", "안전무시", "충돌무시", "안전장치끄",
                       "안전장치해제", "제한무시", "제한해제", "우회해")
#: 무엇·어디를 가리키지 않는 지시어. 자재 이름 없이 이것만 있으면 되묻는다.
VAGUE_REFERENCE_WORDS = ("그거", "저거", "이거", "그것", "저것", "이것", "저쪽", "거기",
                         "저기", "빈자리", "아무데", "알아서", "적당한")
#: 배치 요청으로 볼 수 있는 동사. 없으면 LLM에 넘기지 않는다.
ARRANGE_VERBS = ("옮겨", "옮기", "놔", "놓", "올려", "돌려", "바꿔", "바꾸", "맞바꿔",
                 "교환", "배치", "정리", "채워", "두고", "둬", "빼", "갖다", "가져다")
MOVE_VERBS = ("옮겨", "옮기", "놔", "놓", "올려", "갖다", "가져다", "치워", "돌려")


def _environment_text(jobs, env: dict) -> str:
    """지속 환경을 한 문장으로. 금지 칸에 자재가 있으면 함께 알린다."""
    from validation.conveyor_slots import occupancy, slot_label

    blocked = env.get("blocked_slots") or []
    unavailable = env.get("unavailable_materials") or []
    names = {m: (spec.get("korean") or m) for m, spec in jobs.materials.items()}
    parts = [("사용 금지 위치: " + ", ".join(slot_label(s) for s in blocked))
             if blocked else "사용 금지 위치 없음",
             ("쓰지 않는 자재: " + ", ".join(names.get(m, m) for m in unavailable))
             if unavailable else "쓰지 않는 자재 없음"]
    taken = occupancy(jobs.slots, (jobs.state.status().get("objects") or {}))
    busy = [f"{slot_label(s)}에 {names.get(taken[s], taken[s])}" for s in blocked
            if taken.get(s)]
    text = "작업 환경을 기억했습니다 — " + " · ".join(parts)
    if busy:
        text += " (지금 " + ", ".join(busy) + "이 있습니다 — 옮기려면 목표를 말해 주세요)"
    return text


def _moves_between_slots(request, jobs) -> bool:
    """계획기가 맡아야 하는 단일 문장인가: 칸 → 다른 칸, 점유된 목적지, 출발지 명시."""
    from validation.conveyor_slots import record_slot
    from validation.simulation_demo_state import HELD_ON_TARGET, ON_PALLET

    if request is None or not request.targets:
        return False
    # 컨베이어 칸을 출발지로 말했다(칸 → 칸 문장). 팔레트 출발은 기존 경로가 검사하고,
    # 다른 팔레트에 놓인 자재는 아래 ON_PALLET 기록으로 계획기에 온다.
    if any(str(src).startswith("slot_") for src in request.sources.values()):
        return True
    # 다른 팔레트로 옮기기 — 점유·임시 자리·경로 판단은 계획기가 한다.
    if any(str(where).startswith("loc_") for where in request.targets.values()):
        return True
    objects = (jobs.state.status().get("objects") or {})
    # 다른 팔레트에 놓인 자재를 다시 옮기는 문장도 계획기가 맡는다.
    if any((objects.get(model) or {}).get("state") == ON_PALLET for model in request.targets):
        return True
    occupied = {record_slot(row): model for model, row in objects.items()
                if row.get("state") == HELD_ON_TARGET}
    for model, where in request.targets.items():
        row = objects.get(model) or {}
        if (row.get("state") == HELD_ON_TARGET and where.startswith("slot_")
                and record_slot(row) != where):
            return True
        # 직접 경로가 다른 자재에 막힌다(측정한 경로) — 우회·순서를 계획기가 정한다.
        if where.startswith("slot_") and not row:
            capability = jobs._capability()
            _plan, found = jobs.check_transfer(model, capability.origin_of(model), where)
            if any(f.code == "geometry.path_obstructed" for f in found):
                return True
        # 목적지 칸을 다른 자재가 차지했다 — 비우는 선행 작업을 계획기가 세운다.
        if where.startswith("slot_") and occupied.get(where) not in (None, model):
            return True
    return False


def _arrangement_answer(ctx: RouteContext, jobs, workcell, utterance: str,
                        normalized: str, goal_intent: dict | None, answer,
                        prefer_planner: bool = False):
    """목표 배치·환경 제약 발화 → 작업 순서 계획(확인 카드). 아니면 None.

    기존 계약을 바꾸지 않는다: "전체 복귀"로 확정된 발화, 모호하다고 되묻거나
    막은 전체 목표, 자재 하나를 옮기는 단일 명령은 기존 경로로 둔다.
    """
    from server.sim_demo_arrangement import (
        ANY_SLOT,
        GOAL_ARRANGE,
        ArrangementError,
        arrangement_signal,
        current_locations,
        interpret_with_llm,
        is_arrangement_like,
        parse_arrangement,
    )
    from server.sim_demo_commands import ASK, BLOCK

    if goal_intent is not None and goal_intent.get("decision") != ASK:
        # 전체 목표로 확정됐거나 막힌 발화는 기존 판정을 그대로 둔다.
        return None
    slot_names = [slot.name for slot in (jobs.slots or ())]
    interpretation: dict = {"interpreted_by": "rules"}
    try:
        request, rule_error = parse_arrangement(utterance, workcell, slot_names), None
    except ArrangementError as exc:
        request, rule_error = None, exc
    signal = arrangement_signal(utterance, workcell)
    if goal_intent is not None:
        # 기존 전체 목표 해석이 되물은 문장은, 모든 자재를 컨베이어에 올리는
        # 배치일 때만 이 경로가 받는다.
        takes_over = request is not None and bool(request.targets) and bool(
            request.excluded_materials
            or all(where == ANY_SLOT for where in request.targets.values()))
        if rule_error is not None or not takes_over:
            return None
    elif rule_error is not None and rule_error.decision == BLOCK:
        # 모순·지원 밖. 자재 하나짜리 명령은 기존 경로가 판정한다(기존 계약).
        # 단, 컨베이어 칸을 출발지로 말한 문장·맥락으로 해석한 문장은 여기서 답한다.
        from server.sim_demo_arrangement import states_slot_source
        if not signal and not states_slot_source(utterance) and not prefer_planner:
            return None
        return answer(409, decision=BLOCK, intent=GOAL_ARRANGE, material=None,
                      reason=str(rule_error))
    elif rule_error is None and (_moves_between_slots(request, jobs) or (
            prefer_planner and request is not None and bool(request.targets))):
        # 칸 → 다른 칸, 점유된 목적지, 출발지 명시 — 단일 명령 경로에는 이 판단이
        # 없다. 계획기가 선행 작업·직접/우회 경로를 정하고 확인 카드에 보인다.
        pass
    elif rule_error is not None or not is_arrangement_like(request):
        if not signal:
            if rule_error is not None:
                # 알아들은 부분만 실행하고 나머지를 버리지 않는다.
                return answer(200, decision=ASK, intent=GOAL_ARRANGE, material=None,
                              reason=str(rule_error))
            return None
        if not any(v in re.sub(r"\s+", "", utterance) for v in ARRANGE_VERBS):
            # 무엇을 하라는지(동사) 없는 문장에서 LLM이 목표를 지어내지 않게 한다.
            return answer(200, decision=ASK, intent=GOAL_ARRANGE, material=None,
                          reason="무엇을 하라는지 알 수 없습니다 — 예: 'A자재는 컨베이어"
                                 " 1번, C자재는 원래 자리로'")
        # 배치 문장인데 규칙이 풀지 못했다 → Qwen이 열거값 배치만 낸다.
        from server.material_colors import stray_color_words
        stray = stray_color_words(utterance)
        if stray:
            # 색으로 가리킨 것을 LLM이 자재로 고르게 두지 않는다.
            return answer(200, decision=ASK, intent=GOAL_ARRANGE, material=None,
                          reason=f"'{stray[0]}'이 어느 자재인지 정할 수 없습니다 —"
                                 " 자재 이름이나 등록된 색으로 말해 주세요")
        classifier = getattr(ctx.runtime, "sim_demo_intent", None)
        if classifier is None or getattr(classifier, "client", None) is None:
            if rule_error is None:
                return None
            return answer(200, decision=ASK, intent=GOAL_ARRANGE, material=None,
                          reason=str(rule_error))
        try:
            state = jobs.state.status()
            current = current_locations(dict(state.get("objects") or {}),
                                        jobs.materials)
            request, interpretation = interpret_with_llm(
                classifier.client, utterance, materials=jobs.materials,
                slot_names=slot_names, current=current,
                min_confidence=float(getattr(classifier, "min_confidence", 0.7)))
        except ArrangementError as exc:
            return answer(200 if exc.decision == ASK else 409, decision=exc.decision,
                          intent=GOAL_ARRANGE, material=None, reason=str(exc),
                          arrangement_interpretation={"interpreted_by": "qwen",
                                                      "failed": True})
    goals = getattr(ctx.runtime, "sim_demo_goals", None)
    if goals is None:
        return answer(503, decision=BLOCK, intent=GOAL_ARRANGE, material=None,
                      reason="시연 목표 실행기를 쓸 수 없습니다")
    environment = None
    if request.changes_environment:
        # 작업 환경 정보는 로봇을 움직이지 않는다. 바로 기억하고 화면에 보인다.
        environment = goals.update_environment(request)
    if not request.targets:
        return answer(200, decision=ENVIRONMENT, intent="environment", material=None,
                      reason=_environment_text(jobs, environment or goals.environment()),
                      environment=environment or goals.environment())
    try:
        # 이미 목표 배치면 목표를 만들지 않고 확인도 묻지 않는다 — 움직일 것이 없다.
        planned, _state = goals._arrangement_plan(request)
        if not planned["steps"]:
            interpretation.pop("raw", None)
            return answer(200, decision=NOOP, intent=GOAL_ARRANGE, material=None,
                          reason=("말한 자재가 모두 이미 목표 자리에 있습니다 — 옮길 것이 없어"
                                  " 움직이지 않습니다"),
                          arrangement_interpretation=interpretation)
        goal = goals.create(GOAL_ARRANGE, request)
    except Exception as exc:
        from server.sim_demo_goals import SimDemoGoalError
        if not isinstance(exc, SimDemoGoalError):
            raise
        return answer(exc.status, decision=BLOCK if exc.status == 409 else ASK,
                      intent=GOAL_ARRANGE, material=None, reason=str(exc))
    interpretation.pop("raw", None)
    return answer(200, decision=CONFIRM_GOAL, intent=GOAL_ARRANGE, material=None,
                  reason=None, goal=goal, arrangement_interpretation=interpretation,
                  confirmation=_goal_confirmation(goal, utterance))


def color_registry(ctx: RouteContext, jobs):
    """셀 선언 색 + (있으면) Gazebo 관측 색. 관측기는 런타임이 붙인다."""
    from server.material_colors import ColorRegistry, pallet_models

    workcell = getattr(jobs, "workcell", None) or {}
    observer = getattr(ctx.runtime, "sim_demo_color_observer", None)
    observed, detail, at = None, "관측기 없음 — 설정 색 기준", None
    if observer is not None:
        observed, detail = observer(list(jobs.materials) + pallet_models(workcell))
        at = time.time()
    return ColorRegistry.from_workcell(workcell, observed or None,
                                       observed_at=at, detail=detail)


def _dialogue_for(ctx: RouteContext, payload: dict):
    """요청의 브라우저 세션 → 그 세션의 대화 맥락. **인증이 아니다.**

    세션 id는 서버가 발급했고 아직 쓸 수 있는 것만 받는다(`require_session`). 없거나
    만료·종료면 맥락도 없다 — 그러면 "그거" 같은 말은 되묻는다.
    """
    registry = getattr(ctx.runtime, "sim_demo_contexts", None)
    if registry is None:
        return None, "대화 맥락 기능이 없다"
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return None, "세션이 없다"
    api = getattr(ctx, "api", None)
    if api is not None:
        try:
            api.require_session(session_id)
        except ApiError as exc:
            registry.forget(session_id)
            return None, f"세션을 쓸 수 없다({exc.status})"
    return registry.get(session_id), "세션별 맥락"


def _free_spot_answer(ctx: RouteContext, jobs, *, utterance: str, source: str,
                      detected: dict, classifier: dict | None = None):
    """표면 빈 위치 놓기 → 확인 카드(CONFIRM) 또는 ASK/BLOCK. (fields, status). 작업을 만들지 않는다.

    규칙·분류기가 정하는 것은 자재와 표면뿐이다. 자리·자세는 서버가 계산·검증한다
    (`server/sim_free_spot.py`).
    """
    service = getattr(ctx.runtime, "sim_free_spot", None)
    store = getattr(ctx.runtime, "sim_demo_confirm", None)
    base = {"intent": "spot", "material": detected.get("material"), "job_spec": None,
            "intent_result": classifier, "free_spot": {"detected": detected}}
    if service is None or store is None:
        return {**base, "decision": "BLOCK",
                "reason": "표면 빈 위치 놓기를 쓸 수 없습니다(표면 선언 또는 확인 기능 없음)"}, 409
    if not detected.get("surface_id"):
        return {**base, "decision": "ASK",
                "reason": "어느 표면에 놓을지 하나로 말해 주세요"}, 200
    if not detected.get("material"):
        return {**base, "decision": "ASK",
                "reason": "어느 자재를 놓을지 하나로 말해 주세요(A·B·C 자재)"}, 200
    if source == "stt_final" and detected.get("stt_confidence_low"):
        return {**base, "decision": "ASK", "reason": "다시 말해 주세요"}, 200
    result = service.plan(material=detected["material"], surface_id=detected["surface_id"],
                          stated_source=detected.get("stated_source"))
    base["free_spot"] = {"detected": detected,
                         **{k: result.get(k) for k in ("source", "selection", "why",
                                                        "observed_source_gap_m")}}
    if result["decision"] != "CONFIRM":
        return {**base, "decision": result["decision"], "reason": result["reason"]}, (
            200 if result["decision"] == "ASK" else 409)
    spec = {"action": "spot", "material": detected["material"], "spot": result["spot"],
            "surface_id": detected["surface_id"]}
    status = jobs.status()
    pending = store.create(
        job_spec=spec, status=status, summary=result["summary"],
        evidence={"surface_id": detected["surface_id"],
                  "spot": {k: result["spot"].get(k) for k in ("spot_id", "center_m", "source")},
                  "why": result["why"], "selection": result["selection"],
                  "classifier": classifier,
                  "material_korean": _material_korean(jobs, detected["material"]),
                  "state": _state_brief(jobs, status),
                  "recheck": "확인을 누르면 출발지·자리 빈 상태·도달·충돌·경로를 다시 보고,"
                             " 실행기가 장면 동기화·사전 검사·경로 검사를 한 번 더 합니다"},
        utterance=utterance, source=source)
    return {**base, "decision": CONFIRM, "job_spec": spec, "reason": None,
            "confirmation": pending.to_json(time.time())}, 200


def command_response(ctx: RouteContext, payload: dict) -> Response:
    """발화 하나 → CONFIRM(확인 대기) / STOP / ASK / BLOCK. 작업은 만들지 않는다."""
    from server.sim_demo_commands import (
        ASK,
        BLOCK,
        PASS_THROUGH,
        RUN,
        SIMULATION_NOTICE,
        SOURCES,
        STOP,
        decide,
        looks_like_material_work,
        parse_command,
    )
    from stt.command_normalization import normalize_command

    utterance = str(payload.get("utterance") or "")
    from server.api import check_utterance_length
    check_utterance_length(utterance.strip())          # 모델·해석 전에 길이를 본다(리뷰 8번)
    source = str(payload.get("source") or "")
    raw_transcript = str(payload.get("raw_transcript") or utterance) if source == "stt_final" else None
    normalized = normalize_command(utterance)
    # 실기 상태 키(`real_hardware_*`)는 넣지 않는다 — 공개 필터가 어차피 지우므로
    # 여기서 만들면 "필터가 무엇을 지웠나"를 신호로 쓸 수 없다.
    base = {"is_simulated": True,
            "simulation_notice": SIMULATION_NOTICE, "utterance": utterance,
            "raw_transcript": raw_transcript, "normalized_transcript": normalized,
            "stt_confidence": payload.get("stt_confidence") if source == "stt_final" else None,
            "source": source, "job": None, "job_spec": None, "stop": None,
            "confirmation": None, "intent_result": None,
            "slot": None, "slot_label": None}

    session = {"dialogue": None}

    def answer(status: int, **fields) -> Response:
        response = {**base, **fields}
        # 음성: 어떤 경로(규칙·Qwen·해석)에서 멈췄든 **들은 문장**을 사유에 보인다.
        if (source == "stt_final" and response.get("decision") in (ASK, BLOCK)
                and response.get("reason")
                and not str(response["reason"]).startswith("제가 이렇게 들었습니다")):
            response["reason"] = f"제가 이렇게 들었습니다: {raw_transcript}. {response['reason']}"
        interp = response.get("interpretation") or {}
        if response.get("confirmation") and interp.get("evidence"):
            response["confirmation"] = {**response["confirmation"], "interpretation": interp}
        focus_ctx = session["dialogue"]
        if focus_ctx is not None and response.get("decision") in ("RUN", "CONFIRM",
                                                                 "CONFIRM_GOAL"):
            plan = (response.get("goal") or {}).get("plan") or []
            focus_ctx.set_focus([row.get("material") for row in plan]
                                or [response.get("material")
                                    or (response.get("job") or {}).get("material")])
        if source == "stt_final":
            interpretation = {key: response.get(key) for key in
                              ("decision", "intent", "material", "reason")}
            interpretation["job_spec"] = response.get("job_spec")
            interpretation["qwen_candidate"] = response.get("intent_result")
            response["diagnostic"] = {
                "raw_transcript": raw_transcript,
                "normalized_transcript": normalized,
                "stt_confidence": response["stt_confidence"],
                "interpretation": interpretation,
            }
            logging.getLogger(__name__).info("stt_command_diagnostic %s",
                json.dumps(response["diagnostic"], ensure_ascii=False))
        # 확인 카드·목표를 요청 세션에 묶는다(2026-10-08 리뷰 2번) — 같은 세션만 확인한다.
        req_session = str(payload.get("session_id") or "")
        token = (response.get("confirmation") or {}).get("token")
        if req_session and token:
            store_ = getattr(ctx.runtime, "sim_demo_confirm", None)
            if store_ is not None:
                store_.bind_session(token, req_session)
        goal_id = (response.get("goal") or {}).get("goal_id")
        goals_ = getattr(ctx.runtime, "sim_demo_goals", None)
        if req_session and goal_id and goals_ is not None and hasattr(goals_, "bind_session"):
            goals_.bind_session(goal_id, req_session)
        return json_response(response, status)

    config = getattr(ctx.runtime, "config", None)
    if config is not None and not getattr(config, "enable_sim_demo_command", True):
        # 일반 모드 시험: 시연 명령 해석을 하지 않는다. 화면은 PASS_THROUGH를 받아 계획 생성으로 간다.
        return answer(200, decision=PASS_THROUGH, intent=None, material=None,
                      reason="시연 명령 입구가 꺼져 있다(FORSTICK2_SIM_DEMO_COMMAND=0)"
                             " — 일반 계획 생성(/v1/plan)으로 간다")
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
    from server.sim_demo_commands import stop_intent as _stop_intent
    _stop = _stop_intent(normalized)
    if _stop.kind == "ambiguous":
        from planning.stop_intent import AMBIGUOUS_STOP_MESSAGE
        return answer(200, decision=ASK, intent=None, reason=AMBIGUOUS_STOP_MESSAGE)
    if _stop.is_stop:
        # 정지는 **다른 모든 해석보다 먼저**다 — 색·맥락·배치 해석과 Qwen을 거치지 않는다.
        # (전에는 맥락 해석이 "그거 멈춰"를 ASK로 먼저 끝낼 수 있었다.)
        return answer(200, decision=STOP, intent="stop",
                      stop=jobs.request_stop(reason="sim_demo_command_stop"))
    workcell = getattr(jobs, "workcell", None) or {}
    places = getattr(jobs, "places", ()) or ()
    # 색으로 가리킨 자재 → 셀이 선언한 자재 이름. 정하지 못하면 고르지 않고 묻는다.
    from server.material_colors import ColorResolutionError
    registry = color_registry(ctx, jobs)
    try:
        resolved, substitutions = registry.rewrite(utterance)
    except ColorResolutionError as exc:
        return answer(200, decision=ASK, intent=None, material=None, reason=str(exc),
                      color_resolution={"ok": False, "registry": registry.to_dict()})
    if substitutions:
        utterance = resolved
        normalized = normalize_command(resolved)
    base["color_resolution"] = {"ok": True, "substitutions": substitutions,
                                "resolved_utterance": utterance if substitutions else None}
    # 생략·지시·맥락 표현 → 선언된 이름(대화 맥락·관측 기록·기억된 환경 근거).
    from server.sim_demo_context import facts_from, resolve as resolve_context
    dialogue, dialogue_note = _dialogue_for(ctx, payload)
    session["dialogue"] = dialogue
    base["dialogue"] = {"session_context": dialogue is not None, "note": dialogue_note,
                        "authentication": False}
    goals_rt = getattr(ctx.runtime, "sim_demo_goals", None)
    resolution = resolve_context(
        utterance, facts=facts_from(jobs, goals_rt.environment() if goals_rt else {}),
        context=dialogue, source=source, stt_confidence=payload.get("stt_confidence"))
    evidence = [f"'{row['said']}' = {row['korean']}(등록 색 {row['color_label']},"
                f" 관측 {row['check']})" for row in substitutions] + resolution.evidence
    base["interpretation"] = {"evidence": evidence,
                              "resolved_utterance": resolution.text
                              if resolution.applied else None}
    if resolution.decision:
        reason = resolution.reason
        if resolution.decision == ASK:
            # 맥락 해석이 끝내지 못했다("그거"·"저쪽" 등). 바로 끝내지 않고 Qwen에 묻는다.
            # 서버 검증을 통과해 확인 카드가 나올 때만 그것을 쓰고, 아니면 원래 질문 그대로
            # 되묻는다 — 맥락 해석이 걸어 둔 되묻기(빈칸 채우기) 흐름을 깨지 않는다.
            classified, code = _classify(
                ctx, jobs, normalized, source, ASK, reason, said=utterance,
                registry=registry, dialogue=dialogue,
                original=str(payload.get("utterance") or ""))
            if classified.get("decision") == CONFIRM:
                return answer(code, **classified)
            return answer(200, decision=ASK, intent=None, material=None, reason=reason,
                          intent_result=classified.get("intent_result"))
        return answer(409, decision=resolution.decision, intent=None, material=None,
                      reason=reason)
    if resolution.applied:
        utterance = resolution.text
        normalized = normalize_command(utterance)
    if dialogue is not None:
        # "그거"는 직전에 **말한** 자재다 — 판정 결과와 무관하게 언급을 기억한다.
        from server.sim_demo_context import _mentioned as mentioned_in
        said = mentioned_in(utterance, facts_from(jobs, {}))
        if said:
            dialogue.set_focus(said)
    compact = re.sub(r"\s+", "", utterance)
    bypass = next((w for w in SAFETY_BYPASS_WORDS if w in compact), None)
    if bypass:
        # 검사·확인을 건너뛰라는 요청은 받지 않는다. 실행 경로는 항상 같은 검사를 한다.
        return answer(409, decision=BLOCK, intent=None, material=None,
                      reason=f"'{bypass}' — 검증·확인·안전 검사를 건너뛰는 요청은 받지"
                             " 않습니다. 검사를 거치는 일반 명령으로 말해 주세요")
    if (any(w in compact for w in VAGUE_REFERENCE_WORDS)
            and any(v in compact for v in MOVE_VERBS)
            and "자재" not in compact
            and not re.search(r"컨베이어|팔레트|\d+번", compact)):
        # 자재도 목적지도 없이 지시어만으로 옮기라는 말 — 규칙으로는 정할 수 없다.
        # 바로 끝내지 않고 Qwen에 대화 맥락과 함께 묻는다. 서버가 근거(이름·색·맥락)를
        # 다시 보므로, 가리킬 자재가 없으면 결국 되묻는다(실행하지 않는다).
        vague_reason = "어느 자재를 어디로 옮길지 알 수 없습니다 — 자재 이름과 위치로 말해 주세요"
        classified, status_code = _classify(
            ctx, jobs, normalized, source, ASK, vague_reason,
            said=utterance, registry=registry, dialogue=dialogue,
            original=str(payload.get("utterance") or ""))
        return answer(status_code, **{"decision": ASK, "intent": None, "material": None,
                                      "reason": vague_reason, "job_spec": None,
                                      **classified})
    # 표면만 말한 놓기("A자재를 작업대 빈 곳에 놔"). 자리는 서버가 계산한다.
    from server.sim_free_spot import detect as detect_free_spot, surfaces_of
    free_spot = detect_free_spot(normalized, workcell, surfaces_of(jobs))
    if free_spot is not None:
        fields, code = _free_spot_answer(ctx, jobs, utterance=utterance, source=source,
                                         detected=free_spot)
        return answer(code, **fields)
    # Collective commands have exactly one supported meaning. Resolve them
    # before the single-material parser/classifier so they cannot become a
    # free-form plan or an accidentally selected child job.
    from server.sim_demo_goals import GOAL_RETURN_ALL, interpret_goal_command
    from server.sim_demo_commands import mentions_conveyor
    exact_return_all = (
        "컨베이어" in normalized
        and any(word in normalized for word in ("모두", "전부", "전체"))
        and any(word in normalized for word in ("원래", "제자리", "돌려", "복귀"))
    )
    goal_intent = (
        {"decision": CONFIRM_GOAL, "goal": GOAL_RETURN_ALL}
        if exact_return_all else interpret_goal_command(
            f"{utterance} {normalized}",
            mentions_conveyor=mentions_conveyor(normalized, workcell))
    )
    arrangement = _arrangement_answer(ctx, jobs, workcell, utterance, normalized,
                                      goal_intent, answer,
                                      prefer_planner=resolution.applied or bool(
                                          substitutions and not re.search(
                                              r"옮겨|옮기|놔|놓|올려|돌려|보내|갖다|가져다",
                                              utterance)))
    if arrangement is not None:
        return arrangement
    if goal_intent is not None:
        if goal_intent["decision"] not in (CONFIRM, CONFIRM_GOAL):
            return answer(200 if goal_intent["decision"] == ASK else 409,
                          decision=goal_intent["decision"],
                          intent=GOAL_RETURN_ALL,
                          material=None, reason=goal_intent.get("reason"))
        goals = getattr(ctx.runtime, "sim_demo_goals", None)
        if goals is None:
            return answer(503, decision=BLOCK, intent=GOAL_RETURN_ALL,
                          material=None, reason="시연 목표 실행기를 쓸 수 없습니다")
        try:
            goal = goals.create(GOAL_RETURN_ALL)
        except Exception as exc:
            from server.sim_demo_goals import SimDemoGoalError
            if not isinstance(exc, SimDemoGoalError):
                raise
            return answer(exc.status, decision=BLOCK, intent=GOAL_RETURN_ALL,
                          material=None, reason=str(exc))
        confirmation = _goal_confirmation(goal, utterance)
        return answer(200, decision=goal_intent["decision"], intent=GOAL_RETURN_ALL,
                      material=None, reason=None, goal=goal,
                      confirmation=confirmation)
    parsed = parse_command(normalized, workcell, jobs.slots)
    if parsed.get("decision") == STOP:
        # 계획·LLM을 거치지 않고 즉시 기존 시연 정지로 보낸다.
        return answer(200, decision=STOP, intent="stop",
                      stop=jobs.request_stop(reason="sim_demo_command_stop"))
    # 지시어·'빈자리'는 특정 출발·도착의 근거가 아니다. STOP만 우선한다.
    vague_places = ("저쪽", "이쪽", "그쪽", "저기", "여기", "거기", "빈자리", "어디")
    if source == "stt_final" and any(word in normalized for word in vague_places):
        return answer(200, decision=ASK, intent=None, material=None,
                      reason=f"제가 이렇게 들었습니다: {raw_transcript}. 어느 위치인지 지정해 주세요")
    decision = decide(parsed, jobs.status(), jobs.materials, jobs.slots, places)
    fields = {k: decision.get(k) for k in ("decision", "intent", "material", "reason",
                                           "job_spec")}
    from server.material_colors import stray_color_words
    stray = stray_color_words(utterance)
    colored = _color_materials(registry, stray)
    color_models = {m for models in colored.values() for m in models}
    if (decision.get("decision") == RUN and color_models
            and (decision.get("job_spec") or {}).get("material") not in color_models):
        # 규칙이 자재를 정했지만 발화의 색과 맞지 않는다(예: 자재 없는 복귀가 컨베이어의
        # 다른 자재를 고른 경우). 규칙 결과를 쓰지 않고 Qwen 해석·검증으로 넘긴다.
        decision = {**decision, "decision": ASK, "job_spec": None,
                    "reason": "말한 색과 규칙이 고른 자재가 다릅니다"}
        fields = {k: decision.get(k) for k in ("decision", "intent", "material", "reason",
                                               "job_spec")}
    if decision.get("decision") in (ASK, PASS_THROUGH):
        # 모호한 **자재 작업** 발화만 분류기로 보낸다. 자재 작업처럼 보이지 않으면
        # 그대로 둔다 — PASS_THROUGH는 화면이 기존 계획 생성으로 가져간다.
        if looks_like_material_work(normalized, workcell) or colored:
            unknown = [word for word in stray if not colored.get(word)]
            if unknown:
                # 등록되지 않은 색 표현을 LLM이 자재로 고르게 두지 않는다.
                return answer(200, **{**fields, "decision": ASK,
                                      "reason": f"'{unknown[0]}'이 어느 자재인지 정할 수"
                                                " 없습니다 — 자재 이름이나 등록된 색으로"
                                                " 말해 주세요"})
            # 규칙이 끝내지 못한 자재 작업(ASK·PASS_THROUGH) — Qwen에 묻고 서버가 검증한다.
            classified, status_code = _classify(
                ctx, jobs, normalized, source, decision["decision"],
                decision.get("reason"), said=utterance, registry=registry,
                dialogue=dialogue, original=str(payload.get("utterance") or ""))
            return answer(status_code, **{**fields, **classified})
        return answer(200, **fields)
    if decision.get("decision") != RUN:
        return answer(409, **fields)
    spec = decision["job_spec"]
    # **로봇이 움직이는 동작은 모두 확인 카드를 거친다.** 규칙으로 정확히
    # 해석된 텍스트 명령("A 자재를 컨베이어로 옮겨줘")도 사람이 판정을 한 번
    # 보고 눌러야 한다(요구: 발화 → 확인 한 번 → 실행).
    # 작업은 `/v1/sim-demo/confirm`에서만 만들어진다.
    store = getattr(ctx.runtime, "sim_demo_confirm", None)
    if store is None:
        return answer(409, **{**fields, "decision": BLOCK, "job_spec": None,
                              "reason": "확인 카드가 필요한데 확인 기능을 쓸 수 없어"
                                        " 실행하지 않았습니다"})
    refusal = _material_refusal(ctx, jobs, spec)
    if refusal is not None:
        return answer(refusal[1], **{**fields, **refusal[0]})
    body, code = offer_confirmation(
        jobs, store, spec, utterance=utterance, source=source,
        rule_decision=decision.get("decision"),
        rule_reason=decision.get("reason"))
    return answer(code, **{**fields, **body})


def confirm_response(ctx: RouteContext, payload: dict) -> Response:
    """확인 카드의 버튼 → 작업 생성(확인) 또는 아무것도 하지 않음(취소·거부).

    `confirm`이 통과했을 때만 기존 시연 작업이 만들어진다. 만료·상태 변경·
    실행 중 작업·취소는 전부 **작업 0건**이다.
    """
    from server.sim_demo_commands import BLOCK, RUN, SIMULATION_NOTICE
    from server.sim_demo_confirm import ConfirmRejection
    from server.sim_demo_jobs import SimDemoJobError

    token = str(payload.get("token") or "")
    action = str(payload.get("action") or "confirm")
    base = {"is_simulated": True,
            "simulation_notice": SIMULATION_NOTICE, "token": token,
            "job": None, "job_spec": None, "confirmation": None,
            "slot": None, "slot_label": None}

    def answer(status: int, **fields) -> Response:
        return json_response({**base, **fields}, status)

    jobs = getattr(ctx.runtime, "sim_demo_jobs", None)
    store = getattr(ctx.runtime, "sim_demo_confirm", None)
    if jobs is None or store is None:
        return answer(403, decision=BLOCK,
                      reason="시뮬레이션 시연을 쓸 수 없다: "
                             + str(getattr(ctx.runtime, "sim_demo_disabled_reason", "")))
    if action not in ("confirm", "cancel"):
        return answer(400, decision=BLOCK, reason=f"알 수 없는 동작이다: {action}")
    session_id = str(payload.get("session_id") or "")
    if action == "cancel":
        owner = store.peek(token)
        if owner is not None and owner.session_id and owner.session_id != session_id:
            return answer(409, decision=BLOCK, reason="이 확인 카드를 만든 세션이 아닙니다",
                          confirm_rejection="session")
        removed = store.cancel(token)
        return answer(200, decision=CANCELLED,
                      reason="확인을 취소했습니다 — 작업을 만들지 않았습니다"
                             if removed else "확인 대기가 이미 없습니다")

    if _stop_latched(ctx):
        # 정지 래치 — 확인 카드가 있어도 시작하지 않는다. 카드는 지우지 않는다(남의 카드일 수도 있다 — 만료로 사라진다).
        return answer(409, decision=BLOCK, reason="전체 정지가 걸려 있습니다 — 정지 해제 뒤 다시 요청해 주세요",
                      confirm_rejection="stopped", reason_code=ReasonCode.EXEC_STOPPED.value)
    taken = store.take(token, jobs.status(), session_id=session_id)
    if isinstance(taken, ConfirmRejection):
        return answer(409, decision=BLOCK, reason=taken.reason,
                      confirm_rejection=taken.code)
    spec = taken.job_spec
    if spec.get("action") == "spot":
        # 표면 빈 위치: 누른 시점의 관측으로 출발지·자리·MoveIt 검사를 다시 본다.
        service = getattr(ctx.runtime, "sim_free_spot", None)
        if service is None:
            return answer(409, decision=BLOCK, reason="표면 빈 위치 놓기를 쓸 수 없습니다",
                          confirm_rejection="not_allowed", job_spec=spec)
        ok, why, recheck = service.recheck(spec)
        if not ok:
            return answer(409, decision=BLOCK, reason=f"실행 직전 재검사에서 막았습니다: {why}",
                          confirm_rejection="recheck_failed", job_spec=spec,
                          free_spot={"recheck": recheck})
        try:
            job = jobs.start("spot", spec["material"], spot=spec["spot"])
        except SimDemoJobError as exc:
            return answer(409, decision=BLOCK, reason=str(exc),
                          confirm_rejection="not_allowed", job_spec=spec)
        return answer(202, decision=RUN, intent="spot", material=spec["material"],
                      job_spec=spec, job=job, utterance=taken.utterance,
                      summary=taken.summary, free_spot={"recheck": recheck})
    from server.sim_demo_commands import requested_slot
    check = _material_check(ctx, jobs, spec["material"], spec["action"])
    if check is not None and check["kind"] == "noop":
        return answer(200, decision="NOOP", reason=check["detail"], job_spec=spec, material_check=check)
    if check is not None and check["kind"] != "ok":
        return answer(409, decision=BLOCK, reason=f"실행 직전 확인에서 막았습니다: {check['detail']} — {check['guidance']}",
                      confirm_rejection="material_check", job_spec=spec, material_check=check)
    try:
        job = jobs.start(spec["action"], spec["material"],
                         checkpoint_id=spec.get("checkpoint_id"),
                         requested_slot=requested_slot(spec, jobs.places))
    except SimDemoJobError as exc:
        return answer(409, decision=BLOCK, reason=str(exc),
                      confirm_rejection="not_allowed", job_spec=spec)
    if _stop_latched(ctx):
        # 확인은 이벤트 루프 밖에서 돈다 — 위의 정지 검사와 시작 사이에 들어온 전체 정지는 실행 중 작업이 없어
        # 전달되지 않았다. 시작 직후 다시 보고 바로 정지를 전달한다(작업이 정지 없이 계속 움직이지 않게).
        stop = jobs.request_stop(reason="global_stop")
        return answer(409, decision=BLOCK, reason="시작 직후 전체 정지가 확인돼 바로 정지를 전달했습니다",
                      confirm_rejection="stopped", job_spec=spec, job=job, stop=stop)
    actual_spec = {**spec, "slot": job.get("slot")}
    return answer(202, decision=RUN, intent=spec["action"],
                  material=spec["material"], job_spec=actual_spec, job=job,
                  slot=job.get("slot"), slot_label=job.get("slot_label"),
                  utterance=taken.utterance, summary=taken.summary)
