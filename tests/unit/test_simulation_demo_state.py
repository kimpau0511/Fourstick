"""시뮬레이션 사용자 시연의 셀 상태 유지 단위 검증.

- `simulation_demo_hold`에서 **이송 완료만** 상태 유지 성공으로 기록한다
- STOP·실패·고정 장치 결함은 성공으로 기록하지 않고, 자재가 남았으면
  `simulation_demo_reset_required=true`
- `e2e_reset`(기본값)은 기록하지 않고 되돌린다 — E2E 스크립트는 정책을
  명시적으로 넘기고 실행 전후 셀을 복구한다
- 모든 값은 `is_simulated=true`, 실기 축은 false
- 상태 기록이 있어도 일반 pick/place는 `capability.profile_incomplete`로 막힌다

**ROS·Gazebo 없이** 돈다.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.reason_codes import ReasonCode  # noqa: E402
from validation.simulation_demo_state import (  # noqa: E402
    DISPLAY_LABEL,
    FAULT_UNRESTORED,
    HELD_ON_TARGET,
    POLICY_DEMO_HOLD,
    POLICY_E2E_RESET,
    STOPPED_UNRESTORED,
    SimulationDemoState,
    should_restore,
)
from validation.simulation_e2e import (  # noqa: E402
    CRITERIA,
    Criterion,
    SimulationTransferResult,
    in_placement_zone,
    not_started,
    placement_zone,
)

#: 컨베이어 배치 구역(선언 치수 형태). 값은 판정 규칙 확인용이다.
ZONE = placement_zone(target_center_m=(0.55, 0.0, 0.80),
                      surface_half_extent_m=(0.30, 0.10),
                      object_size_m=(0.06, 0.06, 0.10),
                      surface_top_z_m=0.80, z_tolerance_m=0.01)
ON_CONVEYOR = (0.55, 0.0, 0.85)


def transfer(*, met=True, stop=False, fault=None):
    criteria = []
    for key, label in CRITERIA:
        ok = met and not (fault and key == fault[0])
        criteria.append(Criterion(key=key, label=label, met=ok, detail="관측",
                                  reason_code=None if ok else fault[1]))
    return SimulationTransferResult(
        scenario="loc_pallet_1/mat_a->loc_conveyor", object_id="mat_a",
        support_id="loc_pallet_1", target_id="loc_conveyor",
        criteria=tuple(criteria), stop_requested=stop,
        stop_confirmed=True if stop else None).to_dict()


class DemoStateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "sim_demo_state.json"
        self.state = SimulationDemoState(self.path)

    def record(self, result, *, policy=POLICY_DEMO_HOLD, pose=ON_CONVEYOR,
               restored=None, model="material_a"):
        return self.state.record_run(policy=policy, model=model, result=result,
                                     final_pose_m=pose, restored=restored)

    def assert_simulated(self, status):
        self.assertIs(status["is_simulated"], True)
        self.assertIs(status["real_hardware_ready"], False)
        self.assertIs(status["real_hardware_verified"], False)

    # ── 상태 유지 성공 ──────────────────────────────────────────────────
    def test_completed_hold_keeps_the_object_on_the_conveyor(self):
        last = self.record(transfer())
        self.assertTrue(last["hold_recorded"])
        status = self.state.status()
        self.assert_simulated(status)
        self.assertTrue(status["state_hold_active"])
        self.assertEqual(status["display_label"], DISPLAY_LABEL)
        self.assertEqual(status["held_objects"], ["material_a"])
        self.assertTrue(status["simulation_demo_reset_required"])
        row = status["objects"]["material_a"]
        self.assertEqual(row["state"], HELD_ON_TARGET)
        inside, _ = in_placement_zone(row["pose_m"], ZONE)
        self.assertTrue(inside, "기록된 pose가 컨베이어 배치 구역 안이어야 한다")
        self.assertIn("--restore-only", row["reset_command"])
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIs(raw["is_simulated"], True)

    def test_hold_policy_does_not_restore_only_on_completion(self):
        self.assertFalse(should_restore(policy=POLICY_DEMO_HOLD, completed=True,
                                        stop_requested=False, no_restore=False))
        self.assertTrue(should_restore(policy=POLICY_DEMO_HOLD, completed=False,
                                       stop_requested=False, no_restore=False))

    # ── E2E 정책 ────────────────────────────────────────────────────────
    def test_e2e_reset_restores_and_writes_nothing(self):
        self.assertTrue(should_restore(policy=POLICY_E2E_RESET, completed=True,
                                       stop_requested=False, no_restore=False))
        self.assertIsNone(self.record(transfer(), policy=POLICY_E2E_RESET))
        self.assertFalse(self.path.exists())
        status = self.state.status()
        self.assertFalse(status["simulation_demo_reset_required"])
        self.assertFalse(status["state_hold_active"])
        self.assertIsNone(status["display_label"])

    def test_unknown_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            should_restore(policy="keep", completed=True, stop_requested=False,
                           no_restore=False)
        with self.assertRaises(ValueError):
            self.record(transfer(), policy="keep")

    # ── STOP·실패·고정 장치 결함 ─────────────────────────────────────────
    def test_stop_is_not_a_hold_success_and_requires_reset(self):
        self.assertFalse(should_restore(policy=POLICY_DEMO_HOLD, completed=False,
                                        stop_requested=True, no_restore=False))
        last = self.record(transfer(met=False, stop=True,
                                    fault=("no_fault_during_run",
                                           ReasonCode.EXEC_STOPPED)),
                           pose=(0.4, 0.1, 1.1))
        self.assertFalse(last["hold_recorded"])
        status = self.state.status()
        self.assertFalse(status["state_hold_active"])
        self.assertIsNone(status["display_label"])
        self.assertTrue(status["simulation_demo_reset_required"])
        self.assertEqual(status["objects"]["material_a"]["state"],
                         STOPPED_UNRESTORED)

    def test_completed_criteria_with_stop_flag_is_not_recorded(self):
        # 조건이 모두 met이어도 정지가 요청됐으면 성공이 아니다.
        last = self.record(transfer(stop=True))
        self.assertFalse(last["hold_recorded"])
        self.assertFalse(self.state.status()["state_hold_active"])

    def test_fixture_fault_restored_leaves_no_hold(self):
        last = self.record(
            transfer(met=True, fault=("attached_at_declared_support",
                                      ReasonCode.EXEC_SIM_FIXTURE_FAILED)),
            restored=True)
        self.assertFalse(last["hold_recorded"])
        self.assertIn("exec.sim_fixture_failed", last["reason_codes"])
        status = self.state.status()
        self.assertFalse(status["state_hold_active"])
        self.assertFalse(status["simulation_demo_reset_required"])

    def test_failure_with_unverified_restore_requires_reset(self):
        self.record(transfer(met=True, fault=("placement_in_zone",
                                              ReasonCode.EXEC_SIM_PLACEMENT_OUT_OF_ZONE)),
                    restored=False)
        status = self.state.status()
        self.assertFalse(status["state_hold_active"])
        self.assertTrue(status["simulation_demo_reset_required"])
        self.assertEqual(status["objects"]["material_a"]["state"],
                         FAULT_UNRESTORED)

    def test_completed_without_observed_pose_is_not_recorded(self):
        last = self.record(transfer(), pose=None)
        self.assertFalse(last["hold_recorded"])
        self.assertFalse(self.state.status()["state_hold_active"])

    def test_not_started_keeps_previous_hold(self):
        self.record(transfer())
        result = not_started(
            scenario="loc_pallet_2/mat_b->loc_conveyor", object_id="mat_b",
            support_id="loc_pallet_2", target_id="loc_conveyor",
            reason=ReasonCode.EXEC_SIM_TARGET_OCCUPIED, detail="점유").to_dict()
        last = self.record(result, pose=None, model="material_b")
        self.assertFalse(last["hold_recorded"])
        status = self.state.status()
        self.assertEqual(status["held_objects"], ["material_a"])
        self.assertNotIn("material_b", status["objects"])

    # ── 수동 reset ─────────────────────────────────────────────────────
    def test_only_verified_restore_clears_reset_required(self):
        self.record(transfer())
        self.state.mark_restored("material_a", verified=False)
        self.assertTrue(self.state.status()["simulation_demo_reset_required"])
        self.state.mark_restored("material_a", verified=True, reason="e2e_reset:before")
        status = self.state.status()
        self.assertFalse(status["simulation_demo_reset_required"])
        self.assertFalse(status["state_hold_active"])
        self.assertEqual(status["last_reset"]["reason"], "e2e_reset:before")

    def test_unreadable_record_does_not_claim_no_reset(self):
        self.path.write_text("{", encoding="utf-8")
        status = self.state.status()
        self.assertFalse(status["available"])
        self.assertIsNone(status["simulation_demo_reset_required"])
        self.assert_simulated(status)

    def test_record_claiming_real_hardware_is_rejected(self):
        self.record(transfer())
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["real_hardware_verified"] = True
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        status = self.state.status()
        self.assertFalse(status["available"])
        self.assertIs(status["real_hardware_verified"], False)


class E2eKeepsResetPolicyTest(unittest.TestCase):
    """E2E 스크립트는 `e2e_reset`을 명시하고 실행 전후 셀을 복구한다."""

    @classmethod
    def setUpClass(cls):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "verify_pick_place_sim_e2e_for_demo_state",
            ROOT / "scripts/verify_pick_place_sim_e2e.py")
        cls.e2e = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.e2e)

    def test_demo_command_passes_e2e_reset_explicitly(self):
        args = self.e2e.demo_command("pallet_1", "mat_a", out=Path("/x.json"))
        self.assertEqual(args[args.index("--cell-policy") + 1], POLICY_E2E_RESET)
        self.assertNotIn(POLICY_DEMO_HOLD, args)

    def test_reset_cell_restores_every_material_and_clears_demo_state(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = SimulationDemoState(Path(tmp.name) / "s.json")
        state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                         result=transfer(), final_pose_m=ON_CONVEYOR,
                         restored=None)
        restored = []

        class Fixture:
            def restore(self, model):
                restored.append(model)
                return types.SimpleNamespace(verified=True, pose_m=(0, 0, 0))

        report = self.e2e.reset_cell(
            Fixture(), {"material_a": 1, "material_b": 1, "material_c": 1},
            state, phase="before")
        self.assertEqual(restored, ["material_a", "material_b", "material_c"])
        self.assertTrue(report["all_verified"])
        self.assertEqual(report["policy"], POLICY_E2E_RESET)
        self.assertEqual(report["demo_state_left"], [])
        self.assertFalse(state.status()["simulation_demo_reset_required"])

    def test_reset_cell_reports_unverified_restore(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = SimulationDemoState(Path(tmp.name) / "s.json")
        state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                         result=transfer(), final_pose_m=ON_CONVEYOR,
                         restored=None)

        class Fixture:
            def restore(self, model):
                return types.SimpleNamespace(verified=False, pose_m=None)

        report = self.e2e.reset_cell(Fixture(), {"material_a": 1}, state,
                                     phase="after")
        self.assertFalse(report["all_verified"])
        self.assertEqual(report["demo_state_left"], ["material_a"])


class RobotsRouteCarriesDemoStateTest(unittest.TestCase):
    def setUp(self):
        from server.config import ServerConfig
        from server.runtime import build_runtime

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = dataclasses.replace(
            ServerConfig.from_env(), db_path=Path(tmp.name) / "web.sqlite3",
            enable_stt=False, llm_config_name="__absent__.json")
        self.runtime = build_runtime(config)
        self.addCleanup(self.runtime.repository.close)
        self.state_path = Path(tmp.name) / "sim_demo_state.json"
        self.runtime.simulation_demo_state_path = self.state_path
        self.ctx = types.SimpleNamespace(runtime=self.runtime,
                                         repository=self.runtime.repository)

    def get(self) -> dict:
        from server.routes import robot

        status, _, body = asyncio.run(
            robot.handle(self.ctx, "GET", "/v1/robots", None, {}))
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_route_reports_hold_and_reset_required_fresh(self):
        before = self.get()["simulation_demo"]
        self.assertFalse(before["simulation_demo_reset_required"])
        SimulationDemoState(self.state_path).record_run(
            policy=POLICY_DEMO_HOLD, model="material_a", result=transfer(),
            final_pose_m=ON_CONVEYOR, restored=None)
        body = self.get()
        demo = body["simulation_demo"]
        self.assertTrue(demo["simulation_demo_reset_required"])
        self.assertTrue(demo["state_hold_active"])
        self.assertEqual(demo["display_label"], DISPLAY_LABEL)
        self.assertIs(demo["is_simulated"], True)
        self.assertIs(demo["real_hardware_ready"], False)
        # E2E 요약은 별도 키로 그대로 있다.
        self.assertIn("simulation_e2e", body)

    def test_pick_place_stays_blocked_with_a_held_demo_state(self):
        from tests.unit.test_fr3_gazebo_adapter import build

        SimulationDemoState(self.state_path).record_run(
            policy=POLICY_DEMO_HOLD, model="material_a", result=transfer(),
            final_pose_m=ON_CONVEYOR, restored=None)
        adapter, _, _, _ = build()
        adapter.connect(1.0)
        for result in (adapter.pick("mat_a", "loc_pallet_1", 1.0),
                       adapter.place("mat_a", "loc_conveyor", 1.0)):
            self.assertFalse(result.request_accepted)
            self.assertEqual(result.reason.value, "capability.profile_incomplete")

    def test_gate_and_adapters_do_not_read_demo_state(self):
        for rel in ("validation/pick_place_gate.py",
                    "validation/hardware_readiness.py",
                    "robots/fr3_gazebo/adapter.py"):
            with self.subTest(file=rel):
                text = (ROOT / rel).read_text(encoding="utf-8")
                self.assertNotIn("simulation_demo", text)
        for path in sorted((ROOT / "robots/hardware").glob("*.py")):
            with self.subTest(file=path.name):
                self.assertNotIn("simulation_demo",
                                 path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
