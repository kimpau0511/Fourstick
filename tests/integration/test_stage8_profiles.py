"""8단계 Profile의 API·DB·자산 연결 (md/개발플랜.md 8-01·8-02).

확인하는 것:

- 선언된 구성(팔 + 그리퍼)이 Registry·API·UI에 보인다
- 실제 연결 전에는 `simulation` · `unverified`로 표시되고 실행 대상이 아니다
- 미확보 항목이 그대로 노출된다(무엇이 막혔는지 숨기지 않는다)
- Profile 기록이 append-only로 남고, 버전이 바뀌면 기존 승인이 무효가 된다
- 자산이 없거나 라이선스가 확인되지 않으면 **이유를 분명히 반환한다**
- 공통 코드에 제조사 이름 분기가 없다
"""

from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.loader import (
    load_arm_profile,
    load_asset_manifest,
    load_composite_profile,
    load_gripper_profile,
    load_mounting_profile,
)
from core.asset_manifest import LicenseStatus, Redistribution
from core.reason_codes import ReasonCode
from integration.test_session_isolation import IsolationCase

CONFIG = ROOT / "config"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestDeclaredRobotIsVisible(IsolationCase):
    async def test_robots_endpoint_lists_the_declared_composition(self):
        payload = (await self.client.get("/v1/robots")).json()
        declared = payload["declared"]
        self.assertTrue(declared)
        robot = declared[0]
        self.assertIn("+", robot["display_name"])          # 팔 + 그리퍼
        self.assertEqual(robot["environment"], "simulation")
        self.assertFalse(robot["verified"])
        self.assertFalse(robot["executable"])
        self.assertFalse(robot["registered"])              # 실행 대상이 아니다

    async def test_declared_composition_names_its_three_parts(self):
        robot = (await self.client.get("/v1/robots")).json()["declared"][0]
        self.assertTrue(robot["arm"]["arm_profile_id"])
        self.assertTrue(robot["gripper"]["gripper_profile_id"])
        self.assertTrue(robot["mounting"]["mounting_profile_id"])
        self.assertTrue(robot["asset_manifest_version"])

    async def test_pick_and_place_are_excluded_until_mounting_is_verified(self):
        robot = (await self.client.get("/v1/robots")).json()["declared"][0]
        self.assertNotIn("pick", robot["supported_skills"])
        self.assertNotIn("place", robot["supported_skills"])
        self.assertEqual(set(robot["excluded_skills"]), {"pick", "place"})

    async def test_blocking_items_are_reported_not_hidden(self):
        robot = (await self.client.get("/v1/robots")).json()["declared"][0]
        blocking = robot["blocking_items"]
        self.assertTrue(blocking)
        self.assertTrue(any(item.startswith("arm.") for item in blocking))
        self.assertTrue(any(item.startswith("mounting.") for item in blocking))

    async def test_config_payload_summarises_declared_robots_and_assets(self):
        payload = (await self.client.get("/v1/config")).json()
        self.assertTrue(payload["declared_robots"])
        self.assertTrue(payload["assets"]["manifest_version"])
        self.assertTrue(payload["assets"]["unverified_licenses"])

    async def test_ui_renders_the_declared_block(self):
        """화면이 선언된 구성과 막힌 항목을 보여준다(목업 기준 재구현 화면)."""
        index = (await self.client.get("/")).text
        self.assertIn('id="card-robot"', index)
        catalog = (
            ROOT / "html" / "static" / "js" / "catalog.js"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "FAIRINO FR3-WMS + 2F-85 · Gazebo workcell simulation", catalog)
        self.assertIn('id="card-workcell"', index)
        # 비활성 스킬과 사유가 화면 데이터에 있다.
        self.assertIn("'pick', 'place'", catalog)
        self.assertIn("파지 관측", catalog)
        self.assertIn("실하드웨어 미검증", catalog)
        # 화면 코드가 확인되지 않은 모델 수치를 갖지 않는다.
        self.assertNotIn("0.7929", catalog)
        self.assertNotIn("0.920864", catalog)

    async def test_fake_dev_robot_is_still_the_only_registered_one(self):
        payload = (await self.client.get("/v1/robots")).json()
        self.assertEqual(list(payload["robots"]), ["fake_dev"])
        self.assertFalse(payload["configured"])


