"""SQLite 저장소 구현체.

SQLite 전용 SQL·연결 관리·트랜잭션 처리를 이 파일과 `schema.py` 안에만 둔다.
core·validation·API는 `storage/repository.Repository`만 본다.

스레드 (웹 서버 7단계):
- `sqlite3.threadsafety == 3`(serialized)이므로 한 연결을 여러 스레드에서 쓸 수
  있다. 웹 서버는 이벤트 루프 밖 워커 스레드에서 실행·전사를 돌리므로
  `check_same_thread=False`로 연결하고, **명시적 트랜잭션 구간을 RLock으로
  직렬화**한다. 단일 문장 읽기는 SQLite가 직렬화한다.
- 락을 트랜잭션 구간에만 두는 이유: 전체 메서드를 잠그면 긴 실행 중에 조회가
  막힌다. 어긋나면 안 되는 것은 `BEGIN IMMEDIATE` ~ `COMMIT` 사이다.

연결·트랜잭션 처리 (요구 3, 10):
- `isolation_level=None`으로 파이썬 드라이버의 암묵적 트랜잭션을 끄고, 필요한
  구간을 `BEGIN IMMEDIATE`로 직접 연다. 드라이버가 임의 시점에 커밋하면
  트랜잭션 경계가 문서와 달라진다.
- `PRAGMA foreign_keys = ON` — SQLite는 외래키가 기본 비활성이다.
- 쓰기 연산은 모두 `self._tx()` 안에서 일어난다. 경계는
  `md/저장소_설계.md`의 "트랜잭션 경계" 절에 기록했다.

DB마다 다른 것을 이 안에서 흡수한다:
- Boolean → INTEGER 0/1 (`storage/mappers.py`의 to_bool/from_bool)
- 시각 → REAL(UTC epoch 초)
- JSON → TEXT (DB의 JSON 연산자를 쓰지 않는다)
- 순번 → 트랜잭션 안에서 `MAX(seq)+1`을 애플리케이션이 계산한다
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

from core.execution_result import ExecutionResult
from core.execution_state import ALLOWED_TRANSITIONS, ExecutionState
from core.reason_codes import ReasonCode
from core.task_plan import TaskPlan
from storage.mappers import (
    dump_json,
    from_bool,
    load_json,
    plan_from_row,
    plan_to_json,
    result_from_row,
    result_to_columns,
    to_bool,
    to_reason,
    to_state,
)
from storage.records import (
    ApprovalDecision,
    ApprovalRecord,
    ExecutionRecord,
    ExecutionTrace,
    ObservationRecord,
    PermitReasonRecord,
    PermitRecord,
    PlanningAttemptRecord,
    PlanningAttemptStatus,
    PlanningExecutionPath,
    PlanningPayload,
    PlanRecord,
    RequestRecord,
    ResultRecord,
    RobotProfileRecord,
    SimVerificationRecord,
    RuleResultRecord,
    SessionRecord,
    SessionStatus,
    StateTransitionRecord,
    SttExecutionPath,
    SttInferenceRecord,
    ValidationDecision,
    ValidationRecord,
    ValidationRunRecord,
    ValidationStageResult,
)
from storage.repository import (
    CorruptedRecord,
    IntegrityViolation,
    Repository,
    StorageError,
)
from storage.sqlite.schema import (
    CREATE_MIGRATIONS_TABLE,
    LATEST_VERSION,
    MIGRATIONS,
)


class _Rows:
    """미리 다 읽어 둔 조회 결과. 커서를 잠금 밖으로 내보내지 않기 위한 것."""

    __slots__ = ("rows", "lastrowid", "rowcount")

    def __init__(self, rows, lastrowid, rowcount):
        self.rows = rows
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)


class _SerialConnection:
    """연결 하나를 여러 스레드가 공유하기 위한 감싸개.

    `sqlite3.threadsafety == 3`(직렬화)이라도 **문장 실행과 행 읽기가 두
    단계로 나뉘면** 그 사이에 다른 스레드의 트랜잭션이 끼어든다. 실측으로
    확인한 증상 두 가지:

    - `sqlite3.InterfaceError: bad parameter or other API misuse`
    - 커서가 다른 문장의 행을 돌려줘 `Row`에 없는 열을 찾는 IndexError

    그래서 실행과 행 읽기를 **같은 잠금 안에서 끝내고**, 결과는 이미 읽은 행
    목록(`_Rows`)으로 돌려준다. 잠금은 트랜잭션 구간과 같은 RLock이라, 쓰기
    트랜잭션이 열려 있는 동안 다른 스레드의 조회는 기다린다.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Rows:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            rows = cursor.fetchall() if cursor.description is not None else []
            return _Rows(rows, cursor.lastrowid, cursor.rowcount)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SqliteRepository(Repository):
    def __init__(self, path: str = ":memory:", *, now: float = 0.0):
        raw = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        raw.row_factory = sqlite3.Row
        #: 명시적 트랜잭션 구간과 모든 문장 실행을 직렬화한다.
        #: 재진입 가능(같은 스레드의 트랜잭션 안 조회).
        self._tx_lock = threading.RLock()
        self._conn = _SerialConnection(raw, self._tx_lock)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate(now)

    # ── 연결·트랜잭션 ───────────────────────────────────────────────────
    @contextmanager
    def _tx(self) -> Iterator["_SerialConnection"]:
        with self._tx_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @staticmethod
    def _split_statements(sql: str) -> list[str]:
        """DDL을 문장 단위로 나눈다.

        트리거 본문(`BEGIN ... END;`)은 안에 세미콜론이 있으므로 `;`로만 나누면
        문장이 끊긴다(실측: `OperationalError: incomplete input`). BEGIN과 END의
        개수를 세어 본문이 닫힐 때까지 이어 붙인다. 주석 줄은 실행 전에 뺀다.
        """
        lines = [
            line for line in sql.splitlines()
            if not line.strip().startswith("--")
        ]
        body = "\n".join(lines)
        statements: list[str] = []
        buffer = ""
        for part in body.split(";"):
            buffer = f"{buffer};{part}" if buffer else part
            upper = buffer.upper()
            if upper.count("BEGIN") > upper.count("END"):
                continue   # 트리거 본문이 아직 닫히지 않았다
            if buffer.strip():
                statements.append(buffer)
            buffer = ""
        if buffer.strip():
            statements.append(buffer)
        return statements

    @classmethod
    def _exec_script(cls, conn: "_SerialConnection", sql: str) -> None:
        """DDL을 문장 단위로 실행한다.

        `executescript()`는 열려 있는 트랜잭션을 암묵적으로 커밋해버려서 여기서
        쓰지 않는다 — 마이그레이션이 하나의 트랜잭션 안에서 끝나야 한다.
        """
        for statement in cls._split_statements(sql):
            conn.execute(statement)

    def _migrate(self, now: float) -> None:
        with self._tx() as conn:
            self._exec_script(conn, CREATE_MIGRATIONS_TABLE)
            applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
            for version, description, sql in MIGRATIONS:
                if version in applied:
                    continue
                self._exec_script(conn, sql)
                conn.execute(
                    "INSERT INTO schema_migrations VALUES (?,?,?)", (version, description, now)
                )

    def schema_version(self) -> int:
        row = self._conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return int(row["v"] or 0)

    def close(self) -> None:
        self._conn.close()

    # ── 공통 ────────────────────────────────────────────────────────────
    def _one(self, sql: str, params: tuple[Any, ...], missing: str) -> sqlite3.Row:
        row = self._conn.execute(sql, params).fetchone()
        if row is None:
            raise StorageError(ReasonCode.PLAN_UNKNOWN_RESOURCE, missing)
        return row

    @staticmethod
    def _next_seq(conn: sqlite3.Connection, table: str, execution_id: str) -> int:
        row = conn.execute(
            f"SELECT MAX(seq) AS s FROM {table} WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        return int(row["s"] or 0) + 1

    # ── 세션 ────────────────────────────────────────────────────────────
    _SESSION_COLUMNS = (
        "session_id", "created_at", "last_seen_at", "ended_at", "status",
        "schema_version", "origin",
    )

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> SessionRecord:
        try:
            return SessionRecord(
                session_id=row["session_id"], created_at=row["created_at"],
                last_seen_at=row["last_seen_at"], ended_at=row["ended_at"],
                status=SessionStatus(row["status"]),
                schema_version=row["schema_version"], origin=row["origin"] or "",
            )
        except ValueError as exc:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID, f"세션 기록이 계약에 어긋난다: {exc}"
            ) from exc

    def create_session(self, record: SessionRecord) -> None:
        existing = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (record.session_id,)
        ).fetchone()
        if existing is not None:
            if self._session_from_row(existing) == record:
                return   # 멱등
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID,
                f"이미 다른 내용으로 저장된 session_id: {record.session_id!r}",
            )
        with self._tx() as conn:
            conn.execute(
                f"INSERT INTO sessions ({','.join(self._SESSION_COLUMNS)})"
                f" VALUES ({','.join('?' * len(self._SESSION_COLUMNS))})",
                (
                    record.session_id, record.created_at, record.last_seen_at,
                    record.ended_at, record.status.value, record.schema_version,
                    record.origin,
                ),
            )

    def get_session(self, session_id: str) -> SessionRecord:
        return self._session_from_row(
            self._one(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,),
                f"저장된 세션이 없다: {session_id!r}",
            )
        )

    def touch_session(self, session_id: str, *, at: float) -> SessionRecord:
        current = self.get_session(session_id)
        if not current.usable:
            # 종료·만료된 세션의 활동 시각을 되살리지 않는다.
            return current
        with self._tx() as conn:
            conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE session_id = ?"
                " AND status = 'active'",
                (max(at, current.created_at), session_id),
            )
        return self.get_session(session_id)

    def close_session(
        self, session_id: str, *, at: float, status: SessionStatus
    ) -> SessionRecord:
        if status is SessionStatus.ACTIVE:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID, "close_session에 active를 줄 수 없다"
            )
        current = self.get_session(session_id)
        if not current.usable:
            return current
        with self._tx() as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, ended_at = ? WHERE session_id = ?",
                (status.value, max(at, current.created_at), session_id),
            )
        return self.get_session(session_id)

    def expire_idle_sessions(
        self, *, now: float, idle_timeout_sec: float
    ) -> Sequence[str]:
        rows = self._conn.execute(
            "SELECT session_id FROM sessions WHERE status = 'active'"
            " AND (? - last_seen_at) > ?",
            (now, idle_timeout_sec),
        ).fetchall()
        expired = [r["session_id"] for r in rows]
        for session_id in expired:
            self.close_session(session_id, at=now, status=SessionStatus.EXPIRED)
        return tuple(expired)

    def requests_for_session(self, session_id: str) -> Sequence[RequestRecord]:
        rows = self._conn.execute(
            "SELECT * FROM requests WHERE session_id = ? ORDER BY created_at,"
            " request_id",
            (session_id,),
        ).fetchall()
        return tuple(self._request_from_row(r) for r in rows)

    # ── 요청 ────────────────────────────────────────────────────────────
    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> RequestRecord:
        return RequestRecord(
            request_id=row["request_id"], utterance=row["utterance"],
            schema_version=row["schema_version"], created_at=row["created_at"],
            selected_stt_inference_id=row["selected_stt_inference_id"],
            session_id=row["session_id"],
        )

    def save_request(self, record: RequestRecord) -> None:
        existing = self._conn.execute(
            "SELECT * FROM requests WHERE request_id = ?", (record.request_id,)
        ).fetchone()
        if existing is not None:
            same = (
                existing["utterance"] == record.utterance
                and existing["schema_version"] == record.schema_version
                and existing["created_at"] == record.created_at
                and existing["selected_stt_inference_id"] == record.selected_stt_inference_id
                and existing["session_id"] == record.session_id
            )
            if same:
                return   # 같은 내용의 재저장은 멱등이다
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID,
                f"이미 다른 내용으로 저장된 request_id: {record.request_id!r}",
            )
        if record.selected_stt_inference_id is not None:
            # 아직 없는 기록을 가리키는 요청을 만들지 않는다. 함께 넣어야 하면
            # save_request_with_stt_inference()를 쓴다.
            self.get_stt_inference(record.selected_stt_inference_id)
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO requests"
                " (request_id, utterance, schema_version, created_at,"
                "  selected_stt_inference_id, session_id) VALUES (?,?,?,?,?,?)",
                (
                    record.request_id, record.utterance, record.schema_version,
                    record.created_at, record.selected_stt_inference_id,
                    record.session_id,
                ),
            )

    def get_request(self, request_id: str) -> RequestRecord:
        return self._request_from_row(
            self._one(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,),
                f"저장된 요청이 없다: {request_id!r}",
            )
        )

    # ── STT 실행 기록 (append-only) ─────────────────────────────────────
    _STT_INF_COLUMNS = (
        "stt_inference_id", "session_id", "attempt_no", "schema_version", "created_at",
        "model_name", "model_version", "profile_id", "profile_version", "verification",
        "device", "compute_type", "language",
        "audio_duration_ms", "processing_duration_ms", "model_load_duration_ms", "rtf",
        "transcript", "confidence", "confidence_metric",
        "vad_speech_detected", "vad_max_probability", "final_adopted",
        "reason_code", "execution_path", "request_id",
    )

    @staticmethod
    def _stt_inf_values(r: SttInferenceRecord) -> tuple[Any, ...]:
        return (
            r.stt_inference_id, r.session_id, r.attempt_no, r.schema_version, r.created_at,
            r.model_name, r.model_version, r.profile_id, r.profile_version, r.verification,
            r.device, r.compute_type, r.language,
            r.audio_duration_ms, r.processing_duration_ms, r.model_load_duration_ms,
            # rtf는 원자료에서 파생한 조회용 사본이다. 따로 받아서 어긋나게 두지 않는다.
            r.rtf,
            r.transcript, r.confidence, r.confidence_metric,
            None if r.vad_speech_detected is None else from_bool(r.vad_speech_detected),
            r.vad_max_probability, from_bool(r.final_adopted),
            None if r.reason_code is None else r.reason_code.value,
            r.execution_path.value, r.request_id,
        )

    @staticmethod
    def _stt_inf_from_row(row: sqlite3.Row) -> SttInferenceRecord:
        try:
            reason = None if row["reason_code"] is None else ReasonCode(row["reason_code"])
        except ValueError as exc:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID,
                f"알 수 없는 reason_code: {row['reason_code']!r}",
            ) from exc
        vad = row["vad_speech_detected"]
        try:
            record = SttInferenceRecord(
                stt_inference_id=row["stt_inference_id"], session_id=row["session_id"],
                attempt_no=row["attempt_no"], schema_version=row["schema_version"],
                created_at=row["created_at"],
                model_name=row["model_name"], model_version=row["model_version"],
                profile_id=row["profile_id"], profile_version=row["profile_version"],
                verification=row["verification"], device=row["device"],
                compute_type=row["compute_type"], language=row["language"],
                audio_duration_ms=row["audio_duration_ms"],
                processing_duration_ms=row["processing_duration_ms"],
                model_load_duration_ms=row["model_load_duration_ms"],
                transcript=row["transcript"], confidence=row["confidence"],
                confidence_metric=row["confidence_metric"],
                vad_speech_detected=None if vad is None else to_bool(vad),
                vad_max_probability=row["vad_max_probability"],
                final_adopted=to_bool(row["final_adopted"]),
                reason_code=reason,
                execution_path=SttExecutionPath(row["execution_path"]),
                request_id=row["request_id"],
            )
        except ValueError as exc:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID, f"STT 실행 기록이 계약에 어긋난다: {exc}"
            ) from exc
        # 파생 사본이 원자료와 어긋나면 변조·버그다. 조용히 고치지 않는다.
        stored_rtf = row["rtf"]
        if stored_rtf is None:
            if record.rtf is not None:
                raise CorruptedRecord(
                    ReasonCode.CONFIG_INVALID, "rtf 사본이 비었는데 원자료로는 계산된다"
                )
        elif record.rtf is None or abs(stored_rtf - record.rtf) > 1e-9:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID,
                f"rtf 사본 {stored_rtf}이 원자료 계산값 {record.rtf}과 다르다",
            )
        return record

    def append_stt_inference(self, record: SttInferenceRecord) -> None:
        existing = self._conn.execute(
            "SELECT * FROM stt_inferences WHERE stt_inference_id = ?",
            (record.stt_inference_id,),
        ).fetchone()
        if existing is not None:
            if self._stt_inf_from_row(existing) == record:
                return   # 같은 내용의 재저장은 멱등이다
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID,
                f"이미 다른 내용으로 저장된 stt_inference_id: {record.stt_inference_id!r}"
                " — 실행 기록은 덮어쓰지 않는다",
            )
        try:
            with self._tx() as conn:
                conn.execute(
                    f"INSERT INTO stt_inferences ({','.join(self._STT_INF_COLUMNS)})"
                    f" VALUES ({','.join('?' * len(self._STT_INF_COLUMNS))})",
                    self._stt_inf_values(record),
                )
        except sqlite3.IntegrityError as exc:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID, f"STT 실행 기록을 넣을 수 없다: {exc}"
            ) from exc

    def save_request_with_stt_inference(
        self, record: RequestRecord, inference: SttInferenceRecord
    ) -> None:
        """확정 요청과 채택된 실행 기록을 한 트랜잭션에서 넣는다.

        두 방향의 참조를 따로 넣으면 순서가 순환한다. 여기서 함께 넣고,
        요청이 가리키는 기록이 정말 그 요청의 채택 기록인지 검사한다.
        """
        if record.selected_stt_inference_id != inference.stt_inference_id:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID,
                f"요청이 가리키는 기록 {record.selected_stt_inference_id!r}이 "
                f"넘어온 기록 {inference.stt_inference_id!r}과 다르다",
            )
        if inference.request_id != record.request_id:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID,
                f"실행 기록의 request_id {inference.request_id!r}가 "
                f"요청 {record.request_id!r}과 다르다",
            )
        if not inference.final_adopted:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID, "채택되지 않은 기록을 요청에 연결할 수 없다"
            )
        existing = self._conn.execute(
            "SELECT * FROM requests WHERE request_id = ?", (record.request_id,)
        ).fetchone()
        if existing is not None:
            same = (
                existing["utterance"] == record.utterance
                and existing["schema_version"] == record.schema_version
                and existing["created_at"] == record.created_at
                and existing["selected_stt_inference_id"] == record.selected_stt_inference_id
                and existing["session_id"] == record.session_id
                and self._stt_inference_or_none(inference.stt_inference_id) == inference
            )
            if same:
                return   # 멱등
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID,
                f"이미 다른 내용으로 저장된 request_id: {record.request_id!r}",
            )
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO requests"
                    " (request_id, utterance, schema_version, created_at,"
                    "  selected_stt_inference_id, session_id) VALUES (?,?,?,?,?,?)",
                    (
                        record.request_id, record.utterance, record.schema_version,
                        record.created_at, record.selected_stt_inference_id,
                        record.session_id,
                    ),
                )
                conn.execute(
                    f"INSERT INTO stt_inferences ({','.join(self._STT_INF_COLUMNS)})"
                    f" VALUES ({','.join('?' * len(self._STT_INF_COLUMNS))})",
                    self._stt_inf_values(inference),
                )
        except sqlite3.IntegrityError as exc:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID, f"요청과 실행 기록을 넣을 수 없다: {exc}"
            ) from exc

    def _stt_inference_or_none(self, stt_inference_id: str) -> SttInferenceRecord | None:
        row = self._conn.execute(
            "SELECT * FROM stt_inferences WHERE stt_inference_id = ?", (stt_inference_id,)
        ).fetchone()
        return None if row is None else self._stt_inf_from_row(row)

    def get_stt_inference(self, stt_inference_id: str) -> SttInferenceRecord:
        return self._stt_inf_from_row(
            self._one(
                "SELECT * FROM stt_inferences WHERE stt_inference_id = ?",
                (stt_inference_id,),
                f"저장된 STT 실행 기록이 없다: {stt_inference_id!r}",
            )
        )

    def stt_inferences_for_session(self, session_id: str) -> Sequence[SttInferenceRecord]:
        rows = self._conn.execute(
            "SELECT * FROM stt_inferences WHERE session_id = ? ORDER BY attempt_no",
            (session_id,),
        ).fetchall()
        return tuple(self._stt_inf_from_row(r) for r in rows)

    def stt_inferences_for_request(self, request_id: str) -> Sequence[SttInferenceRecord]:
        rows = self._conn.execute(
            "SELECT * FROM stt_inferences WHERE request_id = ?"
            " ORDER BY created_at, attempt_no",
            (request_id,),
        ).fetchall()
        return tuple(self._stt_inf_from_row(r) for r in rows)

    def stt_inferences_by_path(
        self, execution_path: SttExecutionPath
    ) -> Sequence[SttInferenceRecord]:
        rows = self._conn.execute(
            "SELECT * FROM stt_inferences WHERE execution_path = ?"
            " ORDER BY created_at, attempt_no",
            (execution_path.value,),
        ).fetchall()
        return tuple(self._stt_inf_from_row(r) for r in rows)

    def legacy_stt_metadata(self, request_id: str) -> Mapping[str, Any] | None:
        """0002 시절 컬럼의 원본. canonical 조회와 분리된 유일한 경로다."""
        row = self._conn.execute(
            "SELECT * FROM stt_metadata_legacy_0002 WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        # 원본 컬럼 그대로 돌려준다. SttInferenceRecord로 만들지 않는다 —
        # 없는 측정값을 채워야 하기 때문이다.
        return {"source": "migration_0002_legacy", **{k: row[k] for k in row.keys()}}

    def next_stt_attempt_no(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(attempt_no) AS n FROM stt_inferences WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["n"] or 0) + 1

    def selected_stt_inference(self, request_id: str) -> SttInferenceRecord | None:
        row = self._one(
            "SELECT selected_stt_inference_id FROM requests WHERE request_id = ?",
            (request_id,),
            f"저장된 요청이 없다: {request_id!r}",
        )
        selected = row["selected_stt_inference_id"]
        if selected is None:
            return None
        return self.get_stt_inference(selected)

    # ── 계획 생성 시도 (append-only) ────────────────────────────────────
    _ATTEMPT_COLUMNS = (
        "planning_attempt_id", "request_id", "attempt_no", "schema_version",
        "provider_id", "model_id", "prompt_template_version", "output_schema_version",
        "resource_catalog_version", "skill_catalog_version",
        "started_at", "ended_at", "duration_ms",
        "status", "reason_code", "detail",
        "plan_id", "plan_hash",
        "is_mock", "is_retry", "previous_attempt_id", "stt_inference_id",
        "payload_version", "payload_json", "payload_truncated",
        "payload_retention_days",
        "served_model_id", "server_version", "quantization", "structured_output",
        "thinking_mode", "prompt_tokens", "completion_tokens",
        "validation_stages_json",
        "execution_path", "evaluation_run_id", "evaluation_label",
        "evaluation_case_id",
    )

    @staticmethod
    def _attempt_values(r: PlanningAttemptRecord) -> tuple[Any, ...]:
        p = r.payload
        return (
            r.planning_attempt_id, r.request_id, r.attempt_no, r.schema_version,
            r.provider_id, r.model_id, r.prompt_template_version,
            r.output_schema_version,
            r.resource_catalog_version, r.skill_catalog_version,
            r.started_at, r.ended_at, r.duration_ms,
            r.status.value, None if r.reason_code is None else r.reason_code.value,
            r.detail, r.plan_id, r.plan_hash,
            from_bool(r.is_mock), from_bool(r.is_retry),
            r.previous_attempt_id, r.stt_inference_id,
            None if p is None else p.payload_version,
            None if p is None else p.body_json,
            None if p is None else from_bool(p.truncated),
            None if p is None else p.retention_days,
            r.served_model_id, r.server_version, r.quantization,
            None if r.structured_output is None else from_bool(r.structured_output),
            r.thinking_mode, r.prompt_tokens, r.completion_tokens,
            None if not r.validation_stages else dump_json(
                [stage.to_dict() for stage in r.validation_stages]
            ),
            None if r.execution_path is None else r.execution_path.value,
            r.evaluation_run_id, r.evaluation_label, r.evaluation_case_id,
        )

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> PlanningAttemptRecord:
        try:
            reason = None if row["reason_code"] is None else ReasonCode(row["reason_code"])
            status = PlanningAttemptStatus(row["status"])
        except ValueError as exc:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID, f"계획 시도 기록의 열거형 값이 낯설다: {exc}"
            ) from exc
        try:
            stages = tuple(
                ValidationStageResult.from_dict(item)
                for item in (
                    load_json(row["validation_stages_json"] or "[]", "검증 단계") or []
                )
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID, f"검증 단계 기록을 읽을 수 없다: {exc}"
            ) from exc
        payload = None
        if row["payload_version"] is not None:
            payload = PlanningPayload(
                payload_version=row["payload_version"],
                body_json=row["payload_json"] or "",
                truncated=to_bool(row["payload_truncated"]),
                retention_days=row["payload_retention_days"],
            )
        try:
            return PlanningAttemptRecord(
                planning_attempt_id=row["planning_attempt_id"],
                request_id=row["request_id"], attempt_no=row["attempt_no"],
                schema_version=row["schema_version"],
                provider_id=row["provider_id"], model_id=row["model_id"],
                prompt_template_version=row["prompt_template_version"],
                output_schema_version=row["output_schema_version"],
                resource_catalog_version=row["resource_catalog_version"],
                skill_catalog_version=row["skill_catalog_version"],
                started_at=row["started_at"], ended_at=row["ended_at"],
                duration_ms=row["duration_ms"], status=status, reason_code=reason,
                detail=row["detail"] or "",
                plan_id=row["plan_id"], plan_hash=row["plan_hash"],
                is_mock=to_bool(row["is_mock"]), is_retry=to_bool(row["is_retry"]),
                previous_attempt_id=row["previous_attempt_id"],
                stt_inference_id=row["stt_inference_id"], payload=payload,
                served_model_id=row["served_model_id"],
                server_version=row["server_version"],
                quantization=row["quantization"],
                structured_output=(
                    None if row["structured_output"] is None
                    else to_bool(row["structured_output"])
                ),
                thinking_mode=row["thinking_mode"],
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                validation_stages=stages,
                execution_path=(
                    None if row["execution_path"] is None
                    else PlanningExecutionPath(row["execution_path"])
                ),
                evaluation_run_id=row["evaluation_run_id"],
                evaluation_label=row["evaluation_label"],
                evaluation_case_id=row["evaluation_case_id"],
            )
        except ValueError as exc:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID, f"계획 시도 기록이 계약에 어긋난다: {exc}"
            ) from exc

    def append_planning_attempt(self, record: PlanningAttemptRecord) -> None:
        existing = self._conn.execute(
            "SELECT * FROM planning_attempts WHERE planning_attempt_id = ?",
            (record.planning_attempt_id,),
        ).fetchone()
        if existing is not None:
            if self._attempt_from_row(existing) == record:
                return   # 멱등
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID,
                f"이미 다른 내용으로 저장된 planning_attempt_id:"
                f" {record.planning_attempt_id!r} — 시도 기록은 덮어쓰지 않는다",
            )
        try:
            with self._tx() as conn:
                conn.execute(
                    f"INSERT INTO planning_attempts"
                    f" ({','.join(self._ATTEMPT_COLUMNS)})"
                    f" VALUES ({','.join('?' * len(self._ATTEMPT_COLUMNS))})",
                    self._attempt_values(record),
                )
        except sqlite3.IntegrityError as exc:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID, f"계획 시도 기록을 넣을 수 없다: {exc}"
            ) from exc

    def get_planning_attempt(self, planning_attempt_id: str) -> PlanningAttemptRecord:
        return self._attempt_from_row(
            self._one(
                "SELECT * FROM planning_attempts WHERE planning_attempt_id = ?",
                (planning_attempt_id,),
                f"저장된 계획 시도 기록이 없다: {planning_attempt_id!r}",
            )
        )

    def planning_attempts_for_request(
        self, request_id: str
    ) -> Sequence[PlanningAttemptRecord]:
        rows = self._conn.execute(
            "SELECT * FROM planning_attempts WHERE request_id = ? ORDER BY attempt_no",
            (request_id,),
        ).fetchall()
        return tuple(self._attempt_from_row(r) for r in rows)

    def planning_attempt_for_plan(self, plan_id: str) -> PlanningAttemptRecord | None:
        row = self._conn.execute(
            "SELECT * FROM planning_attempts WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        return None if row is None else self._attempt_from_row(row)

    def next_planning_attempt_no(self, request_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(attempt_no) AS n FROM planning_attempts WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return int(row["n"] or 0) + 1

    # ── 작업자 승인 (append-only) ───────────────────────────────────────
    _APPROVAL_COLUMNS = (
        "approval_id", "plan_id", "plan_hash", "request_id", "decision",
        "decided_at", "schema_version", "planning_attempt_id",
        "consistency_status", "consistency_reason_code", "note", "session_id",
        "robot_id", "profile_id", "profile_version",
        "policy_id", "policy_version", "snapshot_id", "snapshot_version",
        "snapshot_hash", "validation_run_id",
    )

    @staticmethod
    def _approval_values(r: ApprovalRecord) -> tuple[Any, ...]:
        return (
            r.approval_id, r.plan_id, r.plan_hash, r.request_id, r.decision.value,
            r.decided_at, r.schema_version, r.planning_attempt_id,
            r.consistency_status,
            None if r.consistency_reason_code is None else r.consistency_reason_code.value,
            r.note, r.session_id,
            r.robot_id, r.profile_id, r.profile_version,
            r.policy_id, r.policy_version, r.snapshot_id, r.snapshot_version,
            r.snapshot_hash, r.validation_run_id,
        )

    @staticmethod
    def _approval_from_row(row: sqlite3.Row) -> ApprovalRecord:
        try:
            reason = (
                None if row["consistency_reason_code"] is None
                else ReasonCode(row["consistency_reason_code"])
            )
            decision = ApprovalDecision(row["decision"])
        except ValueError as exc:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID, f"승인 기록의 열거형 값이 낯설다: {exc}"
            ) from exc
        try:
            return ApprovalRecord(
                approval_id=row["approval_id"], plan_id=row["plan_id"],
                plan_hash=row["plan_hash"], request_id=row["request_id"],
                decision=decision, decided_at=row["decided_at"],
                schema_version=row["schema_version"],
                planning_attempt_id=row["planning_attempt_id"],
                consistency_status=row["consistency_status"],
                consistency_reason_code=reason, note=row["note"] or "",
                session_id=row["session_id"],
                robot_id=row["robot_id"], profile_id=row["profile_id"],
                profile_version=row["profile_version"],
                policy_id=row["policy_id"], policy_version=row["policy_version"],
                snapshot_id=row["snapshot_id"],
                snapshot_version=row["snapshot_version"],
                snapshot_hash=row["snapshot_hash"],
                validation_run_id=row["validation_run_id"],
            )
        except ValueError as exc:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID, f"승인 기록이 계약에 어긋난다: {exc}"
            ) from exc

    def append_approval(self, record: ApprovalRecord) -> None:
        existing = self._conn.execute(
            "SELECT * FROM plan_approvals WHERE approval_id = ?",
            (record.approval_id,),
        ).fetchone()
        if existing is not None:
            if self._approval_from_row(existing) == record:
                return   # 멱등
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID,
                f"이미 다른 내용으로 저장된 approval_id: {record.approval_id!r}"
                " — 승인 기록은 덮어쓰지 않는다",
            )
        try:
            with self._tx() as conn:
                conn.execute(
                    f"INSERT INTO plan_approvals ({','.join(self._APPROVAL_COLUMNS)})"
                    f" VALUES ({','.join('?' * len(self._APPROVAL_COLUMNS))})",
                    self._approval_values(record),
                )
        except sqlite3.IntegrityError as exc:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID, f"승인 기록을 넣을 수 없다: {exc}"
            ) from exc

    def get_approval(self, approval_id: str) -> ApprovalRecord:
        return self._approval_from_row(
            self._one(
                "SELECT * FROM plan_approvals WHERE approval_id = ?", (approval_id,),
                f"저장된 승인 기록이 없다: {approval_id!r}",
            )
        )

    def approvals_for_plan(self, plan_id: str) -> Sequence[ApprovalRecord]:
        rows = self._conn.execute(
            "SELECT * FROM plan_approvals WHERE plan_id = ?"
            " ORDER BY decided_at, approval_id", (plan_id,),
        ).fetchall()
        return tuple(self._approval_from_row(r) for r in rows)

    def latest_approval(self, plan_id: str) -> ApprovalRecord | None:
        rows = self.approvals_for_plan(plan_id)
        return rows[-1] if rows else None

    # ── 계획 ────────────────────────────────────────────────────────────
    def save_plan(self, request_id: str, plan: TaskPlan, stored_at: float) -> PlanRecord:
        plan_hash = plan.plan_hash()
        dup = self._conn.execute(
            "SELECT plan_id FROM plans WHERE request_id = ? AND plan_hash = ?",
            (request_id, plan_hash),
        ).fetchone()
        if dup is not None:
            if dup["plan_id"] == plan.plan_id:
                return self.get_plan(plan.plan_id)   # 멱등
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID,
                f"같은 요청에 같은 내용의 계획이 이미 있다: {dup['plan_id']!r}",
            )
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        plan.plan_id, request_id, plan_hash, plan_to_json(plan),
                        plan.schema_version, plan.robot_id, plan.profile_id,
                        plan.profile_version, plan.terminal_hold, plan.created_at, stored_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID, f"계획 저장 제약 위반: {exc}"
            ) from exc
        return PlanRecord(plan.plan_id, request_id, plan_hash, plan, stored_at)

    def _plan_record(self, row: sqlite3.Row) -> PlanRecord:
        plan = plan_from_row(
            plan_json=row["plan_json"],
            stored_hash=row["plan_hash"],
            stored_schema_version=row["schema_version"],
            stored_profile_id=row["profile_id"],
            stored_profile_version=row["profile_version"],
        )
        return PlanRecord(row["plan_id"], row["request_id"], row["plan_hash"], plan, row["stored_at"])

    def get_plan(self, plan_id: str) -> PlanRecord:
        return self._plan_record(
            self._one(
                "SELECT * FROM plans WHERE plan_id = ?", (plan_id,),
                f"저장된 계획이 없다: {plan_id!r}",
            )
        )

    def plans_for_request(self, request_id: str) -> Sequence[PlanRecord]:
        rows = self._conn.execute(
            "SELECT * FROM plans WHERE request_id = ? ORDER BY stored_at, plan_id", (request_id,)
        ).fetchall()
        return tuple(self._plan_record(r) for r in rows)

    # ── 검증 ────────────────────────────────────────────────────────────
    def save_validation(self, record: ValidationRecord) -> None:
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO validations VALUES (?,?,?,?,?,?,?)",
                    (
                        record.validation_id, record.plan_id, record.plan_hash,
                        record.decision, record.policy_id, record.policy_version,
                        record.evaluated_at,
                    ),
                )
                for rr in record.rule_results:
                    conn.execute(
                        "INSERT INTO validation_rules VALUES (?,?,?,?,?)",
                        (
                            record.validation_id, rr.rule_code, rr.status,
                            rr.reason.value if rr.reason else None, rr.message,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID, f"검증 기록 제약 위반: {exc}"
            ) from exc

    def validations_for_plan(self, plan_id: str) -> Sequence[ValidationRecord]:
        rows = self._conn.execute(
            "SELECT * FROM validations WHERE plan_id = ? ORDER BY evaluated_at, validation_id",
            (plan_id,),
        ).fetchall()
        out = []
        for r in rows:
            rules = self._conn.execute(
                "SELECT * FROM validation_rules WHERE validation_id = ? ORDER BY rule_code",
                (r["validation_id"],),
            ).fetchall()
            out.append(
                ValidationRecord(
                    validation_id=r["validation_id"], plan_id=r["plan_id"],
                    plan_hash=r["plan_hash"], decision=r["decision"],
                    policy_id=r["policy_id"], policy_version=r["policy_version"],
                    evaluated_at=r["evaluated_at"],
                    rule_results=tuple(
                        RuleResultRecord(
                            rule_code=x["rule_code"], status=x["status"],
                            reason=to_reason(x["reason"]), message=x["message"],
                        )
                        for x in rules
                    ),
                )
            )
        return tuple(out)

    def save_permit(self, record: PermitRecord) -> None:
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO permits VALUES (?,?,?,?,?,?,?)",
                    (
                        record.permit_id, record.plan_id, record.plan_hash,
                        from_bool(record.granted), record.policy_id,
                        record.policy_version, record.decided_at,
                    ),
                )
                for i, pr in enumerate(record.reasons, start=1):
                    conn.execute(
                        "INSERT INTO permit_reasons VALUES (?,?,?,?)",
                        (record.permit_id, i, pr.reason.value, pr.detail),
                    )
        except sqlite3.IntegrityError as exc:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID, f"허가 기록 제약 위반: {exc}"
            ) from exc

    def permits_for_plan(self, plan_id: str) -> Sequence[PermitRecord]:
        rows = self._conn.execute(
            "SELECT * FROM permits WHERE plan_id = ? ORDER BY decided_at, permit_id", (plan_id,)
        ).fetchall()
        out = []
        for r in rows:
            reasons = self._conn.execute(
                "SELECT * FROM permit_reasons WHERE permit_id = ? ORDER BY seq",
                (r["permit_id"],),
            ).fetchall()
            out.append(
                PermitRecord(
                    permit_id=r["permit_id"], plan_id=r["plan_id"], plan_hash=r["plan_hash"],
                    granted=to_bool(r["granted"]), policy_id=r["policy_id"],
                    policy_version=r["policy_version"], decided_at=r["decided_at"],
                    reasons=tuple(
                        PermitReasonRecord(to_reason(x["reason"]), x["detail"]) for x in reasons
                    ),
                )
            )
        return tuple(out)

    # ── 실행 ────────────────────────────────────────────────────────────
    def begin_execution(
        self, *, execution_id: str, request_id: str, plan_id: str, adapter_id: str,
        policy_id: str, policy_version: str, started_at: float,
        session_id: str | None = None, approval_id: str | None = None,
        adapter_kind: str | None = None, is_simulated: bool | None = None,
        environment_confirmed_at: float | None = None,
    ) -> ExecutionRecord:
        plan_row = self._one(
            "SELECT * FROM plans WHERE plan_id = ?", (plan_id,),
            f"저장된 계획이 없다: {plan_id!r}",
        )
        if plan_row["request_id"] != request_id:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID,
                f"계획 {plan_id!r}은 요청 {plan_row['request_id']!r}의 것이다",
            )
        # 멱등성: 결과가 확정되지 않은 시도가 있으면 새 시도를 시작하지 않는다.
        open_rows = self._conn.execute(
            """SELECT e.execution_id FROM executions e
               WHERE e.request_id = ?
                 AND NOT EXISTS (
                     SELECT 1 FROM execution_results r WHERE r.execution_id = e.execution_id
                 )""",
            (request_id,),
        ).fetchall()
        if open_rows:
            raise IntegrityViolation(
                ReasonCode.EXEC_GOAL_REJECTED,
                f"결과가 확정되지 않은 실행 시도가 있다: {[r['execution_id'] for r in open_rows]}",
            )
        row = self._conn.execute(
            "SELECT MAX(attempt_no) AS n FROM executions WHERE request_id = ?", (request_id,)
        ).fetchone()
        attempt_no = int(row["n"] or 0) + 1
        record = ExecutionRecord(
            execution_id=execution_id, request_id=request_id, plan_id=plan_id,
            plan_hash=plan_row["plan_hash"], attempt_no=attempt_no, adapter_id=adapter_id,
            robot_id=plan_row["robot_id"], profile_id=plan_row["profile_id"],
            profile_version=plan_row["profile_version"], policy_id=policy_id,
            policy_version=policy_version, schema_version=plan_row["schema_version"],
            started_at=started_at, session_id=session_id, approval_id=approval_id,
            adapter_kind=adapter_kind, is_simulated=is_simulated,
            environment_confirmed_at=environment_confirmed_at,
        )
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO executions"
                    " (execution_id, request_id, plan_id, plan_hash, attempt_no,"
                    "  adapter_id, robot_id, profile_id, profile_version, policy_id,"
                    "  policy_version, schema_version, started_at, session_id,"
                    "  approval_id, adapter_kind, is_simulated,"
                    "  environment_confirmed_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.execution_id, record.request_id, record.plan_id,
                        record.plan_hash, record.attempt_no, record.adapter_id,
                        record.robot_id, record.profile_id, record.profile_version,
                        record.policy_id, record.policy_version, record.schema_version,
                        record.started_at, record.session_id, record.approval_id,
                        record.adapter_kind,
                        None if record.is_simulated is None
                        else from_bool(record.is_simulated),
                        record.environment_confirmed_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID, f"실행 기록 제약 위반: {exc}"
            ) from exc
        return record

    def _execution_record(self, row: sqlite3.Row) -> ExecutionRecord:
        return ExecutionRecord(
            execution_id=row["execution_id"], request_id=row["request_id"],
            plan_id=row["plan_id"], plan_hash=row["plan_hash"],
            attempt_no=int(row["attempt_no"]), adapter_id=row["adapter_id"],
            robot_id=row["robot_id"], profile_id=row["profile_id"],
            profile_version=row["profile_version"], policy_id=row["policy_id"],
            policy_version=row["policy_version"], schema_version=row["schema_version"],
            started_at=row["started_at"],
            session_id=row["session_id"], approval_id=row["approval_id"],
            adapter_kind=row["adapter_kind"],
            is_simulated=(
                None if row["is_simulated"] is None else to_bool(row["is_simulated"])
            ),
            environment_confirmed_at=row["environment_confirmed_at"],
        )

    def get_execution(self, execution_id: str) -> ExecutionRecord:
        return self._execution_record(
            self._one(
                "SELECT * FROM executions WHERE execution_id = ?", (execution_id,),
                f"저장된 실행이 없다: {execution_id!r}",
            )
        )

    def executions_for_session(self, session_id: str) -> Sequence[ExecutionRecord]:
        rows = self._conn.execute(
            "SELECT * FROM executions WHERE session_id = ?"
            " ORDER BY started_at, attempt_no",
            (session_id,),
        ).fetchall()
        return tuple(self._execution_record(r) for r in rows)

    # ── 로봇 Profile 기록 ───────────────────────────────────────────────
    _ROBOT_PROFILE_COLUMNS = (
        "composite_profile_id", "composite_profile_version", "display_name",
        "arm_profile_id", "arm_profile_version", "gripper_profile_id",
        "gripper_profile_version", "mounting_profile_id", "mounting_profile_version",
        "asset_manifest_version", "source_commit", "source_checksum",
        "environment", "verified", "supported_skills", "unverified_items",
        "recorded_at", "schema_version",
    )

    @staticmethod
    def _robot_profile_values(r: RobotProfileRecord) -> tuple[Any, ...]:
        return (
            r.composite_profile_id, r.composite_profile_version, r.display_name,
            r.arm_profile_id, r.arm_profile_version, r.gripper_profile_id,
            r.gripper_profile_version, r.mounting_profile_id,
            r.mounting_profile_version, r.asset_manifest_version,
            r.source_commit, r.source_checksum, r.environment,
            from_bool(r.verified), dump_json(list(r.supported_skills)),
            dump_json(list(r.unverified_items)), r.recorded_at, r.schema_version,
        )

    @staticmethod
    def _robot_profile_from_row(row: sqlite3.Row) -> RobotProfileRecord:
        try:
            return RobotProfileRecord(
                composite_profile_id=row["composite_profile_id"],
                composite_profile_version=row["composite_profile_version"],
                display_name=row["display_name"],
                arm_profile_id=row["arm_profile_id"],
                arm_profile_version=row["arm_profile_version"],
                gripper_profile_id=row["gripper_profile_id"],
                gripper_profile_version=row["gripper_profile_version"],
                mounting_profile_id=row["mounting_profile_id"],
                mounting_profile_version=row["mounting_profile_version"],
                asset_manifest_version=row["asset_manifest_version"],
                source_commit=row["source_commit"],
                source_checksum=row["source_checksum"],
                environment=row["environment"],
                verified=to_bool(row["verified"]),
                supported_skills=tuple(load_json(row["supported_skills"], "skills")),
                unverified_items=tuple(
                    load_json(row["unverified_items"], "unverified")
                ),
                recorded_at=row["recorded_at"],
                schema_version=row["schema_version"],
            )
        except ValueError as exc:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID, f"Profile 기록이 계약에 어긋난다: {exc}"
            ) from exc

    def record_robot_profile(self, record: RobotProfileRecord) -> None:
        existing = self._conn.execute(
            "SELECT * FROM robot_profiles WHERE composite_profile_id = ?"
            " AND composite_profile_version = ?",
            (record.composite_profile_id, record.composite_profile_version),
        ).fetchone()
        if existing is not None:
            if self._robot_profile_from_row(existing) == record:
                return   # 멱등
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID,
                f"이미 다른 내용으로 저장된 Profile 버전:"
                f" {record.composite_profile_id} {record.composite_profile_version}"
                " — 내용이 바뀌면 새 버전을 쓴다(기존 행을 고치지 않는다)",
            )
        try:
            with self._tx() as conn:
                conn.execute(
                    f"INSERT INTO robot_profiles"
                    f" ({','.join(self._ROBOT_PROFILE_COLUMNS)})"
                    f" VALUES ({','.join('?' * len(self._ROBOT_PROFILE_COLUMNS))})",
                    self._robot_profile_values(record),
                )
        except sqlite3.IntegrityError as exc:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID, f"Profile 기록을 넣을 수 없다: {exc}"
            ) from exc

    def get_robot_profile(
        self, composite_profile_id: str, composite_profile_version: str
    ) -> RobotProfileRecord:
        return self._robot_profile_from_row(
            self._one(
                "SELECT * FROM robot_profiles WHERE composite_profile_id = ?"
                " AND composite_profile_version = ?",
                (composite_profile_id, composite_profile_version),
                f"저장된 Profile이 없다: {composite_profile_id}"
                f" {composite_profile_version}",
            )
        )

    def robot_profiles(self) -> Sequence[RobotProfileRecord]:
        rows = self._conn.execute(
            "SELECT * FROM robot_profiles ORDER BY recorded_at,"
            " composite_profile_id, composite_profile_version", (),
        ).fetchall()
        return tuple(self._robot_profile_from_row(r) for r in rows)

    # ── 시뮬레이터 검증 기록 ────────────────────────────────────────────
    _SIM_COLUMNS = (
        "verification_id", "recorded_at", "step", "composite_profile_id",
        "composite_profile_version", "arm_profile_id", "arm_profile_version",
        "gripper_profile_id", "gripper_profile_version", "mounting_profile_id",
        "mounting_profile_version", "asset_manifest_version", "ros_distro",
        "gazebo_version", "controller_versions", "adapter_kind", "arm_only",
        "is_simulated", "target_json", "observed_json", "aperture_target",
        "aperture_observed", "contact_json", "grasp_json", "command_accepted",
        "motion_completed", "target_reached", "task_succeeded", "state",
        "reason_code", "validation_run_id", "approval_id", "execution_id",
        "attempt_no", "detail", "schema_version",
        # 0015: MoveIt2 계획·충돌 검사 관측
        "moveit_config_version", "kinematics_solver", "kinematics_solver_version",
        "planning_scene_snapshot_id", "planning_scene_snapshot_version",
        "planning_scene_snapshot_hash", "planning_scene_checked_at",
        "collision_decision", "collision_reason_code", "geometry_validator_id",
        "geometry_validator_version", "commanded_joints_json",
        "observed_joints_json", "target_pose_json", "observed_pose_json",
        "sim_fixture_version",
    )

    @staticmethod
    def _sim_values(r: SimVerificationRecord) -> tuple[Any, ...]:
        flag = lambda v: None if v is None else from_bool(v)   # noqa: E731
        return (
            r.verification_id, r.recorded_at, r.step, r.composite_profile_id,
            r.composite_profile_version, r.arm_profile_id, r.arm_profile_version,
            r.gripper_profile_id, r.gripper_profile_version, r.mounting_profile_id,
            r.mounting_profile_version, r.asset_manifest_version, r.ros_distro,
            r.gazebo_version, r.controller_versions, r.adapter_kind,
            from_bool(r.arm_only), from_bool(r.is_simulated),
            dump_json(dict(r.target)), dump_json(dict(r.observed)),
            r.aperture_target, r.aperture_observed,
            None if r.contact is None else dump_json(dict(r.contact)),
            None if r.grasp is None else dump_json(dict(r.grasp)),
            from_bool(r.command_accepted), flag(r.motion_completed),
            flag(r.target_reached), flag(r.task_succeeded), r.state,
            None if r.reason_code is None else r.reason_code.value,
            r.validation_run_id, r.approval_id, r.execution_id, r.attempt_no,
            r.detail, r.schema_version,
            r.moveit_config_version, r.kinematics_solver,
            r.kinematics_solver_version, r.planning_scene_snapshot_id,
            r.planning_scene_snapshot_version, r.planning_scene_snapshot_hash,
            r.planning_scene_checked_at, r.collision_decision,
            None if r.collision_reason_code is None
            else r.collision_reason_code.value,
            r.geometry_validator_id, r.geometry_validator_version,
            None if r.commanded_joints is None
            else dump_json(dict(r.commanded_joints)),
            None if r.observed_joints is None
            else dump_json(dict(r.observed_joints)),
            None if r.target_pose is None else dump_json(dict(r.target_pose)),
            None if r.observed_pose is None else dump_json(dict(r.observed_pose)),
            r.sim_fixture_version,
        )

    @staticmethod
    def _sim_from_row(row: sqlite3.Row) -> SimVerificationRecord:
        maybe = lambda v: None if v is None else to_bool(v)    # noqa: E731
        try:
            reason = (
                None if row["reason_code"] is None else ReasonCode(row["reason_code"])
            )
        except ValueError as exc:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID, f"검증 기록의 이유 코드가 낯설다: {exc}"
            ) from exc
        try:
            return SimVerificationRecord(
                verification_id=row["verification_id"],
                recorded_at=row["recorded_at"], step=row["step"],
                composite_profile_id=row["composite_profile_id"],
                composite_profile_version=row["composite_profile_version"],
                arm_profile_id=row["arm_profile_id"],
                arm_profile_version=row["arm_profile_version"],
                gripper_profile_id=row["gripper_profile_id"],
                gripper_profile_version=row["gripper_profile_version"],
                mounting_profile_id=row["mounting_profile_id"],
                mounting_profile_version=row["mounting_profile_version"],
                asset_manifest_version=row["asset_manifest_version"],
                ros_distro=row["ros_distro"], gazebo_version=row["gazebo_version"],
                controller_versions=row["controller_versions"],
                adapter_kind=row["adapter_kind"],
                arm_only=to_bool(row["arm_only"]),
                is_simulated=to_bool(row["is_simulated"]),
                target=load_json(row["target_json"], "target"),
                observed=load_json(row["observed_json"], "observed"),
                aperture_target=row["aperture_target"],
                aperture_observed=row["aperture_observed"],
                contact=(
                    None if row["contact_json"] is None
                    else load_json(row["contact_json"], "contact")
                ),
                grasp=(
                    None if row["grasp_json"] is None
                    else load_json(row["grasp_json"], "grasp")
                ),
                command_accepted=to_bool(row["command_accepted"]),
                motion_completed=maybe(row["motion_completed"]),
                target_reached=maybe(row["target_reached"]),
                task_succeeded=maybe(row["task_succeeded"]),
                state=row["state"], reason_code=reason,
                validation_run_id=row["validation_run_id"],
                approval_id=row["approval_id"], execution_id=row["execution_id"],
                attempt_no=row["attempt_no"], detail=row["detail"] or "",
                schema_version=row["schema_version"],
                moveit_config_version=row["moveit_config_version"],
                kinematics_solver=row["kinematics_solver"],
                kinematics_solver_version=row["kinematics_solver_version"],
                planning_scene_snapshot_id=row["planning_scene_snapshot_id"],
                planning_scene_snapshot_version=(
                    row["planning_scene_snapshot_version"]),
                planning_scene_snapshot_hash=row["planning_scene_snapshot_hash"],
                planning_scene_checked_at=row["planning_scene_checked_at"],
                collision_decision=row["collision_decision"],
                collision_reason_code=(
                    None if row["collision_reason_code"] is None
                    else ReasonCode(row["collision_reason_code"])
                ),
                geometry_validator_id=row["geometry_validator_id"],
                geometry_validator_version=row["geometry_validator_version"],
                commanded_joints=(
                    None if row["commanded_joints_json"] is None
                    else load_json(row["commanded_joints_json"], "commanded_joints")
                ),
                observed_joints=(
                    None if row["observed_joints_json"] is None
                    else load_json(row["observed_joints_json"], "observed_joints")
                ),
                target_pose=(
                    None if row["target_pose_json"] is None
                    else load_json(row["target_pose_json"], "target_pose")
                ),
                observed_pose=(
                    None if row["observed_pose_json"] is None
                    else load_json(row["observed_pose_json"], "observed_pose")
                ),
                sim_fixture_version=row["sim_fixture_version"],
            )
        except ValueError as exc:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID, f"검증 기록이 계약에 어긋난다: {exc}"
            ) from exc

    def append_sim_verification(self, record: SimVerificationRecord) -> None:
        existing = self._conn.execute(
            "SELECT * FROM sim_verification_runs WHERE verification_id = ?",
            (record.verification_id,),
        ).fetchone()
        if existing is not None:
            if self._sim_from_row(existing) == record:
                return
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID,
                f"이미 다른 내용으로 저장된 verification_id:"
                f" {record.verification_id!r}",
            )
        try:
            with self._tx() as conn:
                conn.execute(
                    f"INSERT INTO sim_verification_runs"
                    f" ({','.join(self._SIM_COLUMNS)})"
                    f" VALUES ({','.join('?' * len(self._SIM_COLUMNS))})",
                    self._sim_values(record),
                )
        except sqlite3.IntegrityError as exc:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID, f"검증 기록을 넣을 수 없다: {exc}"
            ) from exc

    def sim_verifications(
        self, *, limit: int = 50, composite_profile_id: str | None = None
    ) -> Sequence[SimVerificationRecord]:
        if composite_profile_id is None:
            rows = self._conn.execute(
                "SELECT * FROM sim_verification_runs"
                " ORDER BY recorded_at DESC, verification_id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM sim_verification_runs WHERE composite_profile_id = ?"
                " ORDER BY recorded_at DESC, verification_id DESC LIMIT ?",
                (composite_profile_id, int(limit)),
            ).fetchall()
        return tuple(self._sim_from_row(r) for r in rows)

    # ── 검증 실행 이력 ──────────────────────────────────────────────────
    _VALIDATION_RUN_COLUMNS = (
        "validation_run_id", "session_id", "request_id", "plan_id", "plan_hash",
        "execution_id", "validator_kind", "validator_id", "validator_version",
        "robot_id", "profile_id", "profile_version", "policy_id", "policy_version",
        "snapshot_id", "snapshot_version", "snapshot_hash", "input_complete",
        "decision", "reason_code", "detail", "started_at", "finished_at",
        "duration_ms", "schema_version",
    )

    @staticmethod
    def _validation_run_values(r: ValidationRunRecord) -> tuple[Any, ...]:
        return (
            r.validation_run_id, r.session_id, r.request_id, r.plan_id, r.plan_hash,
            r.execution_id, r.validator_kind, r.validator_id, r.validator_version,
            r.robot_id, r.profile_id, r.profile_version, r.policy_id, r.policy_version,
            r.snapshot_id, r.snapshot_version, r.snapshot_hash,
            from_bool(r.input_complete), r.decision.value,
            None if r.reason_code is None else r.reason_code.value,
            r.detail, r.started_at, r.finished_at, r.duration_ms, r.schema_version,
        )

    @staticmethod
    def _validation_run_from_row(row: sqlite3.Row) -> ValidationRunRecord:
        try:
            decision = ValidationDecision(row["decision"])
            reason = (
                None if row["reason_code"] is None else ReasonCode(row["reason_code"])
            )
        except ValueError as exc:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID, f"검증 기록의 열거형 값이 낯설다: {exc}"
            ) from exc
        try:
            return ValidationRunRecord(
                validation_run_id=row["validation_run_id"],
                session_id=row["session_id"], request_id=row["request_id"],
                plan_id=row["plan_id"], plan_hash=row["plan_hash"],
                execution_id=row["execution_id"],
                validator_kind=row["validator_kind"],
                validator_id=row["validator_id"],
                validator_version=row["validator_version"],
                robot_id=row["robot_id"], profile_id=row["profile_id"],
                profile_version=row["profile_version"],
                policy_id=row["policy_id"], policy_version=row["policy_version"],
                snapshot_id=row["snapshot_id"],
                snapshot_version=row["snapshot_version"],
                snapshot_hash=row["snapshot_hash"],
                input_complete=to_bool(row["input_complete"]),
                decision=decision, reason_code=reason, detail=row["detail"] or "",
                started_at=row["started_at"], finished_at=row["finished_at"],
                schema_version=row["schema_version"],
            )
        except ValueError as exc:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID, f"검증 기록이 계약에 어긋난다: {exc}"
            ) from exc

    def append_validation_run(self, record: ValidationRunRecord) -> None:
        existing = self._conn.execute(
            "SELECT * FROM validation_runs WHERE validation_run_id = ?",
            (record.validation_run_id,),
        ).fetchone()
        if existing is not None:
            if self._validation_run_from_row(existing) == record:
                return   # 멱등
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID,
                f"이미 다른 내용으로 저장된 validation_run_id:"
                f" {record.validation_run_id!r} — 검증 기록은 덮어쓰지 않는다",
            )
        try:
            with self._tx() as conn:
                conn.execute(
                    f"INSERT INTO validation_runs"
                    f" ({','.join(self._VALIDATION_RUN_COLUMNS)})"
                    f" VALUES ({','.join('?' * len(self._VALIDATION_RUN_COLUMNS))})",
                    self._validation_run_values(record),
                )
        except sqlite3.IntegrityError as exc:
            raise IntegrityViolation(
                ReasonCode.CONFIG_INVALID, f"검증 기록을 넣을 수 없다: {exc}"
            ) from exc

    def get_validation_run(self, validation_run_id: str) -> ValidationRunRecord:
        return self._validation_run_from_row(
            self._one(
                "SELECT * FROM validation_runs WHERE validation_run_id = ?",
                (validation_run_id,),
                f"저장된 검증 기록이 없다: {validation_run_id!r}",
            )
        )

    def validation_runs_for_plan(
        self, plan_id: str, *, validator_kind: str | None = None
    ) -> Sequence[ValidationRunRecord]:
        if validator_kind is None:
            rows = self._conn.execute(
                "SELECT * FROM validation_runs WHERE plan_id = ?"
                " ORDER BY started_at, validation_run_id",
                (plan_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM validation_runs WHERE plan_id = ? AND validator_kind = ?"
                " ORDER BY started_at, validation_run_id",
                (plan_id, validator_kind),
            ).fetchall()
        return tuple(self._validation_run_from_row(r) for r in rows)

    def validation_runs_for_execution(
        self, execution_id: str
    ) -> Sequence[ValidationRunRecord]:
        rows = self._conn.execute(
            "SELECT * FROM validation_runs WHERE execution_id = ?"
            " ORDER BY started_at, validation_run_id",
            (execution_id,),
        ).fetchall()
        return tuple(self._validation_run_from_row(r) for r in rows)

    def execution_environment_counts(self) -> Mapping[str, int]:
        rows = self._conn.execute(
            "SELECT is_simulated, COUNT(*) AS n FROM executions GROUP BY is_simulated",
            (),
        ).fetchall()
        counts = {"simulated": 0, "real": 0, "unknown": 0}
        for row in rows:
            flag = row["is_simulated"]
            key = "unknown" if flag is None else ("simulated" if to_bool(flag) else "real")
            counts[key] += int(row["n"])
        return counts

    def executions_for_request(self, request_id: str) -> Sequence[ExecutionRecord]:
        rows = self._conn.execute(
            "SELECT * FROM executions WHERE request_id = ? ORDER BY attempt_no", (request_id,)
        ).fetchall()
        records = tuple(self._execution_record(r) for r in rows)
        expected = list(range(1, len(records) + 1))
        if [r.attempt_no for r in records] != expected:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID,
                f"시도 번호가 1부터 이어지지 않는다: {[r.attempt_no for r in records]}",
            )
        return records

    # ── 이력 (append-only) ──────────────────────────────────────────────
    def append_state_transition(
        self, execution_id: str, *, to_state: ExecutionState, occurred_at: float,
        reason: ReasonCode | None = None,
    ) -> StateTransitionRecord:
        self.get_execution(execution_id)   # 존재 확인
        with self._tx() as conn:
            last = conn.execute(
                """SELECT to_state FROM state_transitions WHERE execution_id = ?
                   ORDER BY seq DESC LIMIT 1""",
                (execution_id,),
            ).fetchone()
            from_state = to_state_or_none(last["to_state"]) if last else None
            if from_state is not None and to_state not in ALLOWED_TRANSITIONS[from_state]:
                raise IntegrityViolation(
                    ReasonCode.CONFIG_INVALID,
                    f"허용되지 않은 상태 전이를 기록할 수 없다: {from_state} -> {to_state}",
                )
            seq = self._next_seq(conn, "state_transitions", execution_id)
            conn.execute(
                "INSERT INTO state_transitions VALUES (?,?,?,?,?,?)",
                (
                    execution_id, seq, from_state.value if from_state else None,
                    to_state.value, reason.value if reason else None, occurred_at,
                ),
            )
        return StateTransitionRecord(execution_id, seq, from_state, to_state, reason, occurred_at)

    def state_transitions(self, execution_id: str) -> Sequence[StateTransitionRecord]:
        rows = self._conn.execute(
            "SELECT * FROM state_transitions WHERE execution_id = ? ORDER BY seq", (execution_id,)
        ).fetchall()
        out = []
        for i, r in enumerate(rows, start=1):
            if int(r["seq"]) != i:
                raise CorruptedRecord(
                    ReasonCode.CONFIG_INVALID, f"상태 전이 순번이 끊겼다: {r['seq']} (기대 {i})"
                )
            out.append(
                StateTransitionRecord(
                    execution_id=r["execution_id"], seq=int(r["seq"]),
                    from_state=to_state_or_none(r["from_state"]),
                    to_state=to_state(r["to_state"]), reason=to_reason(r["reason"]),
                    occurred_at=r["occurred_at"],
                )
            )
        for prev, nxt in zip(out, out[1:]):
            if nxt.to_state not in ALLOWED_TRANSITIONS[prev.to_state]:
                raise CorruptedRecord(
                    ReasonCode.CONFIG_INVALID,
                    f"저장된 전이 순서가 계약과 다르다: {prev.to_state} -> {nxt.to_state}",
                )
        return tuple(out)

    def append_observation(
        self, execution_id: str, *, kind: str, observed_at: float, payload: Mapping[str, Any]
    ) -> ObservationRecord:
        self.get_execution(execution_id)
        with self._tx() as conn:
            seq = self._next_seq(conn, "observations", execution_id)
            conn.execute(
                "INSERT INTO observations VALUES (?,?,?,?,?)",
                (execution_id, seq, kind, observed_at, dump_json(dict(payload))),
            )
        return ObservationRecord(execution_id, seq, kind, observed_at, dict(payload))

    def observations(self, execution_id: str) -> Sequence[ObservationRecord]:
        rows = self._conn.execute(
            "SELECT * FROM observations WHERE execution_id = ? ORDER BY seq", (execution_id,)
        ).fetchall()
        return tuple(
            ObservationRecord(
                execution_id=r["execution_id"], seq=int(r["seq"]), kind=r["kind"],
                observed_at=r["observed_at"], payload=load_json(r["payload_json"], "observation"),
            )
            for r in rows
        )

    def append_result(
        self, execution_id: str, result: ExecutionResult, recorded_at: float
    ) -> ResultRecord:
        self.get_execution(execution_id)
        with self._tx() as conn:
            seq = self._next_seq(conn, "execution_results", execution_id)
            conn.execute(
                "INSERT INTO execution_results VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (execution_id, seq, *result_to_columns(result), recorded_at),
            )
        return ResultRecord(execution_id, seq, result, recorded_at)

    def results(self, execution_id: str) -> Sequence[ResultRecord]:
        rows = self._conn.execute(
            "SELECT * FROM execution_results WHERE execution_id = ? ORDER BY seq", (execution_id,)
        ).fetchall()
        return tuple(
            ResultRecord(
                execution_id=r["execution_id"], seq=int(r["seq"]),
                result=result_from_row(r), recorded_at=r["recorded_at"],
            )
            for r in rows
        )

    def trace(self, execution_id: str) -> ExecutionTrace:
        return ExecutionTrace(
            execution=self.get_execution(execution_id),
            transitions=self.state_transitions(execution_id),
            observations=self.observations(execution_id),
            results=self.results(execution_id),
        )


def to_state_or_none(value: str | None) -> ExecutionState | None:
    return None if value is None else to_state(value)
