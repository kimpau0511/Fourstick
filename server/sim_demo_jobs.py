"""웹에서 부르는 **시뮬레이션 시연** 작업 실행기.

웹 화면의 시연 이송·복귀·resume·복구 버튼은 검증된 시연 스크립트
(`scripts/demo_workcell_pick_place.sh`)를 **별도 프로세스 작업**으로 실행한다.
서버 안에서 로봇을 직접 움직이지 않는다 — 스크립트가 진입 조건·재검증·STOP·
체크포인트·래치를 이미 지킨다.

지키는 것:

- 한 번에 **하나의** 시연 작업만 돈다(셀은 하나다).
- 일반 `/v1/plan`·`/v1/execute`의 pick/place 차단과 무관하다. 이 경로는 명시적
  시뮬레이션 시연 경로이며 결과는 모두 `is_simulated=true`다.
- **정지:** 시연 정지·전체 정지는 정지 요청 파일을 쓴다. 스크립트는 이동을
  기다리는 동안 이를 보고 기존 STOP 절차(취소 → 정지 확인 → 수렴 → 체크포인트
  → 래치)로 들어간다. 프로세스를 강제로 죽이지 않는다 — 궤적이 남기 때문이다.
- 가능 동작 표시는 **안내**다. 실제 허용 여부는 스크립트가 관측으로 다시 본다.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from validation.simulation_demo_state import (
    DEFAULT_PATH,
    HELD_ON_TARGET,
    OBJECT_HELD,
    SimulationDemoState,
)

ROOT = Path(__file__).resolve().parents[1]
DEMO_SCRIPT = ROOT / "scripts" / "demo_workcell_pick_place.sh"
JOBS_DIR = ROOT / "reports" / "workcell" / "sim_demo_jobs"
#: 시연 스크립트가 보는 외부 정지 요청 파일(`demo_workcell_pick_place.STOP_REQUEST`).
STOP_REQUEST = Path("/tmp/forstick2_workcell/sim_demo_stop_request.json")

ACTIONS = ("transfer", "return", "resume_preflight", "resume", "restore")
ACTION_LABELS = {
    "transfer": "컨베이어로 이송(상태 유지)",
    "return": "원래 슬롯 복귀",
    "resume_preflight": "resume 사전검증",
    "resume": "체크포인트에서 이어서 이송",
    "restore": "원래 자리로 복구(순간 이동)",
}
_STAGE_LINE = re.compile(r"\[\s*(\d+)/(\d+)\]\s+(.+?)\s+오차\s+([0-9.]+) rad · 도달=(True|False)")


def materials_from_workcell(workcell: Mapping[str, Any]) -> dict[str, dict]:
    """셀 설정의 자재와 원래 팔레트(프레임 부모). 이름을 코드에 적지 않는다."""
    models = workcell["models"]
    frames = workcell["frames"]
    out: dict[str, dict] = {}
    for name, row in models.items():
        if row.get("kind") != "material":
            continue
        parent = (frames.get(row["frame"]) or {}).get("parent")
        support = next((m for m, r in models.items()
                        if r.get("kind") == "pallet" and r.get("frame") == parent), None)
        korean = next((r.get("korean") for r in workcell.get("resource_map", ())
                       if r.get("gazebo_model") == name), name)
        out[name] = {"model": name, "resource_id": row.get("resource_id"),
                     "korean": korean, "support_model": support}
    return out


def available_actions(status: Mapping[str, Any], model: str) -> dict[str, bool]:
    """상태 기록에서 본 **안내용** 가능 동작. 스크립트가 다시 검증한다."""
    objects = dict(status.get("objects") or {})
    row = objects.get(model) or {}
    checkpoint = status.get("checkpoint") or {}
    has_checkpoint = (checkpoint.get("model") == model
                      and model in (status.get("checkpoints") or []))
    clean = status.get("available") is True and not objects and not status.get(
        "checkpoints")
    return {
        "transfer": clean,
        "return": row.get("state") == HELD_ON_TARGET,
        "resume_preflight": has_checkpoint and checkpoint.get("object_state") == OBJECT_HELD,
        "resume": has_checkpoint and checkpoint.get("object_state") == OBJECT_HELD,
        "restore": bool(row) or has_checkpoint
                   or model in (status.get("checkpoint_unavailable") or {}),
    }


def build_argv(action: str, material: Mapping[str, Any],
               checkpoint_id: str | None) -> list[str]:
    model = material["model"]
    if action == "transfer":
        return [material["support_model"], model, "--cell-policy",
                "simulation_demo_hold"]
    if action == "return":
        return [material["support_model"], model, "--return-held-to-origin"]
    if action == "resume_preflight":
        return ["-", model, "--resume-preflight"]
    if action == "resume":
        return ["-", model, "--resume-checkpoint", str(checkpoint_id)]
    if action == "restore":
        return [material["support_model"], model, "--restore-only"]
    raise ValueError(action)


def parse_progress(console: str) -> list[dict]:
    rows = []
    for match in _STAGE_LINE.finditer(console):
        rows.append({"no": int(match.group(1)), "of": int(match.group(2)),
                     "label": match.group(3).strip(),
                     "error_rad": float(match.group(4)),
                     "reached": match.group(5) == "True"})
    return rows


class SimDemoJobError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class SimDemoJobs:
    """시연 작업 하나를 띄우고 지켜본다. 상태 조회는 명령을 보내지 않는다."""

    def __init__(self, *, workcell: Mapping[str, Any],
                 state_path: Path = DEFAULT_PATH, jobs_dir: Path = JOBS_DIR,
                 stop_request: Path = STOP_REQUEST, script: Path = DEMO_SCRIPT,
                 popen: Callable[..., Any] = subprocess.Popen,
                 clock: Callable[[], float] = time.time,
                 environ: Mapping[str, str] | None = None):
        self.workcell = workcell
        self.materials = materials_from_workcell(workcell)
        self.state = SimulationDemoState(state_path)
        self.jobs_dir = Path(jobs_dir)
        self.stop_request = Path(stop_request)
        self.script = Path(script)
        self._popen = popen
        self._clock = clock
        self._environ = dict(os.environ if environ is None else environ)
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._procs: dict[str, Any] = {}
        self._current: str | None = None

    # ── 작업 ────────────────────────────────────────────────────────────
    def _refresh(self, job_id: str) -> dict:
        job = self._jobs[job_id]
        proc = self._procs.get(job_id)
        if job["status"] == "running" and proc is not None:
            code = proc.poll()
            if code is not None:
                job.update(status="finished", exit_code=code,
                           finished_at=self._clock())
                job["report"] = self._read_report(job)
                if self._current == job_id:
                    self._current = None
        return job

    @staticmethod
    def _read_report(job: dict) -> dict | None:
        try:
            return json.loads(Path(job["report_path"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def running(self) -> dict | None:
        with self._lock:
            if self._current is None:
                return None
            job = self._refresh(self._current)
            return dict(job) if job["status"] == "running" else None

    def start(self, action: str, material: str,
              checkpoint_id: str | None = None) -> dict:
        if action not in ACTIONS:
            raise SimDemoJobError(400, f"알 수 없는 시연 동작이다: {action}")
        spec = self.materials.get(material)
        if spec is None:
            raise SimDemoJobError(400, f"셀 선언의 자재가 아니다: {material}")
        if action == "resume" and not checkpoint_id:
            raise SimDemoJobError(400, "resume에는 checkpoint_id가 필요하다")
        with self._lock:
            if self._current is not None:
                self._refresh(self._current)
            if self._current is not None:
                raise SimDemoJobError(409, "다른 시연 작업이 실행 중이다")
            job_id = f"simjob_{uuid.uuid4().hex[:12]}"
            self.jobs_dir.mkdir(parents=True, exist_ok=True)
            report_path = self.jobs_dir / f"{job_id}.json"
            console_path = self.jobs_dir / f"{job_id}.console"
            argv = [str(self.script), *build_argv(action, spec, checkpoint_id),
                    "--out", str(report_path)]
            env = dict(self._environ, FORSTICK2_SIM_PICK_PLACE_DEMO="1")
            console = open(console_path, "w", encoding="utf-8")
            try:
                proc = self._popen(argv, stdout=console, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, env=env,
                                   cwd=str(ROOT), start_new_session=True)
            finally:
                console.close()
            job = {"job_id": job_id, "action": action,
                   "action_label": ACTION_LABELS[action], "material": material,
                   "checkpoint_id": checkpoint_id, "argv": argv,
                   "report_path": str(report_path), "console_path": str(console_path),
                   "started_at": self._clock(), "status": "running",
                   "exit_code": None, "report": None, "is_simulated": True,
                   "stop_requested": None}
            self._jobs[job_id] = job
            self._procs[job_id] = proc
            self._current = job_id
            return dict(job)

    def job(self, job_id: str, *, console_lines: int = 60) -> dict:
        with self._lock:
            if job_id not in self._jobs:
                raise SimDemoJobError(404, f"시연 작업이 없다: {job_id}")
            job = dict(self._refresh(job_id))
        try:
            console = Path(job["console_path"]).read_text(encoding="utf-8",
                                                           errors="replace")
        except OSError:
            console = ""
        job["progress"] = parse_progress(console)
        job["console_tail"] = [line for line in console.splitlines()
                               if not line.startswith(("[INFO", "[WARN"))][-console_lines:]
        return job

    def request_stop(self, *, reason: str) -> dict:
        """실행 중인 시연 작업에 정지를 **요청**한다. 프로세스를 죽이지 않는다."""
        running = self.running()
        if running is None:
            return {"requested": False, "detail": "실행 중인 시연 작업이 없다"}
        request = {"request_id": f"simstopreq_{uuid.uuid4().hex[:12]}",
                   "requested_at": self._clock(), "reason": reason,
                   "job_id": running["job_id"]}
        self.stop_request.parent.mkdir(parents=True, exist_ok=True)
        temp = self.stop_request.with_suffix(".tmp")
        temp.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.stop_request)
        with self._lock:
            self._jobs[running["job_id"]]["stop_requested"] = request
        return {"requested": True, **request}

    # ── 상태 ────────────────────────────────────────────────────────────
    def status(self) -> dict:
        state = self.state.status()
        running = self.running()
        with self._lock:
            recent = sorted(self._jobs.values(), key=lambda j: j["started_at"],
                            reverse=True)[:5]
            recent = [{k: j[k] for k in ("job_id", "action", "action_label",
                                         "material", "status", "exit_code",
                                         "started_at")}
                      | {"result_status": (j.get("report") or {}).get("status")}
                      for j in recent]
        materials = []
        for model, spec in self.materials.items():
            actions = available_actions(state, model)
            if running is not None:
                actions = {k: False for k in actions}
            materials.append({**spec, "record": (state.get("objects") or {}).get(model),
                              "actions": actions})
        return {"is_simulated": True, "real_hardware_ready": False,
                "real_hardware_verified": False, "state": state,
                "running_job": running, "recent_jobs": recent,
                "materials": materials, "action_labels": ACTION_LABELS}
