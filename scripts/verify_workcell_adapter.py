#!/usr/bin/env python3
"""FR3 Gazebo Adapter 검증 (8-08 우선순위 6).

Adapter를 **실제 작업 셀에 붙여** 텍스트 명령 흐름의 마지막 칸을 확인한다.

검증 항목(사용자 요구 검증 목록 2·3·5·7·8·9·10):
  01 connect: world·파티션·도메인 확인
  02 check:   컨트롤러 3종 활성 + 관측 신선도
  03 home:    안전 home 실행
  04 move:    1·2·3번 팔레트, 컨베이어 접근 실행
  05 stop:    실행 중 전체 정지 + 관측 정지 확인
  06 pick/place: 자원 해석까지 하고 BLOCK (capability.profile_incomplete)
  07 없는 자원: plan.unknown_resource로 차단
  08 stale state: 신선도 상한을 0으로 두고 차단 확인
  09 컨트롤러 비활성: 필수 컨트롤러 이름을 바꿔 차단 확인
  10 격리: 다른 world 이름으로 붙이면 차단 확인
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.loader import load_capability_profile  # noqa: E402
from robots.fr3_gazebo.adapter import (  # noqa: E402
    Fr3GazeboAdapter,
    load_workcell_resources,
)
from robots.fr3_gazebo.ros_transport import RosWorkcellTransport  # noqa: E402

WORKCELL = ROOT / "config/workcell/fr3_2f85_workcell.json"
POSES = ROOT / "config/workcell/fr3_2f85_workcell_poses.json"
PROFILE = ROOT / "config/profiles/fr3wms_2f85_workcell_capability.json"
OUT = ROOT / "reports/workcell/adapter_verification.json"
#: 관측 신선도 상한(초). MoveIt planning scene monitor 설정과 같은 근거를 쓴다.
STATE_MAX_AGE_SEC = 0.5


def main() -> int:
    resources = load_workcell_resources(
        WORKCELL, POSES, state_max_age_sec=STATE_MAX_AGE_SEC)
    profile = load_capability_profile(json.loads(PROFILE.read_text(encoding="utf-8")))
    transport = RosWorkcellTransport(
        world_name=resources.world_name,
        gz_partition=resources.gz_partition,
        ros_domain_id=resources.ros_domain_id)
    adapter = Fr3GazeboAdapter(
        "fr3wms_2f85_workcell", profile, transport=transport,
        resources=resources, now=time.time)

    checks: dict[str, dict] = {}

    def record(name: str, result, *, expect_accepted: bool,
               expect_reason: str | None = None, extra: dict | None = None,
               require_no_reason: bool = False) -> bool:
        accepted = result.request_accepted
        reason = None if result.reason is None else str(result.reason)
        ok = accepted == expect_accepted and (
            expect_reason is None or reason == expect_reason)
        if require_no_reason:
            # unverifiable(request_accepted=True + reason)을 통과로 세지 않는다.
            ok = ok and reason is None and result.verified
        checks[name] = {
            "request_accepted": accepted,
            "state": result.state.value,
            "task_succeeded": result.task_succeeded,
            "verified": result.verified,
            "reason": reason,
            "expected_accepted": expect_accepted,
            "expected_reason": expect_reason,
            "evidence": {k: v for k, v in dict(result.evidence).items()
                         if k not in ("target_joint_rad", "observed_joint_rad")},
            "passed": ok,
            **(extra or {}),
        }
        print(f"  [{'통과' if ok else '실패'}] {name}: accepted={accepted}"
              f" reason={reason}")
        return ok

    try:
        # 01 connect
        record("01_connect", adapter.connect(40.0), expect_accepted=True)
        # 02 check
        record("02_check", adapter.check(), expect_accepted=True)
        # 03 home
        record("03_home", adapter.home(40.0), expect_accepted=True)
        # 04 move (4개 위치)
        for resource_id in ("loc_pallet_1", "loc_pallet_2", "loc_pallet_3",
                            "loc_conveyor"):
            pose_name, detail = adapter.resolve_move(resource_id)
            result = adapter.move(resource_id, 45.0)
            record(f"04_move_{resource_id}", result, expect_accepted=True,
                   extra={"resolved_pose": pose_name,
                          "scene_mapping": {k: v for k, v in detail.items()
                                            if k in ("gazebo_model", "frame",
                                                     "korean")}})
        # 05 stop: 긴 이동을 걸고 곧바로 정지한다
        adapter.home(40.0)
        import threading
        moving = {"result": None}

        def run_move():
            moving["result"] = adapter.move("loc_pallet_1", 60.0)

        thread = threading.Thread(target=run_move, daemon=True)
        thread.start()
        time.sleep(2.0)
        stop_result = adapter.stop(10.0)
        confirm = adapter.confirm_stopped(8.0)
        thread.join(timeout=60)
        record("05a_stop_request", stop_result, expect_accepted=True)
        # **정지 확인은 reason이 없어야 통과다.** unverifiable은 실패로 센다.
        record("05b_stop_confirmed", confirm, expect_accepted=True,
               expect_reason=None, require_no_reason=True,
               extra={"move_result_reason": (
                   None if moving["result"] is None or moving["result"].reason is None
                   else str(moving["result"].reason))})
        # 06 pick/place는 차단
        record("06a_pick_blocked", adapter.pick("mat_a", "loc_pallet_1", 10.0),
               expect_accepted=False,
               expect_reason="capability.profile_incomplete")
        record("06b_place_blocked", adapter.place("mat_a", "loc_conveyor", 10.0),
               expect_accepted=False,
               expect_reason="capability.profile_incomplete")
        # 07 없는 자원
        record("07_unknown_resource", adapter.move("loc_nowhere", 10.0),
               expect_accepted=False, expect_reason="plan.unknown_resource")
        # 08 stale state
        stale_resources = load_workcell_resources(
            WORKCELL, POSES, state_max_age_sec=0.0)
        stale_adapter = Fr3GazeboAdapter(
            "fr3wms_2f85_workcell", profile, transport=transport,
            resources=stale_resources, now=lambda: time.time() + 10.0)
        stale_adapter.connect(20.0)
        record("08_stale_state_blocked", stale_adapter.check(),
               expect_accepted=False, expect_reason="robot.state_stale")
        # 09 컨트롤러 비활성 (필수 목록을 바꿔 확인한다)
        import robots.fr3_gazebo.adapter as adapter_module
        original = adapter_module.REQUIRED_CONTROLLERS
        adapter_module.REQUIRED_CONTROLLERS = (*original, "not_a_controller")
        try:
            record("09_controller_inactive_blocked", adapter.check(),
                   expect_accepted=False,
                   expect_reason="robot.state_unavailable")
        finally:
            adapter_module.REQUIRED_CONTROLLERS = original
        # 10 격리: 다른 world 이름
        other_transport = RosWorkcellTransport(
            world_name="forstick2_fr3_cell",
            gz_partition=resources.gz_partition,
            ros_domain_id=resources.ros_domain_id,
            node_name="forstick2_isolation_probe")
        other_adapter = Fr3GazeboAdapter(
            "fr3wms_2f85_workcell", profile, transport=other_transport,
            resources=resources, now=time.time)
        record("10_isolation_other_world_blocked", other_adapter.connect(25.0),
               expect_accepted=False, expect_reason="robot.connection_lost",
               extra={"probed_world": "forstick2_fr3_cell",
                      "note": "단순 조립 셀 world는 이 파티션에서 보이지 않는다 —"
                              " 격리가 유지된다"})
        other_transport.disconnect()

        # 11 환경 충돌 차단: 작업대·팔레트·자재에 닿는 자세는 실행 직전
        # 재검증에서 막혀야 한다. pick 자세는 자재에 닿는 자세다.
        import robots.fr3_gazebo.adapter as adapter_module
        original_move = dict(resources.move_pose)
        try:
            # move 대조표에 pick 자세를 임시로 연결해 **충돌 자세로 이동을 시도**한다.
            object.__setattr__(resources, "move_pose",
                               {**original_move, "loc_pallet_1": "pallet_1_pick"})
            adapter.home(40.0)
            record("11_environment_collision_blocked",
                   adapter.move("loc_pallet_1", 30.0),
                   expect_accepted=False, expect_reason="geometry.collision",
                   extra={"pose": "pallet_1_pick",
                          "note": "자재에 닿는 자세다. planning scene 재검증이"
                                  " 실행 전에 막아야 한다"})
        finally:
            object.__setattr__(resources, "move_pose", original_move)
    finally:
        adapter.disconnect()

    passed = sum(1 for v in checks.values() if v["passed"])
    report = {
        "schema": "forstick2.workcell_adapter_verification/1",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "is_simulated": True,
        "real_hardware_verified": False,
        "adapter": "robots.fr3_gazebo.adapter.Fr3GazeboAdapter",
        "workcell": {
            "workcell_id": resources.workcell_id,
            "workcell_version": resources.workcell_version,
            "world": resources.world_name,
            "gz_partition": resources.gz_partition,
            "ros_domain_id": resources.ros_domain_id,
            "state_max_age_sec": STATE_MAX_AGE_SEC,
        },
        "resource_map": {rid: dict(m) for rid, m in resources.resources.items()},
        "move_poses": dict(resources.move_pose),
        "checks": checks,
        "passed_count": passed,
        "total_count": len(checks),
        "pick_place": {
            "enabled": False,
            "reason_code": "capability.profile_incomplete",
            "message": "2F-85 장착 근거, 그리퍼 close 안정성, 파지 관측,"
                       " pick/place 재검증이 완료되지 않았습니다.",
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                              default=str) + "\n", encoding="utf-8")
    print(f"  {OUT.relative_to(ROOT)} 기록 — {passed}/{len(checks)} 통과")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
