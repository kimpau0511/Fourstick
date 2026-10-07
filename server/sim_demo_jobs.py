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

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from core.workcell_paths import log_dir
from typing import Any, Callable, Mapping, Sequence

from server.cell_execution import CellExecutionLease, CellExecutionManager
from server.sim_demo_places import declared_places
from validation.conveyor_slots import (
    assign_slot,
    load_slots,
    occupancy,
    occupancy_rows,
    record_slot,
    slot_by_name,
    slot_label,
)
from validation.simulation_demo_state import (
    DEFAULT_PATH,
    HELD_ON_TARGET,
    ON_PALLET,
    ON_SURFACE,
    SimulationDemoState,
    checkpoint_resumable,
)

ROOT = Path(__file__).resolve().parents[1]
DEMO_SCRIPT = ROOT / "scripts" / "demo_workcell_pick_place.sh"
JOBS_DIR = ROOT / "reports" / "workcell" / "sim_demo_jobs"
#: 시연 스크립트가 보는 외부 정지 요청 파일(`demo_workcell_pick_place.STOP_REQUEST`).
STOP_REQUEST = log_dir() / "sim_demo_stop_request.json"

ACTIONS = ("transfer", "return", "move", "route", "spot", "resume_preflight", "resume",
           "restore", "reconcile")
#: 자재를 고르지 않는 셀 전체 작업. 로봇에 명령을 보내지 않는다.
CELL_ACTIONS = ("reconcile",)
ACTION_LABELS = {
    "transfer": "컨베이어로 이송(상태 유지)",
    "return": "원래 슬롯 복귀",
    "move": "컨베이어 칸 → 다른 칸 직접 이송",
    "route": "팔레트가 끼는 이송(다른 팔레트·팔레트↔칸)",
    "spot": "표면 빈 위치에 놓기(서버가 계산·검증한 자리)",
    "resume_preflight": "resume 사전검증",
    "resume": "체크포인트에서 이어서 이송",
    "restore": "원래 자리로 복구(순간 이동)",
    "reconcile": "기록↔관측 정합 확인(읽기 전용)",
}
_STAGE_LINE = re.compile(r"\[\s*(\d+)/(\d+)\]\s+(.+?)\s+오차\s+([0-9.]+) rad · 도달=(True|False)")
ACTIVE_JOB_FILE = "active_job.json"
_RECOVERED_EXIT = object()


def _linux_process_identity(pid: int) -> dict:
    """Return a PID-reuse-safe identity for a Linux process."""
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    fields = stat[stat.rfind(")") + 2:].split()
    start_ticks = int(fields[19])  # /proc stat field 22
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
        encoding="utf-8").strip()
    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    return {
        "pid": int(pid), "boot_id": boot_id, "start_ticks": start_ticks,
        "cmdline_sha256": hashlib.sha256(cmdline).hexdigest(),
    }


