"""stt_eval 라우트(2026-10-08) — 음성 인식률(글자 기준) 표시값 조회. **읽기 전용, 로봇 제어와 무관.**

| 경로 | 뜻 |
|---|---|
| `GET /v1/stt/recognition-rate` | 현재 STT 모델·설정의 실제 녹음·사람 확인 정답 평가 결과(파일). 없으면 '미측정' |

평가 결과 파일을 읽기만 한다(`stt/recognition_store.py`). STT 모델을 불러오거나 전사하지 않는다.
모델 신뢰도를 인식률로 쓰지 않고, 정답 없는 현재 발화에 %를 붙이지 않는다.
"""

from __future__ import annotations

from server.routes.common import RouteContext, Response, json_response

PATH = "/v1/stt/recognition-rate"


async def handle(ctx: RouteContext, method: str, path: str, receive, query: dict[str, str]) -> Response | None:
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
