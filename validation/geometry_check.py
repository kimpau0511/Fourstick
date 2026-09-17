"""기하 검사 실행 경계 (md/개발플랜.md 6-05).

`core/geometry.py`가 계약이고, 여기서는 **구현체를 부르는 규칙**을 정한다.

이 층이 책임지는 것:

1. 구현체가 없으면 ASK(`geometry.validator_unavailable`)다. 검사하지 않은 상태를
   안전으로 바꾸지 않는다.
2. 환경 snapshot이 없으면 ASK(`geometry.environment_unavailable`),
   만료면 ASK(`geometry.snapshot_expired`)다. 구현체를 부르지 않는다 —
   부족한 입력으로 판정을 만들지 않게 한다.
3. 계획의 좌표계와 snapshot의 좌표계가 다르면 ASK(`geometry.frame_unknown`)다.
   변환해 주지 않는다 — 변환 책임은 Adapter와 Profile에 있다(`core/frames.py`).
4. 제한시간 초과는 ASK(`geometry.validator_timeout`), 예외는
   ASK(`geometry.validator_error`)다.
5. 구현체가 계약을 어긴 판정(입력 불완전한데 ALLOW 등)을 돌려주면
   ASK(`geometry.verdict_invalid`)로 바꾼다. 구현체 말을 그대로 믿지 않는다.

제한시간 구현: `with ThreadPoolExecutor(...)`는 블록을 벗어날 때
`shutdown(wait=True)`를 불러 **제한시간이 사실상 동작하지 않는다**(4단계 STT에서
실측으로 확인). 그래서 모듈 수준의 지속 실행자를 쓴다. 넘긴 작업은 취소되지
않으므로, 멈춘 구현체는 스레드를 계속 점유한다 — 동시 검사 수를 제한해
서버 전체가 잠기지 않게 한다.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout

from core.geometry import (
    ASK_REASONS,
    EnvironmentSnapshot,
    GeometryDecision,
    GeometryError,
    GeometryReason,
    GeometryRequest,
    GeometryValidator,
    GeometryVerdict,
    ask,
)
from core.reason_codes import ReasonCode

#: 구현체가 없을 때 기록에 남기는 이름. "검사기 없음"도 감사 대상이다.
NO_VALIDATOR_ID = "none"
NO_VALIDATOR_VERSION = "0"

_MAX_CONCURRENT = 2
_executor_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=_MAX_CONCURRENT, thread_name_prefix="geometry"
            )
        return _executor


def shutdown_pool() -> None:
    """테스트·종료 경로에서 실행자를 정리한다. 기다리지 않는다."""
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
            _executor = None


def check_geometry(
    validator: GeometryValidator | None,
    request: GeometryRequest,
    *,
    now: float | None = None,
) -> GeometryVerdict:
    """입력을 확인한 뒤 구현체를 부른다. 부족하면 부르지 않고 ASK를 돌린다."""
    started = request.checked_at if now is None else now
    clock = time.monotonic

    def finish() -> float:
        # 판정 시각은 호출자가 준 시각 기준으로 되돌려, 기록이 한 시계를 쓴다.
        return started + max(0.0, clock() - t0)

    t0 = clock()
    vid = NO_VALIDATOR_ID if validator is None else validator.validator_id
    vver = NO_VALIDATOR_VERSION if validator is None else validator.validator_version

    if validator is None:
        return ask(
            ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE,
            "기하 검사 구현체가 없다 — 검사하지 않은 상태를 안전으로 보지 않는다",
            validator_id=vid, validator_version=vver,
            started_at=started, finished_at=finish(), request=request,
        )

    snapshot = request.snapshot
    if snapshot is None:
        return ask(
            ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE,
            "환경 snapshot이 없다 — 환경을 추정해 검사하지 않는다",
            validator_id=vid, validator_version=vver,
            started_at=started, finished_at=finish(), request=request,
        )
    if snapshot.is_expired(request.checked_at):
        return ask(
            ReasonCode.GEOMETRY_SNAPSHOT_EXPIRED,
            f"환경 snapshot {snapshot.snapshot_id}가 만료됐다"
            f" (관측 후 {snapshot.age_sec(request.checked_at):.1f}s,"
            f" 허용 {snapshot.ttl_sec:.1f}s)",
            validator_id=vid, validator_version=vver,
            started_at=started, finished_at=finish(), request=request,
        )
    if not request.frame_id:
        return ask(
            ReasonCode.GEOMETRY_FRAME_UNKNOWN,
            "계획의 좌표계를 알 수 없다",
            validator_id=vid, validator_version=vver,
            started_at=started, finished_at=finish(), request=request,
        )
    if snapshot.frame_id != request.frame_id:
        return ask(
            ReasonCode.GEOMETRY_FRAME_UNKNOWN,
            f"계획의 좌표계({request.frame_id})와 환경 snapshot의"
            f" 좌표계({snapshot.frame_id})가 다르다 — 여기서 변환하지 않는다",
            validator_id=vid, validator_version=vver,
            started_at=started, finished_at=finish(), request=request,
        )

    future: Future = _pool().submit(validator.check, request)
    try:
        verdict = future.result(timeout=request.timeout_sec)
    except FutureTimeout:
        # 넘긴 작업은 취소되지 않는다. 슬롯을 계속 쓰고 있을 수 있다.
        future.cancel()
        return ask(
            ReasonCode.GEOMETRY_VALIDATOR_TIMEOUT,
            f"기하 검사가 제한시간 {request.timeout_sec:.2f}s를 넘겼다",
            validator_id=vid, validator_version=vver,
            started_at=started, finished_at=finish(), request=request,
        )
    except GeometryError as exc:
        return ask(
            ReasonCode.GEOMETRY_VALIDATOR_ERROR,
            f"기하 검사가 계약 오류를 냈다: {exc}"[:300],
            validator_id=vid, validator_version=vver,
            started_at=started, finished_at=finish(), request=request,
        )
    except Exception as exc:  # noqa: BLE001 — 구현체 오류를 통과로 바꾸지 않는다
        return ask(
            ReasonCode.GEOMETRY_VALIDATOR_ERROR,
            f"기하 검사가 오류를 냈다: {type(exc).__name__}: {exc}"[:300],
            validator_id=vid, validator_version=vver,
            started_at=started, finished_at=finish(), request=request,
        )

    return _verified(verdict, request, vid, vver, started, finish())


def _verified(
    verdict: object, request: GeometryRequest, vid: str, vver: str,
    started: float, finished: float,
) -> GeometryVerdict:
    """구현체 판정을 그대로 믿지 않고 계약을 다시 확인한다."""
    if not isinstance(verdict, GeometryVerdict):
        return ask(
            ReasonCode.GEOMETRY_VERDICT_INVALID,
            f"구현체가 GeometryVerdict가 아닌 값을 돌려줬다: {type(verdict).__name__}",
            validator_id=vid, validator_version=vver,
            started_at=started, finished_at=finished, request=request,
        )
    snapshot = request.snapshot
    problems: list[str] = []
    if verdict.validator_id != vid or verdict.validator_version != vver:
        problems.append(
            f"판정의 구현체 표기({verdict.validator_id} {verdict.validator_version})가"
            f" 호출한 구현체({vid} {vver})와 다르다"
        )
    if verdict.decision is GeometryDecision.ALLOW and snapshot is not None:
        if verdict.snapshot_id != snapshot.snapshot_id:
            problems.append("ALLOW인데 다른 snapshot_id를 남겼다")
        if verdict.snapshot_hash != snapshot.content_hash:
            problems.append("ALLOW인데 snapshot 지문이 요청과 다르다")
        if verdict.frame_id != request.frame_id:
            problems.append("ALLOW인데 좌표계가 요청과 다르다")
    if problems:
        return ask(
            ReasonCode.GEOMETRY_VERDICT_INVALID, "; ".join(problems)[:300],
            validator_id=vid, validator_version=vver,
            started_at=started, finished_at=finished, request=request,
        )
    return verdict


def geometry_rule_result(verdict: GeometryVerdict) -> dict:
    """UI·저장용 규칙 결과 한 줄. 안전 검증 규칙 표에 같은 모양으로 들어간다."""
    status = {
        GeometryDecision.ALLOW: "pass",
        GeometryDecision.BLOCK: "block",
        GeometryDecision.ASK: "insufficient_data",
    }[verdict.decision]
    codes = verdict.reason_codes()
    if verdict.decision is GeometryDecision.ALLOW:
        message = (
            f"기하 검사 통과 — {verdict.validator_id} {verdict.validator_version},"
            f" snapshot {verdict.snapshot_id} ({verdict.frame_id})"
        )
    else:
        message = "; ".join(
            f"{r.reason_code.value}: {r.detail}" for r in verdict.reasons
        )
    return {
        "code": "E-GEOM-001",
        "status": status,
        "message": message,
        "reason_code": None if not codes else codes[0].value,
        # ASK는 환경·구현체가 갖춰지면 해소될 수 있다. BLOCK은 계획을 고쳐야 한다.
        "recoverable": verdict.decision is GeometryDecision.ASK,
    }


def snapshot_or_none(snapshot: EnvironmentSnapshot | None) -> dict | None:
    return None if snapshot is None else snapshot.to_dict()


def blocking_reasons(verdict: GeometryVerdict) -> tuple[GeometryReason, ...]:
    """실행을 막는 이유만. ASK도 막는다 — 정보 부족을 통과로 쓰지 않는다."""
    if verdict.decision is GeometryDecision.ALLOW:
        return ()
    return verdict.reasons


def is_ask(verdict: GeometryVerdict) -> bool:
    return verdict.decision is GeometryDecision.ASK


ALL_ASK_REASONS = ASK_REASONS
