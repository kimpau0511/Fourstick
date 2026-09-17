"""pick·place 활성화 관문과 개구 모델 (md/개발플랜.md 8-08).

**합성 값으로 계약을 검증한다.** 실제 로봇 수치를 만들지 않는다.

확인하는 것:
- 7개 조건 중 하나라도 미충족이면 pick/place가 열리지 않는다
- 조건마다 다른 이유 코드가 붙는다
- 장착 yaw가 없으면 transform 조건이 막힌다
- 어댑터 질량이 자리표면 질량 조건이 막힌다
- 관측 기록이 없거나 실패면 제어·관측 조건이 막힌다
- 기하 검사 ALLOW와 재검증이 모두 있어야 계획 조건이 열린다
- 개구 모델은 표 밖을 추정하지 않고, 관절각과 개구를 선형 동일값으로 보지 않는다
- (8-10) 계획 **사전 검증**을 통과해도 실행 직전 재검증이 없으면 조건이 열리지
  않는다 — 사전 검증 통과를 실행 가능으로 바꾸지 않는다
- (8-10) 시뮬레이터 파지 관측은 관문 조건을 충족시키지 않고, 실기 관측만 충족한다
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.aperture_model import ApertureError, ApertureModel
from core.provenance import Measured, Provenance, VerificationStatus
from core.reason_codes import ReasonCode
from core.robot_profile import MountingProfile
from validation.pick_place_gate import ConditionStatus, blocking_summary, evaluate

NOW = 1_000.0


def measured(value, unit="m", status=VerificationStatus.VERIFIED):
    return Measured(
        value=value, unit=unit,
        provenance=Provenance(
            source_kind="synthetic", source="시험", source_version="1",
            source_commit="", status=status, checked_at=NOW, note="시험값",
        ),
    )


def mounting(*, yaw=0.0, tcp=True, mass_status="verified", collision=True,
             pad_face=True):
    return MountingProfile(
        mounting_profile_id="synth_mount",
        mounting_profile_version="test-1.0",
        arm_flange_frame="flange",
        gripper_base_frame="gripper_base",
        xyz=(measured(0.0), measured(0.0), measured(0.011)),
        rpy=(measured(0.0, "rad"), measured(0.0, "rad"),
             measured(yaw, "rad") if yaw is not None
             else Measured(
                 value=None, unit="rad",
                 provenance=Provenance(
                     source_kind="", source="", source_version="", source_commit="",
                     status=VerificationStatus.UNAVAILABLE, checked_at=NOW,
                     note="근거 없음",
                 ),
             )),
        tcp_frame="gripper_tcp" if tcp else "",
        tcp_xyz=((measured(0.0), measured(0.0), measured(0.13)) if tcp else None),
        coupling={
            "model": "synthetic-coupling",
            "mass_kg": 0.01,
            "mass_status": mass_status,
            "mass_note": "시험용",
            "collision_mesh": "synthetic.stl" if collision else None,
        },
        gripper_official={
            "total_mass_kg": 0.9,
            "pad_face": {"x_local_m": -0.02} if pad_face else None,
        },
    )


def verification(*, open_ok=True, close_ok=True, sequence_ok=True,
                 model_ok=True, failed_steps=()):
    steps = [
        {"step": "gripper_open", "observed_ok": True},
        {"step": "gripper_close", "observed_ok": "gripper_close" not in failed_steps},
    ]
    return {
        "checks": {
            "02_aperture_model": {"passed": model_ok},
            "03_gripper_open": {"passed": open_ok},
            "04_gripper_close": {"passed": close_ok},
            "04b_close_for_object_width": {"passed": True},
            "06_sequence": {"passed": sequence_ok, "steps": steps},
        }
    }


class TestGateBlocksUntilEverythingIsProven(unittest.TestCase):
    def test_all_conditions_met_opens_pick_place(self):
        result = evaluate(
            mounting=mounting(), verification=verification(),
            geometry_decision="allow", revalidated=True,
        )
        self.assertTrue(result.enabled)
        self.assertEqual(result.blocking, ())
        self.assertEqual(len(result.conditions), 7)

    def test_missing_yaw_blocks_the_transform_condition(self):
        result = evaluate(
            mounting=mounting(yaw=None), verification=verification(),
            geometry_decision="allow", revalidated=True,
        )
        self.assertFalse(result.enabled)
        blocked = {item.key for item in result.blocking}
        self.assertEqual(blocked, {"mounting_transform"})
        condition = next(i for i in result.conditions if i.key == "mounting_transform")
        self.assertIs(condition.status, ConditionStatus.MISSING)
        self.assertIs(condition.reason_code, ReasonCode.CONFIG_MISSING)
        self.assertIn("rpy.yaw", condition.detail)

    def test_placeholder_adapter_mass_blocks(self):
        result = evaluate(
            mounting=mounting(mass_status="declared_implausible"),
            verification=verification(), geometry_decision="allow", revalidated=True,
        )
        self.assertFalse(result.enabled)
        condition = next(i for i in result.conditions if i.key == "masses_and_inertia")
        self.assertIs(condition.status, ConditionStatus.UNVERIFIED)
        self.assertIs(condition.reason_code, ReasonCode.CAPABILITY_PROFILE_INCOMPLETE)

    def test_missing_collision_geometry_blocks(self):
        result = evaluate(
            mounting=mounting(collision=False), verification=verification(),
            geometry_decision="allow", revalidated=True,
        )
        condition = next(i for i in result.conditions if i.key == "collision_geometry")
        self.assertIs(condition.reason_code,
                      ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE)
        self.assertFalse(result.enabled)

    def test_missing_tcp_blocks(self):
        result = evaluate(
            mounting=mounting(tcp=False), verification=verification(),
            geometry_decision="allow", revalidated=True,
        )
        blocked = {item.key for item in result.blocking}
        self.assertIn("tcp_defined_and_verified", blocked)

    def test_no_verification_record_blocks_control_and_observation(self):
        result = evaluate(mounting=mounting(), verification=None,
                          geometry_decision="allow", revalidated=True)
        blocked = {item.key for item in result.blocking}
        self.assertEqual(blocked, {"open_close_control",
                                   "aperture_or_grasp_observation"})
        for item in result.blocking:
            self.assertIs(item.reason_code, ReasonCode.EXEC_UNVERIFIABLE)

    def test_failed_sequence_step_blocks_control(self):
        result = evaluate(
            mounting=mounting(),
            verification=verification(sequence_ok=False,
                                      failed_steps=("gripper_close",)),
            geometry_decision="allow", revalidated=True,
        )
        condition = next(i for i in result.conditions if i.key == "open_close_control")
        self.assertIs(condition.status, ConditionStatus.FAILED)
        self.assertIn("gripper_close", condition.detail)
        self.assertFalse(result.enabled)

    def test_geometry_block_and_missing_revalidation_are_different_reasons(self):
        blocked = evaluate(mounting=mounting(), verification=verification(),
                           geometry_decision="block", revalidated=True)
        stale = evaluate(mounting=mounting(), verification=verification(),
                         geometry_decision="allow", revalidated=False)
        absent = evaluate(mounting=mounting(), verification=verification(),
                          geometry_decision=None, revalidated=True)
        codes = []
        for result in (blocked, stale, absent):
            condition = next(i for i in result.conditions
                             if i.key == "plan_collision_revalidation")
            codes.append(condition.reason_code)
        self.assertEqual(codes, [
            ReasonCode.GEOMETRY_COLLISION,
            ReasonCode.EXEC_PERMIT_DENIED,
            ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE,
        ])

    def test_simulation_environment_is_recorded(self):
        result = evaluate(mounting=mounting(), verification=verification(),
                          geometry_decision="allow", revalidated=True,
                          environment="simulation")
        self.assertEqual(result.to_dict()["environment"], "simulation")

    def test_summary_lists_each_blocking_reason(self):
        result = evaluate(mounting=mounting(yaw=None), verification=None)
        summary = blocking_summary(result)
        self.assertEqual(len(summary), len(result.blocking))
        self.assertTrue(all(":" in line for line in summary))


class TestApertureModel(unittest.TestCase):
    def table(self):
        # 비선형 표(관절이 커질수록 개구가 좁아지고, 변화율이 달라진다).
        return [
            {"joint_value_rad": 0.0, "pad_gap_m": 0.085, "tcp_pad_center_z_m": 0.130},
            {"joint_value_rad": 0.2, "pad_gap_m": 0.066, "tcp_pad_center_z_m": 0.136},
            {"joint_value_rad": 0.4, "pad_gap_m": 0.045, "tcp_pad_center_z_m": 0.141},
            {"joint_value_rad": 0.6, "pad_gap_m": 0.023, "tcp_pad_center_z_m": 0.143},
            {"joint_value_rad": 0.8, "pad_gap_m": 0.000, "tcp_pad_center_z_m": 0.144},
        ]

    def model(self):
        return ApertureModel.from_rows(self.table(), source="시험 표 1.0")

    def test_table_is_required_and_sourced(self):
        with self.assertRaises(ApertureError):
            ApertureModel.from_rows(self.table()[:1], source="시험")
        with self.assertRaises(ApertureError):
            ApertureModel.from_rows(self.table(), source="")

    def test_aperture_is_not_linear_in_joint_angle(self):
        model = self.model()
        linear = 0.085 + (0.0 - 0.085) * (0.4 / 0.8)
        self.assertNotAlmostEqual(model.aperture_for(0.4), linear, places=3)

    def test_out_of_range_is_not_estimated(self):
        model = self.model()
        self.assertIsNone(model.aperture_for(-0.01))
        self.assertIsNone(model.aperture_for(0.81))
        self.assertIsNone(model.joint_for(0.2))
        self.assertIsNone(model.tcp_z_for(1.0))

    def test_round_trip_between_joint_and_aperture(self):
        model = self.model()
        for joint in (0.1, 0.35, 0.7):
            aperture = model.aperture_for(joint)
            self.assertAlmostEqual(model.joint_for(aperture), joint, places=6)

    def test_observation_at_table_edge_is_recorded_not_hidden(self):
        model = self.model()
        reading = model.observe(0.8 + 1e-13, edge_tolerance_rad=0.02)
        self.assertIsNotNone(reading["aperture_m"])
        self.assertTrue(reading["at_table_edge"])
        self.assertLess(reading["edge_offset_rad"], 1e-9)

    def test_observation_beyond_tolerance_returns_no_value(self):
        model = self.model()
        reading = model.observe(0.95, edge_tolerance_rad=0.02)
        self.assertIsNone(reading["aperture_m"])
        self.assertFalse(reading["at_table_edge"])
        self.assertIn("만들지 않는다", reading["detail"])

    def test_interpolation_error_is_reported(self):
        model = self.model()
        self.assertGreater(model.max_interpolation_error_m, 0.0)
        self.assertIn("max_interpolation_error_m", model.to_dict())


class TestShippedMountingProfile(unittest.TestCase):
    """실제 구성 파일이 측정 근거와 막힌 항목을 그대로 담고 있는지."""

    @classmethod
    def setUpClass(cls):
        import json

        from config.loader import load_mounting_profile

        path = ROOT / "config/profiles/fr3wms_to_robotiq_2f85_mounting.json"
        cls.profile = load_mounting_profile(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def test_translation_is_verified_and_yaw_is_not(self):
        values = self.profile.scalar_values
        for key in ("xyz.x", "xyz.y", "xyz.z", "rpy.roll", "rpy.pitch"):
            with self.subTest(key=key):
                self.assertIs(values[key].provenance.status,
                              VerificationStatus.VERIFIED)
        self.assertIs(values["rpy.yaw"].provenance.status,
                      VerificationStatus.UNAVAILABLE)
        self.assertEqual(self.profile.missing, ("rpy.yaw",))

    def test_coupling_thickness_comes_from_two_sources(self):
        coupling = self.profile.coupling
        self.assertEqual(coupling["thickness_m"], 0.011)
        self.assertEqual(len(coupling["thickness_sources"]), 2)

    def test_gate_says_pick_place_is_blocked(self):
        result = evaluate(mounting=self.profile, verification=None)
        self.assertFalse(result.enabled)
        self.assertIn("mounting_transform", [i.key for i in result.blocking])

    def test_aperture_table_matches_official_max_opening(self):
        model = ApertureModel.from_rows(
            self.profile.transform_derivation["aperture_table"],
            source=self.profile.mounting_profile_version,
        )
        # 공식 최대 개구 0.085 m와 3 µm 안에서 일치한다.
        self.assertAlmostEqual(model.aperture_for(0.0), 0.085, places=4)
        self.assertLess(model.max_interpolation_error_m, 0.0001)


class TestPlanValidationCondition(unittest.TestCase):
    """8-10: 계획 사전 검증 결과를 7번 조건이 어떻게 읽는가."""

    def passing_validation(self, **overrides) -> dict:
        base = {
            "available": True,
            "plan_verified": True,
            "execution_allowed": False,
            "stage_count": 12,
            "checks_passed": 12,
            "resources_matched": True,
            "scene_stable": True,
            "snapshot": {"snapshot_id": "scene-1"},
            "reason_codes": [],
            "findings": [],
        }
        base.update(overrides)
        return base

    def condition(self, result):
        return next(item for item in result.conditions
                    if item.key == "plan_collision_revalidation")

    def test_plan_verified_without_revalidation_is_not_met(self):
        result = evaluate(mounting=mounting(), verification=verification(),
                          plan_validation=self.passing_validation())
        item = self.condition(result)
        self.assertIs(item.status, ConditionStatus.UNVERIFIED)
        self.assertIs(item.reason_code, ReasonCode.EXEC_PERMIT_DENIED)
        self.assertFalse(item.met)
        self.assertIn("재검증", item.detail)
        self.assertIs(item.evidence["execution_allowed"], False)

    def test_plan_verified_with_revalidation_is_met(self):
        result = evaluate(mounting=mounting(), verification=verification(),
                          plan_validation=self.passing_validation(),
                          revalidated=True)
        self.assertIs(self.condition(result).status, ConditionStatus.MET)

    def test_failed_plan_validation_carries_its_own_reason_code(self):
        result = evaluate(
            mounting=mounting(), verification=verification(),
            plan_validation=self.passing_validation(
                plan_verified=False,
                reason_codes=["plan.resource_mismatch"],
                findings=[{"reason_code": "plan.resource_mismatch",
                           "detail": "받침이 다르다"}]))
        item = self.condition(result)
        self.assertIs(item.status, ConditionStatus.FAILED)
        self.assertIs(item.reason_code, ReasonCode.PLAN_RESOURCE_MISMATCH)

    def test_unavailable_plan_validation_is_missing_not_passed(self):
        result = evaluate(
            mounting=mounting(), verification=verification(),
            plan_validation={"available": False, "detail": "선언이 없다",
                             "reason_code": "geometry.validator_unavailable"})
        item = self.condition(result)
        self.assertIs(item.status, ConditionStatus.MISSING)
        self.assertIs(item.reason_code, ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE)

    def test_gate_stays_closed_even_with_a_verified_plan(self):
        """계획이 전부 통과해도 장착·질량·파지 관측이 남아 있으면 열지 않는다."""
        result = evaluate(mounting=mounting(yaw=None), verification=verification(),
                          plan_validation=self.passing_validation())
        self.assertFalse(result.enabled)
        keys = [item.key for item in result.blocking]
        self.assertIn("mounting_transform", keys)
        self.assertIn("plan_collision_revalidation", keys)


class TestGraspObservationInGate(unittest.TestCase):
    """8-10: 파지 관측 기록이 6번 조건에 어떻게 반영되는가."""

    def condition(self, result):
        return next(item for item in result.conditions
                    if item.key == "aperture_or_grasp_observation")

    def test_unavailable_observation_does_not_open_the_condition(self):
        result = evaluate(
            mounting=mounting(), verification=verification(),
            grasp_observation={"availability": "unavailable", "held": None,
                               "counts_for_gate": False})
        item = self.condition(result)
        self.assertFalse(item.met)
        self.assertEqual(item.evidence["grasp_observation"]["held"], None)

    def test_simulated_observation_does_not_open_the_condition(self):
        result = evaluate(
            mounting=mounting(), verification=verification(),
            grasp_observation={"availability": "simulated_observation",
                               "held": True, "counts_for_gate": False})
        self.assertFalse(self.condition(result).met)

    def test_measured_observation_opens_the_condition(self):
        result = evaluate(
            mounting=mounting(), verification=verification(),
            grasp_observation={"availability": "measured", "held": True,
                               "counts_for_gate": True})
        self.assertTrue(self.condition(result).met)


class TestSimulationResultsCannotOpenTheGate(unittest.TestCase):
    """8-11: 시뮬레이터 이송 결과로 실기 조건을 열지 않는다."""

    def test_gate_has_no_parameter_for_simulation_transfer_results(self):
        import inspect

        params = set(inspect.signature(evaluate).parameters)
        for name in ("simulation_e2e", "simulation_transfer", "sim_result",
                     "simulation_result", "transfer_result"):
            with self.subTest(param=name):
                self.assertNotIn(name, params)

    def test_gate_source_does_not_read_simulation_transfer_reports(self):
        source = (ROOT / "validation" / "pick_place_gate.py").read_text(
            encoding="utf-8")
        for token in ("simulation_transfer", "simulation_e2e",
                      "pick_place_sim_e2e"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_simulated_grasp_observation_keeps_the_gate_closed(self):
        from validation.simulation_e2e import SIMULATED_OBSERVATION

        result = evaluate(
            mounting=mounting(), verification=verification(),
            geometry_decision="allow", revalidated=True,
            grasp_observation={"availability": SIMULATED_OBSERVATION,
                               "held": True, "counts_for_gate": False})
        self.assertFalse(result.enabled)
        self.assertIn("aperture_or_grasp_observation",
                      [item.key for item in result.blocking])

    def test_a_completed_simulation_transfer_is_not_gate_evidence(self):
        """시뮬레이터에서 이송이 끝나도 관문은 그대로 닫혀 있다."""
        from validation.simulation_e2e import CRITERIA, Criterion
        from validation.simulation_e2e import (
            SimulationTransferResult,
            SimulationVerdictError,
        )

        transfer = SimulationTransferResult(
            scenario="s", object_id="o", support_id="p", target_id="t",
            criteria=tuple(
                Criterion(key=key, label=label, met=True, detail="관측됨")
                for key, label in CRITERIA))
        self.assertTrue(transfer.simulation_e2e)
        self.assertFalse(transfer.real_hardware_ready)
        with self.assertRaises(SimulationVerdictError):
            transfer.as_real_hardware_verdict()
        # 관문은 시뮬레이터 결과를 입력으로 받지 않으므로 판정이 달라지지 않는다.
        before = evaluate(mounting=mounting(yaw=None),
                          verification=verification()).to_dict()
        after = evaluate(mounting=mounting(yaw=None),
                         verification=verification()).to_dict()
        self.assertEqual(before, after)
        self.assertFalse(after["enabled"])


if __name__ == "__main__":
    unittest.main()
