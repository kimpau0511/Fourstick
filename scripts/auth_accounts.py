"""로그인 계정 등록·조회·사용 중지 (관리 화면이 생기기 전까지 쓰는 실행용 스크립트, 2026-10-06 결정 Q1).

서버와 같은 DB(`FORSTICK2_DB`, 기본 reports/web.sqlite3)를 연다. 제품 코드에서 import하지 않는다(CODE_RULES 1).

  python scripts/auth_accounts.py add operator@example.com
  python scripts/auth_accounts.py list
  python scripts/auth_accounts.py disable operator@example.com   # 그 계정의 로그인 세션도 지운다
  python scripts/auth_accounts.py enable operator@example.com
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.config import ServerConfig  # noqa: E402
from storage.sqlite.repository import SqliteRepository  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="로그인 계정 관리")
    parser.add_argument("action", choices=("add", "list", "disable", "enable"))
    parser.add_argument("email", nargs="?")
    args = parser.parse_args(argv)
    if args.action != "list" and not args.email:
        parser.error("이메일이 필요하다")

    db = ServerConfig.from_env().db_path
    db.parent.mkdir(parents=True, exist_ok=True)
    repo = SqliteRepository(str(db), now=time.time())
    try:
        if args.action == "add":
            account = repo.add_account(args.email, at=time.time())
            print(f"등록: {account.email} (사용 {'가능' if account.enabled else '중지'})")
        elif args.action in ("disable", "enable"):
            account = repo.set_account_enabled(args.email, args.action == "enable")
            print(f"{account.email}: 사용 {'가능' if account.enabled else '중지'}")
        else:
            for a in repo.list_accounts():
                last = time.strftime("%Y-%m-%d %H:%M", time.localtime(a.last_login_at)) if a.last_login_at else "-"
                print(f"{a.email}\t{'가능' if a.enabled else '중지'}\t마지막 로그인 {last}\t{a.name or ''}")
        print(f"DB: {db}")
    finally:
        repo.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
