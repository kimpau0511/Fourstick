"""실기 어댑터 경계 (md/개발플랜.md 8-12).

**실제 로봇에 명령을 보내지 않는다.** 확인하는 것은 경계가 막히는지다.

- 설정이 없으면 어댑터를 만들 수 없다(주소·한계값 모두)
- 준비되지 않은 어댑터는 연결·스킬·점검을 전부 거절한다
- `state()`는 관측이 없다고 말한다 — 시뮬레이터 값을 대신 넣지 않는다
- 결과 형식이 Gazebo 어댑터와 같은 공통 계약(`ExecutionResult`)이다
- 주소·인증값·엔드포인트·한계값이 **코드에 없다**
- 파지 관측기는 원본 device 상태 없이 파지를 주장하지 않고, 개구·시뮬레이터
  경로는 예외를 던진다
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.capability_profile import CapabilityProfile, JointLimit
from core.execution_result import ExecutionResult
from core.frames import ANGLE_UNIT, FrameKind, Vector3
from core.grasp_observation import GraspObservation, GraspObservationError
from core.reason_codes import ReasonCode
from robots.base.robot_adapter import RobotAdapter
from robots.hardware import (
    FR3HardwareAdapter,
    HardwareConfigError,
    HardwareConnection,
    Robotiq2F85HardwareObservationAdapter,
    load_hardware_connection,
)
from robots.hardware.config import (
    ENV_PREFIX,
    HardwareLimits,
    connection_state,
    load_hardware_limits,
)
from validation import hardware_readiness
from validation.hardware_readiness import Checklist, ChecklistStep, StepStatus

HARDWARE_DIR = ROOT / "robots" / "hardware"


def profile() -> CapabilityProfile:
    """합성 Profile. 실제 로봇 값이 아니다."""
    return CapabilityProfile(
        profile_id="synth_hw", profile_version="test-1.0", dof=1,
        joint_limits=(JointLimit("j1", -1.0, 1.0, 0.5, ANGLE_UNIT, "revolute"),),
        frames={FrameKind.BASE: "base", FrameKind.TOOL: "tool"},
        tcp_offset=Vector3(0.0, 0.0, 0.1), work_radius_m=1.0, payload_kg=1.0,
        supported_skills=("home", "move", "stop"))


def limits() -> HardwareLimits:
    return HardwareLimits(source="합성 한계 문서",
                          joint_limits_rad={"j1": (-1.0, 1.0)},
                          joint_velocity_rad_s={"j1": 0.5})


def connection(*, with_limits: bool = True) -> HardwareConnection:
    return HardwareConnection(
        arm_kind="synthetic-arm", arm_endpoint="synthetic://arm",
        gripper_kind="synthetic-gripper", gripper_endpoint="synthetic://gripper",
        limits=limits() if with_limits else None)


def readiness(*, ready: bool):
    from core.hardware_input import HardwareInput, InputSet, MeasurementMethod

    if ready:
        inputs = (HardwareInput(
            key="k", label="합성", value=1.0, unit="kg", source="s",
            measurement_method=MeasurementMethod.SCALE,
            captured_at="2026-09-17", verified=True, evidence_path="p",
            operator="합성 작업자"),)
        steps = (ChecklistStep(no=1, key="s", label="합성",
                               status=StepStatus.PASSED, evidence_path="p",
                               captured_at="2026-09-17",
                               operator="합성 작업자"),)
        observation = {"availability": "measured", "hardware_complete": True}
        adapter_config = {"arm": "a", "gripper": "b"}
    else:
        inputs = (HardwareInput(key="k", label="합성", unit="kg"),)
        steps = (ChecklistStep(no=1, key="s", label="합성",
                               status=StepStatus.NOT_STARTED),)
        observation = None
        adapter_config = None
    return hardware_readiness.evaluate(
        inputs=InputSet(profile_id="s", profile_version="1", inputs=inputs),
        checklist=Checklist(checklist_id="s", checklist_version="1",
                            steps=steps),
        adapter_config=adapter_config, grasp_observation=observation)


class TestConfigIsRequired(unittest.TestCase):
    def test_missing_environment_refuses(self):
        with self.assertRaises(HardwareConfigError) as caught:
            load_hardware_connection(env={})
        self.assertIs(caught.exception.reason,
                      ReasonCode.HARDWARE_ADAPTER_UNCONFIGURED)

    def test_partial_environment_refuses(self):
        with self.assertRaises(HardwareConfigError):
            load_hardware_connection(env={f"{ENV_PREFIX}ARM_KIND": "x"})

    def test_complete_environment_loads_without_limits(self):
        loaded = load_hardware_connection(env={
            f"{ENV_PREFIX}ARM_KIND": "k", f"{ENV_PREFIX}ARM_ENDPOINT": "e",
            f"{ENV_PREFIX}GRIPPER_KIND": "k", f"{ENV_PREFIX}GRIPPER_ENDPOINT": "e",
        })
        self.assertFalse(loaded.complete)

    def test_connection_dict_does_not_leak_endpoints(self):
        payload = connection().to_dict()
        blob = str(payload)
        self.assertNotIn("synthetic://arm", blob)
        self.assertNotIn("synthetic://gripper", blob)
        self.assertTrue(payload["arm_endpoint_configured"])

    def test_limits_require_a_source(self):
        with self.assertRaises(HardwareConfigError):
            HardwareLimits(source="", joint_limits_rad={"j1": (-1.0, 1.0)},
                           joint_velocity_rad_s={"j1": 0.5})

    def test_limits_require_both_tables(self):
        with self.assertRaises(HardwareConfigError):
            HardwareLimits(source="s", joint_limits_rad={},
                           joint_velocity_rad_s={"j1": 0.5})

    def test_missing_limits_file_refuses(self):
        with self.assertRaises(HardwareConfigError):
            load_hardware_limits(ROOT / "config/hardware/does_not_exist.json")

    def test_connection_state_reports_without_values(self):
        state = connection_state(env={})
        self.assertFalse(state["configured"])
        self.assertEqual(state["reason_code"],
                         ReasonCode.HARDWARE_ADAPTER_UNCONFIGURED.value)


class TestArmAdapterBoundary(unittest.TestCase):
    def test_adapter_follows_the_common_contract(self):
        self.assertTrue(issubclass(FR3HardwareAdapter, RobotAdapter))
        adapter = FR3HardwareAdapter("r", profile(), connection=connection(),
                                     readiness=readiness(ready=False))
        self.assertIsInstance(adapter.home(1.0), ExecutionResult)

    def test_adapter_kind_is_real_and_not_simulated(self):
        self.assertEqual(FR3HardwareAdapter.adapter_kind, "real")
        self.assertFalse(FR3HardwareAdapter.is_simulated_adapter)

    def test_cannot_build_without_limits(self):
        with self.assertRaises(HardwareConfigError) as caught:
            FR3HardwareAdapter("r", profile(),
                               connection=connection(with_limits=False),
                               readiness=readiness(ready=False))
        self.assertIs(caught.exception.reason,
                      ReasonCode.HARDWARE_ADAPTER_UNCONFIGURED)

    def test_cannot_build_without_readiness(self):
        with self.assertRaises(HardwareConfigError) as caught:
            FR3HardwareAdapter("r", profile(), connection=connection(),
                               readiness=None)
        self.assertIs(caught.exception.reason,
                      ReasonCode.HARDWARE_INPUT_MISSING)

    def test_unready_adapter_refuses_connect(self):
        adapter = FR3HardwareAdapter("r", profile(), connection=connection(),
                                     readiness=readiness(ready=False))
        result = adapter.connect(1.0)
        self.assertFalse(result.request_accepted)
        self.assertIs(result.reason, ReasonCode.HARDWARE_INPUT_MISSING)
        self.assertIn("missing", result.evidence)

    def test_unready_adapter_refuses_every_skill(self):
        adapter = FR3HardwareAdapter("r", profile(), connection=connection(),
                                     readiness=readiness(ready=False))
        for result in (adapter.home(1.0), adapter.move("x", 1.0),
                       adapter.pick("o", "s", 1.0),
                       adapter.place("o", "d", 1.0), adapter.check()):
            with self.subTest(reason=result.reason):
                self.assertFalse(result.request_accepted)
                self.assertFalse(result.task_succeeded)

    def test_unready_adapter_supports_nothing(self):
        adapter = FR3HardwareAdapter("r", profile(), connection=connection(),
                                     readiness=readiness(ready=False))
        for skill in ("home", "move", "stop", "pick", "place"):
            with self.subTest(skill=skill):
                self.assertFalse(adapter.supports(skill))

    def test_ready_adapter_still_refuses_connect_without_implementation(self):
        """준비가 끝나도 **연결 구현이 없으면** 연결됐다고 말하지 않는다."""
        adapter = FR3HardwareAdapter("r", profile(), connection=connection(),
                                     readiness=readiness(ready=True))
        self.assertTrue(adapter.ready)
        result = adapter.connect(1.0)
        self.assertFalse(result.request_accepted)
        self.assertIs(result.reason, ReasonCode.HARDWARE_NOT_CONNECTED)

    def test_state_says_it_did_not_observe(self):
        adapter = FR3HardwareAdapter("r", profile(), connection=connection(),
                                     readiness=readiness(ready=True))
        state = adapter.state()
        self.assertFalse(state.valid)
        self.assertFalse(state.hold_observed)
        self.assertIsNone(state.held_object)
        self.assertEqual(dict(state.joint_positions), {})

    def test_stop_without_connection_is_not_a_stop(self):
        adapter = FR3HardwareAdapter("r", profile(), connection=connection(),
                                     readiness=readiness(ready=True))
        for result in (adapter.stop(1.0), adapter.cancel(1.0),
                       adapter.confirm_stopped(1.0)):
            with self.subTest(reason=result.reason):
                self.assertIs(result.reason, ReasonCode.HARDWARE_NOT_CONNECTED)
                # 정지를 시켰다고 말하지 않는다. 요청 자체가 수락되지 않았다.
                self.assertFalse(result.request_accepted)
                self.assertFalse(result.task_succeeded)
                self.assertIsNot(result.state.value, "stopped")

    def test_status_does_not_leak_endpoints(self):
        adapter = FR3HardwareAdapter("r", profile(), connection=connection(),
                                     readiness=readiness(ready=False))
        blob = str(adapter.status())
        self.assertNotIn("synthetic://", blob)
        self.assertFalse(adapter.status()["real_hardware_ready"])


class TestGraspObservationAdapterBoundary(unittest.TestCase):
    def observer(self, *, read=None, interpret=None):
        return Robotiq2F85HardwareObservationAdapter(
            robot_id="r", gripper_id="g", connection=connection(),
            read_device_state=read, interpret=interpret)

    def test_requires_ids(self):
        with self.assertRaises(HardwareConfigError):
            Robotiq2F85HardwareObservationAdapter(
                robot_id="", gripper_id="g", connection=connection())

    def test_requires_a_connection(self):
        with self.assertRaises(HardwareConfigError):
            Robotiq2F85HardwareObservationAdapter(
                robot_id="r", gripper_id="g", connection=None)

    def test_connection_itself_refuses_an_empty_gripper_endpoint(self):
        """빈 endpoint로는 접속 설정 자체를 만들 수 없다."""
        with self.assertRaises(HardwareConfigError) as caught:
            HardwareConnection(arm_kind="a", arm_endpoint="b",
                               gripper_kind="c", gripper_endpoint="")
        self.assertIs(caught.exception.reason,
                      ReasonCode.HARDWARE_ADAPTER_UNCONFIGURED)

    def test_without_reader_observation_is_unavailable(self):
        observation = self.observer().observe("obj")
        self.assertEqual(observation.availability.value, "unavailable")
        self.assertIsNone(observation.held)
        self.assertFalse(observation.hardware_complete)

    def test_without_object_id_does_not_claim_a_grasp(self):
        observation = self.observer(
            read=lambda: {"gOBJ": 2}, interpret=lambda raw: True).observe(None)
        self.assertEqual(observation.availability.value, "unavailable")
        self.assertIsNone(observation.held)

    def test_empty_device_state_does_not_claim_a_grasp(self):
        observation = self.observer(
            read=lambda: {}, interpret=lambda raw: True).observe("obj")
        self.assertEqual(observation.availability.value, "unavailable")

    def test_read_failure_becomes_unavailable_not_an_exception(self):
        def boom():
            raise RuntimeError("bus error")

        observation = self.observer(
            read=boom, interpret=lambda raw: True).observe("obj")
        self.assertEqual(observation.availability.value, "unavailable")

    def test_unclear_interpretation_does_not_claim_a_grasp(self):
        observation = self.observer(
            read=lambda: {"gOBJ": 1}, interpret=lambda raw: None).observe("obj")
        self.assertEqual(observation.availability.value, "unavailable")

    def test_device_observation_carries_everything_the_gate_needs(self):
        observation = self.observer(
            read=lambda: {"gOBJ": 2, "gPO": 120},
            interpret=lambda raw: raw["gOBJ"] == 2).observe("obj")
        self.assertIsInstance(observation, GraspObservation)
        self.assertEqual(observation.availability.value, "measured")
        self.assertTrue(observation.held)
        self.assertTrue(observation.hardware_complete)
        self.assertEqual(observation.raw_state["gOBJ"], 2)
        self.assertEqual(observation.robot_id, "r")
        self.assertEqual(observation.gripper_id, "g")

    def test_aperture_path_raises(self):
        with self.assertRaises(GraspObservationError):
            Robotiq2F85HardwareObservationAdapter.from_aperture(0.05)

    def test_simulation_path_raises(self):
        with self.assertRaises(GraspObservationError):
            Robotiq2F85HardwareObservationAdapter.from_simulation()

    def test_status_does_not_leak_endpoints(self):
        blob = str(self.observer().status())
        self.assertNotIn("synthetic://", blob)


class TestNoHardcodedConnectionValues(unittest.TestCase):
    """주소·인증값·엔드포인트·한계값이 **코드에 없다.**"""

    def code_strings(self, path: Path) -> list[str]:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docs.add(doc)
        return [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str) and node.value not in docs]

    def test_no_addresses_or_credentials(self):
        banned = ("http://", "https://", "tcp://", "192.168.", "10.0.",
                  "127.0.0.1", "localhost", "password", "token", "secret",
                  "/dev/tty", "modbus://")
        for path in sorted(HARDWARE_DIR.glob("*.py")):
            for literal in self.code_strings(path):
                for needle in banned:
                    with self.subTest(file=path.name, needle=needle):
                        self.assertNotIn(needle, literal.lower())

    def test_no_numeric_joint_or_force_limits_in_code(self):
        """한계값 상수를 코드에 두지 않는다 — 설정 파일에서만 온다.

        0.0만 허용한다(관측하지 않았음을 뜻하는 구조적 영값). 그 밖의 실수
        상수는 관절·힘·속도 한계가 코드에 새어 든 것이다.
        """
        for path in sorted(HARDWARE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            numbers = [node.value for node in ast.walk(tree)
                       if isinstance(node, ast.Constant)
                       and isinstance(node.value, float)
                       and node.value != 0.0]
            with self.subTest(file=path.name):
                self.assertEqual(numbers, [],
                                 f"{path.name}에 한계값으로 보이는 실수 상수가 있다")

    def test_hardware_modules_do_not_import_simulation_results(self):
        for path in sorted(HARDWARE_DIR.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            with self.subTest(file=path.name):
                self.assertNotIn("validation.simulation_e2e", imported)
                self.assertNotIn("robots.fr3_gazebo.sim_fixture", imported)

    def test_hardware_modules_have_no_simulation_result_references(self):
        for path in sorted(HARDWARE_DIR.glob("*.py")):
            for literal in self.code_strings(path):
                with self.subTest(file=path.name):
                    self.assertNotIn("simulation_e2e", literal)
                    self.assertNotIn("simulation_transfer", literal)


if __name__ == "__main__":
    unittest.main(verbosity=2)
