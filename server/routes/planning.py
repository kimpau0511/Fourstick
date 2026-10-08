"""planning 라우트 (md/개발플랜.md 3-04).

요청·계획 생성·계획 조회. **계획 생성은 실행하지 않는다.**

모든 경로가 `session_id`를 명시적으로 요구하고, 조회는 `request_id`·`plan_id`를
함께 받는다 — 서버가 '현재 계획'을 추정하지 않는다.
"""

from __future__ import annotations

from server.routes.common import (
    RouteContext,
    Response,
    body_field,
    json_response,
    query_param,
)

PATHS: tuple[str, ...] = ("/v1/plan",)


async def handle(
    ctx: RouteContext, method: str, path: str, receive, query: dict[str, str],
) -> Response | None:
    """맡은 경로면 응답을, 아니면 None을 돌려준다."""
    if path != "/v1/plan":
        return None

    if method == "GET":
        return json_response(ctx.api.get_plan(
            session_id=query_param(query, "session_id"),
            request_id=query_param(query, "request_id"),
            plan_id=query_param(query, "plan_id"),
        ))

    if method == "POST":
        payload = await ctx.read_body(receive)
        result = ctx.api.create_plan(
            session_id=body_field(payload, "session_id"),
            utterance=str(payload.get("utterance", "")),
            stt_inference_id=payload.get("stt_inference_id"),
        )
        _log_voice_outcome(ctx, payload, result)
        # 계획을 만들지 못한 경우도 이유 코드와 함께 돌려준다(422).
        return json_response(result, 200 if result.get("ok") or result.get("stopped") else 422)

    return None


def _log_voice_outcome(ctx: RouteContext, payload: dict, result: dict) -> None:
    """음성으로 시작한 요청의 결과를 남긴다(2026-10-08) — STT 오인식과 해석 실패를 나눠 보기 위해.

    `payload["stt"]`(화면이 보냄): stt_request_id(STT가 저장한 요청), stt_text(STT 확정 전사), stt_raw_text,
    stt_confidence, edited(사용자가 보내기 전에 고쳤는가). STT 전사 원본은 STT 세션이 따로 저장한다.
    분류(기록용, 판정에 쓰지 않는다):
      user_edited    — **사용자 수정 발생**: 보내기 전에 STT 결과를 고쳤다. STT 오인식으로 확정하지 않는다
                       (사용자가 오인식을 고쳤을 수도, 말하려던 내용을 바꿨을 수도 있다 — 사람이 원문과 대조해 판단).
      planned        — 고치지 않고 보냈고 계획이 만들어졌다
      not_planned    — 고치지 않고 보냈는데 계획이 없다(되묻기·차단·해석 실패). 원인(STT 오인식·해석 실패)은
                       확정하지 않는다 — STT 원문과 발화 의도를 사람이 대조해야 한다
    기록 실패는 계획 응답에 영향을 주지 않는다.
    """
    stt = payload.get("stt")
    if not isinstance(stt, dict) or not stt.get("stt_text"):
        return
    import json
    import time

    final_text = str(payload.get("utterance", "")).strip()
    stt_text = str(stt.get("stt_text") or "")
    edited = final_text != stt_text.strip()
    if edited:
        kind = "user_edited"
    elif result.get("ok"):
        kind = "planned"
    else:
        kind = "not_planned"
    row = {
        "at": time.time(), "session_id": payload.get("session_id"),
        "request_id": result.get("request_id"), "plan_id": (result.get("plan") or {}).get("plan_id") or result.get("plan_id"),
        "stt_request_id": stt.get("stt_request_id"), "stt_text": stt_text, "stt_raw_text": stt.get("stt_raw_text"),
        "stt_confidence": stt.get("stt_confidence"), "final_text": final_text, "edited": edited, "kind": kind,
        "plan_ok": bool(result.get("ok")), "reason_code": result.get("reason_code"),
        "clarification": result.get("clarification"),
    }
    try:
        path = ctx.config.db_path.parent / "voice_outcomes.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 — 기록 실패가 계획 응답을 막지 않는다
        pass