class TestProfileRecordsInDb(IsolationCase):
    async def test_profile_version_is_recorded_append_only(self):
        rows = self.runtime.repository.robot_profiles()
        self.assertTrue(rows)
        record = rows[0]
        self.assertEqual(record.environment, "simulation")
        self.assertFalse(record.verified)
        self.assertTrue(record.unverified_items)
        self.assertTrue(record.asset_manifest_version)
        self.assertTrue(record.arm_profile_id and record.arm_profile_version)
        self.assertTrue(record.gripper_profile_id and record.gripper_profile_version)
        self.assertTrue(record.mounting_profile_id)
        # 자산 출처 commit·checksum이 함께 남는다.
        self.assertTrue(record.source_commit)
        self.assertTrue(record.source_checksum)

    async def test_recording_the_same_version_twice_is_idempotent(self):
        repo = self.runtime.repository
        before = len(repo.robot_profiles())
        record = repo.robot_profiles()[0]
        repo.record_robot_profile(record)
        self.assertEqual(len(repo.robot_profiles()), before)

    async def test_changing_content_under_the_same_version_is_refused(self):
        import dataclasses

        from storage.repository import IntegrityViolation

        repo = self.runtime.repository
        record = repo.robot_profiles()[0]
        with self.assertRaises(IntegrityViolation):
            repo.record_robot_profile(
                dataclasses.replace(record, display_name="다른 이름")
            )

    async def test_supported_skills_are_stored_with_the_version(self):
        record = self.runtime.repository.robot_profiles()[0]
        self.assertNotIn("pick", record.supported_skills)
        self.assertIn("home", record.supported_skills)


class TestProfileVersionInvalidatesApproval(IsolationCase):
    """Profile이 바뀌면 기존 승인을 재사용하지 않는다."""

    async def test_composite_version_change_requires_reapproval(self):
        import dataclasses

        bundle = await self.approved_plan(self.a)
        # 실행에 쓰이는 Profile의 버전이 올라간다(구성 교체와 같은 효과).
        self.runtime.profile = dataclasses.replace(
            self.runtime.profile, profile_version="0.2.0-unverified"
        )
        response = await self.execute(self.a, bundle)
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.ROBOT_PROFILE_MISMATCH.value
        )

    async def test_old_records_are_not_rewritten_by_a_new_version(self):
        import dataclasses

        bundle = await self.approved_plan(self.a)
        result = (await self.execute(self.a, bundle)).json()
        repo = self.runtime.repository
        before = repo.get_execution(result["execution_id"])
        self.runtime.profile = dataclasses.replace(
            self.runtime.profile, profile_version="0.2.0-unverified"
        )
        after = repo.get_execution(result["execution_id"])
        # 실행 기록의 Profile 버전은 그대로다.
        self.assertEqual(after.profile_version, before.profile_version)
        approval = repo.get_approval(after.approval_id)
        self.assertEqual(approval.profile_version, before.profile_version)


