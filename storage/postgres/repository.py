"""PostgreSQL 기반 Repository.

최신 런타임 계약은 전용 스키마(기본 ``forstick_runtime``)에 둔다. 기존
``public`` 운영 테이블은 그대로 유지하며 검색 경로의 두 번째에 놓는다. 이로써
기존 데이터와 이름이 같은 ``executions``·``validation_runs``를 덮지 않는다.

저장 메서드의 계약과 행 매핑은 SQLite 구현과 동일하다. SQL 차이는 이 모듈의
연결 래퍼와 마이그레이션 경계에서만 흡수한다.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from storage.sqlite.repository import SqliteRepository, _Rows
from storage.sqlite.schema import CREATE_MIGRATIONS_TABLE, LATEST_VERSION, MIGRATIONS

try:
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor
except ImportError:  # pragma: no cover - 환경 진단은 생성자에서 명확히 보고한다.
    psycopg2 = None
    sql = None
    RealDictCursor = None


_SCHEMA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _qmark(sql_text: str) -> str:
    """Repository의 qmark placeholder를 psycopg2 형식으로 바꾼다.

    저장소 SQL에는 문자열 리터럴 안의 물음표가 없고 placeholder로만 사용한다.
    이 제한을 이 경계에 명시해 DB 방언 변환이 다른 코드로 퍼지지 않게 한다.
    """

    return sql_text.replace("?", "%s")


class _PostgresConnection:
    """psycopg2 연결 하나를 Repository의 직렬화된 실행 계약에 맞춘다."""

    def __init__(self, connection, lock: threading.RLock):
        self._connection = connection
        self._lock = lock

    def execute(self, statement: str, params: tuple = ()) -> _Rows:
        with self._lock:
            cursor = self._connection.cursor(cursor_factory=RealDictCursor)
            try:
                cursor.execute(_qmark(statement), params)
                rows = cursor.fetchall() if cursor.description is not None else []
                return _Rows(rows, None, cursor.rowcount)
            except psycopg2.IntegrityError as exc:
                # 상위 SQLite 구현이 이미 IntegrityError를 도메인 오류로 변환한다.
                # 같은 경계를 유지해 API 응답이 저장 백엔드에 따라 달라지지 않게 한다.
                raise sqlite3.IntegrityError(str(exc)) from exc
            finally:
                cursor.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class PostgresRepository(SqliteRepository):
    """SQLite와 동일한 Repository 계약을 PostgreSQL에 저장한다."""

    backend = "postgres"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        schema: str = "forstick_runtime",
        now: float = 0.0,
        connect_timeout: int = 8,
    ):
        if psycopg2 is None:
            raise RuntimeError(
                "PostgreSQL 저장소를 쓰려면 psycopg2-binary가 필요합니다. "
                "pip install -r requirements-db.txt"
            )
        if not password:
            raise RuntimeError("PostgreSQL 비밀번호 환경변수가 설정되지 않았습니다")
        if not _SCHEMA_NAME.fullmatch(schema):
            raise ValueError(f"허용되지 않는 PostgreSQL schema 이름: {schema!r}")

        raw = psycopg2.connect(
            host=host,
            port=port,
            dbname=database,
            user=user,
            password=password,
            connect_timeout=connect_timeout,
            application_name="fourstick-runtime",
        )
        raw.autocommit = True
        self._tx_lock = threading.RLock()
        self._schema = schema

        with raw.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                sql.Identifier(schema)
            ))
            cursor.execute(sql.SQL("SET search_path TO {}, public").format(
                sql.Identifier(schema)
            ))

        self._conn = _PostgresConnection(raw, self._tx_lock)
        self._migrate_postgres(now)

    @contextmanager
    def _tx(self) -> Iterator[_PostgresConnection]:
        with self._tx_lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @staticmethod
    def _postgres_statements(migration_sql: str) -> list[str]:
        """공통 마이그레이션에서 SQLite 전용 트리거만 제외한다."""

        statements = SqliteRepository._split_statements(migration_sql)
        return [
            statement
            for statement in statements
            if not statement.lstrip().upper().startswith("CREATE TRIGGER")
        ]

    def _migrate_postgres(self, now: float) -> None:
        with self._tx() as connection:
            for statement in self._postgres_statements(CREATE_MIGRATIONS_TABLE):
                connection.execute(statement)
            applied = {
                int(row["version"])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                )
            }
            for version, description, migration_sql in MIGRATIONS:
                if version in applied:
                    continue
                for statement in self._postgres_statements(migration_sql):
                    connection.execute(statement)
                if version == 11:
                    connection.execute(
                        """
                        ALTER TABLE executions ADD CONSTRAINT ck_executions_environment
                        CHECK (
                          (COALESCE(adapter_kind, '') = 'fake' AND is_simulated = 1)
                          OR (COALESCE(adapter_kind, '') NOT IN ('', 'fake') AND (
                                is_simulated = 1
                                OR (is_simulated = 0 AND environment_confirmed_at IS NOT NULL)
                                OR is_simulated IS NULL))
                          OR (COALESCE(adapter_kind, '') = '' AND is_simulated IS NULL)
                        )
                        """
                    )
                if version == 15:
                    connection.execute(
                        """
                        ALTER TABLE sim_verification_runs
                        ADD CONSTRAINT ck_sim_collision_decision
                        CHECK (collision_decision IS NULL OR
                               collision_decision IN ('allow', 'block', 'ask'))
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE sim_verification_runs
                        ADD CONSTRAINT ck_sim_allow_has_scene
                        CHECK (collision_decision <> 'allow' OR (
                          planning_scene_snapshot_id IS NOT NULL
                          AND planning_scene_snapshot_hash IS NOT NULL
                          AND planning_scene_checked_at IS NOT NULL))
                        """
                    )
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?,?,?)",
                    (version, description, now),
                )

        if self.schema_version() != LATEST_VERSION:
            raise RuntimeError(
                f"PostgreSQL 스키마 버전 불일치: {self.schema_version()} != {LATEST_VERSION}"
            )

    def dashboard_snapshot(self, *, limit: int = 8):
        snapshot = dict(super().dashboard_snapshot(limit=limit))
        rows = self._conn.execute(
            """
            SELECT robot_id::text AS public_robot_id, robot_code, model_name,
                   status_code, is_enabled, last_seen_at
            FROM public.robots
            ORDER BY robot_code
            """
        ).fetchall()
        snapshot["catalog_robots"] = [
            {
                **dict(row),
                "last_seen_at": (
                    None if row["last_seen_at"] is None
                    else row["last_seen_at"].isoformat()
                ),
            }
            for row in rows
        ]
        snapshot["schema"] = self._schema
        return snapshot
