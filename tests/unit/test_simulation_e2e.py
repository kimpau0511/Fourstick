"""시뮬레이터 이송 시연 판정 (md/개발플랜.md 8-11).

이 파일이 지키는 것은 하나다 — **시뮬레이터 결과가 실기 결과로 새지 않는다.**

- `real_hardware_ready`는 어떤 경우에도 False다
- `grasp_observation_kind`는 `simulated_observation`으로 고정된다
- 결과 dict에 `task_succeeded`·`verified`·`PASS` 같은 일반 실행 성공 어휘가
  없다(이름이 같으면 집계와 화면에서 섞인다)
- `as_real_hardware_verdict()`는 예외를 던진다 — 승격 경로가 없다
- 조건 7개가 **모두** 충족돼야 `simulation_transfer_completed`다
- 미충족 조건에는 이유 코드가 반드시 붙고, 충족 조건에는 붙지 않는다
- 배치 허용 구역은 선언된 치수에서 나오고, 밖이면 어느 축인지 말한다
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.reason_codes import ReasonCategory, ReasonCode
from validation.simulation_e2e import (
    CRITERIA,
    MEASURED_OBSERVATION,
    SIMULATED_OBSERVATION,
    Criterion,
    SimulationTransferResult,
    SimulationVerdictError,
    TransferStatus,
    in_placement_zone,
    not_started,
    placement_zone,
)

#: 일반 실행 성공 어휘. 시뮬레이터 결과에 **나타나면 안 된다.**
FORBIDDEN_KEYS = ("task_succeeded", "verified", "succeeded", "success",
                  "pass", "real_hardware_ready_at")


def met(key: str) -> Criterion:
    return Criterion(key=key, label=dict(CRITERIA)[key], met=True,
                     detail="관측됨")


def unmet(key: str, code: ReasonCode = ReasonCode.EXEC_SIM_FIXTURE_FAILED) -> Criterion:
    return Criterion(key=key, label=dict(CRITERIA)[key], met=False,
                     detail="관측되지 않음", reason_code=code)


def result(*, all_met: bool = True, **overrides) -> SimulationTransferResult:
    criteria = tuple(met(key) if all_met else unmet(key)
                     for key, _ in CRITERIA)
    payload = dict(
        scenario="loc_pallet_1/mat_a->loc_conveyor", object_id="mat_a",
        support_id="loc_pallet_1", target_id="loc_conveyor",
        criteria=criteria)
    payload.update(overrides)
    return SimulationTransferResult(**payload)


class TestTwoAxesStaySeparate(unittest.TestCase):
    def test_completed_simulation_is_not_real_hardware_ready(self):
        item = result()
        self.assertIs(item.status, TransferStatus.SIMULATION_TRANSFER_COMPLETED)
        self.assertTrue(item.simulation_e2e)
        self.assertFalse(item.real_hardware_ready)
        payload = item.to_dict()
        self.assertTrue(payload["simulation_e2e"])
        self.assertFalse(payload["real_hardware_ready"])
        self.assertFalse(payload["real_hardware_verified"])
        self.assertTrue(payload["is_simulated"])

    def test_grasp_observation_kind_is_fixed_to_simulated(self):
        self.assertEqual(result().grasp_observation_kind, SIMULATED_OBSERVATION)
        self.assertEqual(result(all_met=False).grasp_observation_kind,
                         SIMULATED_OBSERVATION)
        self.assertNotEqual(result().grasp_observation_kind,
                            MEASURED_OBSERVATION)

    def test_payload_never_claims_measured_grasp(self):
        payload = result().to_dict()
        self.assertNotIn(MEASURED_OBSERVATION, str(payload))

    def test_payload_has_no_general_execution_success_vocabulary(self):
        payload = result().to_dict()
        for key in FORBIDDEN_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, payload)

    def test_promotion_to_real_hardware_verdict_raises(self):
        with self.assertRaises(SimulationVerdictError):
            result().as_real_hardware_verdict()

    def test_status_value_is_not_a_general_verdict_word(self):
        for status in TransferStatus:
            with self.subTest(status=status):
                self.assertTrue(status.value.startswith("simulation_transfer_"))


class TestCompletionRequiresEverything(unittest.TestCase):
    def test_all_seven_criteria_are_required(self):
        self.assertEqual(len(CRITERIA), 7)
        for index in range(7):
            criteria = [met(key) for key, _ in CRITERIA]
            criteria[index] = unmet(CRITERIA[index][0])
            item = result()
            item = SimulationTransferResult(
                scenario=item.scenario, object_id=item.object_id,
                support_id=item.support_id, target_id=item.target_id,
                criteria=tuple(criteria))
            with self.subTest(missing=CRITERIA[index][0]):
                self.assertFalse(item.simulation_e2e)
                self.assertIs(item.status,
                              TransferStatus.SIMULATION_TRANSFER_INCOMPLETE)

    def test_no_criteria_is_not_completion(self):
        item = SimulationTransferResult(
            scenario="s", object_id="o", support_id="p", target_id="t",
            criteria=())
        self.assertFalse(item.simulation_e2e)

    def test_stop_request_is_its_own_status(self):
        item = result(stop_requested=True, stop_confirmed=True)
        self.assertIs(item.status, TransferStatus.SIMULATION_TRANSFER_STOPPED)
        self.assertFalse(item.simulation_e2e)

    def test_not_started_carries_its_reason(self):
        item = not_started(
            scenario="s", object_id="mat_a", support_id="loc_pallet_1",
            target_id="loc_conveyor",
            reason=ReasonCode.EXEC_SIM_OBJECT_ABSENT, detail="자리에 없다")
        self.assertIs(item.status,
                      TransferStatus.SIMULATION_TRANSFER_NOT_STARTED)
        self.assertFalse(item.simulation_e2e)
        self.assertEqual(item.reason_codes,
                         (ReasonCode.EXEC_SIM_OBJECT_ABSENT,))
        self.assertFalse(item.to_dict()["real_hardware_ready"])

    def test_unmet_criteria_reason_codes_are_collected(self):
        item = result(all_met=False)
        self.assertEqual(len(item.unmet), 7)
        self.assertIn(ReasonCode.EXEC_SIM_FIXTURE_FAILED, item.reason_codes)


class TestCriterionInvariants(unittest.TestCase):
    def test_unknown_criterion_key_is_refused(self):
        with self.assertRaises(SimulationVerdictError):
            Criterion(key="looks_fine", label="아무거나", met=True, detail="")

    def test_met_criterion_cannot_carry_a_reason_code(self):
        with self.assertRaises(SimulationVerdictError):
            Criterion(key="stages_completed", label="x", met=True, detail="",
                      reason_code=ReasonCode.EXEC_SIM_FIXTURE_FAILED)

    def test_unmet_criterion_must_carry_a_reason_code(self):
        with self.assertRaises(SimulationVerdictError):
            Criterion(key="stages_completed", label="x", met=False, detail="")

    def test_duplicate_criteria_are_refused(self):
        with self.assertRaises(SimulationVerdictError):
            SimulationTransferResult(
                scenario="s", object_id="o", support_id="p", target_id="t",
                criteria=(met("stages_completed"), met("stages_completed")))


class TestSimulationReasonCodes(unittest.TestCase):
    def test_simulation_codes_are_in_the_exec_category(self):
        for code in (ReasonCode.EXEC_SIM_OBJECT_ABSENT,
                     ReasonCode.EXEC_SIM_TARGET_OCCUPIED,
                     ReasonCode.EXEC_SIM_FIXTURE_FAILED,
                     ReasonCode.EXEC_SIM_PLACEMENT_OUT_OF_ZONE):
            with self.subTest(code=code):
                self.assertIs(code.category, ReasonCategory.EXEC)
                self.assertIn(".sim_", code.value)

    def test_each_precondition_has_its_own_code(self):
        values = {ReasonCode.EXEC_SIM_OBJECT_ABSENT.value,
                  ReasonCode.EXEC_SIM_TARGET_OCCUPIED.value,
                  ReasonCode.EXEC_SIM_FIXTURE_FAILED.value,
                  ReasonCode.EXEC_SIM_PLACEMENT_OUT_OF_ZONE.value}
        self.assertEqual(len(values), 4)


class TestSimulationResultsStayOutOfExecutionRecords(unittest.TestCase):
    """시뮬레이터 이송 결과가 실행 기록·집계에 섞이지 않는다."""

    def source(self, relative: str) -> str:
        """docstring과 주석을 뺀 코드만. 설명 문단을 코드로 오인하지 않는다."""
        import io
        import tokenize

        text = (ROOT / relative).read_text(encoding="utf-8")
        docs = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docs.add(doc)
        pieces: list[str] = []
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for kind, value, _start, _end, _line in tokens:
            if kind == tokenize.COMMENT:
                continue
            if kind == tokenize.STRING:
                literal = ast.literal_eval(value)
                if isinstance(literal, str) and literal in docs:
                    continue
            pieces.append(value)
        return "\n".join(pieces)

    def test_demo_runner_does_not_write_to_the_execution_store(self):
        """시연은 보고서 파일에만 남는다 — `/health` 실행 집계에 들어가지 않는다."""
        source = self.source("scripts/demo_workcell_pick_place.py")
        for token in ("storage", "Repository", "save_execution",
                      "append_execution", "ExecutionRecord"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_demo_runner_does_not_build_execution_results(self):
        source = self.source("scripts/demo_workcell_pick_place.py")
        for token in ("ExecutionResult", "task_succeeded", "ExecutionState"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_verdict_module_does_not_import_execution_contracts(self):
        source = self.source("validation/simulation_e2e.py")
        for token in ("execution_result", "ExecutionResult", "ExecutionState",
                      "task_succeeded"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_fixture_never_claims_a_grasp(self):
        source = self.source("robots/fr3_gazebo/sim_fixture.py")
        self.assertIn("simulation_fixture", source)
        # 파지(grasp) 관측을 만들어 내는 경로가 없다.
        for token in ("GraspObservation", "grasp.object_held", "measured"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)


class TestPlacementZone(unittest.TestCase):
    def zone(self) -> dict:
        # 벨트 0.8 × 0.3, 상면 z=0.7, 자재 0.05 × 0.05 × 0.1
        return placement_zone(
            target_center_m=(0.25, -0.5, 0.7),
            surface_half_extent_m=(0.4, 0.15),
            object_size_m=(0.05, 0.05, 0.1),
            surface_top_z_m=0.7, z_tolerance_m=0.01)

    def test_zone_is_derived_from_declared_extents(self):
        zone = self.zone()
        self.assertAlmostEqual(zone["x_min_m"], 0.25 - 0.375)
        self.assertAlmostEqual(zone["x_max_m"], 0.25 + 0.375)
        self.assertAlmostEqual(zone["y_min_m"], -0.5 - 0.125)
        self.assertAlmostEqual(zone["z_center_m"], 0.75)

    def test_centre_of_the_belt_is_inside(self):
        inside, detail = in_placement_zone((0.25, -0.5, 0.75), self.zone())
        self.assertTrue(inside, detail)

    def test_says_which_axis_left_the_zone(self):
        inside, detail = in_placement_zone((0.25, 0.2, 0.75), self.zone())
        self.assertFalse(inside)
        self.assertIn("y=", detail)
        self.assertNotIn("x=", detail)

    def test_height_outside_tolerance_is_outside(self):
        inside, detail = in_placement_zone((0.25, -0.5, 0.84), self.zone())
        self.assertFalse(inside)
        self.assertIn("z=", detail)

    def test_object_half_width_is_kept_inside_the_surface(self):
        # 벨트 끝(x=0.65)에 중심을 두면 자재가 반쯤 걸친다 — 구역 밖이다.
        inside, _ = in_placement_zone((0.65, -0.5, 0.75), self.zone())
        self.assertFalse(inside)


if __name__ == "__main__":
    unittest.main(verbosity=2)
