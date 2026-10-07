"""반복 작업 — 선택한 자재마다 '컨베이어로 이송 → 원래 자리 복귀'를 N회 (2026-10-07).

통합 작업 명령 패널의 '반복 작업'. 반복은 **백엔드가** 관리한다(브라우저 타이머 아님). 새로고침해도
`GET /v1/repeat-runs/current`로 같은 실행을 다시 보고, 실행 중에는 새로 시작하지 않는다.

지키는 것
- **일반 모드 검증·실행기를 그대로 쓴다.** 단계마다 그 순간의 상태로 작업 의도를 만들고
  `Api.create_plan`(계획·안전 관문·요청–계획 일치) → `Api.decide`(승인 기록) → `Api.execute`(실행 직전
  관문 재판정 → 이송 실행기의 계약 재판정·장면 동기화·사전 검사)를 화면 명령과 똑같이 지난다.
  해석(Qwen)만 건너뛴다 — 무엇을 어디로 옮길지는 사용자가 체크박스로 정했고 시작 전에 승인했다.
- **매 동작 직전에** 자재의 현재 위치(기록)·목적지(컨베이어 빈 칸·원래 자리 점유)·셀 상태(실행 중 작업·
  복구 필요·정지 래치)를 다시 본다. 예상과 다르면 움직이지 않고 이유를 남기고 멈춘다.
- **셀 예약**: 시작하면 셀을 이 반복에 예약한다(`CellExecutionManager.reserve`). 일반 실행·시연 작업·목표가
  모두 같은 임대를 쓰므로, 반복의 단계 사이 틈에도 다른 명령이 로봇을 쓰지 못한다.
- **정지**: 헤더 즉시 정지 → 진행 중 단계 중단, 남은 반복 취소. 단계 계획이 정지 래치를 풀지 않는다.
  **일시정지** → 이 반복의 진행 중 실행만 취소(실행기가 정지 지점을 남김). **재개**는 실행기 규칙 그대로
  (운반 중 정지 지점만 이어서 할 수 있다). 실패·관측 누락·연결 이상이면 멈추고 **자동 재시도하지 않는다.**
- **서버 재시작**: 진행 중이던 반복은 이어서 하지 않고 '중단(상태 확인 필요)'으로 남긴다. 그 표시만으로
  셀 잠금을 풀지 않는다 — 셀을 계속 예약해 두고(재시작 뒤 이어받은 실행이 끝난 뒤에도), 사람이 '상태 확인 후
  잠금 해제'(`verify`)를 누르면 실행 종료·로봇 정지·부착 상태를 관측으로 확인한 뒤에만 푼다(`server/cell_quiet.py`).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.task_intent import CONTEXT, REGISTRY, STATE, TRANSFER, Evidence, ResolvedRef, TaskIntent

MAX_COUNT = 100
PREVIEW_TTL_SEC = 300.0
ACTIVE = ("running", "pausing", "paused", "resuming")
TERMINAL = ("completed", "stopped", "failed", "cancelled", "interrupted")
CONVEYOR = "loc_conveyor"
PHASE_LABEL = {"transfer": "컨베이어로 이송", "return": "원래 자리 복귀"}
PHASE_ING = {"transfer": "이송 중", "return": "복귀 중"}
#: 재시작으로 중단된 반복이 확인 전까지 셀을 잡아 두는 예약의 주인(일반 실행 거부 메시지에 그대로 보인다).
INTERRUPTED_OWNER = "repeat_interrupted"
INTERRUPTED_REASON = ("서버가 다시 시작돼 반복을 이어서 하지 않습니다 — 작업 셀 잠금을 유지합니다. 반복 작업의"
                      " '상태 확인 후 잠금 해제'로 실행 종료·로봇 정지·부착 상태를 확인한 뒤 재개·복구하세요")


class RepeatRunError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


class RepeatRuns:
    """한 작업 셀의 반복 작업 하나(동시에 하나뿐)."""

    def __init__(self, *, api: Any, runtime: Any, path: Path, clock=time.time,
                 poll_sec: float = 1.0, quiet_check=None):
        self.api = api
        self.runtime = runtime
        self.path = Path(path)
        self._clock = clock
        self._poll = poll_sec
        self._lock = threading.RLock()
        self._run: dict | None = None
        self._previews: dict[str, dict] = {}
        self._reservation = None
        self._thread: threading.Thread | None = None
        self._quiet_check = quiet_check
        self._load()

    # ── 기록 ────────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        run = data.get("run") if isinstance(data, dict) else None
        if not isinstance(run, dict):
            return
        if run.get("state") in ACTIVE or (run.get("state") == "interrupted" and run.get("lock_held")):
            # 서버가 다시 떴다 — 이어서 하지 않는다. 중단 표시만으로 잠금을 풀지도 않는다: 재시작 전 실행기
            # 프로세스가 아직 로봇을 움직이고 있을 수 있다(이어받은 실행의 임대가 끝나도 이 예약이 남는다).
            if run.get("state") in ACTIVE:
                run["state"] = "interrupted"
                run["reason"] = INTERRUPTED_REASON
                run["check"] = None
            run["lock_held"] = True
            run["updated_at"] = self._clock()
            self._reservation = self.runtime.cell_execution.reserve(
                owner=INTERRUPTED_OWNER, operation_id=run.get("run_id") or "unknown", alongside_active=True)
            self._run = run
            self._run["label"] = self._label(self._run)
            self._save()
        else:
            self._run = run

    def _save(self) -> None:
        if self._run is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"schema": "forstick2.repeat_run/1", "run": self._run},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _update(self, **fields) -> None:
        with self._lock:
            if self._run is None:
                return
            self._run.update(fields, updated_at=self._clock())
            self._run["label"] = self._label(self._run)
            self._save()

    # ── 셀 사실 ─────────────────────────────────────────────────────────
    def _facts(self) -> dict:
        from server.plan_intent import cell_facts

        return cell_facts(self.runtime.resource_catalog, self.runtime.sim_demo_jobs)

    def _materials(self, facts: Mapping[str, Any]) -> list[dict]:
        return list(facts["materials"])           # 셀 설정 순서 = A → B → C

    def _cell_problems(self, facts: Mapping[str, Any], *, own_reservation: bool = False) -> list[str]:
        """셀 전체가 반복을 시작·계속할 수 있는가. 문제마다 사람이 읽는 이유."""
        out: list[str] = []
        jobs = self.runtime.sim_demo_jobs
        status = jobs.status() or {}
        if facts.get("state_error"):
            out.append(f"자재 상태를 확인할 수 없습니다({facts['state_error']})")
        if status.get("running_job"):
            out.append("다른 작업이 실행 중입니다")
        if status.get("recovery_required"):
            out.append("작업 셀 복구가 필요합니다")
        with self.api._flag_lock:
            stopped = self.api._stop_requested
            busy = bool(self.api._active_executions)
        if stopped:
            out.append("전체 정지가 걸려 있습니다 — 헤더의 '정지 해제' 뒤 시작하세요")
        if busy and not own_reservation:
            out.append("다른 실행이 진행 중입니다")
        cell = self.runtime.cell_execution
        held = self._run is not None and self._run.get("lock_held")
        if held:
            out.append("서버 재시작으로 중단된 반복 작업이 있습니다 — '상태 확인 후 잠금 해제'를 먼저 하세요")
        if not own_reservation and not held and (cell.current() is not None or cell.reservation() is not None):
            out.append("작업 셀이 다른 실행에 잡혀 있습니다")
        records = (status.get("state") or {}).get("objects") or {}
        names = {m["model"]: m["name"] for m in facts["materials"]}
        for model, row in records.items():
            if (row or {}).get("state") not in ("held_on_target", "on_pallet"):
                out.append(f"{names.get(model, model)}의 위치가 확정되지 않았습니다({(row or {}).get('state')})"
                           " — 시뮬레이션 보기에서 재개·복구로 정리하세요")
        return out

    # ── 미리보기·시작 ─────────────────────────────────────────────────────
    def preview(self, *, session_id: str, materials: Sequence[str], count: Any) -> dict:
        self.api.require_session(session_id)
        try:
            n = int(count)
        except (TypeError, ValueError):
            raise RepeatRunError(400, "반복 횟수는 양의 정수여야 합니다") from None
        if isinstance(count, bool) or str(count).strip() != str(n) or n < 1 or n > MAX_COUNT:
            raise RepeatRunError(400, f"반복 횟수는 1~{MAX_COUNT} 사이의 정수여야 합니다")
        facts = self._facts()
        known = self._materials(facts)
        wanted = {str(m) for m in materials or ()}
        chosen = [m for m in known if m["id"] in wanted or m["model"] in wanted]
        if not chosen or len(chosen) != len(wanted):
            raise RepeatRunError(400, "자재를 하나 이상 고르세요(등록된 자재만)")
        problems = self._cell_problems(facts)
        locations = {loc["id"]: loc["name"] for loc in facts["locations"]}
        for m in chosen:
            if m["location"] is None:
                problems.append(f"{m['name']}의 현재 위치를 확인할 수 없습니다")
            elif m["location"] != m["origin"]:
                problems.append(f"{m['name']}이(가) 원래 자리({locations.get(m['origin'], m['origin'])})가 아니라"
                                f" {locations.get(m['location'], m['location'])}에 있습니다 — 움직이지 않습니다."
                                " 먼저 원래 자리로 돌려놓은 뒤 시작하세요")
        if not facts.get("free_conveyor_slots"):
            problems.append("컨베이어에 빈 칸이 없습니다")
        sequence = [f"{m['name']} {PHASE_LABEL[p]}" for m in chosen for p in ("transfer", "return")]
        order = " → ".join(m["name"] for m in chosen)
        out = {"ok": not problems, "materials": [{"id": m["id"], "model": m["model"], "name": m["name"]}
                                                  for m in chosen],
               "count": n, "steps_per_round": len(sequence), "total_steps": len(sequence) * n,
               "sequence": sequence,
               "summary": f"[{' → '.join(sequence)}] × {n}회 (총 {len(sequence) * n}동작, 순서 {order})",
               "problems": problems}
        if not problems:
            token = uuid.uuid4().hex
            with self._lock:
                now = self._clock()
                self._previews = {k: v for k, v in self._previews.items()
                                  if now - v["at"] < PREVIEW_TTL_SEC}
                self._previews[token] = {"at": now, "session_id": session_id,
                                         "materials": [m["id"] for m in chosen], "count": n}
            out["token"] = token
        return out

    def start(self, *, session_id: str, token: str) -> dict | None:
        """미리보기에서 사용자가 승인한 내용 그대로 시작한다(토큰은 한 번만 쓴다)."""
        self.api.require_session(session_id)
        with self._lock:
            spec = self._previews.pop(token or "", None)
            if spec is None or spec["session_id"] != session_id \
                    or self._clock() - spec["at"] >= PREVIEW_TTL_SEC:
                raise RepeatRunError(409, "승인한 반복 내용을 찾을 수 없습니다(만료·이미 시작) — 다시 확인해 주세요")
            if self._run is not None and self._run.get("state") in ACTIVE:
                raise RepeatRunError(409, "이미 반복 작업이 진행 중입니다 — 중복으로 시작하지 않습니다")
            facts = self._facts()
            problems = self._cell_problems(facts)
            chosen = [m for m in self._materials(facts) if m["id"] in spec["materials"]]
            problems += [f"{m['name']}이(가) 원래 자리에 있지 않습니다" for m in chosen
                         if m["location"] != m["origin"]]
            if problems:
                raise RepeatRunError(409, " · ".join(problems))
            run_id = f"rep_{uuid.uuid4().hex[:10]}"
            reservation = self.runtime.cell_execution.reserve(owner="repeat_run", operation_id=run_id)
            if reservation is None:
                raise RepeatRunError(409, "작업 셀을 예약하지 못했습니다 — 다른 실행이 진행 중입니다")
            self._reservation = reservation
            steps = [{"round": r, "material": m["id"], "model": m["model"], "name": m["name"], "phase": p}
                     for r in range(1, spec["count"] + 1) for m in chosen for p in ("transfer", "return")]
            now = self._clock()
            self._run = {"run_id": run_id, "session_id": session_id, "state": "running",
                         "materials": [{"id": m["id"], "model": m["model"], "name": m["name"]} for m in chosen],
                         "count": spec["count"], "steps": steps, "cursor": 0,
                         "finish_after_round": False, "pause_requested": False,
                         "approved_at": now, "started_at": now, "updated_at": now,
                         "history": [], "reason": None}
            self._run["label"] = self._label(self._run)
            self._save()
            self._spawn()
            return self.view()

    def _spawn(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="repeat-run", daemon=True)
        self._thread.start()

    # ── 조회·제어 ─────────────────────────────────────────────────────────
    def view(self) -> dict | None:
        with self._lock:
            if self._run is None:
                return None
            run = dict(self._run)
        run.pop("session_id", None)
        steps = run.pop("steps")
        cursor = int(run["cursor"])
        per_round = len(run["materials"]) * 2
        run["total_steps"] = len(steps)
        run["done_steps"] = min(cursor, len(steps))
        run["round"] = min(cursor // per_round + 1, run["count"]) if per_round else 0
        run["current"] = steps[cursor] if cursor < len(steps) else None
        run["active"] = run["state"] in ACTIVE
        run["history"] = run["history"][-20:]
        return run

    def _label(self, run: Mapping[str, Any]) -> str:
        steps = run["steps"]
        cursor = int(run["cursor"])
        per_round = len(run["materials"]) * 2
        state = run["state"]
        if cursor >= len(steps) or state in TERMINAL:
            done_rounds = cursor // per_round if per_round else 0
            word = {"completed": "완료", "stopped": "정지됨", "failed": "실패", "cancelled": "취소됨",
                    "interrupted": "중단됨(확인 필요)"}.get(state, state)
            return f"{done_rounds}/{run['count']}회 · {word}"
        step = steps[cursor]
        doing = PHASE_ING[step["phase"]]
        if state == "paused":
            doing = f"{PHASE_LABEL[step['phase']]} 일시정지"
        elif state == "pausing":
            doing = f"{PHASE_ING[step['phase']]}(일시정지 요청됨)"
        return f"{step['round']}/{run['count']}회 · {step['name']} {doing}"

    def _require(self, run_id: str) -> dict:
        if self._run is None or self._run.get("run_id") != run_id:
            raise RepeatRunError(404, "그 반복 작업이 없습니다")
        return self._run

    def finish_after_round(self, run_id: str) -> dict | None:
        with self._lock:
            run = self._require(run_id)
            if run["state"] not in ACTIVE:
                raise RepeatRunError(409, "진행 중인 반복이 아닙니다")
            self._update(finish_after_round=True)
        return self.view()

    def pause(self, run_id: str) -> dict | None:
        with self._lock:
            run = self._require(run_id)
            if run["state"] != "running":
                raise RepeatRunError(409, "실행 중인 반복이 아닙니다")
            self._update(pause_requested=True, state="pausing")
            session_id = run["session_id"]
        # 진행 중인 단계가 있으면 그 실행만 취소한다(실행기가 정지 지점을 남긴다). 단계 사이면 다음 단계 전에 멈춘다.
        self.api.cancel_active_execution(session_id=session_id)
        return self.view()

    def resume(self, run_id: str) -> dict | None:
        with self._lock:
            run = self._require(run_id)
            if run["state"] != "paused":
                raise RepeatRunError(409, "일시정지된 반복이 아닙니다")
            step = run["steps"][run["cursor"]]
            facts = self._facts()
            row = next((m for m in self.runtime.sim_demo_jobs.status().get("materials") or []
                        if m.get("model") == step["model"]), {})
            record = row.get("record") or {}
            if record and record.get("state") not in ("held_on_target", "on_pallet"):
                if not (row.get("actions") or {}).get("resume"):
                    raise RepeatRunError(409, f"{step['name']}이(가) 이어서 할 수 없는 지점에서 멈췄습니다"
                                              " — 반복을 취소한 뒤 시뮬레이션 보기에서 복구하세요")
                self._update(state="resuming", pause_requested=False)
                threading.Thread(target=self._resume_then_loop, args=(step,), name="repeat-resume",
                                 daemon=True).start()
                return self.view()
            del facts
            self._update(state="running", pause_requested=False)
            self._spawn()
        return self.view()

    def cancel(self, run_id: str) -> dict | None:
        with self._lock:
            run = self._require(run_id)
            if run["state"] not in ("paused",):
                raise RepeatRunError(409, "일시정지된 반복만 취소할 수 있습니다 — 실행 중이면 일시정지 또는 헤더의 즉시 정지를 쓰세요")
            self._finish("cancelled", "사용자가 반복을 취소했습니다")
        return self.view()

    def verify(self, run_id: str) -> dict | None:
        """중단된 반복의 잠금 해제: 실행 종료·로봇 정지·부착 상태가 관측으로 확인될 때만 예약을 푼다."""
        with self._lock:
            run = self._require(run_id)
            if not run.get("lock_held"):
                raise RepeatRunError(409, "잠금이 걸린 중단 반복이 아닙니다")
        check = self._quiet_check or self._default_quiet_check
        result = check()
        with self._lock:
            if result["ok"]:
                if self._reservation is not None:
                    self.runtime.cell_execution.unreserve(self._reservation)
                    self._reservation = None
                self._update(lock_held=False, check=result,
                             reason="상태 확인 완료 — 잠금을 풀었습니다. 필요하면 시뮬레이션 보기에서 재개·복구한 뒤"
                                    " 반복을 새로 시작하세요(이어서 하지 않습니다)")
            else:
                self._update(check=result)
        return self.view()

    def _default_quiet_check(self) -> dict:
        from server.cell_quiet import check_cell_quiet

        return check_cell_quiet(self.runtime, self.api)

    def _advance(self, **fields) -> None:
        with self._lock:
            if self._run is not None:
                self._update(cursor=int(self._run["cursor"]) + 1, **fields)

    def on_global_stop(self) -> None:
        """헤더 즉시 정지. 진행 중 단계는 실행 결과(exec.stopped)로 멈춘다. 일시정지 중이면 여기서 끝낸다."""
        with self._lock:
            if self._run is not None and self._run.get("state") in ("paused", "pausing"):
                self._finish("stopped", "헤더의 즉시 정지로 남은 반복을 취소했습니다")

    # ── 실행 ────────────────────────────────────────────────────────────
    def _finish(self, state: str, reason: str | None) -> None:
        with self._lock:
            self._update(state=state, reason=reason)
            if self._reservation is not None:
                self.runtime.cell_execution.unreserve(self._reservation)
                self._reservation = None

    def _note(self, step: Mapping[str, Any], outcome: str, **extra) -> None:
        with self._lock:
            if self._run is None:
                return
            self._run["history"].append({"round": step["round"], "material": step["name"],
                                         "phase": step["phase"], "outcome": outcome,
                                         "at": self._clock(), **extra})
            self._save()

    def _loop(self) -> None:
        try:
            while True:
                with self._lock:
                    run = self._run
                    if run is None or run["state"] not in ("running", "pausing"):
                        return
                    cursor = run["cursor"]
                    steps = run["steps"]
                    per_round = len(run["materials"]) * 2
                    if cursor >= len(steps):
                        self._finish("completed", None)
                        return
                    if cursor and cursor % per_round == 0 and run["finish_after_round"]:
                        self._finish("completed", f"현재 회차 후 종료 — {cursor // per_round}/{run['count']}회 완료")
                        return
                    if run["pause_requested"]:
                        self._update(state="paused")
                        return
                    step = steps[cursor]
                outcome = self._do_step(step)
                if outcome == "done":
                    self._advance()
                    continue
                return
        except Exception as exc:  # noqa: BLE001 — 실행기 오류는 실패로 멈춘다(재시도 없음)
            self._finish("failed", f"반복 실행기 오류: {type(exc).__name__}: {exc}"[:300])

    def _do_step(self, step: Mapping[str, Any]) -> str:
        from server.api import ApiError

        api = self.api
        with self._lock:
            run = dict(self._run or {})
        with api._flag_lock:
            stopped = api._stop_requested
        if stopped:
            self._note(step, "stopped")
            self._finish("stopped", "헤더의 즉시 정지로 남은 반복을 취소했습니다")
            return "stopped"
        facts = self._facts()
        problems = self._cell_problems(facts, own_reservation=True)
        material = next((m for m in facts["materials"] if m["id"] == step["material"]), None)
        locations = {loc["id"]: loc["name"] for loc in facts["locations"]}
        if material is None:
            problems.append(f"{step['name']}이(가) 셀 설정에 없습니다")
        else:
            here, home = material["location"], material["origin"]
            if step["phase"] == "transfer" and here != home:
                problems.append(f"{step['name']}이(가) 원래 자리가 아니라 {locations.get(here, here or '알 수 없는 곳')}에"
                                " 있습니다 — 움직이지 않습니다")
            if step["phase"] == "return" and here != CONVEYOR:
                problems.append(f"{step['name']}이(가) 컨베이어가 아니라 {locations.get(here, here or '알 수 없는 곳')}에"
                                " 있습니다 — 움직이지 않습니다")
            if step["phase"] == "transfer" and not facts.get("free_conveyor_slots"):
                problems.append("컨베이어에 빈 칸이 없습니다")
            if step["phase"] == "return":
                holder = next((m for m in facts["materials"] if m["id"] != material["id"]
                               and m["location"] == home), None)
                if holder is not None:
                    problems.append(f"원래 자리({locations.get(home, home)})에 {holder['name']}이(가) 있습니다")
        if problems:
            self._note(step, "blocked", reason=" · ".join(problems))
            self._finish("failed", "동작 직전 확인에서 멈췄습니다: " + " · ".join(problems))
            return "failed"
        assert material is not None
        name, rid = material["name"], material["id"]
        source = ResolvedRef(material["location"], Evidence(STATE, f"{name}의 현재 위치(기록)"))
        if step["phase"] == "transfer":
            destination = ResolvedRef(CONVEYOR, Evidence(
                STATE, f"컨베이어 빈 칸 {len(facts['free_conveyor_slots'])}개(기록, 칸은 실행 전 검사가 고른다)"))
        else:
            destination = ResolvedRef(material["origin"], Evidence(
                REGISTRY, f"{name}의 원래 자리(등록: {locations.get(material['origin'], material['origin'])})"))
        intent = TaskIntent(action=TRANSFER,
                            material=ResolvedRef(rid, Evidence(CONTEXT, f"반복 작업 {run['run_id']} 승인: {name}")),
                            source=source, destination=destination, interpreter="repeat-run")
        session_id = run["session_id"]
        text = (f"[반복 작업 {step['round']}/{run['count']}회] {name} {PHASE_LABEL[step['phase']]}")
        try:
            plan = api.create_plan(session_id=session_id, utterance=text, intent=intent, keep_stop_latch=True)
            if not plan.get("ok") or not plan.get("executable"):
                gate = plan.get("validation") or {}
                why = plan.get("detail") or gate.get("detail") or plan.get("reason_code") or "관문이 실행을 허가하지 않았습니다"
                self._note(step, "blocked", reason=why, request_id=plan.get("request_id"))
                self._finish("failed", f"{text}: 안전 검증에서 멈췄습니다 — {why}")
                return "failed"
            p = plan["plan"]
            decision = api.decide(session_id=session_id, request_id=plan["request_id"], plan_id=p["plan_id"],
                                  plan_hash=p["plan_hash"], approve=True,
                                  note=f"반복 작업 {run['run_id']}: 시작 승인에 따른 단계 승인 ({step['round']}/{run['count']}회)")
            result = api.execute(session_id=session_id, request_id=plan["request_id"], plan_id=p["plan_id"],
                                 approval_id=decision.get("approval_id"),
                                 reservation=self._reservation.token if self._reservation else None)
        except ApiError as exc:
            self._note(step, "error", reason=exc.message)
            self._finish("failed", f"{text}: {exc.message}")
            return "failed"
        final = result.get("final") or {}
        execution_id = result.get("execution_id")
        if result.get("ok") and final.get("task_succeeded") is True:
            self._note(step, "done", execution_id=execution_id)
            return "done"
        interrupted = result.get("interrupted")
        with self._lock:
            pause_requested = bool(self._run and self._run.get("pause_requested"))
        if interrupted == "exec.canceled" and pause_requested:
            self._note(step, "paused", execution_id=execution_id)
            self._update(state="paused")
            return "paused"
        if interrupted == "exec.stopped":
            self._note(step, "stopped", execution_id=execution_id)
            self._finish("stopped", f"{text}: 헤더의 즉시 정지로 멈췄습니다 — 남은 반복을 취소했습니다")
            return "stopped"
        why = (final.get("evidence") or {}).get("detail") if isinstance(final.get("evidence"), dict) else None
        why = why or final.get("reason_code") or interrupted or "실행 결과를 확인하지 못했습니다"
        self._note(step, "failed", execution_id=execution_id, reason=why)
        self._finish("failed", f"{text}: {why} — 자동으로 다시 시도하지 않습니다")
        return "failed"

    def _resume_then_loop(self, step: Mapping[str, Any]) -> None:
        """일시정지 지점(체크포인트)에서 이송을 이어서 마친 뒤 반복을 계속한다."""
        jobs = self.runtime.sim_demo_jobs
        goal = f"repeat_resume_{uuid.uuid4().hex[:8]}"
        try:
            checkpoint = ((jobs.status().get("state") or {}).get("checkpoint") or {})
            if checkpoint.get("model") != step["model"]:
                self._finish("failed", "재개할 정지 지점을 찾지 못했습니다")
                return
            if not jobs.reserve_goal(goal, owner="repeat_run",
                                     reservation=self._reservation.token if self._reservation else None):
                self._finish("failed", "재개용으로 작업 셀을 잡지 못했습니다")
                return
            try:
                job = jobs.start("resume", step["model"], checkpoint_id=checkpoint.get("checkpoint_id"), goal_id=goal)
                while True:
                    current = jobs.job(job["job_id"], console_lines=1)
                    if current.get("status") != "running":
                        break
                    time.sleep(self._poll)
            finally:
                jobs.release_goal(goal)
            report = current.get("report") or {}
            facts = self._facts()
            material = next((m for m in facts["materials"] if m["id"] == step["material"]), None)
            reached = material is not None and material["location"] == (
                CONVEYOR if step["phase"] == "transfer" else material["origin"])
            if current.get("exit_code") != 0 or not reached:
                self._note(step, "failed", reason=f"재개 결과 {report.get('status')}")
                self._finish("failed", f"재개가 끝나지 않았습니다({report.get('status')}) — 자동으로 다시 시도하지 않습니다")
                return
            self._note(step, "done", resumed=True, job_id=job["job_id"])
            self._advance(state="running")
            self._loop()
        except Exception as exc:  # noqa: BLE001
            self._finish("failed", f"재개 오류: {type(exc).__name__}: {exc}"[:300])
