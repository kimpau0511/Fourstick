"""Constrained, goal-centric orchestration for the Gazebo sim demo.

Two goals exist:

- ``return_all_to_origin``: every conveyor material back to its origin pallet.
- ``arrange``: a declarative target arrangement (material -> origin | slot |
  any free slot) plus environment constraints.  ``server/sim_demo_arrangement``
  derives the job order from the durable state and explains every step.

Both emit only ordinary ``return``/``transfer`` jobs.  Neither accepts steps,
skills, coordinates, or arbitrary robot actions from a caller.
"""

from __future__ import annotations

import threading
import time
import uuid
import re
from typing import Any, Callable, Mapping

from core.transfer_skill import path_obstructions

from server.sim_demo_arrangement import (
    GOAL_ARRANGE,
    apply_environment,
    empty_environment,
    ORIGIN,
    ArrangementError,
    ArrangementRequest,
    current_locations,
    plan_arrangement,
)
from validation.conveyor_slots import record_slot, slot_label
from validation.simulation_demo_state import HELD_ON_TARGET, ON_PALLET


GOAL_RETURN_ALL = "return_all_to_origin"
ACTIVE = ("planned", "running", "stopping")
FINAL = ("completed", "failed", "stopped", "cancelled")


def interpret_goal_command(text: str, *, mentions_conveyor: bool) -> dict | None:
    """Recognize only the fixed, collective conveyor-return goal.

    ``None`` means this is not a collective goal command and the existing
    single-material command router may inspect it. Goal-like but incomplete
    commands never fall through to free-form planning.
    """
    normalized = re.sub(r"\s+", "", str(text or "")).lower()
    all_materials = "자재" in normalized and any(
        word in normalized for word in ("모두", "전부", "모든", "전체")
    )
    if not all_materials:
        return None

    origin = any(word in normalized for word in (
        "제자리", "원래자리", "원래위치", "원위치", "본래자리", "본래위치",
    ))
    acting = any(word in normalized for word in (
        "가져다", "갖다", "돌려", "되돌", "복귀", "옮겨", "옮기", "놓아", "놔",
    ))
    if mentions_conveyor and origin and acting:
        return {"decision": "CONFIRM", "goal": GOAL_RETURN_ALL}
    destination_words = ("검사대", "작업대", "팔레트", "안전위치", "저쪽", "이쪽", "그쪽")
    if acting and any(word in normalized for word in destination_words):
        return {
            "decision": "BLOCK",
            "reason": ("지원하지 않는 전체 자재 목표입니다 — 지원하는 목표는 "
                       "컨베이어의 모든 자재를 원래 자리로 복귀하는 것입니다"),
        }
    return {
        "decision": "ASK",
        "reason": ("전체 복귀 목표인지 확인할 수 없습니다 — “컨베이어에 있는 자재를 "
                   "모두 원래 자리로 돌려놔”처럼 말해 주세요"),
    }


