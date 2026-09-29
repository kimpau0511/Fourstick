"""설정에 맞는 Repository를 생성하는 단일 진입점."""

from __future__ import annotations

import time

from storage.repository import Repository
from storage.sqlite.repository import SqliteRepository


def build_repository(config, *, now: float | None = None) -> Repository:
    timestamp = time.time() if now is None else now
    if config.db_backend == "sqlite":
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        return SqliteRepository(str(config.db_path), now=timestamp)
    if config.db_backend != "postgres":
        raise ValueError(f"지원하지 않는 저장 백엔드: {config.db_backend!r}")

    from storage.postgres.repository import PostgresRepository

    return PostgresRepository(
        host=config.db_host,
        port=config.db_port,
        database=config.db_name,
        user=config.db_user,
        password=config.db_password or "",
        schema=config.db_schema,
        now=timestamp,
    )
