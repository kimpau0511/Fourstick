"""계약 자체를 구성하는 허용 상수. md/계획.md 27장의 "허용되는 상수"만 여기 둔다.

27장 규칙: 허용 상수도 한 모듈에서만 정의하고 여러 파일에 중복 작성하지 않는다.
따라서 schema version과 원자 스킬 이름은 이 모듈이 유일한 출처다.
실행 상태 enum과 이유 코드 enum은 각각 execution_state.py / reason_codes.py가
유일한 출처이며, 이 모듈에서 다시 정의하지 않는다.

여기에 두면 안 되는 값(27장): 로봇 ID, 관절/링크/토픽 이름, 프레임명, TCP 오프셋,
작업 좌표, 속도·timeout·tolerance·clearance·gain, 그리퍼 open/close, 모델 경로,
주소·포트. 이들은 Capability Profile / Catalog / Policy / 환경 설정에 둔다.
"""

from __future__ import annotations

from typing import Final

# Task Plan 계약 버전. 필드가 바뀌면 올린다.
TASK_PLAN_SCHEMA_VERSION: Final[str] = "2.0"

# 수용하는 버전 목록. 호환 범위 결정과 근거는 md/호환범위_1-10.md에 있다.
# 현재 결정: 구버전을 수용하지 않고 명확히 거부만 한다 — forstick2에 v1
# 생산자·소비자·저장 데이터가 없고 요구정의서에도 호환 요구가 없다.
# 구버전을 수용하기로 바뀌면 이 목록에 추가하고, 변환기와 변환 결과 동등성
# 테스트를 함께 만든다(그 문서의 폐기 조건 3번).
SUPPORTED_SCHEMA_VERSIONS: Final[tuple[str, ...]] = (TASK_PLAN_SCHEMA_VERSION,)

# 5개 원자 스킬. 계약의 일부이므로 상수로 둔다(md/계획.md 7장 1번).
# 순서는 의미가 없다. 로봇별 지원 여부는 Capability Profile이 정한다.
SKILL_HOME: Final[str] = "home"
SKILL_MOVE: Final[str] = "move"
SKILL_PICK: Final[str] = "pick"
SKILL_PLACE: Final[str] = "place"
SKILL_STOP: Final[str] = "stop"

ATOMIC_SKILLS: Final[tuple[str, ...]] = (
    SKILL_HOME,
    SKILL_MOVE,
    SKILL_PICK,
    SKILL_PLACE,
    SKILL_STOP,
)

# 스킬별 필수 인자 이름. 로봇과 무관한 계약이므로 여기 둔다.
# 인자의 "값"(좌표·물체명)은 World/Resource Catalog가 제공한다.
SKILL_REQUIRED_ARGS: Final[dict[str, tuple[str, ...]]] = {
    SKILL_HOME: (),
    SKILL_MOVE: ("to",),
    SKILL_PICK: ("object", "from"),
    SKILL_PLACE: ("object", "to"),
    SKILL_STOP: (),
}

# 계약이 허용하는 인자 이름 전체(오타·임의 확장 차단용).
SKILL_ALLOWED_ARGS: Final[dict[str, tuple[str, ...]]] = {
    SKILL_HOME: (),
    SKILL_MOVE: ("to",),
    SKILL_PICK: ("object", "from"),
    SKILL_PLACE: ("object", "to"),
    SKILL_STOP: (),
}
