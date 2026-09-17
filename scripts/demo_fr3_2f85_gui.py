#!/usr/bin/env python3
"""FR3-WMS + GRP-CPL-062 + 2F-85 **GUI 시연** (md/개발플랜.md 8-08 우선순위 4).

사용자가 Gazebo 창에서 볼 수 있는 순서를 그대로 실행한다.

  1. 초기 home 자세
  2. 안전 위치로 move
  3. 2F-85 open
  4. 2F-85 30 mm 개구로 close
  5. 이동 중 전체 STOP
  6. arm·gripper의 **관측** 정지 상태 표시

pick/place는 포함하지 않는다 — 장착 yaw가 선언값이고 커플링 실측 질량이 없다.

각 단계는 명령 결과와 **관측값을 따로** 기록한다. `reached_goal=True`만으로
성공 처리하지 않는다: 팔은 관절 오차, 그리퍼는 개구로 판정한다.
결과는 reports/gripper/gui_demo.json에 남는다.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import rclpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from core.aperture_model import ApertureModel  # noqa: E402
from verify_fr3_gripper_gazebo import (  # noqa: E402
    APERTURE_TOLERANCE_M,
    ARM_JOINTS,
    ARM_TOLERANCE_RAD,
    GRIPPER_JOINT,
    HOME,
    MOVE_POSE,
    MOUNTING,
    STOP_VELOCITY_RAD_S,
    GripperVerifier,
)

#: 시연 개구 목표(m). 빈 그리퍼를 30 mm로 닫는다 — 사용자가 눈으로 확인한다.
DEMO_APERTURE_M = 0.030
#: 화면에서 따라갈 수 있게 팔을 느리게 움직인다(초).
SLOW_MOVE_SEC = 8.0
#: STOP을 거는 시점(모션 시작 뒤 초). 중간에 멈춘 것이 보여야 한다.
STOP_AFTER_SEC = 2.5


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "reports/gripper/gui_demo.json"))
    args = parser.parse_args()

    profile = json.loads(MOUNTING.read_text(encoding="utf-8"))
    aperture = ApertureModel.from_rows(
        profile["transform_derivation"]["aperture_table"],
        source=f"{profile['mounting_profile_id']}"
               f" {profile['mounting_profile_version']}",
    )
    open_rad = profile["gripper_official"]["command_range_rad"][0]
    close_rad = aperture.joint_for(DEMO_APERTURE_M)
    if close_rad is None:
        print(f"개구 표에서 {DEMO_APERTURE_M} m 목표를 만들 수 없다", file=sys.stderr)
        return 3

    rclpy.init()
    verifier = GripperVerifier(aperture)
    steps: list[dict] = []

    def announce(index: int, title: str) -> None:
        print(f"\n[{index}/6] {title}", flush=True)

    def add(index: int, title: str, command: dict, observation: dict,
            ok: bool, extra: dict | None = None) -> None:
        steps.append({
            "step": index, "title": title,
            "command_result": {k: v for k, v in command.items()
                               if k in ("accepted", "status", "error_code",
                                        "stalled", "reached_goal", "detail",
                                        "reported_position_rad", "cancel_ack")},
            "observation": observation,
            "observed_ok": ok,
            **(extra or {}),
        })
        print(f"      관측 {'통과' if ok else '실패'}: "
              f"{json.dumps(observation, ensure_ascii=False)[:180]}", flush=True)

    def gripper_path(start_index: int) -> dict:
        """단계 동안의 그리퍼 관절 경로. 과도 구간을 성공에 묻지 않으려고 남긴다."""
        values = [sample["gripper_joint_rad"]
                  for sample in verifier.states[start_index:]
                  if sample["gripper_joint_rad"] is not None]
        if not values:
            return {"samples": 0}
        return {"samples": len(values), "first_rad": round(values[0], 6),
                "min_rad": round(min(values), 6), "max_rad": round(max(values), 6),
                "last_rad": round(values[-1], 6)}

    try:
        verifier.spin(4.0)
        start_sample = verifier.latest()
        if start_sample is None:
            print("joint_states가 오지 않는다. GUI 실행 스크립트를 먼저 돌린다.",
                  file=sys.stderr)
            return 2
        print(f"시작 상태: 그리퍼 관절 {start_sample['gripper_joint_rad']:.6f} rad"
              f" · 개구 {start_sample['aperture_m']} m", flush=True)

        # 1) 초기 home 자세
        announce(1, "초기 home 자세")
        command = verifier.move_arm(HOME, seconds=SLOW_MOVE_SEC)
        verifier.spin(1.5)
        observation = verifier.arm_error(HOME)
        add(1, "home", command, observation, observation.get("reached", False))

        # 2) 안전 위치로 move
        announce(2, "안전 위치로 move")
        command = verifier.move_arm(MOVE_POSE, seconds=SLOW_MOVE_SEC)
        verifier.spin(1.5)
        observation = verifier.arm_error(MOVE_POSE)
        add(2, "move", command, observation, observation.get("reached", False),
            {"target_rad": {k: round(v, 4) for k, v in MOVE_POSE.items()}})

        # 3) 2F-85 open
        announce(3, "2F-85 open")
        command = verifier.command_gripper(open_rad)
        verifier.spin(3.0)
        observation = verifier.observe_aperture(open_rad)
        add(3, "gripper_open", command, observation,
            observation.get("within_tolerance", False),
            {"target_joint_rad": open_rad,
             "target_aperture_m": aperture.aperture_for(open_rad)})

        # 4) 2F-85 30 mm 개구로 close
        announce(4, f"2F-85 {DEMO_APERTURE_M * 1000:.0f} mm 개구로 close")
        step_start = len(verifier.states)
        command = verifier.command_gripper(close_rad)
        verifier.spin(3.0)
        observation = verifier.observe_aperture(close_rad)
        path = gripper_path(step_start)
        joint_limit_rad = profile["gripper_official"]["command_range_rad"][1]
        # **과도 구간을 성공에 묻지 않는다.** 목표를 지나 제한까지 갔다면
        # 실물에서는 물체를 으깨는 동작이다. 관측 개구가 맞아도 실패로 본다.
        overshoot = {
            "peak_joint_rad": path.get("max_rad"),
            "target_joint_rad": round(close_rad, 6),
            "joint_limit_rad": joint_limit_rad,
            "overshoot_rad": None if path.get("max_rad") is None
            else round(path["max_rad"] - close_rad, 6),
            "reached_joint_limit": path.get("max_rad") is not None
            and path["max_rad"] >= joint_limit_rad - 1e-6,
            "reason_code": "exec.unverifiable",
            "note": "목표를 지나 관절 제한까지 닫히면 실물에서는 물체를 으깬다."
                    " 관측 개구가 맞아도 성공으로 보지 않는다",
        }
        add(4, "gripper_close_30mm", command, observation,
            observation.get("within_tolerance", False)
            and not overshoot["reached_joint_limit"],
            {"target_joint_rad": round(close_rad, 6),
             "target_aperture_m": DEMO_APERTURE_M,
             "target_basis": "개구 표에서 30 mm에 해당하는 관절값을 역산했다."
                             " 관절 제한(0.8 rad)에서 만들지 않았다",
             "gripper_joint_path": path,
             "overshoot": overshoot})

        # 5) 이동 중 전체 STOP (팔과 그리퍼를 함께 멈춘다)
        announce(5, "이동 중 전체 STOP")
        verifier.move_arm(HOME, seconds=SLOW_MOVE_SEC)
        verifier.spin(1.0)
        arm_stop = verifier.move_arm(MOVE_POSE, seconds=SLOW_MOVE_SEC,
                                     cancel_after=STOP_AFTER_SEC)
        gripper_stop = verifier.command_gripper(open_rad, wait=3.0, cancel_after=0.4)
        stop_reached = verifier.arm_error(MOVE_POSE)
        stopped_short = not stop_reached.get("reached", True)
        add(5, "global_stop", arm_stop,
            {"arm_target_reached": stop_reached.get("reached"),
             "arm_max_error_rad": stop_reached.get("max_error_rad"),
             "stopped_before_target": stopped_short},
            stopped_short,
            {"gripper_cancel": gripper_stop.get("cancel_ack"),
             "gripper_result": {k: v for k, v in gripper_stop.items()
                                if k != "cancel_ack"},
             "stop_basis": "전체 STOP = 팔·그리퍼 활성 goal을 모두 취소한다."
                           " 목표에 도달했다면 STOP이 듣지 않은 것이다"})

        # 6) arm·gripper 관측 정지 상태
        announce(6, "arm·gripper 관측 정지 상태")
        stop_check = verifier.stop_confirmed()
        sample = verifier.latest()
        # 그리퍼가 관절 제한에 잠겨 있으면 속도가 0이다. 그건 STOP이 들었다는
        # 근거가 아니다 — **판정 불가로 남긴다.**
        locked = (sample is not None
                  and sample["gripper_joint_rad"] is not None
                  and sample["gripper_joint_rad"] >= joint_limit_rad - 1e-6)
        if locked:
            stop_check = {
                **stop_check,
                "gripper_stop_verdict": "unavailable",
                "reason": "그리퍼가 관절 제한에 잠겨 있어 속도가 0이다."
                          " STOP이 들었는지 구분할 수 없다",
                "reason_code": "exec.unverifiable",
            }
        add(6, "observed_stop_state", {"accepted": True}, stop_check,
            stop_check.get("stopped", False) and not locked,
            {"arm_at_stop_rad": None if sample is None
             else {k: round(v, 5) for k, v in sample["arm"].items()},
             "gripper_joint_at_stop_rad": None if sample is None
             else round(sample["gripper_joint_rad"], 6),
             "aperture_at_stop_m": None if sample is None
             else sample["aperture_m"],
             "criterion": f"연속 {stop_check.get('samples')}표본의 |속도| <"
                          f" {STOP_VELOCITY_RAD_S} rad/s"})

        report = {
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "purpose": "Gazebo GUI에서 사용자가 눈으로 확인하는 6단계 시연",
            "environment": {
                "is_simulated": True,
                "real_hardware_verified": False,
                "arm_only": False,
                "adapter_included": True,
                "mounting_profile": f"{profile['mounting_profile_id']}"
                                    f" {profile['mounting_profile_version']}",
                "mounting_yaw_source": "declared_by_operator",
                "aperture_model": aperture.to_dict(),
                "arm_tolerance_rad": ARM_TOLERANCE_RAD,
                "aperture_tolerance_m": APERTURE_TOLERANCE_M,
                "gripper_command_joint": GRIPPER_JOINT,
                "arm_joints": ARM_JOINTS,
            },
            "steps": steps,
            "passed_count": sum(1 for step in steps if step["observed_ok"]),
            "total_count": len(steps),
            "pick_place": {
                "enabled": False,
                "included_in_demo": False,
                "reason": "장착 yaw 근거·커플링 실측 질량·파지 관측 미확보",
            },
            "note": "Gazebo simulation 결과다. 실제 하드웨어·실제 파지력은"
                    " 검증하지 않았다",
        }
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                  default=str) + "\n", encoding="utf-8")
        print(f"\n시연 {report['passed_count']}/{report['total_count']} 단계 관측 통과"
              f" — {out}", flush=True)
        print("**pick/place는 시연에 포함하지 않았다.**", flush=True)
        return 0 if report["passed_count"] == report["total_count"] else 1
    finally:
        verifier.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