class TestAssetManifestBehaviour(unittest.TestCase):
    def setUp(self):
        self.manifest = load_asset_manifest(
            load_json(CONFIG / "assets" / "third_party_assets.json")
        )

    def test_unverified_license_asset_is_refused_with_its_reason(self):
        status = self.manifest.resolve("fairino.fr3wms.urdf")
        self.assertFalse(status.available)
        self.assertIs(status.reason, ReasonCode.ASSET_LICENSE_UNVERIFIED)
        self.assertIn("복사·배포하지 않고", status.detail)

    def test_unknown_asset_is_refused_with_its_reason(self):
        status = self.manifest.resolve("없는자산")
        self.assertFalse(status.available)
        self.assertIs(status.reason, ReasonCode.ASSET_MISSING)

    def test_licensed_asset_without_local_copy_reports_missing(self):
        status = self.manifest.resolve("robotiq.2f85.macro.xacro")
        self.assertFalse(status.available)
        self.assertIs(status.reason, ReasonCode.ASSET_MISSING)
        self.assertIn("사용자가 제공", status.detail)

    def test_unverified_assets_are_never_copyable(self):
        for asset_id in self.manifest.unverified_licenses():
            entry = self.manifest.get(asset_id)
            with self.subTest(asset=asset_id):
                self.assertFalse(entry.may_copy_into_repo)
                self.assertIs(entry.redistribution, Redistribution.FORBIDDEN)

    def test_pinned_commit_is_recorded_for_the_arm_source(self):
        entry = self.manifest.get("fairino.frcobot_ros2.repo")
        self.assertEqual(len(entry.commit), 40)
        self.assertIs(entry.license_status, LicenseStatus.UNVERIFIED)

    def test_gripper_source_is_pinned_to_a_tag_with_a_license(self):
        entry = self.manifest.get("robotiq.2f85.macro.xacro")
        self.assertEqual(entry.version, "v1.1.0")
        self.assertEqual(entry.license_spdx, "BSD-3-Clause")
        self.assertIs(entry.license_status, LicenseStatus.CONFIRMED)
        self.assertTrue(entry.checksum.startswith("sha256:"))

    def test_environment_record_is_present(self):
        entry = self.manifest.get("env.ros2_gazebo_stack")
        self.assertIsNotNone(entry)
        # Gazebo Classic과 최신 Gazebo를 혼용하지 않는다는 사실이 기록돼 있다.
        self.assertIn("gz-sim", entry.version)
        self.assertIn("Classic", entry.note)

    def test_no_unlicensed_asset_is_copied_into_the_repository(self):
        """라이선스 미확인 자산이 저장소에 들어와 있지 않다."""
        banned = ("FR3WMS.urdf", "API_Instruction.pdf", "ErrorCode_lnstruction.pdf")
        for name in banned:
            with self.subTest(name=name):
                self.assertEqual(list(ROOT.rglob(name)), [])


