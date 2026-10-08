"""stt_eval 라우트(2026-10-08) — 음성 인식률(글자 기준) 표시값 조회. **읽기 전용, 로봇 제어와 무관.**

| 경로 | 뜻 |
|---|---|
| `GET /v1/stt/recognition-rate` | 현재 STT 모델·설정의 평가 결과(파일). 없으면 '미측정' |
| `POST /v1/stt/utterance-check` | 이번 발화 인식률: `{session_id, stt_request_id, reference(사용자가 확정한 실제 발화)}` → 서버가 그 세션의 STT 기록과 대조해 계산·저장. **계획·승인·실행을 부르지 않는다** |
| `GET /v1/stt/utterance-check?session_id=&stt_request_id=` | 같은 세션의 확정 기록 조회. 다른 세션이면 거부 |

평가 결과 파일을 읽기만 한다(`stt/recognition_store.py`). STT 모델을 불러오거나 전사하지 않는다.
모델 신뢰도를 인식률로 쓰지 않고, 정답 없는 현재 발화에 %를 붙이지 않는다.
"""

from __future__ import annotations

from server.routes.common import RouteContext, Response, json_response

PATH = "/v1/stt/recognition-rate"
CHECK_PATH = "/v1/stt/utterance-check"


async def handle(ctx: RouteContext, method: str, path: str, receive, query: dict[str, str]) -> Response | None:
    if path == CHECK_PATH and method == "POST":
        return await _utterance_check(ctx, receive)
    if path == CHECK_PATH and method == "GET":
        return _utterance_check_get(ctx, query)
    if path != PATH or method != "GET":
        return None
    from stt.recognition_rate import NORMALIZATION_VERSION
    from stt.recognition_store import DISPLAY_NOTE, display_summary, model_key

    config = getattr(ctx.runtime, "stt_model_config", None)
    if config is None:
        return json_response({"status": "unmeasured", "label": "음성 인식률 미측정", "rate_percent": None,
                              "note": DISPLAY_NOTE, "normalization_version": NORMALIZATION_VERSION,
                              "model_key": None, "reason": "STT 모델 설정이 없습니다(STT 꺼짐)"})
    return json_response(display_summary(model_key(config)))


def _errors():
    from core.reason_codes import ReasonCode
    from server.api import ApiError
    from stt.utterance_check import InvalidCheck, NotFound, OwnershipError

    return ((InvalidCheck, 400, ReasonCode.PLAN_ARG_UNKNOWN), (NotFound, 404, ReasonCode.CONFIG_MISSING),
            (OwnershipError, 403, ReasonCode.PLAN_ARG_UNKNOWN)), ApiError


def _raise_mapped(exc):
    table, ApiError = _errors()
    for cls, status, code in table:
        if isinstance(exc, cls):
            raise ApiError(status, code, str(exc)) from exc
    raise exc


async def _utterance_check(ctx: RouteContext, receive) -> Response:
    """사용자가 확정한 실제 발화와 **서버 저장소의** 수정 전 STT 결과를 비교한다(파일 저장만). 로봇 제어 요청을 만들지 않는다.

    세션 확인(`require_session`) → STT 요청 기록이 이 세션 것인지 확인 → 저장소의 STT 문장으로 계산.
    클라이언트가 보낸 stt_text·utterance_id는 쓰지 않는다."""
    from stt.recognition_store import model_key
    from stt.utterance_check import check_dir, confirm, owned_stt, utterance_id_for

    payload = await ctx.read_body(receive)
    session_id = payload.get("session_id")
    ctx.api.require_session(session_id)
    config = getattr(ctx.runtime, "stt_model_config", None)
    try:
        stt = owned_stt(ctx.runtime.repository, session_id=session_id, stt_request_id=payload.get("stt_request_id"))
        record = confirm(utterance_id=utterance_id_for(stt["stt_request_id"]), stt_text=stt["stt_text"],
                         reference=payload.get("reference"), stt_request_id=stt["stt_request_id"],
                         model_key=model_key(config) if config is not None else None,
                         root=check_dir(ctx.config.db_path), session_id=session_id, stt_raw_text=stt["stt_raw_text"])
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)
    return json_response(_public(record))


def _utterance_check_get(ctx: RouteContext, query: dict[str, str]) -> Response:
    from stt.utterance_check import check_dir, owned_stt, read_owned

    session_id = query.get("session_id")
    ctx.api.require_session(session_id)
    try:
        owned_stt(ctx.runtime.repository, session_id=session_id, stt_request_id=query.get("stt_request_id"))
        record = read_owned(root=check_dir(ctx.config.db_path), session_id=session_id,
                            stt_request_id=query.get("stt_request_id"))
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)
    return json_response(_public(record) if record else {"status": "unconfirmed", "label": None})


def _public(record: dict) -> dict:
    score = record["score"]
    return {"utterance_id": record["utterance_id"], "stt_request_id": record["stt_request_id"], "status": score["status"],
            "label": record["label"], "rate_percent": (score.get("counts") or {}).get("rate_percent"),
            "counts": score.get("counts"), "basis": record["basis"], "stt_text": record["stt_text"],
            "confirm_count": record["confirm_count"], "normalization_version": record["normalization_version"]}
