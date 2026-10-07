"""시뮬레이션 작업 셀의 일반 경로 pick/place — 열기 판정과 transfer 묶음.

일반 `/v1/plan → /v1/execute` 경로에서 pick/place를 **시뮬레이션 셀에서만** 연다.
열지 말지는 시뮬레이션 전용 Profile(`core/sim_profile.py`)과 시뮬레이션 관문
(`validation/pick_place_gate.evaluate_simulation`)이 정한다. 실물 Profile과 실물 관문은
그대로다 — `/v1/robots`의 실물 구성은 계속 `pick·place` 차단을 보여준다.

실행은 새로 만들지 않는다. 계획의 `move(S) → pick(o,S) → move(D) → place(o,D)`를 공통
`transfer(o, S, D)` 하나로 묶고(`transfer_units`), 시연 경로가 쓰는 검증된 이송 실행기
(`SimDemoJobs.start_transfer`)에 넘긴다. 이 파일은 묶음 규칙과 자원 id 대응만 갖는다.

지키는 것:

- 작업 셀 매니페스트가 시뮬레이션(`is_simulated=true`)이 아니면 열지 않는다.
- 시뮬레이션 관문 7개 조건 중 하나라도 미충족이면 열지 않고, 이유를 상태에 남긴다.
- 연 Profile은 **버전 문자열이 다르다**(`+sim-pp.<버전>`). 열기 전에 받은 승인은
  Profile 버전 대조로 재사용되지 않는다.
- 묶을 수 없는 pick/place 모양(물체·위치가 엇갈림, pick만, 두 이송 이상)은 BLOCK이다.
  계획을 고쳐 주지 않는다.
- 자재 질량이 시뮬레이션 적재 범위 밖이면 BLOCK이다.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.capability_profile import CapabilityProfile
from core.constants import SKILL_HOME, SKILL_MOVE, SKILL_PICK, SKILL_PLACE
from core.sim_profile import SimulationProfile, apply_to_composite, load_simulation_profile

#: 일반 경로가 묶어 실행하는 공통 스킬 이름(`core/transfer_skill.SKILL_TRANSFER`와 같다).
TRANSFER = "transfer"


@dataclass(frozen=True)
class SimPickPlace:
    """열기 판정 결과. `profile`이 None이면 열지 않은 것이다."""

    enabled: bool
    detail: str
    profile: CapabilityProfile | None = None
    sim: SimulationProfile | None = None
    composite: Any = None
    gate: Any = None

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled, "detail": self.detail,
            "environment": "simulation", "real_hardware_ready": False,
            "profile_version": None if self.profile is None else self.profile.profile_version,
            "simulation_profile": None if self.sim is None else self.sim.to_dict(),
            "composite": None if self.composite is None else {
                "composite_profile_id": self.composite.composite_profile_id,
                "composite_profile_version": self.composite.composite_profile_version,
                "complete": self.composite.complete,
                "blocking_items": list(self.composite.blocking_items),
                "supported_skills": list(self.composite.supported_skills),
            },
            "gate": None if self.gate is None else self.gate.to_dict(),
        }


def _base_composite(profile_dir: Path, composite_id: str):
    """실물 Profile 파일에서 기본 구성을 읽는다(읽기만 — 파일을 고치지 않는다)."""
    from config.loader import (
        load_arm_profile,
        load_composite_profile,
        load_gripper_profile,
        load_mounting_profile,
    )

    arms, grippers, mountings, composite = {}, {}, {}, None
    for path in sorted(profile_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "arm_profile_id" in payload:
            item = load_arm_profile(payload)
            arms[item.arm_profile_id] = item
        elif "gripper_profile_id" in payload:
            item = load_gripper_profile(payload)
            grippers[item.gripper_profile_id] = item
        elif "mounting_profile_id" in payload:
            item = load_mounting_profile(payload)
            mountings[item.mounting_profile_id] = item
        elif payload.get("composite_profile_id") == composite_id:
            composite = payload
    if composite is None:
        raise ValueError(f"기본 구성 {composite_id}를 {profile_dir}에서 찾지 못했다")
    return load_composite_profile(composite, arms=arms, grippers=grippers, mountings=mountings)


def open_simulation_pick_place(
    *, manifest: Mapping[str, Any], sim_profile_path: Path, profile_dir: Path,
    workcell_profile: CapabilityProfile, verification: Mapping[str, Any] | None,
) -> SimPickPlace:
    """시뮬레이션 셀에서 pick/place를 열 수 있는지 판정하고, 열면 새 Profile을 만든다."""
    from validation import pick_place_gate

    if manifest.get("is_simulated") is not True or manifest.get("real_hardware_verified"):
        return SimPickPlace(False, "작업 셀이 시뮬레이션이 아니다 — 시뮬레이션 근거를 쓰지 않는다")
    try:
        sim = load_simulation_profile(json.loads(sim_profile_path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001 — 근거가 없으면 열지 않는다
        return SimPickPlace(False, f"시뮬레이션 Profile을 읽지 못했다: {exc}"[:300])
    applies = dict(sim.applies_to)
    if (applies.get("adapter_module") != manifest.get("adapter_module")
            or applies.get("robot_id") != workcell_profile.profile_id):
        return SimPickPlace(False, f"시뮬레이션 Profile 적용 대상({applies})이 이 셀이 아니다", sim=sim)
    try:
        composite = apply_to_composite(_base_composite(profile_dir, sim.base.get("composite", "")),
                                       sim)
    except Exception as exc:  # noqa: BLE001
        return SimPickPlace(False, f"시뮬레이션 구성을 만들지 못했다: {exc}"[:300], sim=sim)
    gate = pick_place_gate.evaluate_simulation(
        mounting=composite.mounting, verification=verification, simulation=sim.to_dict())
    if not composite.complete or not gate.enabled:
        why = ([f"미확보: {', '.join(composite.blocking_items)}"] if not composite.complete else [])
        why += pick_place_gate.blocking_summary(gate)
        return SimPickPlace(False, "; ".join(why)[:500], sim=sim, composite=composite, gate=gate)
    gripper = composite.gripper.to_gripper_spec()
    if gripper.max_effort != sim.gripper_max_effort.value:
        return SimPickPlace(False, "그리퍼 effort 근거가 시뮬레이션 Profile과 다르다",
                            sim=sim, composite=composite, gate=gate)
    skills = tuple(dict.fromkeys((*workcell_profile.supported_skills, SKILL_PICK, SKILL_PLACE)))
    extras = {k: v for k, v in dict(workcell_profile.extras).items() if k != "gated_skills"}
    extras["simulation_pick_place"] = {
        "sim_profile": f"{sim.sim_profile_id} {sim.sim_profile_version}",
        "evidence_report": sim.evidence_report, "environment": "simulation",
        "execution": "pick/place는 transfer 한 단위로 묶어 이송 실행기가 실행한다",
        "payload_scope_kg": dict(sim.verified_materials_kg),
    }
    provenance = dict(workcell_profile.provenance)
    provenance.update({
        "payload_kg": f"시뮬레이션 전용 — {sim.values['arm.payload'].provenance.note}",
        "gripper": f"시뮬레이션 전용 — 완전 닫힘 패드 간격 {gripper.grasp_aperture_m} m"
                   f" ({sim.evidence_report}), effort는 관절 토크 한계(파지력 아님)",
        "supported_skills": "home/move/stop + 시뮬레이션 관문(evaluate_simulation)을 통과한 pick/place",
    })
    opened = replace(
        workcell_profile,
        profile_version=f"{workcell_profile.profile_version}+sim-pp.{sim.sim_profile_version}",
        payload_kg=sim.max_payload_kg, supported_skills=skills, gripper=gripper,
        provenance=provenance, extras=extras)
    return SimPickPlace(True, "시뮬레이션 관문 7/7 충족 — Gazebo 셀에서만 pick/place를 연다",
                        profile=opened, sim=sim, composite=composite, gate=gate)


# ── 계획 → transfer 묶음 ───────────────────────────────────────────────
@dataclass(frozen=True)
class TransferUnit:
    """계획 스텝 몇 개를 덮는 transfer 한 번. 번호는 1부터(계획 스텝 번호)."""

    material: str            # 계획의 물체 자원 id
    source: str              # 계획의 출발 위치 자원 id
    destination: str         # 계획의 도착 위치 자원 id
    steps: tuple[int, ...]   # 이 transfer가 대신하는 계획 스텝 번호

    def to_dict(self) -> dict:
        return {"skill": TRANSFER, "material": self.material, "source": self.source,
                "destination": self.destination, "plan_steps": list(self.steps)}


@dataclass(frozen=True)
class TransferShape:
    units: tuple[TransferUnit, ...] = ()
    problem: str | None = None
    #: transfer 밖에서 어댑터가 실행하는 스텝 번호.
    others: tuple[int, ...] = field(default_factory=tuple)


def transfer_units(steps: Sequence[Any]) -> TransferShape:
    """`[move(S)] pick(o,S) [move(D)] place(o,D)`를 찾는다. 다른 모양이면 이유를 돌려준다.

    pick 앞의 move(S)와 pick/place 사이 move(D)는 이송 실행기의 접근 단계가 대신하므로
    같은 단위에 넣는다. 그 밖의 스텝(home 등)은 어댑터가 그대로 실행한다.
    """
    def skill(i: int) -> str:
        return getattr(steps[i], "skill", "")

    def args(i: int) -> dict:
        return dict(getattr(steps[i], "args", {}) or {})

    picks = [i for i in range(len(steps)) if skill(i) == SKILL_PICK]
    places = [i for i in range(len(steps)) if skill(i) == SKILL_PLACE]
    if not picks and not places:
        return TransferShape(others=tuple(range(1, len(steps) + 1)))
    if len(picks) != 1 or len(places) != 1:
        return TransferShape(problem=f"pick {len(picks)}개·place {len(places)}개 — 이송 한 번"
                                     "(pick 하나 뒤 place 하나)만 실행한다")
    p, q = picks[0], places[0]
    if q < p:
        return TransferShape(problem="place가 pick보다 앞에 있다")
    pick, place = args(p), args(q)
    if pick.get("object") != place.get("object"):
        return TransferShape(problem=f"pick 물체({pick.get('object')})와 place 물체"
                                     f"({place.get('object')})가 다르다")
    source, destination = pick.get("from"), place.get("to")
    covered = [p, q]
    if p > 0 and skill(p - 1) == SKILL_MOVE and args(p - 1).get("to") == source:
        covered.append(p - 1)
    between = list(range(p + 1, q))
    for i in between:
        if skill(i) != SKILL_MOVE:
            return TransferShape(problem=f"pick과 place 사이에 {skill(i)} 스텝이 있다 —"
                                         " 물체를 든 채 다른 동작을 하지 않는다")
        if args(i).get("to") != destination:
            return TransferShape(problem=f"pick과 place 사이 move가 도착지({destination})가 아닌"
                                         f" {args(i).get('to')}로 간다")
        covered.append(i)
    if len(between) > 1:
        return TransferShape(problem="pick과 place 사이 move가 둘 이상이다")
    others = tuple(i + 1 for i in range(len(steps)) if i not in covered)
    for i in others:
        if skill(i - 1) not in (SKILL_HOME, SKILL_MOVE):
            return TransferShape(problem=f"이송 밖의 {skill(i - 1)} 스텝은 함께 실행하지 않는다")
    unit = TransferUnit(material=str(pick.get("object")), source=str(source),
                        destination=str(destination),
                        steps=tuple(sorted(i + 1 for i in covered)))
    if any(o > unit.steps[0] and o < unit.steps[-1] for o in others):
        return TransferShape(problem="이송 단계 사이에 다른 스텝이 끼어 있다")
    # 이송 실행기는 안전 home에서 시작한다. 앞에는 home만 둔다 — 다른 위치로 먼저 가 있으면
    # 실행기의 첫 단계가 검증되지 않은 자세에서 출발한다.
    if any(o < unit.steps[0] and skill(o - 1) != SKILL_HOME for o in others):
        return TransferShape(problem="이송 앞에 home이 아닌 동작이 있다 — 이송은 안전 home에서 시작한다")
    return TransferShape(units=(unit,), others=others)


# ── 관문: 계획의 transfer를 공통 계약으로 판정 ─────────────────────────
def _finding_reason(code: str, decision: str):
    """공통 계약 사유 → ReasonCode(이송 실행기 `_contract_reason`과 같은 대응)."""
    from core.reason_codes import ReasonCode

    if code == "geometry.path_obstructed":
        return ReasonCode.GEOMETRY_COLLISION
    if code.startswith("geometry"):
        return ReasonCode.GEOMETRY_GRASP_POSE_UNAVAILABLE
    if code == "plan.destination_occupied":
        return ReasonCode.EXEC_SIM_TARGET_OCCUPIED
    if code == "plan.unknown_resource":
        return ReasonCode.PLAN_UNKNOWN_RESOURCE
    if code == "capability.route_unsupported":
        return ReasonCode.CAPABILITY_SKILL_UNSUPPORTED
    if code in ("state.unobserved", "state.unconfirmed"):
        return ReasonCode.EXEC_UNVERIFIABLE
    return ReasonCode.PLAN_RESOURCE_MISMATCH


def resolve_transfer(jobs, unit: TransferUnit) -> dict:
    """계획 자원 id → 이송 실행기 id(자재 모델·팔레트·컨베이어 칸) + 공통 계약 판정.

    돌려주는 dict: decision(ALLOW/ASK/BLOCK), reason(ReasonCode|None), detail,
    material_model, source, destination(실행기 id), route.
    컨베이어를 출발지로 말하면 **기록된 그 자재의 칸**을, 도착지로 말하면 **비어 있는 첫
    검증 칸**을 쓴다(시연 경로의 배정 규칙과 같다). 칸을 새로 만들지 않는다.
    """
    from core.reason_codes import ReasonCode
    from server.sim_demo_places import CONVEYOR_ID
    from validation.conveyor_slots import assign_slot, record_slot
    from validation.simulation_demo_state import HELD_ON_TARGET

    def answer(decision, reason, detail, **extra):
        return {"decision": decision, "reason": reason, "detail": detail, **extra}

    model = next((m for m, spec in jobs.materials.items()
                  if spec.get("resource_id") == unit.material), None)
    if model is None:
        return answer("BLOCK", ReasonCode.PLAN_UNKNOWN_RESOURCE,
                      f"이송 실행기가 아는 자재가 아니다: {unit.material}")
    korean = jobs.materials[model].get("korean") or model
    if unit.source == unit.destination:
        return answer("BLOCK", ReasonCode.PLAN_RESOURCE_MISMATCH,
                      f"출발지와 도착지가 같다({unit.source}) — 칸을 골라 주지 않는다",
                      material_model=model)
    status = jobs.state.status()
    if status.get("available") is not True:
        return answer("ASK", ReasonCode.EXEC_UNVERIFIABLE,
                      "자재 상태 기록을 확인할 수 없다", material_model=model)
    objects = dict(status.get("objects") or {})
    source = unit.source
    if source == CONVEYOR_ID:
        row = objects.get(model) or {}
        if row.get("state") != HELD_ON_TARGET:
            where = row.get("pallet") or jobs._capability().origin_of(model)
            return answer("ASK", ReasonCode.PLAN_RESOURCE_MISMATCH,
                          f"{korean}는 컨베이어가 아니라 {where}에 있다(기록) — 출발지를 확인해 주세요",
                          material_model=model)
        source = record_slot(row)
    destination = unit.destination
    if destination == CONVEYOR_ID:
        others = {m: r for m, r in objects.items() if m != model}
        slot = assign_slot(jobs.slots, others) if jobs.slots else None
        if slot is None:
            return answer("BLOCK", ReasonCode.EXEC_SIM_TARGET_OCCUPIED,
                          "컨베이어의 검증된 칸이 모두 찼다 — 먼저 하나를 비워야 한다",
                          material_model=model)
        destination = slot.name
    plan, findings = jobs.check_transfer(model, source, destination)
    if plan is None:
        first = findings[0]
        return answer(first.decision, _finding_reason(first.code, first.decision),
                      f"{first.code}: {first.detail}", material_model=model,
                      source=source, destination=destination,
                      findings=[f.to_dict() for f in findings])
    return answer("ALLOW", None, f"공통 이송 계약 통과: {korean} {source} → {destination}"
                  f" ({plan.route})", material_model=model, source=source,
                  destination=destination, route=plan.route)


def gate_transfer(runtime, plan) -> dict | None:
    """계획에 pick/place가 있으면 transfer로 묶어 판정한다. 없으면 None."""
    from core.reason_codes import ReasonCode

    opened = getattr(runtime, "sim_pick_place", None)
    if opened is None:
        # 시뮬레이션 작업 셀이 아니다(개발용 Fake 등) — 기존 Profile 판정을 그대로 둔다.
        return None
    shape = transfer_units(getattr(plan, "steps", ()))
    if not shape.units and shape.problem is None:
        return None
    if not opened.enabled:
        return {"decision": "BLOCK", "reason": ReasonCode.CAPABILITY_PROFILE_INCOMPLETE,
                "detail": f"일반 경로 pick/place가 열려 있지 않다: {opened.detail}"}
    if getattr(runtime, "profile", None) is not opened.profile:
        return {"decision": "BLOCK", "reason": ReasonCode.ROBOT_PROFILE_MISMATCH,
                "detail": "등록된 로봇 Profile이 시뮬레이션 pick/place를 연 Profile이 아니다"}
    if shape.problem is not None:
        return {"decision": "BLOCK", "reason": ReasonCode.CAPABILITY_SKILL_UNSUPPORTED,
                "detail": f"이 셀이 실행하는 pick/place 모양이 아니다: {shape.problem}"}
    jobs = getattr(runtime, "sim_demo_jobs", None)
    if jobs is None:
        return {"decision": "BLOCK", "reason": ReasonCode.CAPABILITY_PROFILE_INCOMPLETE,
                "detail": "이송 실행기가 없다(시뮬레이션 작업 셀이 아니다)"}
    unit = shape.units[0]
    result = resolve_transfer(jobs, unit)
    result["unit"] = unit.to_dict()
    model = result.get("material_model")
    if result["decision"] == "ALLOW" and model is not None:
        mass = (jobs.workcell.get("models") or {}).get(model, {}).get("mass_kg")
        ok, why = opened.sim.payload_allows(model, None if mass is None else float(mass))
        result["payload"] = why
        if not ok:
            result.update(decision="BLOCK", reason=ReasonCode.CAPABILITY_LIMIT_EXCEEDED,
                          detail=f"시뮬레이션 적재 범위 밖: {why}")
    if result["decision"] == "ALLOW":
        running = jobs.running()
        if running is not None or jobs.recovery_required is not None:
            result.update(decision="ASK", reason=ReasonCode.EXEC_GOAL_REJECTED,
                          detail="다른 작업 셀 작업이 진행 중이거나 복구 확인이 필요하다")
    return result


# ── 실행: 검증된 이송 실행기에 넘기고 끝을 관측으로 확인 ────────────────
#: 이송 실행기 보고서의 성공 상태(실행기가 정한 값 — 여기서 새로 정의하지 않는다).
TRANSFER_SUCCESS = frozenset({"simulation_transfer_completed",
                              "simulation_transfer_resumed_completed",
                              "returned_to_origin", "route_arrived", "slot_moved"})
#: 정지로 끝난 상태.
TRANSFER_STOPPED = frozenset({"simulation_transfer_stopped", "route_stopped",
                              "slot_move_stopped", "return_stopped"})
#: 작업 상태를 다시 읽는 간격(s). 화면 상태 주기(sim-demo 2 s)보다 촘촘하게 둔다.
POLL_SEC = 0.5
#: 끝난 뒤 새 pose 표본을 기다리는 시간(s). pose/info는 수십 Hz로 오고 `sim_view`가
#: 0.5 s 넘은 표본을 낡은 것으로 본다 — 그 스무 배 동안 새 표본이 없으면 관측 실패다.
FINAL_POSE_WAIT_SEC = 10.0


def location_center(jobs, location: str):
    """실행기 위치 id → 그 자리의 자재 중심(설정값). 칸은 검증된 칸 중심, 팔레트는 그
    팔레트에 원래 놓인 자재의 선언 위치(`scripts/demo_workcell_pick_place.location_center`와
    같은 규칙). 모르면 None — 만들어 내지 않는다."""
    from validation.simulation_demo_state import origin_slot

    for slot in jobs.slots or ():
        if slot.name == location:
            return tuple(slot.center_m)
    for model in jobs.materials:
        origin = origin_slot(jobs.workcell, model)
        if origin.support_id == location:
            return tuple(origin.home_pose_m)
    return None


def observe_final(runtime, jobs, model: str, destination: str, *, after: float) -> dict:
    """끝난 뒤 **새로 받은** Gazebo pose로 자재가 도착지 중심에 있는지 본다.

    허용치는 기록↔관측 정합과 같은 `ORIGIN_TOLERANCE_M`이다(같은 자리 판정에 같은 값).
    """
    from validation.simulation_demo_state import ORIGIN_TOLERANCE_M

    expected = location_center(jobs, destination)
    out = {"destination": destination, "expected_center_m": expected,
           "tolerance_m": ORIGIN_TOLERANCE_M, "observed_m": None, "gap_m": None,
           "within": False, "source": "gazebo pose/info (sim_view)"}
    view = getattr(runtime, "sim_view", None)
    if view is None or expected is None:
        out["detail"] = "Gazebo pose 구독이 없거나 도착지 중심을 모른다"
        return out
    deadline = time.monotonic() + FINAL_POSE_WAIT_SEC
    sample = None
    while time.monotonic() < deadline:
        sample = view.state.sample() if hasattr(view, "state") else view.sample()
        pose = (sample.get("materials") or {}).get(model)
        fresh = (sample.get("pose_time") or 0.0) > after and not sample.get("stale")
        if pose is not None and fresh:
            gap = math.dist([float(v) for v in pose[:3]], [float(v) for v in expected])
            out.update(observed_m=[round(float(v), 6) for v in pose[:3]],
                       gap_m=round(gap, 6), within=gap <= ORIGIN_TOLERANCE_M,
                       pose_age_sec=sample.get("pose_age_sec"))
            return out
        time.sleep(POLL_SEC)
    out["detail"] = "끝난 뒤 새 pose 표본을 받지 못했다"
    return out


def report_detached(report: Mapping[str, Any]) -> bool | None:
    """실행기 보고서에서 '고정 장치를 도착지에서 풀었다'는 관측. 모르면 None.

    보고서 형식이 이송 종류마다 다르다: 복귀·경로 이송은 최상위 `detached`, 정방향 이송은
    `criteria[detached_at_target]`에 둔다. 둘 다 없으면 확인하지 못한 것이다.
    """
    if isinstance(report.get("detached"), bool):
        return report["detached"]
    for row in report.get("criteria") or ():
        if row.get("key") == "detached_at_target" and isinstance(row.get("met"), bool):
            return row["met"]
    return None


def transfer_progress(rows: Sequence[Mapping[str, Any]]) -> int:
    """실행기 단계 줄(`parse_progress`) → transfer가 덮는 계획 스텝(이동·집기·이동·놓기) 중 **지난** 수(0~4).

    화면 진행 표시용이다(2026-10-07: 이송 한 번이 끝날 때까지 계획 스텝 1~4가 한꺼번에 기록돼, 진행 표시가
    멈춰 있다가 5로 건너뛰었다). 그리퍼 단계를 기준점으로 쓴다 — 이송·복귀 실행기 모두 같은 순서다:
    출발지 접근 → 그리퍼 열기 → 접근 → **그리퍼 닫기** → 들기 → 목적지 접근 → **그리퍼 열기(해제)** → 물러나기.
    성공 판정이 아니다. 성공은 실행이 끝난 뒤 보고서·기록·관측으로 따로 정한다.
    """
    labels = [str(r.get("label") or "") for r in rows if r.get("reached") is True]
    close = next((i for i, x in enumerate(labels) if x.startswith("그리퍼 닫기")), None)
    release = next((i for i, x in enumerate(labels) if "(해제)" in x), None)
    if release is not None:
        return 4 if len(labels) > release + 1 else 3
    if close is not None:
        return 2 if len(labels) > close + 1 else 1
    return 1 if any(x.startswith("그리퍼 열기") for x in labels) else 0


def execute_transfer(runtime, unit: TransferUnit, *, goal_id: str, interruption,
                     speed_percent: int | None = None, on_progress=None) -> Any:
    """transfer 한 번을 이송 실행기로 돌리고 ExecutionResult를 돌려준다.

    - 실행 직전에 계약을 **다시** 본다(승인 뒤 상태가 바뀌었으면 시작하지 않는다).
    - 정지·취소는 실행기 정지 요청으로 넘긴다. 프로세스를 죽이지 않는다(궤적이 남는다).
    - 성공은 ① 실행기 보고서 상태 ② 상태 기록의 위치 ③ 새 Gazebo pose가 도착지 중심
      허용치 안 — 셋이 모두 맞을 때만이다.
    """
    from core.execution_result import ExecutionResult, rejected, unverifiable
    from core.execution_state import ExecutionState
    from core.reason_codes import ReasonCode

    jobs = runtime.sim_demo_jobs
    resolved = resolve_transfer(jobs, unit)
    evidence: dict = {"executed_by": "transfer", "unit": unit.to_dict(),
                      "is_simulated": True, "contract": {k: resolved.get(k) for k in
                                                          ("decision", "detail", "source",
                                                           "destination", "route")}}
    if resolved["decision"] != "ALLOW":
        return rejected(resolved["reason"] or ReasonCode.EXEC_PERMIT_DENIED,
                        {**evidence, "detail": f"실행 직전 계약 재판정: {resolved['detail']}"})
    model, source, destination = (resolved["material_model"], resolved["source"],
                                  resolved["destination"])
    # 계약 판정 사이에 정지·취소가 들어왔으면 시작하지 않는다.
    why = interruption()
    if why is not None:
        return rejected(why, {**evidence, "detail": "시작 전에 정지·취소가 들어와 시작하지 않았다"})
    try:
        kwargs = {} if speed_percent is None else {"speed_percent": speed_percent}
        job = jobs.start_transfer(model, source, destination, goal_id=goal_id, **kwargs)
    except Exception as exc:  # noqa: BLE001 — 시작 거부는 실행 거부다
        return rejected(ReasonCode.EXEC_GOAL_REJECTED,
                        {**evidence, "detail": f"이송 실행기가 시작을 거부했다: {exc}"[:300]})
    job_id = job["job_id"]
    evidence["job_id"] = job_id
    stop_sent = None
    last_progress = None
    while True:
        current = jobs.job(job_id, console_lines=1)
        if current.get("status") != "running":
            break
        if on_progress is not None:
            rows = current.get("progress") or []
            mark = (transfer_progress(rows), len(rows))
            if mark != last_progress:
                last_progress = mark
                try:
                    on_progress(mark[0], rows[-1] if rows else None)
                except Exception:  # noqa: BLE001 — 진행 알림 실패가 실행을 멈추지 않는다
                    pass
        why = interruption()
        if why is not None and stop_sent is None:
            # 실행기가 늦게 떠서 버리는 요청은 `SimDemoJobs.request_stop`이 다시 쓴다.
            sent = jobs.request_stop(reason=f"general_execute:{why.value}")
            if sent.get("requested"):
                stop_sent = sent
                evidence.update(stop_request=stop_sent)
        time.sleep(POLL_SEC)
    finished_at = float(current.get("finished_at") or time.time())
    report = current.get("report") or {}
    status = report.get("status")
    detached = report_detached(report)
    evidence.update(job_status=status, exit_code=current.get("exit_code"),
                    report_path=current.get("report_path"),
                    stages=len(report.get("stage_records") or ()),
                    detached=detached, stop_checkpoint=report.get("stop_checkpoint"),
                    external_stop_seen=report.get("external_stop_request"))
    codes = [c for c in (report.get("reason_codes") or ()) if c]
    if status in TRANSFER_STOPPED:
        return ExecutionResult(
            state=ExecutionState.STOPPED, request_accepted=True, motion_completed=False,
            target_reached=False, task_succeeded=False, verified=True,
            reason=ReasonCode.EXEC_STOPPED,
            evidence={**evidence, "stop_confirmed": True,
                      "detail": "이송 실행기가 정지 절차(취소→정지 확인→체크포인트)로 끝냈다"})
    if status not in TRANSFER_SUCCESS or current.get("exit_code") != 0:
        reason = ReasonCode.EXEC_TASK_FAILED
        for code in codes:
            try:
                reason = ReasonCode(code)
                break
            except ValueError:
                continue
        return ExecutionResult(
            state=ExecutionState.FAILED, request_accepted=True, motion_completed=False,
            target_reached=False, task_succeeded=False, verified=True, reason=reason,
            evidence={**evidence, "reason_codes": codes,
                      "stop_requested_before_end": stop_sent is not None,
                      "detail": f"이송 실행기 결과 {status} (종료 코드 {current.get('exit_code')})"})
    world = jobs.transfer_world()
    recorded = None if world is None else world.location_of.get(model)
    final = observe_final(runtime, jobs, model, destination, after=finished_at)
    evidence.update(recorded_location=recorded, final_position=final)
    if final.get("observed_m") is None:
        return unverifiable(ReasonCode.EXEC_UNVERIFIABLE,
                            {**evidence, "detail": f"최종 위치를 관측하지 못했다: {final.get('detail')}"})
    if detached is not True:
        return unverifiable(ReasonCode.EXEC_UNVERIFIABLE,
                            {**evidence, "detail": "도착지에서 고정 장치를 풀었다는 관측이 보고서에 없다"})
    if recorded != destination or not final["within"]:
        return ExecutionResult(
            state=ExecutionState.FAILED, request_accepted=True, motion_completed=True,
            target_reached=False, task_succeeded=False, verified=True,
            reason=ReasonCode.EXEC_SIM_PLACEMENT_OUT_OF_ZONE,
            evidence={**evidence, "detail": f"기록 위치 {recorded} · 관측 간격 {final['gap_m']} m"
                                            f" (허용 {final['tolerance_m']} m) — 도착지 {destination}와 맞지 않는다"})
    return ExecutionResult(
        state=ExecutionState.COMPLETED, request_accepted=True, motion_completed=True,
        target_reached=True, task_succeeded=True, verified=True,
        evidence={**evidence, "detail": f"{model} {source} → {destination}: 실행기 {status},"
                                        f" 기록 {recorded}, 관측 간격 {final['gap_m']} m"})
