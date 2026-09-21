#!/usr/bin/env python3
"""Gazebo pick/place 시뮬레이션 E2E 확인 (8-11).

```
FORSTICK2_SIM_PICK_PLACE_DEMO=1 .venv/bin/python scripts/verify_pick_place_sim_e2e.py
```

**시뮬레이터 데모 검증이다.** 확인하는 것과 확인하지 않는 것을 나눠 적는다.

| # | 항목 | 기대 |
|---|---|---|
| 01~03 | A/B/C 이송 | `simulation_transfer_completed` · `real_hardware_ready=false` |
| 04 | 잘못된 팔레트-자재 조합 | `plan.resource_mismatch`로 시작하지 않음 |
| 05 | 자재 누락 | `exec.sim_object_absent` |
| 06 | 컨베이어 점유 | `exec.sim_target_occupied` |
| 07 | 고정 실패 | `exec.sim_fixture_failed` |
| 08 | 배치 구역 밖 해제 | `exec.sim_placement_out_of_zone` 판정 규칙 |
| 09 | 적재 물체 충돌 | `geometry.collision` |
| 10 | 실행 중 scene 변경 | `geometry.snapshot_expired` |
| 11 | 이동 중 STOP과 재실행 | `simulation_transfer_stopped` → 래치 해제·재검증 |
| 12 | 일반 웹/API pick/place | 계속 `capability.profile_incomplete` BLOCK |
| 13 | 웹 장면 카메라 | 프레임 변화로 이동 확인 |
| 14 | home/move/STOP·명령·장면 회귀 | 7/7 · 6/6 · 장면 스트림 살아 있음 |

결함은 **실제로 만든다.** 05·06은 물체를 진짜로 옮기고, 09·10·07은 시연
스크립트의 검증 전용 결함 주입 옵션으로 실제 경로를 태운다. 끝나면 되돌린다.

**셀 정책은 `e2e_reset`으로 고정한다.** 사용자 시연(`simulation_demo_hold`)이
셀에 남긴 자재가 있어도 실행 **전**에 모든 자재를 선언된 자리로 되돌리고,
실행 **뒤**에도 한 번 더 되돌린다. 그래야 같은 판정을 반복할 수 있다. 복구
결과는 판정 항목이 아니라 보고서의 `cell_reset`에 따로 남긴다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from robots.fr3_gazebo.sim_fixture import (  # noqa: E402
    GazeboObjectFixture,
    declarations_from_config,
)
from validation.simulation_demo_state import (  # noqa: E402
    POLICY_E2E_RESET,
    SimulationDemoState,
)
from validation.simulation_e2e import (  # noqa: E402
    SIMULATED_OBSERVATION,
    in_placement_zone,
    placement_zone,
)

WORKCELL = ROOT / "config/workcell/fr3_2f85_workcell.json"
OUT = ROOT / "reports/workcell/pick_place_sim_e2e_verify.json"
RUNS = ROOT / "reports/workcell/sim_e2e"
DEMO = ROOT / "scripts/demo_workcell_pick_place.sh"
LATCH = Path("/tmp/forstick2_workcell/sim_stop_latch.json")
PORT = int(os.environ.get("FORSTICK2_PORT", "8093"))
BASE = f"http://127.0.0.1:{PORT}"

SCENARIOS = (
    ("01_e2e_pallet_1_mat_a", "pallet_1", "mat_a"),
    ("02_e2e_pallet_2_mat_b", "pallet_2", "mat_b"),
    ("03_e2e_pallet_3_mat_c", "pallet_3", "mat_c"),
)


def _process_evidence(done) -> dict:
    """하위 프로세스 결과. 실패 원인이 보고서에 남도록 stderr도 담는다."""
    return {"exit_code": done.returncode,
            "stdout_tail": (done.stdout or "").strip().splitlines()[-3:],
            "stderr_tail": (done.stderr or "").strip().splitlines()[-3:]}


def run_regression_subscripts(base: str, *, run=subprocess.run) -> dict:
    """item 14의 두 하위 스크립트를 **부모와 같은 서버 주소**로 돌린다.

    두 스크립트는 `FORSTICK2_PORT`를 읽지 않는다 — 판정 계약은 `--base`,
    명령 시연은 `FORSTICK2_WEB_BASE`로 주소를 받는다. 넘기지 않으면 기본값
    8093으로 붙어, 서버가 다른 포트에 있을 때 출력 없이 실패한다.
    """
    outcome = run(
        [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/verify_workcell_outcome.py"),
         "--base", base],
        capture_output=True, text=True, timeout=900,
        env=dict(os.environ, FORSTICK2_PORT=str(PORT)))
    demo = run(
        [str(ROOT / "scripts/demo_workcell_commands.sh")],
        capture_output=True, text=True, timeout=900,
        env=dict(os.environ, FORSTICK2_PORT=str(PORT), FORSTICK2_WEB_BASE=base))
    return {
        "outcome_ok": outcome.returncode == 0 and "7/7 통과" in (outcome.stdout or ""),
        "demo_ok": demo.returncode == 0 and "6/6 통과" in (demo.stdout or ""),
        "outcome": _process_evidence(outcome),
        "demo": _process_evidence(demo),
    }


def demo_command(*args: str, out: Path) -> list[str]:
    """시연 명령. E2E는 셀 정책을 **명시적으로** `e2e_reset`으로 준다."""
    return [str(DEMO), *args, "--cell-policy", POLICY_E2E_RESET,
            "--out", str(out)]


def reset_cell(fixture, models, state: SimulationDemoState, *,
               phase: str) -> dict:
    """모든 자재를 선언된 자리로 되돌린다. **확인된 복귀만** 시연 기록에서 뺀다."""
    rows = []
    for model in sorted(models):
        event = fixture.restore(model)
        state.mark_restored(model, verified=event.verified,
                            reason=f"e2e_reset:{phase}")
        rows.append({"model": model, "verified": bool(event.verified),
                     "pose_m": None if event.pose_m is None
                     else list(event.pose_m)})
    return {"phase": phase, "policy": POLICY_E2E_RESET,
            "all_verified": all(row["verified"] for row in rows),
            "objects": rows,
            "demo_state_left": sorted(state.objects())}


def run_demo(name: str, *args: str, timeout: float = 600.0) -> tuple[int, dict, str]:
    """시연 스크립트를 돌리고 그 보고서를 읽는다."""
    RUNS.mkdir(parents=True, exist_ok=True)
    out = RUNS / f"{name}.json"
    env = dict(os.environ, FORSTICK2_SIM_PICK_PLACE_DEMO="1")
    process = subprocess.run(
        demo_command(*args, out=out), env=env, timeout=timeout,
        capture_output=True, text=True)
    payload = {}
    if out.is_file():
        payload = json.loads(out.read_text(encoding="utf-8"))
    tail = "\n".join(
        line for line in (process.stdout + process.stderr).splitlines()
        if not line.startswith("[INFO") and not line.startswith("[WARN"))[-1200:]
    return process.returncode, payload, tail


def post(path: str, body: dict, timeout: float = 300.0) -> dict:
    request = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())


def get(path: str, timeout: float = 30.0) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as response:
        return json.loads(response.read())


def build_fixture():
    data = json.loads(WORKCELL.read_text(encoding="utf-8"))
    objects = declarations_from_config(data["models"], data["frames"],
                                        source=str(WORKCELL))
    fixture = GazeboObjectFixture(
        world_name=data["world_name"], gz_partition=data["gz_partition"],
        objects=objects)
    return data, objects, fixture


def conveyor_zone(data: dict, objects: dict, model: str) -> dict:
    conveyor = data["models"]["conveyor"]
    belt = next(p for p in conveyor["parts"] if p["name"] == "belt")
    rails = [p for p in conveyor["parts"] if p["name"].startswith("frame_")]
    center = _resolve(data["frames"], conveyor["frame"])
    rail_inner = min(abs(p["center_xyz_m"][1]) - p["size_m"][1] / 2
                     for p in rails)
    return placement_zone(
        target_center_m=center,
        surface_half_extent_m=(belt["size_m"][0] / 2,
                               min(belt["size_m"][1] / 2, rail_inner)),
        object_size_m=objects[model].size_m,
        surface_top_z_m=center[2], z_tolerance_m=0.01)


def _resolve(frames, name):
    out = [0.0, 0.0, 0.0]
    while name is not None:
        out = [a + b for a, b in zip(out, frames[name]["xyz_m"])]
        name = frames[name]["parent"]
    return tuple(out)


def main() -> int:
    if os.environ.get("FORSTICK2_SIM_PICK_PLACE_DEMO") != "1":
        print("거부: FORSTICK2_SIM_PICK_PLACE_DEMO=1을 명시하지 않았다.",
              file=sys.stderr)
        return 3

    data, objects, fixture = build_fixture()
    checks: list[dict] = []
    demo_state = SimulationDemoState()
    # 사용자 시연이 남긴 상태를 지우고 시작한다(판정 항목 아님).
    cell_reset = [reset_cell(fixture, objects, demo_state, phase="before")]
    print(f"[셀 초기화] 실행 전 복구 확인={cell_reset[0]['all_verified']}")
    time.sleep(1.5)

    def record(key: str, ok: bool, detail: str, evidence: dict | None = None):
        checks.append({"key": key, "passed": bool(ok), "detail": detail,
                       "evidence": evidence or {}})
        print(f"[{'OK ' if ok else 'BAD'}] {key}: {detail}")

    def restore(model: str) -> None:
        event = fixture.restore(model)
        if not event.verified:
            print(f"  (경고) {model} 되돌리기 확인 실패: {event.detail}")

    # ── 01~03 세 자재의 시뮬레이션 E2E ────────────────────────────────
    for key, support, obj in SCENARIOS:
        code, payload, tail = run_demo(key, support, obj)
        ok = (
            code == 0
            and payload.get("status") == "simulation_transfer_completed"
            and payload.get("simulation_e2e") is True
            and payload.get("is_simulated") is True
            and payload.get("real_hardware_ready") is False
            and payload.get("real_hardware_verified") is False
            and payload.get("grasp_observation_kind") == SIMULATED_OBSERVATION
            and not payload.get("unmet")
        )
        placed = next((c for c in payload.get("criteria", [])
                       if c["key"] == "placement_in_zone"), {})
        record(key, ok,
               f"{payload.get('status')} · simulation_e2e="
               f"{payload.get('simulation_e2e')}"
               f" · real_hardware_ready={payload.get('real_hardware_ready')}"
               f" · grasp={payload.get('grasp_observation_kind')}"
               f" · {placed.get('detail', '')}",
               {"status": payload.get("status"),
                "criteria": payload.get("criteria"),
                "fixture_events": payload.get("fixture_events"),
                "object_pose_timeline": payload.get("object_pose_timeline"),
                "stage_records": [
                    {k: v for k, v in row.items() if k != "check"}
                    for row in payload.get("stage_records", [])],
                "exit_code": code})

    # ── 04 잘못된 팔레트-자재 조합 ────────────────────────────────────
    code, payload, tail = run_demo("04_wrong_combination", "pallet_1", "mat_c",
                                   timeout=300.0)
    ok = (code != 0
          and payload.get("status") == "simulation_transfer_not_started"
          and "plan.resource_mismatch" in (payload.get("reason_codes") or []))
    record("04_wrong_combination", ok,
           f"{payload.get('status')} · {payload.get('reason_codes')}",
           {"detail": payload.get("detail"), "tail": tail})

    # ── 05 자재 누락: 자재를 **실제로** 다른 곳에 둔다 ────────────────
    away = (0.9, 0.6, 0.9)
    fixture.place("material_a", away)
    time.sleep(1.5)
    code, payload, tail = run_demo("05_object_absent", "pallet_1", "mat_a",
                                   timeout=300.0)
    ok = (code != 0
          and "exec.sim_object_absent" in (payload.get("reason_codes") or []))
    record("05_object_absent", ok,
           f"{payload.get('status')} · {payload.get('reason_codes')}",
           {"moved_to_m": list(away), "detail": payload.get("detail")})
    restore("material_a")
    time.sleep(1.0)

    # ── 06 컨베이어 점유: A를 남겨 둔 뒤 B를 시도한다 ─────────────────
    zone = conveyor_zone(data, objects, "material_a")
    occupy = (zone["x_min_m"] + 0.05, (zone["y_min_m"] + zone["y_max_m"]) / 2,
              zone["z_center_m"])
    fixture.place("material_a", occupy)
    time.sleep(2.0)
    settled = fixture.pose_of("material_a", timeout_sec=3.0, fresh=True)
    inside, zone_detail = in_placement_zone(settled, zone) if settled else (False, "")
    code, payload, tail = run_demo("06_target_occupied", "pallet_2", "mat_b",
                                   timeout=300.0)
    ok = (inside and code != 0
          and "exec.sim_target_occupied" in (payload.get("reason_codes") or []))
    record("06_target_occupied", ok,
           f"A를 구역 안({zone_detail})에 둔 뒤 B 시도 →"
           f" {payload.get('reason_codes')}",
           {"occupied_pose_m": None if settled is None else list(settled),
            "zone": zone, "detail": payload.get("detail")})
    restore("material_a")
    time.sleep(1.0)

    # ── 07 고정 실패 (결함 주입) ──────────────────────────────────────
    code, payload, tail = run_demo("07_fixture_failure", "pallet_1", "mat_a",
                                   "--inject-attach-failure")
    unmet = payload.get("unmet") or []
    codes = payload.get("reason_codes") or []
    ok = (code != 0 and "exec.sim_fixture_failed" in codes
          and "attached_at_declared_support" in unmet)
    record("07_fixture_failure", ok,
           f"{payload.get('status')} · {codes}",
           {"unmet": unmet, "limitations": payload.get("limitations")})
    restore("material_a")
    time.sleep(1.0)

    # ── 08 배치 구역 밖 해제: 실제로 구역 밖에 놓아 본다 ──────────────
    outside = (0.5, 0.2, 0.95)
    fixture.place("material_a", outside)
    time.sleep(2.0)
    landed = fixture.settle("material_a")
    out_of_zone, out_detail = (in_placement_zone(landed, zone) if landed
                               else (False, "pose를 관측하지 못했다"))
    record("08_placement_out_of_zone", (landed is not None and not out_of_zone),
           f"구역 밖 판정: {out_detail}",
           {"pose_m": None if landed is None else list(landed), "zone": zone,
            "expected_reason_code": "exec.sim_placement_out_of_zone"})
    restore("material_a")
    time.sleep(1.0)

    # ── 09 적재 물체 충돌 (결함 주입) ─────────────────────────────────
    code, payload, tail = run_demo("09_attached_collision", "pallet_1", "mat_a",
                                   "--inject-attach-offset-z", "0.12",
                                   timeout=300.0)
    ok = (code != 0
          and "geometry.collision" in (payload.get("reason_codes") or []))
    record("09_attached_collision", ok,
           f"{payload.get('status')} · {payload.get('reason_codes')}",
           {"detail": payload.get("detail"),
            "limitations": payload.get("limitations")})

    # ── 10 실행 중 scene 변경 (결함 주입) ─────────────────────────────
    code, payload, tail = run_demo("10_scene_change", "pallet_1", "mat_a",
                                   "--inject-scene-change-at", "place_approach")
    fault = next((c for c in payload.get("criteria", [])
                  if c["key"] == "no_fault_during_run"), {})
    ok = (code != 0
          and fault.get("reason_code") == "geometry.snapshot_expired")
    record("10_scene_change_aborts", ok,
           f"{payload.get('status')} · {fault.get('detail', '')}",
           {"criteria": payload.get("criteria")})
    restore("material_a")
    time.sleep(1.0)

    # ── 11 이동 중 STOP과 재실행 ──────────────────────────────────────
    LATCH.unlink(missing_ok=True)
    code, payload, tail = run_demo("11a_stop_during_transfer", "pallet_3",
                                   "mat_c", "--stop-at", "place_approach")
    latch = json.loads(LATCH.read_text(encoding="utf-8")) if LATCH.is_file() else None
    stopped_ok = (payload.get("status") == "simulation_transfer_stopped"
                  and payload.get("stop_requested") is True
                  and payload.get("stop_confirmed") is True
                  and latch is not None
                  and latch.get("held_by_fixture") == ["material_c"]
                  and latch.get("arm_peak_rad_s", 1.0) < 0.01)
    record("11a_stop_during_transfer", stopped_ok,
           f"{payload.get('status')} · 정지확인={payload.get('stop_confirmed')}"
           f" · 고정 상태={None if latch is None else latch.get('held_by_fixture')}"
           f" · arm 최대속도={None if latch is None else latch.get('arm_peak_rad_s')}",
           {"latch": latch, "criteria": payload.get("criteria"),
            "object_pose_timeline": payload.get("object_pose_timeline")})

    code, payload, tail = run_demo("11b_rerun_after_stop", "pallet_3", "mat_c",
                                   timeout=300.0)
    released = "정지 래치 해제" in tail or "재검증 후 해제" in tail
    rerun_ok = (released and not LATCH.is_file()
                and "exec.sim_object_absent" in (payload.get("reason_codes") or []))
    record("11b_rerun_requires_release_and_revalidation", rerun_ok,
           f"래치 해제 로그={released} · 래치 파일 남음={LATCH.is_file()}"
           f" · 이어서 {payload.get('reason_codes')}"
           " (정지 상태의 셀은 자재가 제자리에 없어 다시 준비해야 한다)",
           {"tail": tail, "reason_codes": payload.get("reason_codes")})
    restore("material_c")
    time.sleep(1.0)

    code, payload, tail = run_demo("11c_rerun_after_setup", "pallet_3", "mat_c")
    rerun_done = (code == 0
                  and payload.get("status") == "simulation_transfer_completed")
    record("11c_rerun_completes_after_setup", rerun_done,
           f"{payload.get('status')} · simulation_e2e="
           f"{payload.get('simulation_e2e')}",
           {"criteria": payload.get("criteria")})

    # ── 12 일반 웹/API pick/place는 계속 BLOCK ────────────────────────
    session = post("/v1/sessions", {})
    reply = post("/v1/plan", {
        "session_id": session.get("session_id", ""),
        "utterance": "1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘"})
    plan_validation = reply.get("plan_validation") or {}
    body = json.dumps(reply, ensure_ascii=False)
    api_ok = (reply.get("ok") is False
              and reply.get("reason_code") == "capability.profile_incomplete"
              and plan_validation.get("execution_allowed") is False
              # 시뮬레이터 결과가 일반 API 응답으로 새지 않는다.
              and "simulation_e2e" not in body
              and "simulation_transfer_completed" not in body)
    record("12_api_pick_place_still_blocked", api_ok,
           f"{reply.get('reason_code')} · 실행가능="
           f"{plan_validation.get('execution_allowed')}"
           " · 응답에 시뮬레이터 이송 결과 없음="
           f"{'simulation_e2e' not in body}",
           {"reason_code": reply.get("reason_code"),
            "blocked": reply.get("blocked")})

    # ── 13 웹 장면 카메라에서 이동이 보이는가 ─────────────────────────
    scene_view = subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify_sim_e2e_scene_view.py"),
         "pallet_1", "mat_a"],
        capture_output=True, text=True, timeout=900,
        env=dict(os.environ, FORSTICK2_SIM_PICK_PLACE_DEMO="1",
                 FORSTICK2_PORT=str(PORT)))
    view_report = ROOT / "reports/workcell/sim_e2e_scene_view.json"
    view = json.loads(view_report.read_text(encoding="utf-8")) \
        if view_report.is_file() else {}
    record("13_scene_camera_shows_the_transfer",
           scene_view.returncode == 0 and view.get("scene_shows_transfer") is True,
           f"{view.get('detail', '기록 없음')}"
           f" · 프레임 {view.get('frames_captured')}장",
           {"change": view.get("change"), "windows": view.get("windows"),
            "frames_dir": view.get("frames_dir"),
            "transfer_status": view.get("transfer_status")})

    # ── 14 회귀 ───────────────────────────────────────────────────────
    regression = run_regression_subscripts(BASE)
    outcome_ok, demo_ok = regression["outcome_ok"], regression["demo_ok"]
    scene = get("/v1/scene")
    scene_ok = bool(scene.get("available"))
    record("14_regression_home_move_stop_commands_scene",
           outcome_ok and demo_ok and scene_ok,
           f"판정 계약 7/7={outcome_ok} · 명령 시연 6/6={demo_ok}"
           f" · 장면 스트림 available={scene_ok} (live={scene.get('live')})",
           {"outcome": regression["outcome"],
            "demo": regression["demo"],
            "scene": scene})

    cell_reset.append(reset_cell(fixture, objects, demo_state, phase="after"))
    print(f"[셀 초기화] 실행 뒤 복구 확인={cell_reset[-1]['all_verified']}")
    fixture.close()
    passed = sum(1 for row in checks if row["passed"])
    report = {
        "schema": "forstick2.workcell_pick_place_sim_e2e/1",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "is_simulated": True,
        "simulation_e2e_scope": "Gazebo 물체 이동·배치 시연",
        "real_hardware_verified": False,
        "real_hardware_ready": False,
        "grasp_observation_kind": SIMULATED_OBSERVATION,
        "passed_count": passed,
        "total_count": len(checks),
        "checks": checks,
        # 셀 복구는 판정 항목이 아니다. 반복 실행 전제를 기록만 한다.
        "cell_reset": cell_reset,
        "runs_dir": str(RUNS.relative_to(ROOT)),
        "note": "시뮬레이터 이송 시연 결과다. 실제 로봇 pick/place 가능"
                " 판정으로 승격하지 않는다. 일반 웹/API의 pick/place는 계속"
                " capability.profile_incomplete로 막혀 있다",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"\n시뮬레이션 E2E 확인 {passed}/{len(checks)} 통과"
          f" — {OUT.relative_to(ROOT)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    _code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # gz 구독 콜백은 C++ 스레드에서 파이썬을 호출한다. 인터프리터가 정리되는
    # 중에 콜백이 들어오면 프로세스가 죽는다(실측: 종료 코드 -11 SIGSEGV,
    # 판정은 이미 기록된 뒤였다). 남은 일이 종료뿐이므로 정리를 건너뛰고 바로
    # 끝낸다 — 그래야 종료 코드가 판정을 그대로 말한다.
    os._exit(_code)
