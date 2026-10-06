"""종료 멈춤 대비 감시 스레드 (`server/asgi.py`의 lifespan shutdown에서 켠다)."""

from __future__ import annotations

import os

#: 앱 종료(lifespan shutdown) 뒤 프로세스가 이만큼 지나도 살아 있으면 강제로 끝낸다.
EXIT_WATCHDOG_SEC = float(os.environ.get("FORSTICK2_EXIT_WATCHDOG_SEC", "15"))


def arm_exit_watchdog() -> None:
    """종료 멈춤 대비. 정상 종료면 아무 일도 하지 않는다.

    SIGINT 뒤 포트는 닫혔는데 프로세스가 끝나지 않는 일이 반복됐다(2026-09-29·30,
    두 번 모두 kill -9 필요). 재현하지 못해 원인은 미확정이다. 그래서 멈추면
    **모든 스레드의 스택을 로그로 남기고** 끝낸다 — 다음 멈춤에서 원인을 확인하기
    위해서다. `os._exit`은 atexit을 건너뛰므로 DB는 이미 닫힌 뒤에만 켠다.
    """
    if EXIT_WATCHDOG_SEC <= 0:
        return
    import faulthandler
    import sys
    import threading
    import time
    from pathlib import Path

    def watch() -> None:
        time.sleep(EXIT_WATCHDOG_SEC)
        log = Path(os.environ.get("FORSTICK2_EXIT_HANG_LOG", "reports/exit_hang_stacks.log"))
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as f:
                f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} "
                        f"종료 {EXIT_WATCHDOG_SEC:.0f}s 초과 — 강제 종료 ===\n")
                f.write("threads: " + ", ".join(t.name for t in threading.enumerate()) + "\n")
                f.flush()
                faulthandler.dump_traceback(file=f, all_threads=True)
        except OSError:
            pass
        print(f"[asgi] 종료가 {EXIT_WATCHDOG_SEC:.0f}s 안에 끝나지 않아 강제 종료한다 "
              f"(스택: {log})", file=sys.stderr, flush=True)
        os._exit(1)

    threading.Thread(target=watch, name="exit-watchdog", daemon=True).start()