class SimDemoGoalError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class SimDemoGoals:
    """Plan, confirm, and execute the fixed return-all goal one job at a time."""

    def __init__(self, jobs, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 poll_sec: float = 0.1, auto_run: bool = True,
                 environment_path=None):
        self.jobs = jobs
        from pathlib import Path
        from core.paths import workcell_log_dir as _workcell_log_dir
        state_path = getattr(getattr(jobs, "state", None), "path", None)
        self._environment_path = Path(
            environment_path if environment_path is not None else
            (Path(state_path).with_name("sim_demo_environment.json") if state_path
             else _workcell_log_dir() / "sim_demo_environment.json"))
        self._clock = clock
        self._sleep = sleep
        self._poll_sec = poll_sec
        self._auto_run = auto_run
        self._lock = threading.RLock()
        self._goals: dict[str, dict] = {}
        self._current: str | None = None

    def _authoritative_plan(self) -> tuple[list[dict], dict]:
        state = self.jobs.state.status()
        if state.get("available") is not True:
            raise SimDemoGoalError(409, state.get("detail") or
                                   "시연 현재 상태를 읽을 수 없다")
        objects = dict(state.get("objects") or {})
        uncertain = sorted(model for model, row in objects.items()
                           if row.get("state") not in (HELD_ON_TARGET, ON_PALLET))
        if uncertain:
            raise SimDemoGoalError(
                409, "컨베이어 여부가 확정되지 않은 자재가 있어 복귀 목표를 "
                "계획하지 않는다: " + ", ".join(uncertain))
        unknown = sorted(set(objects) - set(self.jobs.materials))
        if unknown:
            raise SimDemoGoalError(409, "셀 선언에 없는 자재 기록이 있다: "
                                   + ", ".join(unknown))

        slot_order = {slot.name: no for no, slot in enumerate(self.jobs.slots)}
        models = sorted(objects, key=lambda model: (
            slot_order.get(record_slot(objects[model]), len(slot_order)), model))
        models = self._return_order(models, objects)
        plan = []
        for no, model in enumerate(models, 1):
            spec = self.jobs.materials[model]
            slot = record_slot(objects[model])
            pallet = objects[model].get("pallet") if objects[model].get(
                "state") == ON_PALLET else None
            plan.append({
                "step": no, "action": "return", "material": model,
                "material_label": spec.get("korean") or model,
                "from": pallet or slot or "loc_conveyor",
                "from_label": (f"{str(pallet).rsplit('_', 1)[-1]}번 팔레트" if pallet
                               else slot_label(slot) if slot else "컨베이어"),
                "to": spec.get("resource_id") or spec.get("support_model"),
                "to_label": "원래 자리",
                "status": "pending", "job_id": None, "result": None,
            })
        return plan, state

    def _return_order(self, models: list[str], objects: Mapping[str, Any]) -> list[str]:
        """복귀 순서: 측정된 경로가 **아직 남은 자재가 있는 자리**를 스치지 않는 것부터.

        정한 순서(칸 번호)를 기본으로 하고, 막힌 자재는 앞을 막는 자재가 빠질 때까지
        미룬다. 어느 것도 못 가면 계획하지 않는다(실행 중 차단보다 먼저 알린다).
        """
        capability = self.jobs._capability()

        def where(model: str) -> str | None:
            row = objects.get(model) or {}
            if row.get("state") == ON_PALLET:
                return row.get("pallet")
            return record_slot(row)

        away = {m: where(m) for m in models}
        home = {m: capability.origin_of(m) for m in self.jobs.materials}
        occupied = {home[m]: m for m in self.jobs.materials if m not in away}
        occupied.update({loc: m for m, loc in away.items() if loc})
        order: list[str] = []
        left = list(models)
        while left:
            for model in left:
                source = away[model]
                if source is None:
                    break
                cross = path_obstructions(capability, model, source, home[model])
                if not cross or not any(occupied.get(loc) not in (None, model)
                                        for loc in cross):
                    break
            else:
                raise SimDemoGoalError(
                    409, "복귀 경로가 서로 막혀 순서를 정할 수 없다(측정한 경로 표본): "
                    + ", ".join(left))
            left.remove(model)
            order.append(model)
            occupied.pop(away[model], None)
            occupied[home[model]] = model
        return order

    # ── 작업 환경(지속) ─────────────────────────────────────────────
    def environment(self) -> dict:
        """사용자가 알려 준 작업 환경. 없으면 빈 환경이다."""
        import json
        try:
            data = json.loads(self._environment_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty_environment()
        env = empty_environment()
        slot_names = {slot.name for slot in self.jobs.slots}
        env["blocked_slots"] = [s for s in data.get("blocked_slots") or ()
                                if s in slot_names]
        env["unavailable_materials"] = [m for m in data.get("unavailable_materials") or ()
                                        if m in self.jobs.materials]
        env["updated_at"] = data.get("updated_at")
        return env

    def set_environment(self, env: Mapping[str, Any]) -> dict:
        import json
        import os
        slot_names = {slot.name for slot in self.jobs.slots}
        blocked = [str(s) for s in env.get("blocked_slots") or ()]
        unavailable = [str(m) for m in env.get("unavailable_materials") or ()]
        unknown = ([s for s in blocked if s not in slot_names]
                   + [m for m in unavailable if m not in self.jobs.materials])
        if unknown:
            raise SimDemoGoalError(400, "셀에 없는 칸·자재다: " + ", ".join(unknown))
        record = {"blocked_slots": list(dict.fromkeys(blocked)),
                  "unavailable_materials": list(dict.fromkeys(unavailable)),
                  "updated_at": self._clock()}
        self._environment_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._environment_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._environment_path)
        return self.environment()

    def update_environment(self, request: ArrangementRequest) -> dict:
        """요청의 환경 정보를 지속 환경에 반영한다. 로봇을 움직이지 않는다."""
        with self._lock:
            return self.set_environment(apply_environment(self.environment(), request))

    def _route_ok(self, material: str, here: str, there: str) -> tuple[bool, str]:
        """계획기용: 경로·자세만(로봇 Capability). 원래 자리는 자재의 원래 팔레트다."""
        from core.transfer_skill import route_supported

        capability = self.jobs._capability()
        origin = capability.origin_of(material)
        return route_supported(capability, material,
                               origin if here == ORIGIN else here,
                               origin if there == ORIGIN else there)

    def _route_path(self, material: str, here: str, there: str) -> frozenset[str] | None:
        """계획기용: 측정된 경로가 스치는 자리(로봇 Capability). 모르면 None."""
        from core.transfer_skill import path_obstructions

        capability = self.jobs._capability()
        origin = capability.origin_of(material)
        return path_obstructions(capability, material,
                                 origin if here == ORIGIN else here,
                                 origin if there == ORIGIN else there)

    def _origins(self) -> dict[str, str]:
        capability = self.jobs._capability()
        return {m: rid for m in self.jobs.materials
                if (rid := capability.origin_of(m))}

    def _start_step(self, step: Mapping[str, Any], goal_id: str) -> dict:
        """계획 단계 → 공통 `transfer(자재, 출발, 도착)`. 실행 시점 상태로 다시 검사한다."""
        capability = self.jobs._capability()
        material = step["material"]
        origin = capability.origin_of(material)
        # 계획의 ORIGIN은 자재의 원래 팔레트다. 나머지는 칸·다른 팔레트 id 그대로.
        action = step.get("action", "return")
        source = origin if step["from"] == ORIGIN or action == "transfer" else step["from"]
        destination = origin if step["to"] == ORIGIN or action == "return" else step["to"]
        env = self.environment()
        return self.jobs.start_transfer(
            material, source, destination, goal_id=goal_id,
            blocked=tuple(env.get("blocked_slots") or ()),
            unavailable=tuple(env.get("unavailable_materials") or ()))

    def _arrangement_plan(self, request: ArrangementRequest) -> tuple[dict, dict]:
        state = self.jobs.state.status()
        if state.get("available") is not True:
            raise SimDemoGoalError(409, state.get("detail") or
                                   "시연 현재 상태를 읽을 수 없다")
        try:
            planned = plan_arrangement(
                request, materials=self.jobs.materials,
                slot_names=[slot.name for slot in self.jobs.slots],
                objects=dict(state.get("objects") or {}),
                environment=self.environment(), route_ok=self._route_ok,
                origins=self._origins(), route_path=self._route_path)
        except ArrangementError as exc:
            raise SimDemoGoalError(409 if exc.decision == "BLOCK" else 400,
                                   str(exc)) from exc
        return planned, state

    def _plan_for(self, goal_type: str, request: ArrangementRequest | None):
        if goal_type == GOAL_ARRANGE:
            planned, state = self._arrangement_plan(request)
            return planned["steps"], state, planned
        plan, state = self._authoritative_plan()
        return plan, state, None

    @staticmethod
    def _signature(plan: list[dict]) -> list[tuple]:
        return [(row.get("action", "return"), row["material"], row["from"],
                 row.get("to")) for row in plan]

    def create(self, goal_type: str, spec: Mapping[str, Any] | None = None) -> dict:
        if goal_type not in (GOAL_RETURN_ALL, GOAL_ARRANGE):
            raise SimDemoGoalError(400, f"지원하지 않는 목표다: {goal_type}")
        request = None
        if goal_type == GOAL_ARRANGE:
            try:
                request = (spec if isinstance(spec, ArrangementRequest)
                           else ArrangementRequest.from_dict(spec or {}))
            except ArrangementError as exc:
                raise SimDemoGoalError(400, str(exc)) from exc
        with self._lock:
            if self._current is not None:
                current = self._goals[self._current]
                if current["status"] in ACTIVE:
                    raise SimDemoGoalError(409, "다른 목표가 진행 중이다")
            if self.jobs.running() is not None:
                raise SimDemoGoalError(409, "다른 시연 작업이 실행 중이다")
            plan, state, arranged = self._plan_for(goal_type, request)
            goal_id = f"simgoal_{uuid.uuid4().hex[:12]}"
            now = self._clock()
            summary = (arranged["summary"] if arranged is not None else
                       f"컨베이어의 자재 {len(plan)}개를 순서대로 원래 자리로 돌려놓습니다.")
            goal = {
                "goal_id": goal_id, "goal": goal_type,
                "summary": summary,
                "status": "planned", "confirmation_required": True,
                "confirmed_at": None, "created_at": now,
                "started_at": None, "finished_at": None,
                "plan": plan, "current_step": None,
                "progress": {"completed": 0, "total": len(plan),
                             "percent": 0},
                "final_result": None,
                "planned_state_updated_at": state.get("updated_at"),
                "stop_requested": False,
                "request": None if request is None else request.to_dict(),
                "reasoning": None if arranged is None else {
                    "current": arranged["current"],
                    "expected": arranged["expected"],
                    "blocked_slots": arranged["blocked_slots"],
                    "environment": arranged.get("environment"),
                    "notes": arranged["notes"],
                },
            }
            self._goals[goal_id] = goal
            self._current = goal_id
            return self._copy(goal)

    def confirm(self, goal_id: str, action: str) -> dict:
        if action not in ("confirm", "cancel"):
            raise SimDemoGoalError(400, f"알 수 없는 확인 동작이다: {action}")
        with self._lock:
            goal = self._require(goal_id)
            if goal["status"] != "planned":
                raise SimDemoGoalError(409, "확인할 수 있는 계획 상태가 아니다")
            if action == "cancel":
                self._finish(goal, "cancelled", False,
                             "사용자가 실행 전에 목표를 취소했다")
                return self._copy(goal)
            if not self.jobs.reserve_goal(goal_id):
                raise SimDemoGoalError(409, "다른 작업이 작업 셀을 사용 중이다")
            try:
                request = (None if goal.get("request") is None else
                           ArrangementRequest.from_dict(goal["request"]))
                fresh, _, _ = self._plan_for(goal["goal"], request)
                if self._signature(fresh) != self._signature(goal["plan"]):
                    raise SimDemoGoalError(
                        409, "계획 후 컨베이어 상태가 바뀌었다 — 새 계획이 필요하다")
            except BaseException:
                self.jobs.release_goal(goal_id)
                raise
            now = self._clock()
            goal.update(status="running", confirmation_required=False,
                        confirmed_at=now, started_at=now)
            if not goal["plan"]:
                self.jobs.release_goal(goal_id)
                self._finish(goal, "completed", True,
                             "이미 목표 배치와 같다" if goal["goal"] == GOAL_ARRANGE
                             else "컨베이어에 복귀할 자재가 없다")
                return self._copy(goal)
            if self._auto_run:
                try:
                    threading.Thread(target=self.run, args=(goal_id,),
                        name=f"sim-goal-{goal_id}", daemon=True).start()
                except BaseException as exc:
                    self.jobs.release_goal(goal_id)
                    self._finish(goal, "failed", False,
                                 f"목표 실행 스레드를 시작하지 못했다: {exc}")
                    raise
            return self._copy(goal)

    def run(self, goal_id: str) -> None:
        """Execute a confirmed goal. Public for deterministic unit tests."""
        try:
            while True:
                # Keep the goal lock through launch.  A concurrent STOP either
                # wins before this block (so no next child starts), or waits
                # until the child is visible and then stops that active child.
                with self._lock:
                    goal = self._require(goal_id)
                    if goal["stop_requested"]:
                        self._finish(goal, "stopped", False,
                                     "STOP 요청으로 다음 작업을 시작하지 않았다")
                        return
                    completed = goal["progress"]["completed"]
                    if completed >= len(goal["plan"]):
                        ok, detail = self._final_check(goal)
                        self._finish(goal, "completed" if ok else "failed", ok, detail)
                        return
                    step = goal["plan"][completed]
                    step["status"] = "starting"
                    goal["current_step"] = step["step"]
                    action = step.get("action", "return")
                    try:
                        job = self._start_step(step, goal_id)
                    except Exception as exc:  # fail closed; no later step starts
                        step["status"] = "failed"
                        step["result"] = {"detail": str(exc)}
                        self._finish(goal, "failed", False,
                                     f"{step['material']} {action} 작업을 시작하지 못했다: {exc}")
                        return
                    step.update(status="running", job_id=job["job_id"])
                while True:
                    detail = self.jobs.job(job["job_id"])
                    if detail["status"] != "running":
                        break
                    self._sleep(self._poll_sec)
                # The durable state is authoritative.  A successful process/report
                # alone is never enough to advance the goal.
                state = self.jobs.state.status()
                record = (state.get("objects") or {}).get(step["material"])
                with self._lock:
                    step["result"] = {
                        "job_status": detail.get("status"),
                        "exit_code": detail.get("exit_code"),
                        "report_status": (detail.get("report") or {}).get("status"),
                        "state_available": state.get("available"),
                        "still_on_conveyor": record is not None,
                        "observed_slot": None if record is None else record_slot(record),
                    }
                    if goal["stop_requested"]:
                        step["status"] = "stopped"
                        self._finish(goal, "stopped", False,
                                     "STOP 요청으로 현재 작업을 멈췄고 다음 작업을 시작하지 않았다")
                        return
                    reached, why = self._step_reached(action, step, state, record)
                    if not reached:
                        step["status"] = "failed"
                        self._finish(goal, "failed", False,
                                     f"단계 {step['step']} 결과를 확인하지 못해 중단했다: {why}")
                        return
                    step["status"] = "completed"
                    done = completed + 1
                    goal["progress"] = {
                        "completed": done, "total": len(goal["plan"]),
                        "percent": round(done * 100 / len(goal["plan"])),
                    }
        except Exception as exc:
            # Unexpected orchestration/state failures are terminal too.  The
            # finally block stays the only goal-lease release for run().
            with self._lock:
                goal = self._goals.get(goal_id)
                if goal is not None and goal["status"] in ("running", "stopping"):
                    self._finish(goal, "failed", False,
                                 f"목표 실행 중 예외가 발생했다: {exc}")
        finally:
            self.jobs.release_goal(goal_id)

    @staticmethod
    def _step_reached(action: str, step: Mapping[str, Any],
                      state: Mapping[str, Any],
                      record: Mapping[str, Any] | None) -> tuple[bool, str]:
        """단계 성공은 **상태 기록**으로만 판정한다. 프로세스 종료는 근거가 아니다."""
        if state.get("available") is not True:
            return False, str(state.get("detail") or "상태 기록을 읽을 수 없다")
        there = ORIGIN if action == "return" else str(step.get("to") or "")
        if there.startswith("slot_"):
            if record is None or record.get("state") != HELD_ON_TARGET:
                return False, f"{step['material']}이 컨베이어에 놓였다는 기록이 없다"
            if record_slot(record) != there:
                return False, (f"{step['material']}이 {record_slot(record)}에 기록됐다"
                               f" (계획: {there})")
            return True, ""
        if there != ORIGIN:
            if record is None or record.get("state") != ON_PALLET:
                return False, f"{step['material']}이 {there}에 놓였다는 기록이 없다"
            if record.get("pallet") != there:
                return False, (f"{step['material']}이 {record.get('pallet')}에 기록됐다"
                               f" (계획: {there})")
            return True, ""
        if record is not None:
            return False, f"{step['material']}이 컨베이어 상태 기록에 남아 있다"
        return True, ""

    def _final_check(self, goal: Mapping[str, Any]) -> tuple[bool, str]:
        if goal["goal"] != GOAL_ARRANGE:
            return True, "계획한 모든 자재가 컨베이어에서 없어졌다"
        expected = (goal.get("reasoning") or {}).get("expected") or {}
        state = self.jobs.state.status()
        if state.get("available") is not True:
            return False, "최종 상태 기록을 읽을 수 없어 배치 완료를 확인하지 못했다"
        try:
            seen = current_locations(dict(state.get("objects") or {}),
                                     self.jobs.materials)
        except ArrangementError as exc:
            return False, f"최종 배치를 확인하지 못했다: {exc}"
        wrong = sorted(m for m, where in expected.items() if seen.get(m) != where)
        if wrong:
            return False, ("최종 배치가 목표와 다르다: " + ", ".join(
                f"{m}={seen.get(m)} (목표 {expected[m]})" for m in wrong))
        return True, "상태 기록 기준으로 목표 배치에 도달했다"

    def request_stop(self, goal_id: str | None = None) -> dict:
        with self._lock:
            if goal_id is None:
                goal_id = self._current
            if goal_id is None:
                return {"requested": False, "detail": "진행 중인 목표가 없다"}
            goal = self._require(goal_id)
            if goal["status"] not in ("running", "stopping"):
                return {"requested": False, "goal_id": goal_id,
                        "detail": "실행 중인 목표가 아니다"}
            goal["stop_requested"] = True
            goal["status"] = "stopping"
        job_stop = self.jobs.request_stop(reason="sim_demo_goal_stop")
        return {"requested": True, "goal_id": goal_id,
                "job_stop": job_stop,
                "detail": "목표 STOP을 요청했다"}

    def get(self, goal_id: str) -> dict:
        with self._lock:
            return self._copy(self._require(goal_id))

    def current(self) -> dict | None:
        with self._lock:
            return None if self._current is None else self._copy(
                self._goals[self._current])

    def _require(self, goal_id: str) -> dict:
        try:
            return self._goals[goal_id]
        except KeyError as exc:
            raise SimDemoGoalError(404, f"시연 목표가 없다: {goal_id}") from exc

    def _finish(self, goal: dict, status: str, ok: bool, detail: str) -> None:
        goal.update(status=status, current_step=None, finished_at=self._clock(),
                    confirmation_required=False,
                    final_result={"ok": ok, "status": status, "detail": detail})

    @staticmethod
    def _copy(goal: Mapping[str, Any]) -> dict:
        # JSON-compatible state; this also keeps callers from mutating internals.
        import copy
        return copy.deepcopy(dict(goal))