class TestShippedProfilesStayHonest(unittest.TestCase):
    """실제 구성 파일이 '미확보 상태 그대로'인지 확인한다."""

    def setUp(self):
        self.arm = load_arm_profile(load_json(CONFIG / "profiles" / "fr3wms_arm.json"))
        self.gripper = load_gripper_profile(
            load_json(CONFIG / "profiles" / "robotiq_2f85_gripper.json")
        )
        self.mounting = load_mounting_profile(
            load_json(CONFIG / "profiles" / "fr3wms_to_robotiq_2f85_mounting.json")
        )
        self.composite = load_composite_profile(
            load_json(CONFIG / "profiles" / "composite_fr3wms_2f85.json"),
            arms={self.arm.arm_profile_id: self.arm},
            grippers={self.gripper.gripper_profile_id: self.gripper},
            mountings={self.mounting.mounting_profile_id: self.mounting},
        )

    def test_arm_limits_come_from_the_official_urdf(self):
        """공식 URDF에서 읽은 값은 있고, 출처(commit)가 붙어 있다."""
        self.assertEqual(self.arm.dof, 6)
        for joint in self.arm.joints:
            with self.subTest(joint=joint.name):
                self.assertIsNotNone(joint.lower.value)
                self.assertIsNotNone(joint.upper.value)
                self.assertIsNotNone(joint.max_velocity.value)
                self.assertEqual(len(joint.lower.provenance.source_commit), 40)
                self.assertIn("FR3WMS.urdf", joint.lower.provenance.source)
                # 가속도 한계는 URDF에 없다 — 만들어 넣지 않는다.
                self.assertIsNone(joint.max_acceleration.value)

    def test_arm_payload_stays_unavailable(self):
        """정격 페이로드는 데이터시트 값이다. 없으면 pick·place가 제외된다."""
        self.assertIsNone(self.arm.payload.value)
        self.assertEqual(self.arm.lift_missing, ("payload",))
        self.assertEqual(self.arm.payload.unit, "kg")

    def test_arm_reach_has_an_explicit_definition_and_stays_unverified(self):
        """작업 반경은 **정의를 명시**하고, 공식 표기와의 차이를 남긴다."""
        from core.provenance import VerificationStatus

        note = self.arm.reach.provenance.note
        self.assertIsNotNone(self.arm.reach.value)
        self.assertIs(self.arm.reach.status, VerificationStatus.UNVERIFIED)
        self.assertIn("수평 반경", note)          # 정의
        self.assertIn("0.622", note)              # 공식 표기와 대조
        self.assertIn("mm", note)                 # 남은 차이를 적었다
        # 단순 링크 합을 그대로 쓰지 않는다.
        self.assertNotAlmostEqual(self.arm.reach.value, 0.622, places=3)

    def test_arm_motion_values_present_but_profile_not_verified(self):
        self.assertTrue(self.arm.complete)          # 이동 값은 채워졌다
        self.assertFalse(self.arm.verified)         # 실기·시뮬레이터 검증 전이다
        self.assertIn("urdf_license", self.arm.unverified_items)

    def test_arm_keeps_units_for_future_values(self):
        units = {item.unit for item in self.arm.scalar_values.values()}
        self.assertEqual(units, {"kg", "m", "s"})

    def test_gripper_values_come_from_the_official_repository(self):
        for name in ("open_position", "closed_position", "joint_lower",
                     "joint_upper", "max_velocity", "max_effort"):
            item = getattr(self.gripper, name)
            with self.subTest(name=name):
                self.assertIsNotNone(item.value)
                self.assertIn("robotiq/ros", item.provenance.source)
                self.assertEqual(item.provenance.source_version, "v1.1.0")
                self.assertEqual(len(item.provenance.source_commit), 40)

    def test_gripper_aperture_uses_the_official_spec(self):
        """개방폭은 **공식 매뉴얼 값**(0.085 m)이고, 모델 값과 구분해 쓴다."""
        self.assertEqual(self.gripper.pad_aperture_open.value, 0.085)
        self.assertIn("공식 매뉴얼", self.gripper.pad_aperture_open.provenance.note)
        # 완전 닫힘 패드 간격(0 mm)은 계약이 0을 허용하지 않아 별 항목으로 둔다.
        self.assertIsNone(self.gripper.pad_aperture_closed.value)
        self.assertFalse(self.gripper.complete)

    def test_model_tolerance_separates_spec_from_simulation(self):
        """물리 명세·시뮬레이션 관측값·허용 오차를 각각 기록한다."""
        tolerance = self.gripper.model_tolerance["aperture_m"]
        self.assertEqual(tolerance["physical_spec"], 0.085)
        self.assertEqual(tolerance["simulation_model_max"], 0.084837)
        self.assertAlmostEqual(tolerance["difference_m"], 0.000163, places=6)
        usage = tolerance["usage"]
        self.assertIn("0.085", usage["api_contract"])
        self.assertIn("FK", usage["collision_and_observation"])
        self.assertGreater(usage["tolerance_m"], tolerance["difference_m"])
        self.assertTrue(usage["tolerance_basis"])
        # 질량도 같은 방식으로 명세와 모델을 구분한다.
        self.assertEqual(self.gripper.model_tolerance["mass_kg"]["physical_spec"], 0.925)

    def test_gripper_aperture_travel_comes_from_urdf_kinematics(self):
        """행정은 URDF 기구학으로 계산했고, 선형 가정이 아니다."""
        from core.provenance import VerificationStatus

        travel = self.gripper.aperture_travel
        self.assertIsNotNone(travel.value)
        self.assertEqual(travel.unit, "m")
        self.assertIs(travel.status, VerificationStatus.UNVERIFIED)
        self.assertIn("기구학", travel.provenance.note)
        open_sep = self.gripper.tip_separation_open.value
        closed_sep = self.gripper.tip_separation_closed.value
        self.assertAlmostEqual(travel.value, open_sep - closed_sep, places=5)
        # 중간각이 선형 보간과 다르다는 사실이 기록돼 있다.
        self.assertIn("선형", self.gripper.tip_separation_closed.provenance.note)

    def test_gripper_is_selected_not_candidate(self):
        self.assertEqual(self.gripper.selection_status, "selected")

    def test_gripper_force_and_effort_are_separate_quantities(self):
        self.assertEqual(self.gripper.max_effort.unit, "N*m")
        self.assertEqual(self.gripper.max_grasp_force.unit, "N")
        self.assertNotEqual(
            self.gripper.max_effort.value, self.gripper.max_grasp_force.value
        )
        self.assertIn("파지력이 아니다", self.gripper.max_effort.provenance.note)

    def test_gripper_bus_semantics_are_recorded(self):
        for register in ("rACT", "rPR", "rSP", "rFR", "gSTA", "gOBJ", "gFLT"):
            with self.subTest(register=register):
                self.assertIn(register, self.gripper.bus_semantics)
        self.assertIn("Modbus RTU", self.gripper.bus_defaults["protocol"])
        self.assertTrue(self.gripper.object_detection_states)

    def test_gripper_effort_is_torque_not_grasp_force(self):
        self.assertEqual(self.gripper.max_effort.unit, "N*m")
        self.assertIn("파지력", self.gripper.max_effort.provenance.note)

    def test_gripper_object_detection_differs_between_real_and_simulation(self):
        self.assertTrue(self.gripper.object_detection_real)
        self.assertFalse(self.gripper.object_detection_simulation)

    def test_gripper_declares_activation_interfaces(self):
        self.assertIn("reactivate_gripper_cmd", self.gripper.activation_interfaces)

    def test_mounting_values_have_sources_and_yaw_stays_blocked(self):
        """8-08에서 측정으로 값이 생겼다. **근거 없는 값은 여전히 없다.**

        (이전에는 모든 값이 비어 있었다. 지금은 공식 자산 측정으로 xyz·roll·
        pitch·TCP가 채워졌고, yaw만 근거가 없다 — 그래서 여전히 미완료다.)
        """
        for name, item in self.mounting.scalar_values.items():
            with self.subTest(name=name):
                if item.value is None:
                    # 값이 없으면 상태가 unavailable이어야 한다.
                    self.assertEqual(item.provenance.status.value, "unavailable")
                else:
                    # 값이 있으면 출처와 확인 상태가 있어야 한다.
                    self.assertTrue(item.provenance.source)
                    self.assertEqual(item.provenance.status.value, "verified")
        self.assertFalse(self.mounting.complete)
        self.assertEqual(self.mounting.missing, ("rpy.yaw",))
        self.assertEqual(self.mounting.arm_flange_frame, "tool_Link")

    def test_composite_is_simulation_and_unverified(self):
        self.assertEqual(self.composite.environment.value, "simulation")
        self.assertFalse(self.composite.verified)
        self.assertFalse(self.composite.complete)
        self.assertNotIn("pick", self.composite.supported_skills)

    def test_composite_can_move_but_not_lift(self):
        """이동은 가능하고, 장착·페이로드가 없어 pick·place는 제외된다."""
        self.assertTrue(self.composite.motion_ready)
        profile = self.composite.capability_profile()
        self.assertEqual(set(profile.supported_skills), {"home", "move", "stop"})
        self.assertIsNone(profile.payload_kg)
        self.assertIsNone(profile.gripper)
        self.assertFalse(profile.can_lift)

    def test_composite_blocking_items_name_the_gaps(self):
        blocking = self.composite.blocking_items
        self.assertIn("arm.payload", blocking)
        self.assertTrue(any(item.startswith("mounting.") for item in blocking))
        self.assertTrue(any(item.startswith("gripper.") for item in blocking))


