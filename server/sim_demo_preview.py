"""시연 명령 **해석 미리보기** — 실행하지 않는다.

`/v1/sim-demo/command`는 실행 가능한 명령이면 CONFIRM 확인 대기를 만들고,
환경 발화("2번 칸 고장났어")면 셀 환경을 바꾼다. 미리보기는 그 해석 결과만
보여 주고 운영 확인 대기와 환경은 바꾸지 않는다.

그래서 **같은 라우트 코드**를 일회용 샌드박스에서 돌린다(평가 도구
`scripts/eval_sim_commands.py`와 같은 방식):

- 시연 상태 파일을 임시 폴더로 **복사**한다 — 원본은 읽기만 한다.
- 작업 실행기는 프로세스를 띄우지 않는 가짜다. 정지 요청 파일도 임시 폴더에 쓴다.
- 확인 대기·목표·대화 맥락은 샌드박스 전용이다. 실제 확인 카드가 생기지 않는다.
- 셀 환경(막힌 칸·없는 자재)은 현재 값을 **복사**해 넣는다.
- Qwen 분류기는 실제 것을 쓴다(해석 품질이 운영과 같아야 하므로).

한계: 샌드박스는 실제로 돌고 있는 작업을 모른다. 그래서 응답에 실제 실행 중
작업(`live_running_job`)을 함께 싣는다 — 운영 경로였다면 BLOCK이었을 수 있다.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import types
from pathlib import Path
from typing import Any

#: 해석 미리보기의 대화 맥락은 운영 맥락과 섞지 않는다.
_CONTEXTS_LOCK = threading.Lock()


class _NoProcess:
    """작업 프로세스를 띄우지 않는다. 띄우려 한 명령만 기록한다."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        return types.SimpleNamespace(poll=lambda: None, pid=None)


def _preview_contexts(runtime):
    with _CONTEXTS_LOCK:
        contexts = getattr(runtime, "sim_demo_preview_contexts", None)
        if contexts is None:
            from server.sim_demo_context import DialogueContexts
            contexts = DialogueContexts()
            runtime.sim_demo_preview_contexts = contexts
        return contexts


def preview(runtime, payload: dict) -> tuple[int, dict]:
    """발화 하나를 샌드박스에서 해석한다. `(HTTP 상태, 응답)`을 돌려준다."""
    import json

    from server.routes.sim_demo import command_response
    from server.sim_demo_confirm import ConfirmStore
    from server.sim_demo_goals import SimDemoGoals
    from server.sim_demo_jobs import SimDemoJobs

    live_jobs = getattr(runtime, "sim_demo_jobs", None)
    live_goals = getattr(runtime, "sim_demo_goals", None)
    if live_jobs is None:
        reason = getattr(runtime, "sim_demo_disabled_reason", None) or "시연 작업 셀이 없습니다"
        return 503, {"error": reason, "reason_code": None}

    with tempfile.TemporaryDirectory(prefix="forstick2_preview_") as tmp:
        tmp_path = Path(tmp)
        state_path = tmp_path / "state.json"
        source = Path(live_jobs.state.path)
        if source.is_file():
            shutil.copyfile(source, state_path)
        popen = _NoProcess()
        jobs = SimDemoJobs(workcell=live_jobs.workcell, state_path=state_path,
                           jobs_dir=tmp_path / "jobs", stop_request=tmp_path / "stop.json",
                           popen=popen, environ={"PATH": "/usr/bin"},
                           grasp_config=live_jobs.grasp_config,
                           poses_config=live_jobs.poses_config)
        goals = SimDemoGoals(jobs, auto_run=False, sleep=lambda _: None,
                             environment_path=tmp_path / "environment.json")
        if live_goals is not None:
            env = live_goals.environment()
            goals.set_environment({"blocked_slots": env.get("blocked_slots") or [],
                                   "unavailable_materials": env.get("unavailable_materials") or []})
        sandbox = types.SimpleNamespace(
            sim_demo_jobs=jobs, sim_demo_goals=goals, sim_demo_disabled_reason=None,
            sim_demo_confirm=ConfirmStore(ttl_sec=60),
            sim_demo_intent=getattr(runtime, "sim_demo_intent", None),
            sim_demo_intent_disabled_reason=getattr(runtime, "sim_demo_intent_disabled_reason", None),
            sim_demo_contexts=_preview_contexts(runtime),
        )
        ctx = types.SimpleNamespace(runtime=sandbox)
        body = {**payload, "mode": "simulation_demo"}
        status, _headers, raw = command_response(ctx, body)
        result: dict[str, Any] = json.loads(raw)

    result["preview"] = True
    result["preview_notice"] = ("해석 미리보기 — 작업·확인 카드·셀 환경을 만들지 않았습니다."
                                " 실행은 운영 화면에서 합니다")
    result["would_start_job"] = bool(popen.calls)
    result["would_require_confirmation"] = result.get("decision") in ("CONFIRM", "CONFIRM_GOAL")
    result["live_running_job"] = (live_jobs.status() or {}).get("running_job")
    return status, result