def _probe_process_identity(identity: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``alive``, ``dead``, or ``uncertain`` for a saved identity."""
    try:
        pid = identity.get("pid")
        expected_boot = identity.get("boot_id")
        expected_start = identity.get("start_ticks")
        expected_cmdline = identity.get("cmdline_sha256")
        job_marker = identity.get("job_marker")
        if not isinstance(pid, int) or pid <= 0 or not expected_boot \
                or not isinstance(expected_start, int) \
                or not isinstance(expected_cmdline, str) \
                or not re.fullmatch(r"[0-9a-f]{64}", expected_cmdline):
            return "uncertain", "저장된 프로세스 식별자가 불완전하다"
        current_boot = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8").strip()
        if current_boot != expected_boot:
            return "dead", "저장 이후 시스템이 재부팅되었다"
        try:
            current = _linux_process_identity(pid)
        except (FileNotFoundError, ProcessLookupError):
            return "dead", "저장된 프로세스가 없다"
        except (OSError, ValueError) as exc:
            return "uncertain", f"프로세스 식별자를 확인할 수 없다: {type(exc).__name__}"
        if current["start_ticks"] != expected_start:
            return "dead", "PID가 다른 프로세스에 재사용되었다"
        if current["cmdline_sha256"] != expected_cmdline:
            # The bash launcher uses exec(2), so a restart may observe its final
            # Python command line rather than the short-lived shell command line
            # hashed immediately after Popen.  The unique report path survives
            # that exec and proves this is still the journaled invocation.
            if not isinstance(job_marker, str) or not job_marker:
                return "uncertain", "프로세스 명령행이 저장된 작업과 일치하지 않는다"
            try:
                argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            except OSError as exc:
                return "uncertain", (
                    "프로세스 명령행을 확인할 수 없다: "
                    f"{type(exc).__name__}")
            if os.fsencode(job_marker) not in argv:
                return "uncertain", "프로세스 명령행에서 작업 소유권을 확인할 수 없다"
        return "alive", "커널 프로세스 식별자가 일치한다"
    except (OSError, ValueError, TypeError) as exc:
        return "uncertain", f"프로세스 식별자를 확인할 수 없다: {type(exc).__name__}"


class _RecoveredProcess:
    """Popen-like observer for a child inherited across server restart."""

    def __init__(self, identity: Mapping[str, Any], probe):
        self.identity = dict(identity)
        self._probe = probe

    def poll(self):
        state, _ = self._probe(self.identity)
        return None if state in ("alive", "uncertain") else _RECOVERED_EXIT

    def wait(self):
        while self.poll() is None:
            time.sleep(0.2)
        return _RECOVERED_EXIT


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
        resource_row = next((r for r in workcell.get("resource_map", ())
                            if r.get("gazebo_model") == name), {})
        korean = resource_row.get("korean") or name
        # 색 이름도 셀 설정 선언에서만 온다. 화면이 "무엇으로 부를 수 있는지"를
        # 보여주기 위한 것이며, 발화 해석은 `server/sim_demo_commands.py`가 한다.
        support_row = next((r for r in workcell.get("resource_map", ())
                            if r.get("gazebo_model") == support), {})
        out[name] = {"model": name, "resource_id": row.get("resource_id"),
                     "korean": korean, "support_model": support,
                     "support_korean": support_row.get("korean"),
                     "korean_colors": list(resource_row.get("korean_colors") or ())}
    return out


def available_actions(status: Mapping[str, Any], model: str,
                      slots: Sequence[Any] = ()) -> dict[str, bool]:
    """상태 기록에서 본 **안내용** 가능 동작. 스크립트가 다시 검증한다.

    슬롯이 설정돼 있으면 이송 조건이 "셀 전체가 비었을 때"가 아니라 **"이 자재가
    컨베이어에 없고 빈 슬롯이 있을 때"**다. 슬롯이 없으면(설정 미적용) 예전
    규칙 그대로 — 컨베이어 배치 위치가 하나뿐이던 때와 같다.

    체크포인트가 하나라도 있으면 이송을 열지 않는다. 정지가 풀리지 않은 셀에
    새 자재를 올리면 사람이 복구할 대상이 늘어난다.
    """
    objects = dict(status.get("objects") or {})
    row = objects.get(model) or {}
    checkpoint = status.get("checkpoint") or {}
    has_checkpoint = (checkpoint.get("model") == model
                      and model in (status.get("checkpoints") or []))
    resumable = has_checkpoint and checkpoint_resumable(checkpoint)[0]
    available = status.get("available") is True
    any_checkpoint = bool(status.get("checkpoints"))
    if slots:
        from validation.conveyor_slots import assign_slot

        free = assign_slot(slots, objects) is not None
        can_transfer = available and not row and not any_checkpoint and free
    else:
        can_transfer = available and not objects and not any_checkpoint
    return {
        "transfer": can_transfer,
        "return": row.get("state") == HELD_ON_TARGET,
        "resume_preflight": resumable,
        "resume": resumable,
        "restore": bool(row) or has_checkpoint
                   or model in (status.get("checkpoint_unavailable") or {}),
    }


def build_argv(action: str, material: Mapping[str, Any] | None,
               checkpoint_id: str | None, slot: str | None = None,
               spot_path: str | None = None, source: str | None = None) -> list[str]:
    """시연 스크립트 인자. **슬롯은 서버가 정해서 넘긴다** — 스크립트가 고르지
    않고, 모델(LLM)이 좌표를 만들 자리도 없다."""
    if action == "reconcile":
        # 셀 전체 작업이다. 자재 인자를 쓰지 않는다.
        return ["-", "-", "--reconcile-state"]
    model = material["model"]
    slot_args = ["--slot", str(slot)] if slot else []
    if action == "transfer":
        return [material["support_model"], model, "--cell-policy",
                "simulation_demo_hold", *slot_args]
    if action == "return":
        return [material["support_model"], model, "--return-held-to-origin",
                *slot_args]
    if action == "spot":
        # 자리·자세는 서버가 계산·검증해 파일로 넘긴다. 실행기가 FK·관측·MoveIt으로 다시 본다.
        if not spot_path or not source:
            raise ValueError("빈 위치 놓기에는 출발지와 자리 파일이 필요하다")
        return [material["support_model"], model, "--route-from", source,
                "--route-to-spot", spot_path]
    if action in ("move", "route"):
        # slot = "출발>도착". 서버가 잠금 안에서 기록·점유·계약으로 정한 값이다.
        source, _, destination = str(slot or "").partition(">")
        if not source or not destination:
            raise ValueError("이송에는 출발·도착이 모두 필요하다")
        flags = (("--move-from-slot", "--move-to-slot") if action == "move"
                 else ("--route-from", "--route-to"))
        return [material["support_model"], model, flags[0], source, flags[1], destination]
    # 재개는 중단된 이송의 **기록 칸**에 놓는다. 스크립트가 기록과 다시 대조한다.
    if action == "resume_preflight":
        return ["-", model, "--resume-preflight", *slot_args]
    if action == "resume":
        return ["-", model, "--resume-checkpoint", str(checkpoint_id), *slot_args]
    if action == "restore":
        return [material["support_model"], model, "--restore-only"]
    raise ValueError(action)


#: 실행기가 정지 요청 기준 시각(`EXTERNAL_STOP.since`)을 정한 **바로 뒤** 콘솔에 찍는 줄
#: (`scripts/demo_workcell_pick_place.py` main). 이 줄 뒤에 쓴 정지 요청은 인정된다.
EXECUTOR_ARMED_MARK = "[원본 로그]"
#: 정지 요청을 다시 쓰는 간격(s). 실행기가 기준 시각을 정하기까지(실측 약 0.26 s)보다 길고,
#: 단계 사이 정지 확인 주기보다 짧게 둔다.
STOP_REASSERT_SEC = 0.5


def _executor_armed(job: Mapping[str, Any]) -> bool:
    try:
        return EXECUTOR_ARMED_MARK in Path(job["console_path"]).read_text(
            encoding="utf-8", errors="replace")
    except (OSError, KeyError, TypeError):
        return False


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
                 environ: Mapping[str, str] | None = None,
                 grasp_config: Mapping[str, Any] | None = None,
                 poses_config: Mapping[str, Any] | None = None,
                 cell_execution: CellExecutionManager | None = None,
                 surfaces_config: Mapping[str, Any] | None = None,
                 motion: Any = None,
                 process_identity: Callable[[int], dict] = _linux_process_identity,
                 process_probe: Callable[[Mapping[str, Any]],
                                         tuple[str, str]] = _probe_process_identity):
        self.workcell = workcell
        self.materials = materials_from_workcell(workcell)
        # 검증된 컨베이어 슬롯. **여기서 좌표를 만들지 않는다** — 설정에 있는
        # `status=verified`이고 모든 MoveIt 검사가 clean인 것만 들어온다.
        # 비어 있으면 슬롯 기능이 꺼진 것이고, 기존 단일 배치 경로가 그대로다.
        self.grasp_config = dict(grasp_config or {})
        self.poses_config = dict(poses_config or {})
        self.slots = load_slots(grasp_config or {})
        # 자연어 pick/place에서 **말할 수 있는 자리**의 전부. 셀 설정과 검증된
        # 슬롯에서만 만든다 — 여기 없는 자리는 존재하지 않는 자리다.
        # 빈 위치 놓기를 허용한 표면 선언(매니페스트 `surfaces`). 없으면 그 기능이 꺼진다.
        self.surfaces = dict(surfaces_config or {})
        # 이동 속도 설정(server/sim_demo_motion.MotionSettings). 없으면 실행기 고정 시간 그대로다.
        self.motion = motion
        self.places = declared_places(workcell, self.slots, poses_config, self.surfaces)
        self.state = SimulationDemoState(state_path)
        self.jobs_dir = Path(jobs_dir)
        self.stop_request = Path(stop_request)
        self.script = Path(script)
        self._popen = popen
        self._clock = clock
        self._environ = dict(os.environ if environ is None else environ)
        self._process_identity = process_identity
        self._process_probe = process_probe
        # General /v1/execute and this subprocess runner receive the same manager
        # from Runtime.  A private default keeps standalone/test construction safe.
        self.cell_execution = cell_execution or CellExecutionManager(clock=clock)
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._procs: dict[str, Any] = {}
        self._leases: dict[str, CellExecutionLease] = {}
        self._current: str | None = None
        # A confirmed goal owns the shared cell lease for its entire sequence.
        # Its child jobs run under that lease instead of trying to acquire it
        # again (which would deadlock against their own parent reservation).
        # The string is also a narrow capability: only start(..., goal_id=...)
        # with the matching id may launch while the reservation is live.
        self._goal_reservation: str | None = None
        self._goal_lease: CellExecutionLease | None = None
        self.recovery_required: dict | None = None
        self._recover_active_job()

    @property
    def _journal_path(self) -> Path:
        return self.jobs_dir / ACTIVE_JOB_FILE

    def _atomic_write_journal(self, record: Mapping[str, Any]) -> None:
        """Durably replace the single active-motion journal."""
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        temp = self.jobs_dir / f".{ACTIVE_JOB_FILE}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temp, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self._journal_path)
            try:
                directory_fd = os.open(self.jobs_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _journal_record(job: Mapping[str, Any], identity: Mapping[str, Any]) -> dict:
        return {
            "version": 1, "job_id": job["job_id"],
            "process_identity": dict(identity), "action": job["action"],
            "material": job.get("material"), "slot": job.get("slot"),
            "checkpoint_id": job.get("checkpoint_id"),
            "goal_id": job.get("goal_id"), "started_at": job["started_at"],
            "paths": {"report": job["report_path"],
                      "console": job["console_path"]},
            "status": job["status"],
        }

    def _set_recovery_required(self, reason: str, *, job_id: str | None = None) -> None:
        self.recovery_required = {"required": True, "reason": reason,
                                  "job_id": job_id}

    def _clear_journal(self, job_id: str) -> bool:
        """Remove only the journal known to describe this terminal job."""
        try:
            record = json.loads(self._journal_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return True
        except (OSError, json.JSONDecodeError) as exc:
            self._set_recovery_required(
                f"종료된 작업 기록을 확인할 수 없다: {type(exc).__name__}",
                job_id=job_id)
            return False
        if not isinstance(record, dict) or record.get("job_id") != job_id:
            self._set_recovery_required(
                "활성 작업 기록이 종료된 프로세스와 일치하지 않는다", job_id=job_id)
            return False
        try:
            self._journal_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._set_recovery_required(
                f"종료된 작업 기록을 지울 수 없다: {type(exc).__name__}",
                job_id=job_id)
            return False
        if (self.recovery_required is not None
                and self.recovery_required.get("job_id") == job_id):
            self.recovery_required = None
        return True

    def _recover_active_job(self) -> None:
        """Adopt a live motion child or fail closed on ambiguous ownership."""
        try:
            record = json.loads(self._journal_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            self._set_recovery_required(
                f"활성 작업 기록을 읽을 수 없다: {type(exc).__name__}")
            return
        if not isinstance(record, dict):
            self._set_recovery_required("활성 작업 기록 형식이 올바르지 않다")
            return
        identity = record.get("process_identity")
        if not isinstance(identity, Mapping):
            self._set_recovery_required("저장된 프로세스 식별자가 없다",
                                        job_id=record.get("job_id"))
            return
        process_state, detail = self._process_probe(identity)
        if process_state == "dead":
            try:
                self._journal_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._set_recovery_required(
                    f"오래된 작업 기록을 지울 수 없다: {type(exc).__name__}",
                    job_id=record.get("job_id"))
            return
        if process_state != "alive":
            self._set_recovery_required(detail, job_id=record.get("job_id"))
            return

        job_id = record.get("job_id")
        action = record.get("action")
        material = record.get("material")
        paths = record.get("paths")
        started_at = record.get("started_at")
        if (not isinstance(job_id, str) or not job_id
                or action not in ACTIONS or action in CELL_ACTIONS
                or material not in self.materials or not isinstance(paths, Mapping)
                or not isinstance(paths.get("report"), str)
                or not isinstance(paths.get("console"), str)
                or not isinstance(started_at, (int, float))
                or record.get("status") != "running"):
            self._set_recovery_required(
                "살아 있는 프로세스의 작업 기록이 불완전하다", job_id=job_id)
            return

        job = {
            "job_id": job_id, "action": action,
            "action_label": ACTION_LABELS[action], "material": material,
            "goal_id": record.get("goal_id"),
            "checkpoint_id": record.get("checkpoint_id"),
            "slot": record.get("slot"),
            "slot_label": (slot_label(record.get("slot"))
                           if record.get("slot") else None),
            "argv": None, "report_path": paths["report"],
            "console_path": paths["console"], "started_at": float(started_at),
            "status": "running", "exit_code": None, "report": None,
            "is_simulated": True, "stop_requested": None, "recovered": True,
        }
        owner = "sim_demo_goal" if job["goal_id"] else "sim_demo"
        operation_id = job["goal_id"] or job_id
        lease = self.cell_execution.try_acquire(owner=owner, operation_id=operation_id)
        if lease is None:
            self._set_recovery_required(
                "살아 있는 작업의 공유 실행 권한을 확보할 수 없다", job_id=job_id)
        else:
            self._leases[job_id] = lease
        proc = _RecoveredProcess(identity, self._process_probe)
        self._jobs[job_id] = job
        self._procs[job_id] = proc
        self._current = job_id
        watcher = threading.Thread(
            target=self._watch_process, args=(job_id, proc),
            name=f"sim-demo-recovered-{job_id}", daemon=True)
        watcher.start()

    def reserve_goal(self, goal_id: str, *, owner: str = "sim_demo_goal",
                     reservation: str | None = None) -> bool:
        """Atomically reserve the sim-demo runner and shared cell for a goal.

        ``owner`` names who holds the shared cell lease (the general ``/v1/execute``
        path reserves with its own owner while its transfer child job runs)."""
        if not goal_id:
            raise ValueError("goal_id is required")
        with self._lock:
            if self.recovery_required is not None:
                return False
            if self._current is not None:
                self._refresh(self._current)
            if self._current is not None or self._goal_reservation is not None:
                return False
            lease = self.cell_execution.try_acquire(
                owner=owner, operation_id=goal_id, reservation=reservation)
            if lease is None:
                return False
            self._goal_reservation = goal_id
            self._goal_lease = lease
            return True

    def release_goal(self, goal_id: str) -> bool:
        """Release only the matching goal's idle reservation.

        Goal orchestration calls this after its child has reached a terminal
        state.  Refusing an early release is deliberate: it must never expose
        the cell while that child process can still move the robot.
        """
        with self._lock:
            if self._goal_reservation != goal_id:
                return False
            if self._current is not None:
                self._refresh(self._current)
            if self._current is not None:
                return False
            if not self.cell_execution.release(self._goal_lease):
                return False
            self._goal_reservation = None
            self._goal_lease = None
            return True

    # ── 작업 ────────────────────────────────────────────────────────────
    def _refresh(self, job_id: str) -> dict:
        job = self._jobs[job_id]
        proc = self._procs.get(job_id)
        if job["status"] == "running" and proc is not None:
            code = proc.poll()
            if code is not None:
                exit_code = None if code is _RECOVERED_EXIT else code
                job.update(status="finished", exit_code=exit_code,
                           finished_at=self._clock())
                job["report"] = self._read_report(job)
                # Goal children have no child lease: the parent goal owns the
                # cell continuously across all of its steps.  Only standalone
                # jobs release here; release_goal() owns the goal lease.
                lease = self._leases.pop(job_id, None)
                if lease is not None:
                    self.cell_execution.release(lease)
                if self._current == job_id:
                    self._current = None
                if job.get("journaled") or job.get("recovered"):
                    self._clear_journal(job_id)
        return job

    def _watch_process(self, job_id: str, proc: Any) -> None:
        """Notice process exit even when no client polls the job endpoint."""
        wait = getattr(proc, "wait", None)
        if not callable(wait):
            return
        try:
            wait()
        except Exception:  # noqa: BLE001 — a later status poll can still reap it
            return
        with self._lock:
            if job_id in self._jobs:
                self._refresh(job_id)

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

    # ── 공통 transfer 진입 ─────────────────────────────────────────
    def transfer_world(self, *, blocked: tuple[str, ...] = (),
                       unavailable: tuple[str, ...] = ()):
        """계약 판정용 현재 상태(확정 기록 기준). 확정되지 않은 기록이 있으면 None."""
        from core.transfer_skill import WorldView

        state = self.state.status()
        if state.get("available") is not True:
            return None
        location_of = {m: self._capability().origin_of(m) for m in self.materials}
        for model, row in (state.get("objects") or {}).items():
            if row.get("state") == HELD_ON_TARGET:
                location_of[model] = record_slot(row)
            elif row.get("state") == ON_PALLET:
                location_of[model] = row.get("pallet")
            elif row.get("state") == ON_SURFACE and (row.get("spot") or {}).get("id"):
                # 표면 빈 위치. 선언된 자리가 아니므로 이 자재를 출발지로 쓰는 이송은
                # 계약이 막는다(자리 모름) — 다른 자재의 도착 점유 판단에만 쓰인다.
                location_of[model] = row["spot"]["id"]
            else:
                return None
        return WorldView(location_of=location_of, blocked=frozenset(blocked),
                         unavailable=frozenset(unavailable))

    def _capability(self):
        if getattr(self, "_transfer_capability", None) is None:
            # 로봇별 Capability는 **작업 셀 매니페스트가 가리키는 어댑터 패키지**에서
            # 찾는다. 공통 코드는 로봇 이름을 모른다(어댑터 등록과 같은 방식).
            import importlib

            manifest = json.loads((ROOT / "config/workcell/active.json")
                                  .read_text(encoding="utf-8"))
            module = importlib.import_module(str(manifest["adapter_module"]))
            build = getattr(module, "build_transfer_capability")
            self._transfer_capability = build(
                workcell=self.workcell, grasp=self.grasp_config or None,
                poses=self.poses_config or None)
        return self._transfer_capability

    def check_transfer(self, material: str, source: str, destination: str, *,
                       blocked: tuple[str, ...] = (), unavailable: tuple[str, ...] = ()):
        """공통 계약 검사만(실행 없음). (계획, 이유들)."""
        from core.transfer_skill import Finding, TransferRequest, validate_transfer

        capability = self._capability()
        world = self.transfer_world(blocked=blocked, unavailable=unavailable)
        if world is None:
            return None, [Finding("BLOCK", "state.unconfirmed",
                                  "확정되지 않은 자재 기록이 있어 이송을 판정하지 않는다")]
        return validate_transfer(TransferRequest(capability.robot_id, material, source,
                                                 destination), capability, world)

    def start_transfer(self, material: str, source: str, destination: str, *,
                       goal_id: str | None = None, blocked: tuple[str, ...] = (),
                       unavailable: tuple[str, ...] = (), speed_percent: int | None = None) -> dict:
        """`transfer(material, source, destination)` — 계약 검사 뒤 로봇 실행기로 보낸다."""
        plan, findings = self.check_transfer(material, source, destination,
                                             blocked=blocked, unavailable=unavailable)
        if plan is None:
            first = findings[0]
            raise SimDemoJobError(400 if first.decision == "ASK" else 409,
                                  f"{first.code}: {first.detail}")
        route = plan.route
        origin = self._capability().origin_of(material)
        if route == "pallet->conveyor_slot" and source == origin:
            job = self.start("transfer", material, requested_slot=destination,
                             goal_id=goal_id, speed_percent=speed_percent)
        elif route == "conveyor_slot->pallet" and destination == origin:
            job = self.start("return", material, goal_id=goal_id, speed_percent=speed_percent)
        elif route == "conveyor_slot->conveyor_slot":
            job = self.start("move", material, requested_slot=destination, goal_id=goal_id, speed_percent=speed_percent)
        elif route in ("pallet->pallet", "pallet->conveyor_slot", "conveyor_slot->pallet"):
            # 다른 팔레트가 끼는 경로 — 공통 경로 실행기.
            job = self.start("route", material, goal_id=goal_id, route=(source, destination), speed_percent=speed_percent)
        else:
            raise SimDemoJobError(409, f"capability.route_unsupported: {route}")
        job["transfer"] = plan.to_dict()
        return job

    def start(self, action: str, material: str | None = None,
              checkpoint_id: str | None = None, slot: str | None = None,
              *, requested_slot: str | None = None,
              goal_id: str | None = None, route: tuple[str, str] | None = None,
              spot: Mapping[str, Any] | None = None,
              speed_percent: int | None = None) -> dict:
        """Start one job with its conveyor slot decided under the cell lease.

        ``slot`` is retained as an internal compatibility argument, but it is never
        authoritative.  ``requested_slot`` is a server-parsed natural-language
        preference; the direct HTTP card route never forwards one.  After acquiring
        the same lease that is held for motion, the runner reads durable demo state,
        validates a named verified position or selects the first verified free one.
        Return/resume recover the position from the material record.
        """
        if action not in ACTIONS:
            raise SimDemoJobError(400, f"알 수 없는 시연 동작이다: {action}")
        if action in CELL_ACTIONS:
            spec = None
        else:
            spec = self.materials.get(material)
            if spec is None:
                raise SimDemoJobError(400, f"셀 선언의 자재가 아니다: {material}")
        if action == "resume" and not checkpoint_id:
            raise SimDemoJobError(400, "resume에는 checkpoint_id가 필요하다")
        with self._lock:
            if self.recovery_required is not None and action not in CELL_ACTIONS:
                raise SimDemoJobError(
                    409, "이전 시연 작업의 소유권을 확인해야 새 동작을 시작할 수 있다: "
                    f"{self.recovery_required['reason']}")
            if goal_id is not None and self._goal_reservation != goal_id:
                raise SimDemoJobError(409, "유효한 복귀 목표 예약이 아니다")
            if self._goal_reservation is not None and goal_id is None:
                raise SimDemoJobError(
                    409, "복귀 목표가 작업 셀을 예약하고 있다"
                    f" (goal_id={self._goal_reservation})")
            if self._current is not None:
                self._refresh(self._current)
            if self._current is not None:
                raise SimDemoJobError(409, "다른 시연 작업이 실행 중이다")
            job_id = f"simjob_{uuid.uuid4().hex[:12]}"
            job = None
            lease = None
            if action not in CELL_ACTIONS and goal_id is None:
                lease = self.cell_execution.try_acquire(
                    owner="sim_demo", operation_id=job_id)
                if lease is None:
                    active = self.cell_execution.current()
                    detail = "다른 작업 셀 실행이 진행 중이다"
                    if active is not None:
                        detail += (f" (owner={active.owner}, "
                                   f"operation_id={active.operation_id})")
                    raise SimDemoJobError(409, detail)
            try:
                # Slot allocation and process launch are one lease-protected
                # operation.  Nothing else using this workcell can select from the
                # same occupancy snapshot before this job owns the cell.
                if (lease is not None or goal_id is not None) and self.slots:
                    state = self.state.status()
                    objects = state.get("objects") or {}
                    if action == "transfer":
                        picked = slot_by_name(self.slots, requested_slot)
                        if requested_slot and picked is None:
                            raise SimDemoJobError(
                                400, f"검증된 컨베이어 위치가 아닙니다: {requested_slot}")
                        if picked is not None:
                            taken = occupancy(self.slots, objects).get(picked.name)
                            if taken is not None:
                                raise SimDemoJobError(
                                    409, f"{picked.label}에는 이미 자재가 있습니다")
                        else:
                            picked = assign_slot(self.slots, objects)
                        if picked is None:
                            raise SimDemoJobError(
                                409,
                                f"컨베이어 {len(self.slots)}개 위치가 모두 찼습니다 — "
                                "먼저 하나를 원래 자리로 돌려놔 주세요")
                        # Deliberately overwrite any caller-provided value.
                        slot = picked.name
                    elif action == "move":
                        row = objects.get(material) or {}
                        source = record_slot(row) if row.get("state") == HELD_ON_TARGET else None
                        picked = slot_by_name(self.slots, requested_slot)
                        if source is None or slot_by_name(self.slots, source) is None:
                            raise SimDemoJobError(
                                409, "컨베이어 칸에 유지 중이라는 기록이 없어 칸을 옮길 수 없습니다")
                        if picked is None:
                            raise SimDemoJobError(
                                400, f"검증된 컨베이어 위치가 아닙니다: {requested_slot}")
                        if picked.name == source:
                            raise SimDemoJobError(400, "출발 칸과 도착 칸이 같습니다")
                        taken = occupancy(self.slots, objects).get(picked.name)
                        if taken is not None:
                            raise SimDemoJobError(
                                409, f"{picked.label}에는 이미 자재가 있습니다")
                        slot = f"{source}>{picked.name}"
                    elif action == "route":
                        # 잠금 안에서 계약을 다시 본다(그 사이 상태가 바뀌었으면 막는다).
                        if route is None:
                            raise SimDemoJobError(400, "이송 경로(출발·도착)가 없다")
                        plan, found = self.check_transfer(material, route[0], route[1])
                        if plan is None:
                            raise SimDemoJobError(409, f"{found[0].code}: {found[0].detail}")
                        slot = f"{route[0]}>{route[1]}"
                    elif action == "spot":
                        # 확인 뒤 기록이 바뀌었으면 시작하지 않는다(출발지·자리 점유).
                        if not spot or not spot.get("spot_id") or not spot.get("source"):
                            raise SimDemoJobError(400, "빈 위치 계획이 없다")
                        world = self.transfer_world()
                        if world is None:
                            raise SimDemoJobError(409, "확정되지 않은 자재 기록이 있다")
                        if world.location_of.get(material) != spot["source"]:
                            raise SimDemoJobError(
                                409, f"{material}의 기록 위치가 {spot['source']}가 아니다"
                                     f"({world.location_of.get(material)}) — 다시 말해 주세요")
                        if any(m != material and where == spot["spot_id"]
                               for m, where in world.location_of.items()):
                            raise SimDemoJobError(409, "그 빈 위치를 다른 자재가 차지했다")
                    elif action in ("resume", "resume_preflight") and \
                            (objects.get(material) or {}).get("pallet"):
                        # 팔레트로 가던 이송의 재개 — 목적지는 체크포인트에 묶여 있다.
                        slot = None
                    elif action in ("return", "resume", "resume_preflight"):
                        assigned = record_slot(objects.get(material))
                        if slot_by_name(self.slots, assigned) is None:
                            raise SimDemoJobError(
                                409, "자재에 배정된 검증된 컨베이어 위치가 없습니다")
                        slot = assigned
                self.jobs_dir.mkdir(parents=True, exist_ok=True)
                report_path = self.jobs_dir / f"{job_id}.json"
                console_path = self.jobs_dir / f"{job_id}.console"
                spot_path = None
                if action == "spot":
                    spot_path = self.jobs_dir / f"{job_id}_spot.json"
                    spot_path.write_text(json.dumps(dict(spot), ensure_ascii=False, indent=1),
                                         encoding="utf-8")
                argv = [str(self.script),
                        *build_argv(action, spec, checkpoint_id, slot,
                                    spot_path=None if spot_path is None else str(spot_path),
                                    source=(spot or {}).get("source")),
                        "--out", str(report_path)]
                # 속도는 시작할 때 한 번 정한다 — 실행 중에 설정을 바꿔도 이 작업은 그대로다.
                if action not in CELL_ACTIONS:
                    if getattr(self, "motion_error", None):
                        raise SimDemoJobError(503, self.motion_error)
                    if self.motion is not None:
                        from core.policy import PolicyError
                        try:
                            speed_percent = (self.motion.snapshot() if speed_percent is None
                                             else self.motion.policy.execution_percent(speed_percent))
                        except PolicyError as exc:
                            raise SimDemoJobError(409, str(exc)) from exc
                    elif speed_percent is not None:
                        raise SimDemoJobError(503, "이동 속도 정책이 없습니다")
                if speed_percent is not None:
                    argv += ["--speed-percent", str(speed_percent)]
                job = {"job_id": job_id, "action": action,
                       "action_label": ACTION_LABELS[action], "material": material,
                       "goal_id": goal_id,
                       "checkpoint_id": checkpoint_id, "slot": slot,
                       "spot": None if spot is None else {
                           k: spot.get(k) for k in ("spot_id", "surface_id", "center_m",
                                                    "source")},
                       "slot_label": (None if not slot else " → ".join(
                           slot_label(part) for part in str(slot).split(">"))),
                       "argv": argv,
                       "report_path": str(report_path),
                       "console_path": str(console_path),
                       "started_at": self._clock(), "status": "running",
                       "exit_code": None, "report": None, "is_simulated": True,
                       "stop_requested": None, "speed_percent": speed_percent}
                if action not in CELL_ACTIONS:
                    # Close the crash window before spawn.  Until the real PID
                    # identity replaces this launch marker, restart recovery is
                    # intentionally uncertain and therefore fail-closed.
                    self._atomic_write_journal(
                        self._journal_record(job, {"pid": None}))
                    job["journaled"] = True
                env = dict(self._environ, FORSTICK2_SIM_PICK_PLACE_DEMO="1")
                with open(console_path, "w", encoding="utf-8") as console:
                    proc = self._popen(argv, stdout=console, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, env=env,
                                       cwd=str(ROOT), start_new_session=True)
            except BaseException:
                # A goal child is running under its parent's lease.  Failed
                # child startup must leave that lease for the goal runner's one
                # release_goal() cleanup.
                if lease is not None:
                    self.cell_execution.release(lease)
                if job is not None and job.get("journaled"):
                    self._clear_journal(job_id)
                raise
            if action not in CELL_ACTIONS:
                pid = getattr(proc, "pid", None)
                if isinstance(pid, int) and pid > 0:
                    try:
                        identity = self._process_identity(pid)
                        identity["job_marker"] = str(report_path)
                        self._atomic_write_journal(self._journal_record(job, identity))
                        job["journaled"] = True
                    except (OSError, ValueError, TypeError) as exc:
                        self._set_recovery_required(
                            f"실행 중인 작업 기록을 저장할 수 없다: {type(exc).__name__}",
                            job_id=job_id)
                        # A malformed identity is deliberately persisted when
                        # possible: after another restart it yields an uncertain
                        # probe and therefore keeps motion fail-closed.
                        try:
                            self._atomic_write_journal(
                                self._journal_record(job, {"pid": pid}))
                            job["journaled"] = True
                        except OSError:
                            pass
                else:
                    # Lightweight test doubles have no OS process identity.
                    # Production Popen instances always have a positive pid.
                    self._clear_journal(job_id)
                    job.pop("journaled", None)
            self._jobs[job_id] = job
            self._procs[job_id] = proc
            if lease is not None:
                self._leases[job_id] = lease
            self._current = job_id
            # Popen.wait() is performed away from request threads.  The lease stays
            # live for the full subprocess lifetime and is released at its exit.
            watcher = threading.Thread(
                target=self._watch_process, args=(job_id, proc),
                name=f"sim-demo-{job_id}", daemon=True)
            watcher.start()
            return dict(job)

    # ── 기록↔관측 정합 ──────────────────────────────────────────────────
    @staticmethod
    def reconcile_needed(status: Mapping[str, Any]) -> bool:
        """맞출 기록이 남아 있는가. **판정은 스크립트가 관측으로 한다.**

        여기서는 "확인할 거리가 있는지"만 본다 — 기록이 없으면 Gazebo를 다시
        띄웠든 아니든 어긋날 것이 없다.
        """
        if status.get("available") is not True:
            return False
        return bool(status.get("objects") or status.get("checkpoints"))

    def start_reconcile_if_needed(self) -> dict:
        """기록이 남아 있으면 정합 작업을 **한 번** 띄운다(읽기 전용).

        서버 기동 시 부른다. Gazebo를 다시 띄우면 world는 초기 자리로 돌아가지만
        기록 파일은 남기 때문이다. 기록이 없으면 아무것도 하지 않는다.
        """
        status = self.state.status()
        if not self.reconcile_needed(status):
            return {"started": False, "detail": "맞출 기록이 없다"}
        try:
            job = self.start("reconcile")
        except SimDemoJobError as exc:
            return {"started": False, "detail": str(exc)}
        except OSError as exc:  # 스크립트를 띄우지 못해도 서버는 뜬다
            return {"started": False, "detail": f"{type(exc).__name__}: {exc}"}
        return {"started": True, "job_id": job["job_id"]}

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

    def _write_stop_request(self, job_id: str, reason: str) -> dict:
        request = {"request_id": f"simstopreq_{uuid.uuid4().hex[:12]}",
                   "requested_at": self._clock(), "reason": reason, "job_id": job_id}
        self.stop_request.parent.mkdir(parents=True, exist_ok=True)
        # 쓰는 쪽이 여럿일 수 있다(요청 경로·재전송 스레드) — 임시 파일 이름을 겹치지 않게 한다.
        temp = self.stop_request.with_suffix(f".{request['request_id']}.tmp")
        temp.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.stop_request)
        return request

    def request_stop(self, *, reason: str) -> dict:
        """실행 중인 시연 작업에 정지를 **요청**한다. 프로세스를 죽이지 않는다.

        실행기는 **자기 시작 시각 뒤에 쓰인** 요청만 인정한다(오래된 요청 무시). 작업을 띄운
        직후(실측 약 0.26 s) 쓴 요청은 버려지므로, 실행기가 기준 시각을 정한 것(콘솔
        `EXECUTOR_ARMED_MARK`)이 보일 때까지 다시 쓰고, 보인 뒤 한 번 더 쓴다
        (2026-10-02 독립 검증 MAJOR-1, 시연 경로 실측 재현).
        """
        running = self.running()
        if running is None:
            return {"requested": False, "detail": "실행 중인 시연 작업이 없다"}
        request = self._write_stop_request(running["job_id"], reason)
        with self._lock:
            self._jobs[running["job_id"]]["stop_requested"] = request
        if not _executor_armed(running):
            threading.Thread(target=self._reassert_stop, args=(running["job_id"], reason),
                             daemon=True, name=f"stop-{running['job_id']}").start()
        return {"requested": True, **request}

    def _reassert_stop(self, job_id: str, reason: str) -> None:
        while True:
            time.sleep(STOP_REASSERT_SEC)
            with self._lock:
                job = dict(self._refresh(job_id)) if job_id in self._jobs else None
            if job is None or job["status"] != "running":
                return
            armed = _executor_armed(job)
            request = self._write_stop_request(job_id, reason)
            with self._lock:
                self._jobs[job_id]["stop_requested"] = request
            if armed:
                return

    # ── 상태 ────────────────────────────────────────────────────────────
    def status(self) -> dict:
        state = self.state.status()
        running = self.running()
        with self._lock:
            recent = sorted(self._jobs.values(), key=lambda j: j["started_at"],
                            reverse=True)[:5]
            recent = [{k: j[k] for k in ("job_id", "action", "action_label",
                                         "material", "slot", "slot_label",
                                         "status", "exit_code",
                                         "started_at")}
                      | {"result_status": (j.get("report") or {}).get("status")}
                      for j in recent]
        objects = state.get("objects") or {}
        materials = []
        for model, spec in self.materials.items():
            actions = available_actions(state, model, self.slots)
            if running is not None:
                actions = {k: False for k in actions}
            elif self.recovery_required is not None:
                actions = {k: False for k in actions}
            record = objects.get(model)
            materials.append({**spec, "record": record, "actions": actions,
                              "slot": record_slot(record) if record else None,
                              "slot_label": (slot_label(record_slot(record))
                                             if record else None)})
        free = assign_slot(self.slots, objects) if self.slots else None
        conveyor = {
            "enabled": bool(self.slots),
            "slot_count": len(self.slots),
            "slots": occupancy_rows(self.slots, objects, self.materials),
            "next_slot": free.name if free else None,
            "next_slot_label": free.label if free else None,
            "full": bool(self.slots) and free is None,
        }
        return {"is_simulated": True, "real_hardware_ready": False,
                "real_hardware_verified": False, "state": state,
                "running_job": running, "recent_jobs": recent,
                "materials": materials, "action_labels": ACTION_LABELS,
                "conveyor": conveyor,
                "recovery_required": self.recovery_required,
                "reconcile": {"needed": self.reconcile_needed(state),
                              "available": running is None,
                              "last": state.get("last_reconcile")},
                "goal_reservation": self._goal_reservation}