class TestNoVendorBranchingInCommonCode(unittest.TestCase):
    """공통 코드가 모델 이름으로 분기하지 않는다(AST 기준)."""

    COMMON = ("core", "validation", "robots/base", "server", "server/routes",
              "planning", "storage")
    BANNED = ("fairino", "fr3", "ur5e", "robotiq", "panda", "doosan", "kinova")

    def test_no_vendor_names_in_code(self):
        for directory in self.COMMON:
            for path in sorted((ROOT / directory).glob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                docs = set()
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                         ast.AsyncFunctionDef)):
                        doc = ast.get_docstring(node, clean=False)
                        if doc:
                            docs.add(doc)
                tokens = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name):
                        tokens.append(node.id)
                    elif isinstance(node, ast.Attribute):
                        tokens.append(node.attr)
                    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                        if node.value not in docs:
                            tokens.append(node.value)
                blob = " ".join(tokens).lower()
                for banned in self.BANNED:
                    with self.subTest(file=f"{directory}/{path.name}", banned=banned):
                        self.assertNotIn(banned, blob)

    def test_model_names_live_only_in_configuration(self):
        arm = load_json(CONFIG / "profiles" / "fr3wms_arm.json")
        self.assertIn("FR3", arm["display_name"])
        gripper = load_json(CONFIG / "profiles" / "robotiq_2f85_gripper.json")
        self.assertIn("Robotiq", gripper["display_name"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
