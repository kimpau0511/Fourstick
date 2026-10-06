"""history 라우트 — 대시보드 이력 화면용 **읽기 전용** 조회.

`GET /v1/history?limit=N`

- `commands`: 최근 요청(발화) — SQLite `requests`와 마지막 검증 판정·실행 상태.
  음성 명령은 STT 확정 때 요청으로 저장된다.
- `sim_jobs`: 최근 Gazebo 시연 작업 결과 — `reports/workcell/sim_demo_jobs/*.json`.
  상태 동기화(`reconcile`) 작업은 내부 작업이라 기본에서 뺀다(`include_reconcile=1`).

**한계:** 시연 명령의 발화와 그 발화가 만든 작업은 연결해 기록되지 않는다. 두 목록을
억지로 짝짓지 않는다. 쓰기 경로가 없다.
"""

from __future__ import annotations

import json
from pathlib import Path

from server.routes.common import RouteContext, Response, json_response

PATHS: tuple[str, ...] = ("/v1/history",)

MAX_LIMIT = 200

#: 작업 결과 파일의 `mode`(없으면 schema) → 화면용 동작 이름.
_ACTION_LABELS = {
    "return_held_to_origin": "원래 자리로 복귀",
    "transfer_route": "지정 위치로 이송",
    "slot_move": "컨베이어 칸 이동",
    "reconcile_state": "상태 동기화(내부)",
    None: "컨베이어로 이송",
}


async def handle(
    ctx: RouteContext, method: str, path: str, receive, query: dict[str, str],
) -> Response | None:
    if method != "GET" or path != "/v1/history":
        return None
    try:
        limit = max(1, min(MAX_LIMIT, int(query.get("limit") or 20)))
    except ValueError:
        limit = 20
    include_reconcile = query.get("include_reconcile") == "1"
    runtime = ctx.runtime
    commands = list(runtime.repository.recent_request_summaries(limit))
    jobs = getattr(runtime, "sim_demo_jobs", None)
    jobs_dir = Path(jobs.jobs_dir) if jobs is not None else None
    return json_response({
        "is_simulated": True,
        "commands": commands,
        "sim_jobs": _sim_jobs(jobs_dir, limit, include_reconcile) if jobs_dir else [],
        "notice": "발화와 시연 작업은 연결해 기록되지 않습니다 — 두 목록은 따로입니다",
    })


def _sim_jobs(jobs_dir: Path, limit: int, include_reconcile: bool) -> list[dict]:
    if not jobs_dir.is_dir():
        return []
    files = sorted(jobs_dir.glob("simjob_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict] = []
    for file in files:
        if len(out) >= limit:
            break
        try:
            record = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        mode = record.get("mode")
        if mode == "reconcile_state" and not include_reconcile:
            continue
        attempt = record.get("attempt") or {}
        out.append({
            "job_id": file.stem,
            "action": mode or "transfer",
            "action_label": _ACTION_LABELS.get(mode, mode or "작업"),
            "material": record.get("model"),
            "status": record.get("status"),
            "completed": attempt.get("completed"),
            "stop_requested": attempt.get("stop_requested"),
            "reason_codes": record.get("reason_codes") or attempt.get("reason_codes") or [],
            "written_at": record.get("written_at"),
            "is_simulated": record.get("is_simulated", True),
        })
    return out
