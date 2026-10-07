"""검증된 작업 의도 — 일반 경로(`/v1/plan`) 이송 요청의 **하나뿐인 해석 결과** (2026-10-06).

계획 생성과 요청↔계획 일치 검증이 **같은 의도**를 본다. 의도가 없던 때는 슬롯(별칭으로 찾은
id 목록)을 각자 해석했다: 모델은 역할(출발·목적지)을 짐작했고, 일치 검증은 역할 없이 id 집합만
대조했다. 그래서 출발·목적지를 뒤바꾼 계획이 통과하고, "원래 자리"로 만든 맞는 계획은 막혔다.

지키는 것:
- 자재·출발지·목적지는 각각 **카탈로그 id 하나**와 **근거**를 가진다. 근거는 셋 중 하나다.
  - `utterance`: 발화 원문의 일부(그 표현이 그 id를 가리킨다는 것을 서버가 확인했다)
  - `state`: 상태 기록(자재의 현재 위치, 컨베이어 빈 칸)
  - `registry`: 등록 정보(자재의 원래 팔레트)
  - `context`: 같은 세션의 앞선 대화(직전에 옮긴 자재, 앞 질문에서 확인한 값). 원문에 가리키는
    말('그거' 등)이 있거나 앞 질문의 답일 때만 쓴다.
- 근거 없는 id는 의도에 들어가지 않는다. **모델이 만든 계획을 근거로 쓰지 않는다.**
- 이 모듈은 값만 담는다. 해석(모델)·해소·검증은 `planning/intent_interpreter.py`,
  `server/plan_intent.py`가 한다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

#: 근거 종류.
UTTERANCE, STATE, REGISTRY, CONTEXT = "utterance", "state", "registry", "context"
EVIDENCE_KINDS = (UTTERANCE, STATE, REGISTRY, CONTEXT)
#: 지원하는 의도. 지금은 자재 하나를 한 곳에서 다른 곳으로 옮기는 이송뿐이다.
TRANSFER = "transfer"


@dataclass(frozen=True)
class Evidence:
    kind: str
    #: 원문 일부(utterance) 또는 상태·등록 정보를 사람이 읽는 말로.
    text: str

    def __post_init__(self) -> None:
        if self.kind not in EVIDENCE_KINDS:
            raise ValueError(f"근거 종류가 아니다: {self.kind!r}")
        if not self.text:
            raise ValueError("근거 내용이 비었다")


@dataclass(frozen=True)
class ResolvedRef:
    resource_id: str
    evidence: Evidence


@dataclass(frozen=True)
class TaskIntent:
    action: str
    material: ResolvedRef
    source: ResolvedRef
    destination: ResolvedRef
    #: 해석기 식별(모델 id)과 확신·짧은 근거. 판정에는 쓰지 않는다(기록용).
    interpreter: str = ""
    confidence: float | None = None
    model_reason: str = ""

    def __post_init__(self) -> None:
        if self.action != TRANSFER:
            raise ValueError(f"지원하지 않는 의도다: {self.action!r}")
        if self.source.resource_id == self.destination.resource_id:
            raise ValueError("출발지와 목적지가 같다")

    def resource_ids(self) -> tuple[str, ...]:
        return (self.material.resource_id, self.source.resource_id,
                self.destination.resource_id)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "TaskIntent":
        def ref(row: Mapping[str, Any]) -> ResolvedRef:
            ev = row["evidence"]
            return ResolvedRef(str(row["resource_id"]), Evidence(str(ev["kind"]), str(ev["text"])))

        return TaskIntent(
            action=str(data["action"]), material=ref(data["material"]),
            source=ref(data["source"]), destination=ref(data["destination"]),
            interpreter=str(data.get("interpreter") or ""),
            confidence=data.get("confidence"),
            model_reason=str(data.get("model_reason") or ""))
