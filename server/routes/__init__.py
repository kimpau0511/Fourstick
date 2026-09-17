"""라우트 모듈 (md/개발플랜.md 3-04).

`server/asgi.py`는 앱 조립과 오류 응답만 담당하고, 경로 처리는 여기 모듈들이
맡는다.

| 모듈 | 담당 |
|---|---|
| `common` | RouteContext, 식별자 읽기, ReasonCode 응답, 이벤트 팬아웃 |
| `session` | 세션 발급·이어받기·종료, `/health`, `/v1/config` |
| `planning` | 요청·계획 생성·계획 조회 |
| `execution` | 검증·승인·실행·취소·STOP·상태 복원·상태 이벤트 WebSocket |
| `robot` | Registry 목록·연결 상태·Capability·지원 스킬 |
| `scene` | 작업 셀 장면 영상(서버가 렌더링한 카메라 프레임) |
| `stt` | 오디오 WebSocket과 partial/final |

**라우트 사이에 전역 '현재 상태'를 두지 않는다.** 모든 조회는 명시적 식별자와
Repository로 한다. `HTTP_ROUTES` 순서대로 물어보고, 맡은 경로가 아니면
`None`을 돌려준다.
"""

from __future__ import annotations

from server.routes import execution, planning, robot, scene, session, stt

#: HTTP 처리 순서. 겹치는 경로가 없으므로 순서는 성능 외 의미가 없다.
HTTP_ROUTES = (session, planning, execution, robot, scene)

__all__ = [
    "HTTP_ROUTES", "common", "execution", "planning", "robot", "scene",
    "session", "stt",
]
