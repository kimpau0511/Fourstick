"""이송 시작 전 자재 확인(2026-10-08 리뷰 11번) — 서버 기록과 실제 관측을 **함께** 본다.

서버 기록만으로 위치를 확정하지 않고, 관측 검사를 기록으로 대신하지 않는다. 결과는 다섯 갈래:

  ok                 기록·부착·관측이 모두 출발지와 맞는다 — 시작해도 된다(실행기가 직전에 다시 본다).
  noop               기록과 관측 모두 이미 목적지다 — 할 일 없음.
  record_not_ready   기록상 출발지에 있지 않거나(정지·실패·다른 자리) 시작할 수 없는 상태다 — 차단, 복구 안내.
  mismatch           기록과 관측이 다르다(기록은 출발지, 관측은 다른 곳) — 차단, 정합 확인(복구) 안내.
  attach_unknown     그리퍼 부착 상태를 확인할 수 없거나 붙어 있다 — 차단.
  observation_missing  관측이 없거나 오래됐다 — 확인할 수 없으니 시작하지 않는다(기록으로 대신하지 않는다).

부착 기록(`fixture_joints.json`)은 Gazebo가 다시 뜨면 의미가 없다(붙임 시스템이 새로 생긴다) — 현재 Gazebo가 뜬 시각
(`gz_server.pid` 시각)보다 오래된 기록은 보지 않는다.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

OK, NOOP, RECORD_NOT_READY, MISMATCH, ATTACH_UNKNOWN, OBSERVATION_MISSING = (
    "ok", "noop", "record_not_ready", "mismatch", "attach_unknown", "observation_missing")
#: 기록↔관측 정합과 같은 허용치(validation/simulation_demo_state.ORIGIN_TOLERANCE_M).
from validation.simulation_demo_state import HELD_ON_TARGET, ORIGIN_TOLERANCE_M  # noqa: E402

GUIDANCE = {
    RECORD_NOT_READY: "시뮬레이션 보기에서 재개·복구로 먼저 정리해 주세요",
    MISMATCH: "기록과 실제 위치가 다릅니다 — 기록↔관측 정합 확인(복구)을 한 뒤 다시 요청해 주세요",
    ATTACH_UNKNOWN: "그리퍼 부착 상태가 확인되지 않았습니다 — 관리자 확인(시뮬레이션 재시작 시 초기화)이 필요합니다",
    OBSERVATION_MISSING: "Gazebo 관측을 받지 못했습니다 — 시뮬레이션 연결을 확인한 뒤 다시 요청해 주세요",
}


def _log_dir() -> Path:
    return Path(os.environ.get("FORSTICK2_WORKCELL_LOG_DIR", "/tmp/forstick2_workcell"))


def read_attachment(model: str, log_dir: Path | None = None) -> dict:
    """{'state': attached|detached|unknown|None, 'at', 'current': 현재 Gazebo 이후 기록인가}."""
    base = log_dir or _log_dir()
    try:
        rows = json.loads((base / "fixture_joints.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rows = {}
    row = rows.get(model) or {}
    try:
        started = (base / "gz_server.pid").stat().st_mtime
    except OSError:
        started = None
    at = row.get("at")
    current = bool(row) and (started is None or (isinstance(at, (int, float)) and at >= started))
    return {"state": row.get("state"), "at": at, "current": current, "gazebo_started_at": started}


def observe_attachment(sim_view, model: str) -> tuple[bool | None, str]:
    """월드 상태로 본 붙임 여부(True 붙음·False 떨어짐·None 모름). 기록의 'unknown'을 관측으로 확인한다."""
    view = getattr(sim_view, "state", sim_view)
    probe = getattr(view, "attached_models", None)
    if not callable(probe):
        return None, "붙임 관절을 관측할 수 없다(관측 기능 없음)"
    models, detail = probe()
    if models is None:
        return None, detail
    return model in models, detail


def observed_pose(sim_view, model: str) -> tuple[list[float] | None, str | None]:
    """sim_view 최신 표본의 자재 위치. 없거나 낡았으면 (None, 이유)."""
    if sim_view is None:
        return None, "Gazebo 관측 구독이 없다"
    try:
        sample = sim_view.state.sample() if hasattr(sim_view, "state") else sim_view.sample()
    except Exception as exc:  # noqa: BLE001
        return None, f"관측을 읽지 못했다: {exc}"[:160]
    pose = (sample.get("materials") or {}).get(model)
    if pose is None:
        return None, "이 자재의 관측이 없다"
    if sample.get("stale"):
        return None, f"관측이 오래됐다({sample.get('pose_age_sec')}s)"
    return [float(v) for v in pose[:3]], None


def _where(jobs, model: str, record: Mapping[str, Any] | None) -> tuple[str | None, str]:
    """기록이 말하는 자재 위치(실행기 id)와 설명."""
    from validation.conveyor_slots import record_slot
    from validation.simulation_demo_state import origin_slot

    if not record:
        return origin_slot(jobs.workcell, model).support_id, "원래 자리(기록 없음)"
    if record.get("state") == HELD_ON_TARGET:
        return record_slot(record), "컨베이어(기록)"
    if record.get("pallet"):
        return str(record["pallet"]), "다른 팔레트(기록)"
    return None, f"{record.get('state')}(기록)"


def _center(jobs, location: str | None, record: Mapping[str, Any] | None, here: str | None):
    """그 자리의 기대 중심. 설정(칸·팔레트)에서 먼저 찾고, 칸 설정이 없을 때만 그 자리 기록의 pose_m을 쓴다."""
    from server.sim_pick_place import location_center

    center = None if location is None else location_center(jobs, location)
    if center is None and record and location is not None and location == here and record.get("pose_m"):
        center = record["pose_m"]
    return center


def check_material_start(jobs, sim_view, model: str, source: str | None, destination: str | None, *,
                         log_dir: Path | None = None) -> dict:
    """이송(출발지 → 목적지)을 시작해도 되는가. source·destination은 실행기 id(팔레트·칸). destination이
    컨베이어 칸 배정 전이면 'conveyor'를 넘긴다."""
    status = jobs.status()
    record = ((status.get("state") or {}).get("objects") or {}).get(model)
    here, here_text = _where(jobs, model, record)
    out: dict[str, Any] = {"model": model, "source": source, "destination": destination, "record": record,
                           "record_location": here, "tolerance_m": ORIGIN_TOLERANCE_M}

    def answer(kind: str, detail: str, **extra) -> dict:
        return {**out, **extra, "kind": kind, "detail": detail, "guidance": GUIDANCE.get(kind)}

    at_destination = here is not None and (here == destination or (
        destination == "conveyor" and record and record.get("state") == HELD_ON_TARGET))
    pose, why = observed_pose(sim_view, model)
    out["observed_m"] = pose
    if at_destination:
        # 기록은 이미 목적지다 — 관측도 그 자리면 할 일 없음, 다르면 기록·관측 불일치.
        if pose is None:
            return answer(OBSERVATION_MISSING, f"기록상 이미 목적지인데 관측으로 확인하지 못했다: {why}")
        center = _center(jobs, here, record, here)
        gap = None if center is None else math.dist(pose, [float(v) for v in center])
        if gap is not None and gap <= ORIGIN_TOLERANCE_M:
            return answer(NOOP, "이미 목적지에 있습니다(기록·관측 일치) — 옮길 필요가 없습니다", gap_m=round(gap, 4))
        return answer(MISMATCH, f"기록은 목적지인데 관측 위치가 다르다(차이 {gap if gap is None else round(gap, 3)} m)",
                      gap_m=gap)
    if here is None or here != source:
        return answer(RECORD_NOT_READY, f"기록상 출발지({source})에 있지 않다 — {here_text}")
    attach = read_attachment(model, log_dir)
    out["attachment"] = attach
    if attach["current"] and attach["state"] != "detached":
        # 기록이 '떨어짐'이 아니면 월드 상태의 붙임 관절로 확인한다(이미 떨어진 관절에 보낸 detach는 알림이 없어
        # 'unknown'으로 남는다). 관측으로 떨어짐이 확인될 때만 통과한다 — 모르면 막는다.
        attached, seen = observe_attachment(sim_view, model)
        attach.update(observed_attached=attached, observed_detail=seen)
        if attached is None:
            return answer(ATTACH_UNKNOWN, f"부착 기록이 '{attach['state']}'이고 관측으로도 확인하지 못했다({seen})")
        if attached:
            return answer(ATTACH_UNKNOWN, f"그리퍼에 붙어 있다(관측: {seen}) — 출발지에 놓인 자재가 아니다")
    if pose is None:
        return answer(OBSERVATION_MISSING, f"출발지 관측을 확인하지 못했다: {why}")
    center = _center(jobs, source, record, here)
    if center is None:
        return answer(OBSERVATION_MISSING, f"출발지({source}) 중심을 모른다 — 관측과 비교할 수 없다")
    gap = math.dist(pose, [float(v) for v in center])
    out["gap_m"] = round(gap, 4)
    if gap > ORIGIN_TOLERANCE_M:
        return answer(MISMATCH, f"기록은 {source}인데 관측 위치가 {gap:.3f} m 떨어져 있다(허용 {ORIGIN_TOLERANCE_M} m)")
    return answer(OK, "기록·부착·관측이 출발지와 맞다")


def check_for_action(jobs, sim_view, model: str, action: str, *, log_dir: Path | None = None) -> dict | None:
    """직접 요청 동작(transfer·return) → 출발·목적 실행기 id를 정해 확인한다. 다른 동작은 None(각자의 사전 검사)."""
    from validation.conveyor_slots import record_slot
    from validation.simulation_demo_state import origin_slot

    origin = origin_slot(jobs.workcell, model).support_id
    if action == "transfer":
        return check_material_start(jobs, sim_view, model, origin, "conveyor", log_dir=log_dir)
    if action == "return":
        record = ((jobs.status().get("state") or {}).get("objects") or {}).get(model)
        return check_material_start(jobs, sim_view, model, record_slot(record) if record else None, origin,
                                    log_dir=log_dir)
    return None
