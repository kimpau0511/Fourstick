"""시뮬레이션 사용자 시연의 **셀 상태 유지** 기록 (8-11 후속).

E2E 검증과 사용자 시연은 끝난 뒤의 셀 처리 방식이 다르다.

| 정책 | 쓰는 곳 | 성공 뒤 자재 | 이 모듈 기록 |
|---|---|---|---|
| `e2e_reset` | E2E 검증(기본값) | 선언된 원래 자리로 되돌린다 | 하지 않는다 |
| `simulation_demo_hold` | 명시적 시연 호출만 | 컨베이어에 **그대로 둔다** | 한다 |

상태 유지는 `simulation_demo_hold`를 **명시했을 때만** 고른다. 기본값은 E2E와
같은 `e2e_reset`이다.

## 무엇을 성공으로 기록하는가

`simulation_transfer_completed`(7개 조건 전부 관측)이고 정지 요청이 없으며
최종 pose를 관측한 경우만 `held_on_target`으로 남긴다. STOP·실패·고정 장치
결함은 **상태 유지 성공으로 기록하지 않는다.** 자재를 되돌리지 못했으면
`stopped_unrestored`·`fault_unrestored`로 남겨 수동 reset이 필요하다고 알린다.

셀에 남은 자재가 하나라도 있으면 `simulation_demo_reset_required=true`다 —
다음 시연은 자재 누락·배치 구역 점유로 시작하지 않는다.

## 원래 슬롯 복귀 (`--return-held-to-origin`)

`held_on_target` 기록이 있는 자재만 컨베이어 → **선언된 원래 팔레트 슬롯**으로
되돌린다. 슬롯은 셀 설정의 자재 프레임 부모(팔레트 프레임)에서만 찾는다.
복귀 전 네 조건을 모두 통과해야 움직인다 — 기록 · 현재 컨베이어 배치 ·
원래 슬롯 비점유 · 단계별 geometry/scene 재검증. 컨베이어 위 자재를 집는
**검증된 파지 자세**(grasp 파일의 `conveyor_grasp_poses`,
`scripts/derive_grasp_poses.py --conveyor` 측정)가 없으면 자세를 만들지 않고
`geometry.grasp_pose_unavailable`로 막는다. 자재가 측정 기준 위치(선언된
벨트 중심)에서 허용치 넘게 벗어나 있어도 그 자세로 집지 않는다.
원래 슬롯에 놓는 단계는 그 자재의 측정된 놓기 자세(`pallet_place_poses`,
`derive_grasp_poses.py --pallet-place`)가 있으면 그것을, 없으면 팔레트 파지
자세를 쓴다. 들고 있는 동안의 받침 접촉 예외는 만들지 않는다. 관측으로 슬롯 복귀를
확인했을 때만 그 자재 기록을 지운다. STOP·실패·관측 불가는 기록을 남긴다.

## STOP 체크포인트 (재개 실행 없음)

사용자 시연(`simulation_demo_hold` 정방향, 원래 슬롯 복귀) 중 STOP이 **확인되면**
이어서 계획할 수 있는 현재 상태를 체크포인트로 남긴다. STOP은 그대로 즉시
정지이고, 이 기록은 **재개를 열지 않는다**(`resume_available`은 항상 false).
다음 중 하나라도 없으면 만들지 않는다 — 확인된 STOP, 정지 요청 뒤의 새 관절
관측, 그리퍼 관측, 자재 pose 관측, 정지 시점 scene hash와 같은 현재 scene hash,
판별된 자재 상태(held/pallet/conveyor). 자재를 **들고 있으면** 같은 최신 관절의
FK + 파지 offset 기대 pose에 자재가 수렴했다는 관측(연속 fresh 표본)이 있어야
한다 — 없으면 `simulation_demo_checkpoint_unavailable`로 만들지 않는다(고정
장치의 늦은 `set_pose` 반영 중 캡처하면 자재 pose가 관절과 어긋난다, 실측
2026-09-18 B 83.4 mm). E2E 초기화·수동 restore·같은 자재의 완료된 새 실행은
그 자재의 체크포인트를 지운다.

## resume 사전검증·재계획 (실행 없음)

체크포인트가 있어도 `resume_available`은 false다. 명시적 사전검증
(`--resume-preflight`)이 **관측으로** 모두 통과했을 때만 `resume_preflight`에
`allowed=true`와 새 재계획(`resume_plan_id`)을 남긴다. 중단된 궤적을 재생하지
않는다 — 현재 관절에서 정지 단계의 목표 자세로 가는 **새 복구 접근**을 만들고,
그 뒤에는 체크포인트의 남은 단계만 쓴다. 하나라도 어긋나면 BLOCK하고
체크포인트는 그대로 둔다. 사전검증은 로봇 명령을 보내지 않는다.

## resume 실행 결과

`--resume-checkpoint <id>`는 실행 직전에 사전검증 조건을 **다시 전부** 관측한다.
실패하면 로봇 명령 없이 BLOCK이고 체크포인트·래치를 그대로 둔다(기록 안 함).
실행을 시작한 뒤에는 옛 체크포인트를 재사용 가능 상태로 남기지 않는다.

| 결과 | 자재 기록 | 체크포인트 |
|---|---|---|
| 완료(`simulation_transfer_resumed_completed`) | `held_on_target`(컨베이어) | 제거 |
| 확인된 STOP | `stopped_unrestored` | 옛 것 제거, 새 현재 상태로 교체 |
| 실패·미확인 STOP | `fault_unrestored`·`stopped_unrestored` + 사유 | 제거 |

## 축

이 기록은 **시뮬레이터 상태**다. `is_simulated`는 항상 true,
`real_hardware_ready`·`real_hardware_verified`는 항상 false다. 파일에서 다른
값이 읽히면 기록을 믿지 않는다(`available=false`).
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.reason_codes import ReasonCode

POLICY_E2E_RESET = "e2e_reset"
POLICY_DEMO_HOLD = "simulation_demo_hold"
POLICIES: tuple[str, ...] = (POLICY_E2E_RESET, POLICY_DEMO_HOLD)

#: 화면이 상태 유지 중에 보여줄 문구. API는 값만 싣는다.
DISPLAY_LABEL = "시뮬레이션 시연 · 상태 유지"

#: 시연 스크립트는 별도 프로세스라 파일로 남긴다(정지 래치와 같은 자리).
DEFAULT_PATH = Path("/tmp/forstick2_workcell/sim_demo_state.json")

SCHEMA = "forstick2.simulation_demo_state/1"

#: 셀에 남은 자재 상태.
HELD_ON_TARGET = "held_on_target"
STOPPED_UNRESTORED = "stopped_unrestored"
FAULT_UNRESTORED = "fault_unrestored"
#: 원래 슬롯 복귀를 시도했지만 확인되지 않았다(기록은 남는다).
RETURN_STOPPED = "return_stopped"
RETURN_FAILED = "return_failed"

#: STOP 체크포인트의 자재 상태.
OBJECT_HELD = "held"
OBJECT_PALLET = "pallet"
OBJECT_CONVEYOR = "conveyor"
OBJECT_UNKNOWN = "unknown"
#: 체크포인트를 만들지 못한 사유 코드(기록용).
CHECKPOINT_UNAVAILABLE = "simulation_demo_checkpoint_unavailable"

#: 원래 슬롯 복귀 판정 허용치(m). 시연 진입 조건 "자재가 선언된 자리에
#: 있는가"(`demo_workcell_pick_place.py`의 드리프트 0.02 m)와 같은 값이다.
ORIGIN_TOLERANCE_M = 0.02

_STATUS_COMPLETED = "simulation_transfer_completed"
_STATUS_STOPPED = "simulation_transfer_stopped"


def should_restore(*, policy: str, completed: bool, stop_requested: bool,
                   no_restore: bool) -> bool:
    """시연이 끝난 뒤 자재를 원래 자리로 되돌릴지.

    - STOP 뒤에는 되돌리지 않는다(정지 상태를 그대로 기록한다 — 기존 규칙).
    - `simulation_demo_hold`이고 이송이 **완료됐을 때만** 되돌리지 않는다.
      실패는 정책과 무관하게 되돌린다.
    """
    if policy not in POLICIES:
        raise ValueError(f"알 수 없는 셀 정책: {policy}")
    if no_restore or stop_requested:
        return False
    if policy == POLICY_DEMO_HOLD and completed:
        return False
    return True


def reset_command(model: str) -> str:
    return ("FORSTICK2_SIM_PICK_PLACE_DEMO=1 ./scripts/demo_workcell_pick_place.sh"
            f" - {model} --restore-only")


class SimulationDemoState:
    """시연 상태 파일. 쓰기는 시연 스크립트, 읽기는 서버가 한다."""

    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)

    # ── 읽기 ─────────────────────────────────────────────────────────────
    def _read(self) -> dict:
        """원본 기록. 없으면 빈 기록, 읽지 못하면 예외."""
        if not self.path.is_file():
            return {"objects": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("objects", {}), dict):
            raise ValueError("기록 형식이 아니다")
        if (data.get("is_simulated") is not True
                or data.get("real_hardware_ready") is not False
                or data.get("real_hardware_verified") is not False):
            raise ValueError("시뮬레이터 축 불변식이 깨진 기록이다")
        data.setdefault("objects", {})
        return data

    def _write(self, data: dict) -> None:
        data = dict(data)
        data.update({"schema": SCHEMA, "is_simulated": True,
                     "real_hardware_ready": False,
                     "real_hardware_verified": False,
                     "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        os.replace(temp, self.path)

    def _read_for_update(self) -> dict:
        # 깨진 기록 위에 덮어쓰면 남은 자재 정보를 잃는다. 원인을 남기고 새로 쓴다.
        try:
            return self._read()
        except (OSError, ValueError) as exc:
            return {"objects": {}, "previous_record_unreadable":
                    f"{type(exc).__name__}: {exc}"[:200]}

    def objects(self) -> dict:
        try:
            return dict(self._read().get("objects") or {})
        except (OSError, ValueError):
            return {}

    # ── 쓰기 ─────────────────────────────────────────────────────────────
    def record_run(self, *, policy: str, model: str, result: Mapping[str, Any],
                   final_pose_m: Sequence[float] | None, restored: bool | None,
                   ) -> dict | None:
        """시연 한 번의 결과를 반영한다. `e2e_reset`이면 **아무것도 쓰지 않는다.**

        `result`는 `SimulationTransferResult.to_dict()` 형태다.
        `restored`: 끝난 뒤 되돌렸고 확인됐으면 True, 되돌리지 않았으면 None,
        되돌렸지만 확인하지 못했으면 False.
        """
        if policy not in POLICIES:
            raise ValueError(f"알 수 없는 셀 정책: {policy}")
        if policy != POLICY_DEMO_HOLD:
            return None
        data = self._read_for_update()
        objects = dict(data.get("objects") or {})
        status = str(result.get("status") or "")
        stop_requested = bool(result.get("stop_requested"))
        completed = (status == _STATUS_COMPLETED
                     and result.get("simulation_e2e") is True
                     and not stop_requested
                     and final_pose_m is not None)
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        entry_base = {
            "scenario": result.get("scenario"),
            "object_id": result.get("object_id"),
            "support_id": result.get("support_id"),
            "target_id": result.get("target_id"),
            "pose_m": (None if final_pose_m is None
                       else [round(float(v), 6) for v in final_pose_m]),
            "recorded_at": now,
            "reset_command": reset_command(model),
        }
        if completed:
            objects[model] = {**entry_base, "state": HELD_ON_TARGET}
        elif status == _STATUS_STOPPED or stop_requested:
            objects[model] = {**entry_base, "state": STOPPED_UNRESTORED}
        elif restored is True:
            # 실패했지만 원래 자리로 되돌렸다(확인됨). 이 자재는 셀에 남지 않는다.
            objects.pop(model, None)
        elif status != "simulation_transfer_not_started" or restored is False:
            objects[model] = {**entry_base, "state": FAULT_UNRESTORED}
        data["objects"] = objects
        if completed or restored is True:
            # 이 자재의 이전 정지 상태는 더 이상 셀과 맞지 않는다.
            self._drop_checkpoint(data, model)
        data["policy"] = POLICY_DEMO_HOLD
        data["last_run"] = {
            "model": model, "scenario": result.get("scenario"),
            "status": status, "stop_requested": stop_requested,
            "reason_codes": list(result.get("reason_codes") or ()),
            "hold_recorded": completed, "restored": restored,
            "recorded_at": now,
        }
        self._write(data)
        return data["last_run"]

    def mark_restored(self, model: str, *, verified: bool,
                      reason: str = "manual_restore") -> None:
        """자재를 원래 자리로 되돌렸다. **확인됐을 때만** 셀 기록에서 뺀다."""
        if not verified:
            return
        data = self._read_for_update()
        objects = dict(data.get("objects") or {})
        objects.pop(model, None)
        data["objects"] = objects
        self._drop_checkpoint(data, model)
        data["last_reset"] = {"model": model, "reason": reason,
                              "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        self._write(data)

    # ── 원래 슬롯 복귀 ──────────────────────────────────────────────────
    # ── STOP 체크포인트 ─────────────────────────────────────────────────
    @staticmethod
    def _drop_checkpoint(data: dict, model: str) -> None:
        checkpoints = dict(data.get("checkpoints") or {})
        if checkpoints.pop(model, None) is not None:
            data["checkpoints"] = checkpoints
        # 사전검증은 체크포인트에 딸린 기록이다. 함께 지운다.
        preflights = dict(data.get("resume_preflight") or {})
        if preflights.pop(model, None) is not None:
            data["resume_preflight"] = preflights
        unavailable = dict(data.get("checkpoint_unavailable") or {})
        if unavailable.pop(model, None) is not None:
            data["checkpoint_unavailable"] = unavailable

    def record_resume_preflight(self, model: str, record: Mapping[str, Any]) -> dict:
        """사전검증 결과를 남긴다. **체크포인트는 바꾸지 않는다.**"""
        data = self._read_for_update()
        checkpoint = (data.get("checkpoints") or {}).get(model)
        if checkpoint is None or checkpoint.get("checkpoint_id") != record.get(
                "checkpoint_id"):
            raise ValueError("현재 체크포인트에 대한 사전검증이 아니다")
        if record.get("allowed") is True and not record.get("resume_plan_id"):
            raise ValueError("통과한 사전검증에는 재계획 id가 있어야 한다")
        preflights = dict(data.get("resume_preflight") or {})
        preflights[model] = {**dict(record), "is_simulated": True,
                             "resume_executed": False}
        data["resume_preflight"] = preflights
        self._write(data)
        return dict(preflights[model])

    def record_checkpoint(self, checkpoint: Mapping[str, Any]) -> dict:
        """`build_stop_checkpoint`가 만든 체크포인트만 남긴다. 자재별 최신 1개."""
        if (checkpoint.get("is_simulated") is not True
                or checkpoint.get("stop_confirmed") is not True
                or checkpoint.get("resume_available") is not False
                or checkpoint.get("object_state") not in (
                    OBJECT_HELD, OBJECT_PALLET, OBJECT_CONVEYOR)):
            raise ValueError("체크포인트 계약을 만족하지 않는다")
        data = self._read_for_update()
        checkpoints = dict(data.get("checkpoints") or {})
        checkpoints[str(checkpoint["model"])] = dict(checkpoint)
        data["checkpoints"] = checkpoints
        unavailable = dict(data.get("checkpoint_unavailable") or {})
        if unavailable.pop(str(checkpoint["model"]), None) is not None:
            data["checkpoint_unavailable"] = unavailable
        self._write(data)
        return dict(checkpoint)

    def record_checkpoint_unavailable(self, model: str,
                                      details: Mapping[str, Any]) -> None:
        """확인된 STOP이었지만 체크포인트를 만들지 못한 사유를 남긴다.

        자재 기록(`stopped_unrestored` → reset_required)과 래치는 건드리지 않는다.
        """
        data = self._read_for_update()
        rows = dict(data.get("checkpoint_unavailable") or {})
        rows[model] = {**dict(details), "reason_code": CHECKPOINT_UNAVAILABLE,
                       "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        data["checkpoint_unavailable"] = rows
        self._write(data)

    def checkpoints(self) -> dict:
        try:
            return dict(self._read().get("checkpoints") or {})
        except (OSError, ValueError):
            return {}

    def record_resume(self, model: str, *, checkpoint_id: str, outcome: str,
                      final_pose_m: Sequence[float] | None,
                      reasons: Sequence[str] = (),
                      new_checkpoint: Mapping[str, Any] | None = None) -> dict:
        """resume **실행 뒤** 결과를 반영한다. 옛 체크포인트는 어떤 경우에도 뺀다.

        outcome: `completed` · `stopped` · `failed`.
        """
        if outcome not in ("completed", "stopped", "failed"):
            raise ValueError(f"알 수 없는 resume 결과: {outcome}")
        if new_checkpoint is not None and outcome != "stopped":
            raise ValueError("새 체크포인트는 확인된 STOP에서만 만든다")
        data = self._read_for_update()
        old = dict((data.get("checkpoints") or {}).get(model) or {})
        if old.get("checkpoint_id") != checkpoint_id:
            raise ValueError("현재 체크포인트에 대한 resume 결과가 아니다")
        self._drop_checkpoint(data, model)
        objects = dict(data.get("objects") or {})
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        entry = {
            "scenario": f"{old.get('source_id')}/{old.get('object_id')}"
                        f"->{old.get('destination_id')}",
            "object_id": old.get("object_id"), "support_id": old.get("source_id"),
            "target_id": old.get("destination_id"),
            "pose_m": (None if final_pose_m is None
                       else [round(float(v), 6) for v in final_pose_m]),
            "recorded_at": now, "reset_command": reset_command(model),
            "resumed_from_checkpoint": checkpoint_id,
        }
        if outcome == "completed":
            objects[model] = {**entry, "state": HELD_ON_TARGET}
        elif outcome == "stopped":
            objects[model] = {**entry, "state": STOPPED_UNRESTORED,
                              "reasons": list(reasons)}
        else:
            objects[model] = {**entry, "state": FAULT_UNRESTORED,
                              "reasons": list(reasons)}
        data["objects"] = objects
        data["policy"] = POLICY_DEMO_HOLD
        data["last_run"] = {
            "model": model, "mode": "resume_checkpoint",
            "status": ("simulation_transfer_resumed_completed"
                       if outcome == "completed" else f"resume_{outcome}"),
            "resumed_from_checkpoint": checkpoint_id,
            "hold_recorded": outcome == "completed",
            "reasons": list(reasons), "recorded_at": now,
        }
        if new_checkpoint is not None:
            if (new_checkpoint.get("stop_confirmed") is not True
                    or new_checkpoint.get("is_simulated") is not True
                    or new_checkpoint.get("resume_available") is not False
                    or new_checkpoint.get("checkpoint_id") == checkpoint_id):
                raise ValueError("새 체크포인트 계약을 만족하지 않는다")
            checkpoints = dict(data.get("checkpoints") or {})
            checkpoints[model] = dict(new_checkpoint)
            data["checkpoints"] = checkpoints
        self._write(data)
        return data["last_run"]

    def held_record(self, model: str) -> dict | None:
        """복귀 대상 기록. `held_on_target`만 복귀를 허용한다."""
        row = self.objects().get(model)
        if not row or row.get("state") != HELD_ON_TARGET:
            return None
        return dict(row)

    def record_return(self, model: str, *, completed: bool, stop_requested: bool,
                      final_pose_m: Sequence[float] | None,
                      origin_home_m: Sequence[float],
                      reason_codes: Sequence[str] = (), detail: str = "",
                      ) -> dict:
        """복귀 시도 결과를 반영한다. **관측으로 슬롯 복귀를 확인했을 때만** 지운다."""
        data = self._read_for_update()
        objects = dict(data.get("objects") or {})
        if model not in objects:
            raise ValueError(f"복귀 기록 대상이 아니다: {model}")
        gap = (None if final_pose_m is None
               else math.dist([float(v) for v in final_pose_m],
                              [float(v) for v in origin_home_m]))
        returned = bool(completed and not stop_requested and gap is not None
                        and gap <= ORIGIN_TOLERANCE_M)
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        attempt = {
            "at": now, "completed": bool(completed),
            "stop_requested": bool(stop_requested),
            "final_pose_m": (None if final_pose_m is None
                             else [round(float(v), 6) for v in final_pose_m]),
            "origin_home_m": [round(float(v), 6) for v in origin_home_m],
            "gap_m": None if gap is None else round(gap, 6),
            "tolerance_m": ORIGIN_TOLERANCE_M,
            "reason_codes": list(reason_codes), "detail": detail[:300],
            "returned_to_origin": returned,
        }
        if returned:
            objects.pop(model)
            self._drop_checkpoint(data, model)
            data["last_reset"] = {"model": model, "reason": "return_to_origin",
                                  "at": now}
        else:
            row = dict(objects[model])
            row.setdefault("held_pose_m", row.get("pose_m"))
            row["pose_m"] = attempt["final_pose_m"]
            row["state"] = RETURN_STOPPED if stop_requested else RETURN_FAILED
            row["return_attempt"] = attempt
            objects[model] = row
        data["objects"] = objects
        data["last_run"] = {"model": model, "mode": "return_held_to_origin",
                            "returned_to_origin": returned,
                            "hold_recorded": False, "recorded_at": now,
                            "reason_codes": list(reason_codes)}
        self._write(data)
        return attempt

    # ── API 상태 ──────────────────────────────────────────────────────────
    def status(self) -> dict:
        """API에 싣는 값. 화면 표시는 `display_label`로만 한다(UI 수정 없음)."""
        base = {"is_simulated": True, "real_hardware_ready": False,
                "real_hardware_verified": False, "source": str(self.path)}
        try:
            data = self._read()
        except (OSError, ValueError) as exc:
            # 읽지 못하면 reset이 필요 없다고 말하지 않는다.
            return {**base, "available": False,
                    "detail": f"시연 상태를 읽지 못했다: {type(exc).__name__}"[:200],
                    "state_hold_active": None, "display_label": None,
                    "simulation_demo_reset_required": None,
                    "objects": {}, "reset_reasons": [],
                    "checkpoint_available": None, "checkpoint_id": None,
                    "object_state": None, "resume_available": False,
                    "checkpoint": None}
        objects = dict(data.get("objects") or {})
        held = sorted(m for m, row in objects.items()
                      if row.get("state") == HELD_ON_TARGET)
        reasons = [{"model": m, "state": row.get("state"),
                    "reset_command": row.get("reset_command")}
                   for m, row in sorted(objects.items())]
        return {
            **base,
            "available": True,
            "policy": data.get("policy"),
            "state_hold_active": bool(held),
            "display_label": DISPLAY_LABEL if held else None,
            "held_objects": held,
            "simulation_demo_reset_required": bool(objects),
            "reset_reasons": reasons,
            "objects": objects,
            "last_run": data.get("last_run"),
            "last_reset": data.get("last_reset"),
            "updated_at": data.get("updated_at"),
            **_checkpoint_status(data),
        }


# ── 원래 슬롯 복귀: 선언 조회·단계·사전 조건 ─────────────────────────────
@dataclass(frozen=True)
class OriginSlot:
    """자재의 원래 슬롯. **셀 설정의 프레임 부모 관계에서만** 나온다."""

    model: str
    object_id: str
    support_model: str
    support_id: str
    frame: str
    support_frame: str
    home_pose_m: tuple[float, float, float]
    size_m: tuple[float, float, float]


def _resolve(frames: Mapping[str, Any], name: str | None) -> tuple[float, float, float]:
    out = [0.0, 0.0, 0.0]
    while name is not None:
        out = [a + b for a, b in zip(out, frames[name]["xyz_m"])]
        name = frames[name]["parent"]
    return (out[0], out[1], out[2])


def origin_slot(workcell: Mapping[str, Any], model: str) -> OriginSlot:
    """자재 프레임의 부모가 가리키는 팔레트를 찾는다. 없거나 모호하면 예외."""
    models = workcell["models"]
    frames = workcell["frames"]
    row = models.get(model)
    if not row or row.get("kind") != "material":
        raise ValueError(f"셀 선언의 자재가 아니다: {model}")
    frame = row["frame"]
    parent = (frames.get(frame) or {}).get("parent")
    supports = [name for name, item in models.items()
                if item.get("kind") == "pallet" and item.get("frame") == parent]
    if len(supports) != 1:
        raise ValueError(f"{model}의 원래 팔레트가 선언에서 하나로 정해지지"
                         f" 않는다 (부모 프레임 {parent}, 후보 {supports})")
    support = supports[0]
    return OriginSlot(
        model=model, object_id=str(row["resource_id"]),
        support_model=support, support_id=str(models[support]["resource_id"]),
        frame=frame, support_frame=str(parent),
        home_pose_m=_resolve(frames, frame),
        size_m=tuple(float(v) for v in row["size_m"]))


#: 컨베이어 위 자재 파지 자세가 있는 grasp 파일 키. 팔레트 파지(`poses`)와
#: 따로 둔다 — 로더가 자재별 파지 자세를 `poses`에서 잇기 때문이다.
CONVEYOR_GRASP_KEY = "conveyor_grasp_poses"


def _conveyor_grasp_entry(grasp_config: Mapping[str, Any], name: str | None):
    if not name:
        return {}
    return dict((grasp_config.get(CONVEYOR_GRASP_KEY) or {}).get(name) or {})


def conveyor_grasp_pose(grasp_config: Mapping[str, Any], *, object_id: str,
                        target_id: str) -> str | None:
    """**target 위에 놓인** 자재를 집는 검증된 파지 자세 이름. 없으면 None.

    팔레트 파지 자세나 놓기 자세로 대신하지 않는다 — 놓기 자세는 자재를
    벨트 위에서 떨어뜨리는 높이라 안착한 자재를 쥐는 높이가 아니다.
    """
    for name, pose in (grasp_config.get(CONVEYOR_GRASP_KEY) or {}).items():
        if (pose.get("status") == "verified" and pose.get("kind") == "grasp"
                and pose.get("object_resource_id") == object_id
                and pose.get("support_resource_id") == target_id
                and pose.get("joint_rad")):
            return str(name)
    return None


#: 원래 슬롯 놓기 자세가 있는 grasp 파일 키.
PALLET_PLACE_KEY = "pallet_place_poses"


def pallet_place_pose(grasp_config: Mapping[str, Any], *, object_id: str,
                      support_id: str) -> str | None:
    """든 자재를 원래 슬롯에 놓는 측정·검증된 자세 이름. 없으면 None."""
    for name, pose in (grasp_config.get(PALLET_PLACE_KEY) or {}).items():
        if (pose.get("status") == "verified" and pose.get("kind") == "place"
                and pose.get("object_resource_id") == object_id
                and pose.get("support_resource_id") == support_id
                and pose.get("joint_rad")):
            return str(name)
    return None


def conveyor_grasp_center(grasp_config: Mapping[str, Any],
                          name: str | None) -> tuple[float, float, float] | None:
    """그 파지 자세를 측정한 자재 중심(선언 기준). 없으면 None."""
    center = _conveyor_grasp_entry(grasp_config, name).get("object_world_center_m")
    return None if not center else tuple(float(v) for v in center)


RETURN_LABELS: Mapping[str, str] = {
    "home_start": "안전 home",
    "pick_approach": "컨베이어 접근",
    "pre_grasp": "pre-grasp(컨베이어)",
    "gripper_open_before_grasp": "그리퍼 열기",
    "grasp_approach": "컨베이어 pick 접근",
    "gripper_close": "그리퍼 닫기",
    "lift": "lift",
    "place_approach": "원래 팔레트 접근",
    "place_descend": "원래 슬롯 접근",
    "gripper_open_release": "그리퍼 열기(해제)",
    "retreat": "retreat",
    "home_end": "안전 home 복귀",
}


def return_bindings(bindings, *, object_id: str, target_id: str,
                    grasp_config: Mapping[str, Any] | None = None,
                    origin_id: str | None = None):
    """복귀 검사용 선언. 컨베이어 파지 단계의 받침면을 **컨베이어로** 둔다.

    허용 접촉을 넓히지 않는다 — 받침 예외는 원래 규칙 그대로 파지 단계에만
    적용되고, 그 받침이 지금 자재가 놓인 곳(컨베이어)일 뿐이다. 컨베이어 파지
    자세에 붙일 물체 선언이 있으면 그것을 쓴다.
    """
    support = dict(bindings.object_support)
    support[object_id] = target_id
    attached = {rid: dict(spec) for rid, spec in bindings.attached.items()}
    poses = {name: dict(joints) for name, joints in bindings.poses.items()}
    evidence = {name: dict(row) for name, row in bindings.pose_evidence.items()}
    if grasp_config is not None:
        name = conveyor_grasp_pose(grasp_config, object_id=object_id,
                                   target_id=target_id)
        entry = _conveyor_grasp_entry(grasp_config, name)
        if entry:
            # 관절값은 **측정 결과 파일에서만** 온다.
            poses[name] = {k: float(v) for k, v in entry["joint_rad"].items()}
            evidence[name] = {k: entry.get(k) for k in (
                "status", "kind", "target_world_xyz_m", "position_error_m",
                "orientation_error_rad", "usable_dz_range_m", "sweep_step_m",
                "measurement_method", "source")}
            if entry.get("attached_object"):
                attached[object_id] = dict(entry["attached_object"])
        place = (pallet_place_pose(grasp_config, object_id=object_id,
                                   support_id=origin_id) if origin_id else None)
        if place:
            row = dict(grasp_config[PALLET_PLACE_KEY][place])
            poses[place] = {k: float(v) for k, v in row["joint_rad"].items()}
            evidence[place] = {k: row.get(k) for k in (
                "status", "kind", "target_world_xyz_m", "position_error_m",
                "orientation_error_rad", "usable_dz_range_m", "sweep_step_m",
                "measurement_method", "source")}
    return replace(bindings, object_support=support, attached=attached,
                   poses=poses, pose_evidence=evidence)


def build_return_stages(bindings, *, object_id: str, origin_id: str,
                        target_id: str, conveyor_grasp: str | None,
                        origin_place: str | None = None):
    """컨베이어 → 원래 슬롯 12단계. **검증된 자세가 없으면 만들지 않는다.**

    반환: (단계들, [(이유 코드, 설명)]).
    """
    from validation.pick_place_plan import (
        GRIPPER_STAGES,
        STAGE_SEQUENCE,
        PickPlaceStage,
    )

    home = bindings.home_pose
    conveyor_approach = bindings.approach_pose.get(target_id)
    origin_approach = bindings.approach_pose.get(origin_id)
    # 측정된 놓기 자세가 있으면 그것을 쓴다. 없으면 팔레트 파지 자세다.
    origin_place = origin_place or bindings.grasp_pose.get(object_id)
    needed = {
        "home_start": home,
        "pick_approach": conveyor_approach,
        "grasp_approach": conveyor_grasp,
        "place_approach": origin_approach,
        "place_descend": origin_place,
    }
    #: 파지 자세가 없는 것은 IK·작업공간 불가가 아니다. 이유를 나눈다.
    grasp_stages = {"grasp_approach", "place_descend"}
    findings = []
    for stage, pose in needed.items():
        if pose is None or pose not in bindings.poses:
            code = (ReasonCode.GEOMETRY_GRASP_POSE_UNAVAILABLE
                    if stage in grasp_stages else ReasonCode.CONFIG_MISSING)
            findings.append((
                code,
                f"{RETURN_LABELS[stage]} 단계에 쓸 검증된 자세가 없다"
                f" (필요한 자세: {pose or '미지정'}) — 자세를 만들지 않는다"))
    if origin_place is not None and bindings.object_support.get(object_id) not in (
            origin_id, target_id):
        findings.append((ReasonCode.PLAN_RESOURCE_MISMATCH,
                         f"{object_id}의 원래 슬롯 파지 자세가 {origin_id} 선언과"
                         " 맞지 않는다"))
    open_rad = bindings.gripper_open_rad
    grasp_rad = bindings.gripper_grasp_rad
    if open_rad is None or grasp_rad is None:
        findings.append((ReasonCode.CONFIG_MISSING,
                         "그리퍼 열림·파지 명령값의 근거가 없다"))
    if findings:
        return (), findings

    obj, source, target = object_id, target_id, origin_id
    layout = (
        ("home_start", home, open_rad, None, ()),
        ("pick_approach", conveyor_approach, open_rad, None, (source,)),
        ("pre_grasp", conveyor_approach, open_rad, None, (source,)),
        ("gripper_open_before_grasp", conveyor_approach, open_rad, None, (source,)),
        ("grasp_approach", conveyor_grasp, open_rad, None, (obj, source)),
        ("gripper_close", conveyor_grasp, grasp_rad, obj, (obj, source)),
        ("lift", conveyor_approach, grasp_rad, obj, (obj, source)),
        ("place_approach", origin_approach, grasp_rad, obj, (obj, target)),
        ("place_descend", origin_place, grasp_rad, obj, (obj, target)),
        ("gripper_open_release", origin_place, open_rad, None, (target,)),
        ("retreat", origin_approach, open_rad, None, (target,)),
        ("home_end", home, open_rad, None, ()),
    )
    assert tuple(row[0] for row in layout) == STAGE_SEQUENCE
    stages = []
    for no, (stage, pose, gripper, holds, resources) in enumerate(layout, start=1):
        stages.append(PickPlaceStage(
            no=no, stage=stage, label=RETURN_LABELS[stage],
            kind="gripper" if stage in GRIPPER_STAGES else "arm_motion",
            pose_name=pose, joint_rad=dict(bindings.poses[pose]),
            gripper_joint_rad=gripper, holds_object=holds,
            resources=tuple(resources),
            scene_models=tuple(m for m in (bindings.scene_model(r)
                                           for r in resources) if m),
            frames=tuple(f for f in (bindings.frame(r) for r in resources) if f),
            source_step=None,
            detail="시뮬레이션 시연 복귀 — 컨베이어에서 원래 슬롯으로"))
    return tuple(stages), []


def slot_occupant(slot: OriginSlot, others: Mapping[str, tuple]) -> str | None:
    """원래 슬롯 자리를 차지한 **다른** 자재. others: {모델: (pose, size)}.

    두 자재의 바닥 사각형이 겹치고 높이가 슬롯 자재 높이 안이면 점유다.
    pose를 관측하지 못한 자재는 호출하는 쪽이 따로 막는다.
    """
    hx, hy, hz = (v / 2 for v in slot.size_m)
    for model, (pose, size) in sorted(others.items()):
        if model == slot.model or pose is None:
            continue
        ox, oy, _ = (float(v) / 2 for v in size)
        if (abs(pose[0] - slot.home_pose_m[0]) < hx + ox
                and abs(pose[1] - slot.home_pose_m[1]) < hy + oy
                and abs(pose[2] - slot.home_pose_m[2]) < 2 * hz):
            return model
    return None


def return_preflight(*, record: Mapping[str, Any] | None, model: str,
                     observed_pose: Sequence[float] | None,
                     on_conveyor: tuple[bool, str] | None,
                     occupant: str | None, unobserved_others: Sequence[str] = (),
                     grasp_object_center: Sequence[float] | None = None,
                     stage_findings: Sequence[tuple] = (),
                     check_findings: Sequence[tuple] = (),
                     scene_stable: bool | None = None) -> list[tuple]:
    """복귀 전 조건. 하나라도 걸리면 [(이유 코드, 설명)]을 돌려준다.

    순서: 기록 → 현재 컨베이어 배치 → 원래 슬롯 비점유 → geometry/scene.
    """
    if record is None or record.get("state") != HELD_ON_TARGET:
        return [(ReasonCode.PLAN_RESOURCE_MISMATCH,
                 f"{model}은(는) 시뮬레이션 시연 상태 유지 기록(held_on_target)이"
                 " 없다 — 복귀 대상이 아니다")]
    out: list[tuple] = []
    if observed_pose is None:
        out.append((ReasonCode.EXEC_UNVERIFIABLE,
                    f"{model}의 현재 pose를 관측하지 못했다"))
    elif on_conveyor is None or not on_conveyor[0]:
        out.append((ReasonCode.EXEC_SIM_OBJECT_ABSENT,
                    f"{model}이 컨베이어 배치 구역에 없다"
                    f" ({'' if on_conveyor is None else on_conveyor[1]})"))
    elif grasp_object_center is not None:
        # 파지 자세는 이 위치에 놓인 자재로 측정했다. 벗어나면 그 자세로 집지 않는다.
        gap = math.dist([float(v) for v in observed_pose],
                        [float(v) for v in grasp_object_center])
        if gap > ORIGIN_TOLERANCE_M:
            out.append((ReasonCode.EXEC_SIM_PLACEMENT_OUT_OF_ZONE,
                        f"{model}이 컨베이어 파지 측정 위치에서 {gap:.4f} m 벗어났다"
                        f" (허용 {ORIGIN_TOLERANCE_M} m)"))
    if occupant is not None:
        out.append((ReasonCode.EXEC_SIM_TARGET_OCCUPIED,
                    f"원래 슬롯이 {occupant}로 점유됐다"))
    if unobserved_others:
        out.append((ReasonCode.EXEC_UNVERIFIABLE,
                    "원래 슬롯 점유를 확인하지 못했다 — pose 관측 불가:"
                    f" {', '.join(unobserved_others)}"))
    out.extend(stage_findings)
    out.extend(check_findings)
    if not stage_findings and scene_stable is not True:
        out.append((ReasonCode.GEOMETRY_SNAPSHOT_EXPIRED,
                    "복귀 단계 검사 중 planning scene이 바뀌었거나 검사하지"
                    " 못했다"))
    return out


# ── STOP 체크포인트 ─────────────────────────────────────────────────────
def _checkpoint_status(data: Mapping[str, Any]) -> dict:
    """API용 체크포인트 요약. 가장 최근 것을 대표로 싣는다. 재개는 열지 않는다."""
    checkpoints = dict(data.get("checkpoints") or {})
    latest = max(checkpoints.values(), key=lambda row: row.get("captured_at_epoch", 0),
                 default=None)
    preflight = None
    if latest is not None:
        row = (data.get("resume_preflight") or {}).get(latest.get("model"))
        if row and row.get("checkpoint_id") == latest.get("checkpoint_id"):
            preflight = dict(row)
    return {
        # 명시적 사전검증 결과(없으면 null). 통과해도 실행은 열리지 않는다.
        "resume_preflight": preflight,
        "checkpoint_available": latest is not None,
        "checkpoint_id": None if latest is None else latest.get("checkpoint_id"),
        "object_state": None if latest is None else latest.get("object_state"),
        # 재개 실행은 아직 없다. 체크포인트가 있어도 **항상 false**다.
        "resume_available": False,
        "checkpoint": None if latest is None else dict(latest),
        "checkpoints": sorted(checkpoints),
        # 확인된 STOP이었지만 수렴 등으로 체크포인트를 만들지 못한 기록.
        "checkpoint_unavailable": dict(data.get("checkpoint_unavailable") or {}),
    }


def classify_object_state(*, held: bool, pose_m: Sequence[float] | None,
                          conveyor_zone: Mapping[str, Any] | None,
                          origin_home_m: Sequence[float]) -> str:
    """정지 뒤 자재 상태. 관측으로 정해지지 않으면 `unknown`이다."""
    from validation.simulation_e2e import in_placement_zone

    if held:
        return OBJECT_HELD
    if pose_m is None:
        return OBJECT_UNKNOWN
    if math.dist([float(v) for v in pose_m],
                 [float(v) for v in origin_home_m]) <= ORIGIN_TOLERANCE_M:
        return OBJECT_PALLET
    if conveyor_zone is not None and in_placement_zone(pose_m, conveyor_zone)[0]:
        return OBJECT_CONVEYOR
    return OBJECT_UNKNOWN


def build_stop_checkpoint(
    *, run_id: str, mode: str, model: str, object_id: str, source_id: str,
    destination_id: str, origin_slot_id: str, stage_names: Sequence[str],
    stopped_stage: str, completed_stages: Sequence[str], stop_confirmed: bool | None,
    stop_execution_id: str, stop_requested_at: float,
    joint_observation: Mapping[str, Any] | None, gripper_joint: str,
    held: bool, object_pose_m: Sequence[float] | None,
    conveyor_zone: Mapping[str, Any] | None, origin_home_m: Sequence[float],
    scene_hash_at_stop: str | None, scene_hash_now: str | None,
    convergence: Mapping[str, Any] | None = None,
) -> tuple[dict | None, list[str]]:
    """확인된 STOP 뒤의 현재 상태. 조건이 하나라도 빠지면 (None, 사유들).

    **재개하지 않는다.** 이 함수는 기록할 값을 만들 뿐이다.
    """
    reasons: list[str] = []
    if stop_confirmed is not True:
        reasons.append("STOP이 확인되지 않았다(unconfirmed)")
    obs = dict(joint_observation or {})
    positions = dict(obs.get("positions") or {})
    observed_at = obs.get("observed_at")
    if not obs.get("valid") or not positions:
        reasons.append("관절 관측이 없다")
    elif observed_at is None or float(observed_at) < float(stop_requested_at):
        reasons.append("정지 요청 뒤의 새 관절 관측이 아니다")
    if gripper_joint not in positions:
        reasons.append("그리퍼 관측이 없다")
    if object_pose_m is None:
        reasons.append("자재 pose를 관측하지 못했다")
    if not scene_hash_at_stop or not scene_hash_now:
        reasons.append("scene hash가 없다")
    elif scene_hash_at_stop != scene_hash_now:
        reasons.append("정지 뒤 scene이 바뀌었다"
                       f" ({scene_hash_at_stop[:12]} → {scene_hash_now[:12]})")
    state = classify_object_state(held=held, pose_m=object_pose_m,
                                  conveyor_zone=conveyor_zone,
                                  origin_home_m=origin_home_m)
    if state == OBJECT_UNKNOWN:
        reasons.append("자재 상태를 판별하지 못했다(unknown)")
    if stopped_stage not in stage_names:
        reasons.append(f"정지 단계를 계획에서 찾지 못했다: {stopped_stage}")
    if held:
        # 든 자재는 도구 위치(관절 FK + 파지 offset)에 수렴했다는 관측이 있어야 한다.
        if not convergence or convergence.get("converged") is not True:
            detail = ("수렴 관측이 없다" if not convergence else
                      f"기대 pose와 {convergence.get('error_m')} m"
                      f" (허용 {convergence.get('tolerance_m')} m),"
                      f" 대기 {convergence.get('settle_elapsed_s')} s")
            reasons.append(f"{CHECKPOINT_UNAVAILABLE}: 자재가 도구 위치에"
                           f" 수렴하지 않았다 — {detail}")
        elif object_pose_m is not None and convergence.get("observed_pose_m") and (
                math.dist([float(v) for v in object_pose_m],
                          [float(v) for v in convergence["observed_pose_m"]]) > 1e-9):
            reasons.append(f"{CHECKPOINT_UNAVAILABLE}: 저장할 자재 pose가 수렴 관측과"
                           " 다르다")
    if reasons:
        return None, reasons

    import uuid

    index = list(stage_names).index(stopped_stage)
    now = time.time()
    checkpoint = {
        "schema": "forstick2.simulation_demo_checkpoint/1",
        "checkpoint_id": f"simckpt_{uuid.uuid4().hex[:16]}",
        "run_id": run_id, "mode": mode, "model": model, "object_id": object_id,
        "source_id": source_id, "destination_id": destination_id,
        "origin_slot_id": origin_slot_id,
        "previous_stage": completed_stages[-1] if completed_stages else None,
        "stopped_stage": stopped_stage,
        # 정지된 단계는 끝나지 않았다 — 남은 단계에 포함한다.
        "remaining_stages": list(stage_names[index:]),
        "completed_stages": list(completed_stages),
        "joint_state": {"positions": {k: round(float(v), 6)
                                      for k, v in positions.items()},
                        "observed_at": float(observed_at)},
        "gripper": {"joint": gripper_joint,
                    "position_rad": round(float(positions[gripper_joint]), 6)},
        "object_state": state,
        "object_pose_m": [round(float(v), 6) for v in object_pose_m],
        "scene_hash": scene_hash_now,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
        "captured_at_epoch": now,
        "stop_execution_id": stop_execution_id,
        "stop_confirmed": True,
        # 도구-자재 수렴 관측(든 자재일 때). 캡처 pose의 근거다.
        "convergence": None if convergence is None else dict(convergence),
        "is_simulated": True,
        "real_hardware_ready": False,
        "real_hardware_verified": False,
        "resume_available": False,
        "note": "시뮬레이터 정지 상태 기록이다. 재개 실행은 없고 자동 재개하지"
                " 않는다. 셀을 다시 준비하려면 --restore-only 또는 E2E 초기화",
    }
    return checkpoint, []


# ── resume 사전검증·재계획 ─────────────────────────────────────────────
#: 복구 접근 경로 표본 간격(rad). 실행 도달 판정 허용치
#: (`demo_workcell_pick_place.JOINT_TOLERANCE_RAD` 0.05)와 같은 값이다.
RESUME_PATH_STEP_RAD = 0.05
#: 현재 관절이 체크포인트와 같다고 볼 허용치(rad). 위와 같은 값이다.
RESUME_JOINT_TOLERANCE_RAD = 0.05
#: 자재 pose가 체크포인트와 같다고 볼 허용치(m). 고정 장치 pose 확인
#: 허용치(`sim_fixture.POSE_VERIFY_TOLERANCE_M`)와 같다.
RESUME_POSE_TOLERANCE_M = 0.005
#: 지금 재계획을 만들 수 있는 정지 단계. 도구가 자재를 든 채 목적지 접근
#: 자세로 가던 중이다. 다른 단계는 아직 계획하지 않고 막는다.
RESUMABLE_STAGES = frozenset({"place_approach"})


def interpolate_joints(start: Mapping[str, float], goal: Mapping[str, float],
                       *, step_rad: float = RESUME_PATH_STEP_RAD) -> list[dict]:
    """관절 공간 직선 보간 표본(양 끝 포함). 새 경로를 만들 뿐 실행하지 않는다."""
    names = [name for name in goal if name in start]
    span = max((abs(float(goal[n]) - float(start[n])) for n in names), default=0.0)
    count = max(1, math.ceil(span / step_rad))
    return [{n: float(start[n]) + (float(goal[n]) - float(start[n])) * i / count
             for n in names} for i in range(count + 1)]


def resume_preflight_findings(
    *, checkpoint: Mapping[str, Any] | None, model: str,
    scene_hash_now: str | None, object_pose_m: Sequence[float] | None,
    attachment_static: bool | None, joint_observation: Mapping[str, Any] | None,
    preflight_started_at: float, gripper_joint: str, latch: Mapping[str, Any] | None,
    live_goals: int | None, tool_object_gap_m: float | None,
    follow_tolerance_m: float,
) -> list[str]:
    """관측 조건 1~6. 하나라도 걸리면 사유 목록(BLOCK). geometry(7)는 따로 본다."""
    reasons: list[str] = []
    if checkpoint is None:
        return [f"{model}의 체크포인트가 없다"]
    if checkpoint.get("stop_confirmed") is not True:
        reasons.append("체크포인트가 확인된 STOP에서 만들어지지 않았다")
    if checkpoint.get("model") != model:
        reasons.append("체크포인트 자재가 다르다")
    if checkpoint.get("object_state") != OBJECT_HELD:
        reasons.append(f"자재 상태가 held가 아니다: {checkpoint.get('object_state')}")
    if checkpoint.get("stopped_stage") not in RESUMABLE_STAGES:
        reasons.append(f"재계획을 지원하지 않는 정지 단계다:"
                       f" {checkpoint.get('stopped_stage')}")
    if not scene_hash_now:
        reasons.append("현재 scene hash가 없다")
    elif scene_hash_now != checkpoint.get("scene_hash"):
        reasons.append("scene이 체크포인트와 다르다"
                       f" ({str(checkpoint.get('scene_hash'))[:12]} → {scene_hash_now[:12]})")
    if object_pose_m is None:
        reasons.append("자재 pose를 관측하지 못했다")
    else:
        gap = math.dist([float(v) for v in object_pose_m],
                        [float(v) for v in checkpoint.get("object_pose_m") or (0, 0, 0)])
        if gap > RESUME_POSE_TOLERANCE_M:
            reasons.append(f"자재 pose가 체크포인트와 {gap:.4f} m 다르다"
                           f" (허용 {RESUME_POSE_TOLERANCE_M} m)")
    if attachment_static is not True:
        reasons.append("고정 장치 attachment를 관측하지 못했다"
                       " (자재가 정적 고정 상태가 아니다)")
    obs = dict(joint_observation or {})
    positions = dict(obs.get("positions") or {})
    if not obs.get("valid") or not positions:
        reasons.append("관절 관측이 없다")
    elif float(obs.get("observed_at") or 0.0) < float(preflight_started_at):
        reasons.append("사전검증 시작 뒤의 새 관절 관측이 아니다")
    else:
        if gripper_joint not in positions:
            reasons.append("그리퍼 관측이 없다")
        saved = (checkpoint.get("joint_state") or {}).get("positions") or {}
        drift = max((abs(float(positions.get(n, float("inf"))) - float(v))
                     for n, v in saved.items()), default=float("inf"))
        if drift > RESUME_JOINT_TOLERANCE_RAD:
            reasons.append(f"관절이 체크포인트와 {drift:.4f} rad 다르다")
    if tool_object_gap_m is None:
        reasons.append("도구-자재 상대 위치를 확인하지 못했다")
    elif tool_object_gap_m > follow_tolerance_m:
        reasons.append(f"자재가 도구에서 {tool_object_gap_m:.4f} m 떨어져 있다")
    if latch is None:
        reasons.append("STOP 래치가 없다")
    elif latch.get("stop_execution_id") != checkpoint.get("stop_execution_id"):
        reasons.append("STOP 래치가 이 체크포인트의 정지가 아니다")
    if live_goals is None:
        reasons.append("추적 중인 goal 수를 확인하지 못했다")
    elif live_goals:
        reasons.append(f"추적 중인 goal이 {live_goals}개 남아 있다")
    return reasons


def build_resume_stages(stages, checkpoint: Mapping[str, Any],
                        current_joints: Mapping[str, float]):
    """복구 접근 + 체크포인트의 남은 단계. 중단 궤적은 쓰지 않는다.

    반환: (복구 접근 표본 단계들, 남은 단계들, 사유 목록).
    """
    from dataclasses import replace as dc_replace

    names = [stage.stage for stage in stages]
    stopped = checkpoint.get("stopped_stage")
    remaining = list(checkpoint.get("remaining_stages") or ())
    if stopped not in names or not remaining or remaining[0] != stopped:
        return (), (), ["체크포인트의 남은 단계가 계획과 맞지 않는다"]
    target = stages[names.index(stopped)]
    arm_start = {n: float(v) for n, v in current_joints.items() if n.startswith("j")}
    samples = interpolate_joints(arm_start, dict(target.joint_rad))
    approach = tuple(
        dc_replace(target, no=0, label=f"복구 접근 {i}/{len(samples) - 1}",
                   pose_name=(target.pose_name if i == len(samples) - 1
                              else "recovery_interpolated"),
                   joint_rad=sample, source_step=None,
                   detail="현재 관측 관절 → 정지 단계 목표 자세(새 경로)")
        for i, sample in enumerate(samples))
    after = tuple(stage for stage in stages
                  if stage.stage in remaining and stage.stage != stopped)
    if [s.stage for s in after] != remaining[1:]:
        return (), (), ["남은 단계를 계획에서 순서대로 찾지 못했다"]
    return approach, after, []
