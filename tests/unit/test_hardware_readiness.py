"""실기 전환 준비 입력·관문 (md/개발플랜.md 8-12).

**합성 값으로 계약을 검증한다.** 실제 장착 수치를 만들지 않는다.

확인하는 것:

- 입력 하나에 여덟 필드(value·unit·source·measurement_method·captured_at·
  verified·evidence_path·**operator**)가 모두 있어야 `verified`가 될 수 있다 —
  측정값에는 **측정한 사람**이 붙어 있어야 한다
- 시뮬레이터에서 온 값(`simulation`·`simulated_observation`)과 선언값
  (`declared`)은 **`verified`가 될 수 없다**
- 값·근거·확인 상태가 각각 다른 이유 코드로 갈린다
- 근거 없는 체크리스트 `passed`는 `evidence_missing`으로 되돌아간다
- 시뮬레이터 결과를 실기 준비로 바꾸려 하면 예외다
- 모든 근거가 채워져도 **연결 확인 전에는** `real_hardware_verified`가 false다
- 저장소에 들어 있는 실제 설정은 지금 **차단 상태**다
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.grasp_observation import measured_from_device, simulated, unavailable
from core.hardware_input import (
    HARDWARE_EVIDENCE_METHODS,
    SIMULATION_METHODS,
    HardwareInput,
    HardwareInputError,
    InputSet,
    MeasurementMethod,
    load_input,
)
from core.reason_codes import ReasonCategory, ReasonCode
from validation import hardware_readiness
from validation.hardware_readiness import (
    HUMAN_ACTORS,
    Checklist,
    ChecklistStep,
    HardwareReadinessError,
    StepActor,
    StepStatus,
)

MANIFEST = ROOT / "config/hardware/active.json"


def ready_input(key: str = "coupling_mass") -> HardwareInput:
    return HardwareInput(
        key=key, label=f"합성 {key}", value=1.0, unit="kg",
        source="합성 근거 문서", measurement_method=MeasurementMethod.SCALE,
        captured_at="2026-09-17", verified=True,
        evidence_path="reports/hardware/evidence/synthetic.txt",
        operator="합성 작업자")


def pending_input(key: str = "coupling_inertia", **over) -> HardwareInput:
    payload = dict(key=key, label=f"합성 {key}", value=None, unit="kg",
                   measurement_method=MeasurementMethod.NONE)
    payload.update(over)
    return HardwareInput(**payload)


def checklist(status: StepStatus = StepStatus.NOT_STARTED,
              *, evidence: bool = False, blocking: bool = True) -> Checklist:
    return Checklist(
        checklist_id="synth", checklist_version="1.0",
        steps=(ChecklistStep(
            no=1, key="s", label="합성 단계", status=status, blocking=blocking,
            evidence_path="p" if evidence else "",
            captured_at="2026-09-17" if evidence else "",
            operator="합성 작업자" if evidence else ""),))


def observation_dict():
    return measured_from_device(
        held=True, object_id="obj", observed_at=1.0, source="device",
        method="gripper_object_detection_status", robot_id="r",
        gripper_id="g", raw_state={"gOBJ": 2}).to_dict()


def evaluate(**over):
    payload = dict(
        inputs=InputSet(profile_id="synth", profile_version="1.0",
                        inputs=(ready_input(),), source="synthetic"),
        checklist=checklist(StepStatus.PASSED, evidence=True),
        adapter_config={"arm": "a", "gripper": "b"},
        grasp_observation=observation_dict(),
    )
    payload.update(over)
    return hardware_readiness.evaluate(**payload)


class TestRequiredFields(unittest.TestCase):
    def test_a_ready_input_has_every_required_field(self):
        item = ready_input()
        payload = item.to_dict()
        for name in ("value", "unit", "source", "measurement_method",
                     "captured_at", "verified", "evidence_path", "operator"):
            with self.subTest(field=name):
                self.assertIn(name, payload)
                self.assertTrue(payload[name] not in (None, ""))
        self.assertTrue(item.ready)

    def test_verified_without_value_is_refused(self):
        with self.assertRaises(HardwareInputError):
            HardwareInput(key="k", label="l", value=None, unit="kg",
                          source="s", measurement_method=MeasurementMethod.SCALE,
                          captured_at="2026-09-17", verified=True,
                          evidence_path="p")

    def test_verified_without_evidence_path_is_refused(self):
        with self.assertRaises(HardwareInputError):
            HardwareInput(key="k", label="l", value=1.0, unit="kg",
                          source="s", measurement_method=MeasurementMethod.SCALE,
                          captured_at="2026-09-17", verified=True,
                          evidence_path="")

    def test_verified_without_captured_at_is_refused(self):
        with self.assertRaises(HardwareInputError):
            HardwareInput(key="k", label="l", value=1.0, unit="kg",
                          source="s", measurement_method=MeasurementMethod.SCALE,
                          captured_at="", verified=True, evidence_path="p")

    def test_value_without_unit_is_refused(self):
        with self.assertRaises(HardwareInputError):
            HardwareInput(key="k", label="l", value=1.0, unit="")

    def test_non_numeric_input_may_omit_the_unit(self):
        item = HardwareInput(key="k", label="l", value="RS-485", unit="",
                             numeric=False)
        self.assertTrue(item.has_value)

    def test_captured_at_must_be_iso_8601(self):
        with self.assertRaises(HardwareInputError):
            HardwareInput(key="k", label="l", captured_at="2026년 9월 17일")
        HardwareInput(key="k", label="l", captured_at="2026-09-17")
        HardwareInput(key="k", label="l", captured_at="2026-09-17T10:30:00+09:00")


class TestSimulationValuesCannotBeVerified(unittest.TestCase):
    def test_simulation_method_cannot_be_verified(self):
        for method in SIMULATION_METHODS:
            with self.subTest(method=method), self.assertRaises(HardwareInputError):
                HardwareInput(key="k", label="l", value=1.0, unit="kg",
                              source="시뮬레이터 설정", measurement_method=method,
                              captured_at="2026-09-17", verified=True,
                              evidence_path="p")

    def test_declared_method_cannot_be_verified(self):
        with self.assertRaises(HardwareInputError):
            HardwareInput(key="k", label="l", value=1.0, unit="kg",
                          source="문서", measurement_method=MeasurementMethod.DECLARED,
                          captured_at="2026-09-17", verified=True,
                          evidence_path="p")

    def test_hardware_evidence_methods_exclude_simulation_and_declared(self):
        self.assertFalse(HARDWARE_EVIDENCE_METHODS & SIMULATION_METHODS)
        self.assertNotIn(MeasurementMethod.DECLARED, HARDWARE_EVIDENCE_METHODS)

    def test_each_evidence_method_can_be_verified(self):
        for method in HARDWARE_EVIDENCE_METHODS:
            with self.subTest(method=method):
                item = HardwareInput(
                    key="k", label="l", value=1.0, unit="kg", source="s",
                    measurement_method=method, captured_at="2026-09-17",
                    verified=True, evidence_path="p", operator="합성 작업자")
                self.assertTrue(item.ready)


class TestReasonCodesAreSeparated(unittest.TestCase):
    def finding(self, item: HardwareInput):
        result = evaluate(inputs=InputSet(
            profile_id="s", profile_version="1", inputs=(item,)))
        return next((f for f in result.findings if f.key == item.key), None)

    def test_missing_value_gets_input_missing(self):
        self.assertIs(self.finding(pending_input()).reason_code,
                      ReasonCode.HARDWARE_INPUT_MISSING)

    def test_declared_value_gets_declared_only(self):
        item = pending_input(
            value=0.0, source="문서",
            measurement_method=MeasurementMethod.DECLARED,
            captured_at="2026-09-17", evidence_path="p")
        self.assertIs(self.finding(item).reason_code,
                      ReasonCode.HARDWARE_DECLARED_ONLY)

    def test_simulation_value_gets_simulated_value_rejected(self):
        item = pending_input(
            value=1.0, measurement_method=MeasurementMethod.SIMULATION)
        self.assertIs(self.finding(item).reason_code,
                      ReasonCode.HARDWARE_SIMULATED_VALUE_REJECTED)

    def test_value_without_evidence_gets_evidence_missing(self):
        item = pending_input(
            value=1.0, measurement_method=MeasurementMethod.SCALE)
        finding = self.finding(item)
        self.assertIs(finding.reason_code, ReasonCode.HARDWARE_EVIDENCE_MISSING)
        self.assertIn("source", finding.detail)

    def test_value_with_evidence_but_unverified_gets_unverified(self):
        item = pending_input(
            value=1.0, source="s", measurement_method=MeasurementMethod.SCALE,
            captured_at="2026-09-17", evidence_path="p",
            operator="합성 작업자")
        self.assertIs(self.finding(item).reason_code,
                      ReasonCode.HARDWARE_UNVERIFIED)

    def test_input_declared_nowhere_is_still_required(self):
        result = evaluate(required_keys=("not_in_config",))
        finding = next(f for f in result.findings if f.key == "not_in_config")
        self.assertIs(finding.reason_code, ReasonCode.HARDWARE_INPUT_MISSING)

    def test_hardware_codes_are_in_the_hardware_category(self):
        for code in (ReasonCode.HARDWARE_INPUT_MISSING,
                     ReasonCode.HARDWARE_EVIDENCE_MISSING,
                     ReasonCode.HARDWARE_UNVERIFIED,
                     ReasonCode.HARDWARE_SIMULATED_VALUE_REJECTED,
                     ReasonCode.HARDWARE_DECLARED_ONLY,
                     ReasonCode.HARDWARE_OBSERVATION_UNAVAILABLE,
                     ReasonCode.HARDWARE_ADAPTER_UNCONFIGURED,
                     ReasonCode.HARDWARE_CHECKLIST_INCOMPLETE,
                     ReasonCode.HARDWARE_NOT_CONNECTED):
            with self.subTest(code=code):
                self.assertIs(code.category, ReasonCategory.HARDWARE)


class TestOperatorIsRequired(unittest.TestCase):
    """측정값과 단계 통과에는 **수행한 사람**이 붙어 있어야 한다."""

    def test_verified_without_operator_is_refused(self):
        with self.assertRaises(HardwareInputError):
            HardwareInput(key="k", label="l", value=1.0, unit="kg", source="s",
                          measurement_method=MeasurementMethod.SCALE,
                          captured_at="2026-09-17", verified=True,
                          evidence_path="p")

    def test_missing_operator_makes_evidence_incomplete(self):
        item = pending_input(
            value=1.0, source="s", measurement_method=MeasurementMethod.SCALE,
            captured_at="2026-09-17", evidence_path="p")
        self.assertFalse(item.has_evidence)
        finding = evaluate(inputs=InputSet(
            profile_id="s", profile_version="1", inputs=(item,))).findings[0]
        self.assertIs(finding.reason_code, ReasonCode.HARDWARE_EVIDENCE_MISSING)
        self.assertIn("operator", finding.detail)

    def test_passed_step_without_operator_is_evidence_missing(self):
        step = ChecklistStep(no=1, key="s", label="합성",
                             status=StepStatus.PASSED, evidence_path="p",
                             captured_at="2026-09-17")
        self.assertIs(step.effective_status, StepStatus.EVIDENCE_MISSING)
        self.assertFalse(step.done)

    def test_passed_step_with_operator_is_done(self):
        step = ChecklistStep(no=1, key="s", label="합성",
                             status=StepStatus.PASSED, evidence_path="p",
                             captured_at="2026-09-17", operator="합성 작업자")
        self.assertTrue(step.done)


class TestStepActorSeparation(unittest.TestCase):
    """실제 장비를 만지는 단계는 **사람이 수행한다.**"""

    def test_actor_values_are_declared(self):
        self.assertEqual(
            {a.value for a in StepActor},
            {"field_operator", "field_operator_with_robot", "code"})

    def test_human_actors_exclude_code(self):
        self.assertNotIn(StepActor.CODE, HUMAN_ACTORS)
        self.assertEqual(len(HUMAN_ACTORS), 2)

    def test_default_actor_is_a_human(self):
        step = ChecklistStep(no=1, key="s", label="l",
                             status=StepStatus.NOT_STARTED)
        self.assertTrue(step.performed_by_human)

    def test_unknown_actor_in_config_is_refused(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                          delete=False) as handle:
            json.dump({"steps": [{"no": 1, "key": "k", "label": "l",
                                  "status": "not_started",
                                  "actor": "the_robot_itself"}]}, handle)
            path = handle.name
        with self.assertRaises(HardwareReadinessError):
            hardware_readiness.load_checklist(path)


class TestChecklist(unittest.TestCase):
    def test_passed_without_evidence_becomes_evidence_missing(self):
        step = checklist(StepStatus.PASSED, evidence=False).steps[0]
        self.assertIs(step.effective_status, StepStatus.EVIDENCE_MISSING)
        self.assertFalse(step.done)

    def test_passed_with_evidence_is_done(self):
        step = checklist(StepStatus.PASSED, evidence=True).steps[0]
        self.assertIs(step.effective_status, StepStatus.PASSED)
        self.assertTrue(step.done)

    def test_every_status_value_is_declared(self):
        self.assertEqual(
            {s.value for s in StepStatus},
            {"not_started", "in_progress", "passed", "failed",
             "evidence_missing"})

    def test_pending_step_blocks_readiness(self):
        result = evaluate(checklist=checklist(StepStatus.IN_PROGRESS))
        self.assertFalse(result.real_hardware_ready)
        self.assertIn(ReasonCode.HARDWARE_CHECKLIST_INCOMPLETE,
                      result.reason_codes)

    def test_failed_step_blocks_readiness(self):
        result = evaluate(checklist=checklist(StepStatus.FAILED))
        self.assertFalse(result.real_hardware_ready)

    def test_non_blocking_step_does_not_block(self):
        result = evaluate(checklist=Checklist(
            checklist_id="s", checklist_version="1",
            steps=(ChecklistStep(no=1, key="a", label="필수", blocking=True,
                                 status=StepStatus.PASSED, evidence_path="p",
                                 captured_at="2026-09-17",
                                 operator="합성 작업자"),
                   ChecklistStep(no=2, key="b", label="참고", blocking=False,
                                 status=StepStatus.NOT_STARTED))))
        self.assertTrue(result.real_hardware_ready)

    def test_unknown_status_in_config_is_refused(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                          delete=False) as handle:
            json.dump({"steps": [{"no": 1, "key": "k", "label": "l",
                                  "status": "probably_fine"}]}, handle)
            path = handle.name
        with self.assertRaises(HardwareReadinessError):
            hardware_readiness.load_checklist(path)


class TestGraspObservationRequirement(unittest.TestCase):
    def test_no_observation_blocks(self):
        result = evaluate(grasp_observation=None)
        self.assertFalse(result.real_hardware_ready)
        self.assertIn(ReasonCode.HARDWARE_OBSERVATION_UNAVAILABLE,
                      result.reason_codes)

    def test_unavailable_observation_blocks(self):
        result = evaluate(grasp_observation=unavailable(
            detail="수단 없음").to_dict())
        self.assertFalse(result.real_hardware_ready)

    def test_simulated_observation_is_rejected(self):
        result = evaluate(grasp_observation=simulated(
            held=True, object_id="o", observed_at=1.0, source="sim",
            method="simulator_fixture").to_dict())
        finding = next(f for f in result.findings
                       if f.key == "grasp_observation")
        self.assertIs(finding.reason_code,
                      ReasonCode.HARDWARE_SIMULATED_VALUE_REJECTED)

    def test_measured_without_raw_state_is_incomplete(self):
        from core.grasp_observation import measured

        observation = measured(
            held=True, object_id="o", observed_at=1.0, source="device",
            method="object_presence_sensor").to_dict()
        self.assertFalse(observation["hardware_complete"])
        result = evaluate(grasp_observation=observation)
        finding = next(f for f in result.findings
                       if f.key == "grasp_observation")
        self.assertIs(finding.reason_code,
                      ReasonCode.HARDWARE_OBSERVATION_UNAVAILABLE)
        self.assertIn("raw_state", finding.detail)

    def test_complete_device_observation_is_accepted(self):
        result = evaluate()
        self.assertTrue(result.real_hardware_ready, result.missing_labels)

    def test_device_observation_requires_raw_state(self):
        from core.grasp_observation import GraspObservationError

        with self.assertRaises(GraspObservationError):
            measured_from_device(
                held=True, object_id="o", observed_at=1.0, source="d",
                method="object_presence", robot_id="r", gripper_id="g",
                raw_state={})

    def test_device_observation_requires_robot_and_gripper_ids(self):
        from core.grasp_observation import GraspObservationError

        with self.assertRaises(GraspObservationError):
            measured_from_device(
                held=True, object_id="o", observed_at=1.0, source="d",
                method="object_presence", robot_id="", gripper_id="g",
                raw_state={"gOBJ": 2})


class TestTwoAxesAndPromotion(unittest.TestCase):
    def test_ready_is_not_verified_without_connection(self):
        result = evaluate(hardware_connected=False)
        self.assertTrue(result.real_hardware_ready)
        self.assertFalse(result.real_hardware_verified)
        payload = result.to_dict()
        self.assertFalse(payload["real_hardware_verified"])
        self.assertFalse(payload["real_hardware_connected"])
        self.assertFalse(payload["is_simulated"])

    def test_verified_needs_connection_confirmed(self):
        self.assertTrue(evaluate(hardware_connected=True).real_hardware_verified)

    def test_promoting_simulation_raises(self):
        with self.assertRaises(HardwareReadinessError):
            evaluate().promote_simulation({"simulation_e2e": True})

    def test_gate_has_no_parameter_for_simulation_results(self):
        import inspect

        params = set(inspect.signature(hardware_readiness.evaluate).parameters)
        for name in ("simulation_e2e", "simulation_transfer", "transfer_result",
                     "sim_result"):
            with self.subTest(param=name):
                self.assertNotIn(name, params)

    def test_collection_guide_tells_people_what_to_bring(self):
        result = evaluate(inputs=InputSet(
            profile_id="s", profile_version="1",
            inputs=(pending_input(over_needed := "coupling_inertia"),)))
        guide = hardware_readiness.collection_guide(result)
        self.assertTrue(guide)
        for row in guide:
            with self.subTest(row=row["label"]):
                self.assertTrue(row["needed"])
                self.assertTrue(row["reason_code"])


class TestPinnedState(unittest.TestCase):
    """지금 상태를 **다섯 줄로 고정**한다. 숫자가 다른 뜻으로 읽히지 않는다."""

    def live(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        inputs = hardware_readiness.load_input_set(ROOT / manifest["inputs"])
        checklist = hardware_readiness.load_checklist(
            ROOT / manifest["checklist"])
        return hardware_readiness.evaluate(
            inputs=inputs, checklist=checklist, adapter_config=None,
            grasp_observation=None, hardware_connected=False)

    def test_shipped_state_is_exactly_the_declared_five_lines(self):
        state = hardware_readiness.pinned_state(self.live(),
                                                 simulation_e2e=True)
        self.assertEqual(
            hardware_readiness.state_lines(state),
            ("simulation_e2e=true",
             "real_hardware_ready=false",
             "real_hardware_verified=false",
             "hardware_evidence=0/18",
             "hardware_checklist=0/11"))

    def test_simulation_true_does_not_change_the_hardware_axes(self):
        """시뮬레이터가 참이어도 실기 값은 바뀌지 않는다."""
        result = self.live()
        for flag in (True, False, None):
            with self.subTest(simulation_e2e=flag):
                state = hardware_readiness.pinned_state(result,
                                                         simulation_e2e=flag)
                self.assertFalse(state["real_hardware_ready"])
                self.assertFalse(state["real_hardware_verified"])
                self.assertEqual(state["hardware_evidence"], "0/18")

    def test_zero_evidence_means_not_verified(self):
        """0/18 · 0/11이면 verified는 반드시 false다."""
        state = hardware_readiness.pinned_state(self.live())
        self.assertEqual(state["hardware_evidence"].split("/")[0], "0")
        self.assertEqual(state["hardware_checklist"].split("/")[0], "0")
        self.assertIs(state["real_hardware_verified"], False)
        self.assertIs(state["real_hardware_ready"], False)

    def test_state_keys_are_declared_in_order(self):
        self.assertEqual(
            hardware_readiness.STATE_KEYS,
            ("simulation_e2e", "real_hardware_ready",
             "real_hardware_verified", "hardware_evidence",
             "hardware_checklist"))

    def test_counts_are_strings_not_booleans(self):
        """근거 수집 수는 `a/b` 문자열이다 — 참/거짓으로 읽히지 않는다."""
        state = hardware_readiness.pinned_state(self.live())
        for key in ("hardware_evidence", "hardware_checklist"):
            with self.subTest(key=key):
                self.assertIsInstance(state[key], str)
                self.assertIn("/", state[key])
        for key in ("real_hardware_ready", "real_hardware_verified"):
            with self.subTest(key=key):
                self.assertIsInstance(state[key], bool)

    def test_unknown_simulation_axis_is_not_reported_as_false(self):
        """시뮬레이터 기록이 없으면 `unknown`이다 — 거짓으로 단정하지 않는다."""
        state = hardware_readiness.pinned_state(self.live(),
                                                 simulation_e2e=None)
        self.assertIsNone(state["simulation_e2e"])
        self.assertIn("simulation_e2e=unknown",
                      hardware_readiness.state_lines(state))

    def test_verified_needs_both_evidence_and_connection(self):
        """근거가 다 차도 연결 확인 전에는 verified가 false다."""
        ready = evaluate(hardware_connected=False)
        state = hardware_readiness.pinned_state(ready, simulation_e2e=True)
        self.assertTrue(state["real_hardware_ready"])
        self.assertFalse(state["real_hardware_verified"])


class TestNoPromotionPathFromSimulation(unittest.TestCase):
    """Gazebo 결과를 실기 검증으로 오인시키는 경로가 없는지 다시 검사한다."""

    def code_strings(self, relative: str) -> list[str]:
        """docstring·주석을 뺀 코드 문자열만."""
        import ast
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
        out = []
        for kind, value, _s, _e, _l in tokenize.generate_tokens(
                io.StringIO(text).readline):
            if kind == tokenize.STRING:
                literal = ast.literal_eval(value)
                if isinstance(literal, str) and literal not in docs:
                    out.append(literal)
        return out

    def test_evaluate_takes_no_simulation_result(self):
        import inspect

        params = set(inspect.signature(hardware_readiness.evaluate).parameters)
        for name in ("simulation_e2e", "simulation_transfer", "transfer_result",
                     "sim_result", "simulation_result"):
            with self.subTest(param=name):
                self.assertNotIn(name, params)

    def test_promote_simulation_raises(self):
        with self.assertRaises(HardwareReadinessError):
            evaluate().promote_simulation({"simulation_e2e": True})

    def test_transfer_result_cannot_become_a_hardware_verdict(self):
        from validation.simulation_e2e import (
            CRITERIA,
            Criterion,
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

    def test_pinned_state_simulation_axis_is_display_only(self):
        """`pinned_state`가 시뮬레이터 축을 받아도 실기 값을 바꾸지 않는다."""
        import inspect

        source = inspect.getsource(hardware_readiness.pinned_state)
        # 실기 두 축은 result에서만 읽는다.
        self.assertIn("result.real_hardware_ready", source)
        self.assertIn("result.real_hardware_verified", source)
        # 시뮬레이터 인자가 그 두 값에 쓰이지 않는다.
        for line in source.splitlines():
            if "real_hardware_" in line:
                with self.subTest(line=line.strip()[:60]):
                    self.assertNotIn("simulation_e2e", line)

    def test_simulation_methods_cannot_be_verified_inputs(self):
        for method in SIMULATION_METHODS:
            with self.subTest(method=method), self.assertRaises(
                    HardwareInputError):
                HardwareInput(key="k", label="l", value=1.0, unit="kg",
                              source="s", measurement_method=method,
                              captured_at="2026-09-17", verified=True,
                              evidence_path="p", operator="합성 작업자")

    def test_hardware_adapters_do_not_import_simulation_modules(self):
        import ast

        for name in ("fr3.py", "robotiq.py", "config.py", "__init__.py"):
            path = ROOT / "robots" / "hardware" / name
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            with self.subTest(file=name):
                self.assertNotIn("validation.simulation_e2e", imported)
                self.assertNotIn("robots.fr3_gazebo.sim_fixture", imported)

    def test_readiness_gate_has_no_simulation_literals_in_code(self):
        for literal in self.code_strings("validation/hardware_readiness.py"):
            with self.subTest(literal=literal[:40]):
                self.assertNotIn("simulation_transfer", literal)
                self.assertNotIn("simulation_e2e_verify", literal)

    def test_reports_never_pair_verified_with_a_count(self):
        """보고서에서 `real_hardware_verified` 가 수와 섞이지 않는다."""
        path = ROOT / "reports/hardware/readiness.json"
        if not path.is_file():
            self.skipTest("검증 보고서가 없다")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsInstance(payload["real_hardware_verified"], bool)
        self.assertFalse(payload["real_hardware_verified"])
        # 검사 통과 수는 이름으로 구분된다.
        self.assertNotIn("passed_count", payload)
        self.assertIn("gate_checks_passed", payload)
        self.assertEqual(payload["state"]["hardware_evidence"], "0/18")


class TestShippedConfigIsBlocked(unittest.TestCase):
    """저장소에 들어 있는 실제 설정은 **지금 차단 상태여야 한다.**"""

    def setUp(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.inputs = hardware_readiness.load_input_set(
            ROOT / manifest["inputs"])
        self.checklist = hardware_readiness.load_checklist(
            ROOT / manifest["checklist"])

    def test_manifest_is_enabled_and_declares_both_files(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertTrue(manifest["enabled"])
        self.assertTrue((ROOT / manifest["inputs"]).is_file())
        self.assertTrue((ROOT / manifest["checklist"]).is_file())
        self.assertFalse(manifest["real_hardware_connected"])

    def test_no_input_is_verified_yet(self):
        self.assertEqual(self.inputs.ready_keys, ())
        self.assertEqual(len(self.inputs.pending_keys), len(self.inputs.inputs))

    def test_every_input_says_what_to_bring(self):
        for item in self.inputs.inputs:
            with self.subTest(key=item.key):
                self.assertTrue(item.needed, f"{item.key}에 needed가 없다")
                self.assertTrue(item.label)

    def test_required_inputs_cover_the_declared_scope(self):
        keys = {item.key for item in self.inputs.inputs}
        for expected in ("mounting_transform_xyz", "mounting_transform_rpy",
                         "mounting_yaw_evidence", "coupling_measured_mass",
                         "coupling_center_of_mass", "coupling_inertia",
                         "tcp_coordinates", "tcp_measurement_procedure",
                         "gripper_power_interface",
                         "gripper_communication_interface",
                         "arm_controller_interface",
                         "grasp_observation_interface",
                         "workbench_frame_measured", "pallet_frames_measured",
                         "conveyor_frame_measured"):
            with self.subTest(key=expected):
                self.assertIn(expected, keys)

    def test_checklist_has_every_declared_step(self):
        keys = {step.key for step in self.checklist.steps}
        for expected in ("power_off_before_mounting", "bolt_spec_confirmed",
                         "coupling_faces_and_boss", "yaw_direction_photo",
                         "cable_clearance_and_range", "tcp_measured",
                         "slow_unloaded_home", "slow_open_close", "real_stop",
                         "grasp_observation_confirmed",
                         "cell_frame_calibration"):
            with self.subTest(key=expected):
                self.assertIn(expected, keys)

    def test_no_checklist_step_is_done_yet(self):
        self.assertEqual(self.checklist.pending, self.checklist.blocking_steps)
        self.assertFalse(self.checklist.complete)

    def test_evaluation_blocks_with_actionable_findings(self):
        result = hardware_readiness.evaluate(
            inputs=self.inputs, checklist=self.checklist,
            adapter_config=None, grasp_observation=None)
        self.assertFalse(result.real_hardware_ready)
        self.assertFalse(result.real_hardware_verified)
        self.assertGreater(len(result.findings), 0)
        for finding in result.findings:
            with self.subTest(key=finding.key):
                self.assertTrue(finding.needed or finding.detail)

    def test_shipped_config_has_no_simulation_sourced_values(self):
        for item in self.inputs.inputs:
            with self.subTest(key=item.key):
                self.assertNotIn(item.measurement_method, SIMULATION_METHODS)

    def test_load_input_refuses_unknown_measurement_method(self):
        with self.assertRaises(HardwareInputError):
            load_input({"key": "k", "label": "l",
                        "measurement_method": "vibes"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
