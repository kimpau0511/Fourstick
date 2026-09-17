"""외부 경계에서 발생한 검증 실패를 공통 이유 코드로 바꾼다.

Pydantic은 외부 입출력과 설정 파일의 "형태"(타입·미지의 필드·범위)를 검증하고,
core 계약은 "의미"(지원 스킬·인자·버전·단위·조합)를 검증한다. 둘 다 실패를
문자열이 아니라 ReasonCode로 표현해야 UI·감사 로그가 같은 코드를 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from core.reason_codes import ReasonCode


@dataclass(frozen=True)
class FieldIssue:
    location: str
    message: str

    def __str__(self) -> str:
        return f"{self.location}: {self.message}"


class BoundaryValidationError(Exception):
    """경계 검증 실패. 여러 필드가 동시에 틀릴 수 있어 전부 담는다."""

    def __init__(self, reason: ReasonCode, issues: Sequence[FieldIssue]):
        self.reason = reason
        self.issues = tuple(issues)
        detail = "; ".join(str(i) for i in self.issues) or "(상세 없음)"
        super().__init__(f"[{reason}] {detail}")


def issues_from_pydantic(exc: Any) -> tuple[FieldIssue, ...]:
    """pydantic.ValidationError -> FieldIssue 목록. pydantic을 core에 import하지
    않기 위해 덕타이핑으로 받는다."""
    out: list[FieldIssue] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        out.append(FieldIssue(loc, err.get("msg", "invalid")))
    return tuple(out)
