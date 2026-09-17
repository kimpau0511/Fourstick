"""MoveIt2 설정·기하 검사 기록의 계약 (md/개발플랜.md 8-07).

확인하는 것:

- 장착 근거가 확인되기 전에는 MountingProfile이 custom adapter required이고,
  좌표·질량·TCP가 비어 있다(**identity transform을 쓰지 않는다**)
- 자기충돌 행렬이 자동 생성됐고, 계획 성공을 위해 임의로 제외한 쌍이 없다
- MoveIt 설정 조각에 위치·속도 수치를 중복해 적지 않는다(URDF가 단일 출처)
- 시뮬레이터 검증 기록이 MoveIt 설정·solver·planning scene·충돌 판정을
  함께 남기고, **snapshot 없는 통과는 저장에서 거부된다**
- 시뮬레이션 기록이 real 실행으로 집계되지 않는다
- pick/place는 여전히 지원 기술에서 빠져 있다
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import yaml

from config.loader import load_mounting_profile
from core.constants import TASK_PLAN_SCHEMA_VERSION
from core.reason_codes import ReasonCode
from storage.records import SimVerificationRecord
from storage.sqlite.repository import SqliteRepository

CONFIG = ROOT / "config"
MOVEIT = CONFIG / "moveit"
GENERATED_SRDF = Path("/tmp/forstick2_gazebo/fr3wms_arm.srdf")


def record(**over) -> SimVerificationRecord:
    base = dict(
        verification_id="simv_test_0001", recorded_at=time.time(),
        step="moveit/test", composite_profile_id="composite", 
        composite_profile_version="0.1", arm_profile_id="arm",
        arm_profile_version="0.1", asset_manifest_version="0.1",
        ros_distro="test", gazebo_version="test", controller_versions="test",
        adapter_kind="gazebo_sim", arm_only=True, is_simulated=True,
        target={"a": 1}, observed={"a": 1}, command_accepted=True,
        state="verified", schema_version=TASK_PLAN_SCHEMA_VERSION,
    )
    base.update(over)
    return SimVerificationRecord(**base)


class TestMountingIsMeasuredButYawBlocked(unittest.TestCase):
    """8-08: 측정으로 확정된 값과 **아직 막힌 값**을 구분해 담고 있는지."""

    def setUp(self):
        self.mounting = load_mounting_profile(json.loads(
            (CONFIG / "profiles" / "fr3wms_to_robotiq_2f85_mounting.json")
            .read_text(encoding="utf-8")))

    def test_version_says_measured_and_yaw_is_blocked(self):
        version = self.mounting.mounting_profile_version
        self.assertIn("measured", version)
        # 값이 하나라도 없으면 complete가 아니다 → pick·place가 열리지 않는다.
        self.assertFalse(self.mounting.complete)
        self.assertEqual(self.mounting.missing, ("rpy.yaw",))

    def test_translation_and_tilt_are_measured_not_assumed(self):
        """xyz·roll·pitch는 **측정 근거**를 갖는다. identity 가정이 아니다."""
        values = self.mounting.scalar_values
        for key in ("xyz.x", "xyz.y", "xyz.z", "rpy.roll", "rpy.pitch"):
            with self.subTest(value=key):
                measured = values[key]
                self.assertIsNotNone(measured.value)
                self.assertEqual(measured.provenance.status.value, "verified")
                self.assertTrue(measured.provenance.source)
                # 출처 종류가 **측정 또는 공식 자산**이어야 한다.
                self.assertIn(
                    measured.provenance.source_kind,
                    ("mesh_measurement", "official_urdf_and_mesh", "official_urdf_fk"),
                )
                self.assertTrue(measured.provenance.note)

    def test_yaw_has_no_value_and_names_what_is_needed(self):
        yaw = self.mounting.rpy[2]
        self.assertIsNone(yaw.value)
        self.assertEqual(yaw.provenance.status.value, "unavailable")
        self.assertIn("도면", yaw.provenance.note)

    def test_coupling_thickness_has_two_independent_sources(self):
        coupling = self.mounting.coupling
        self.assertEqual(coupling["model"], "Robotiq GRP-CPL-062")
        self.assertEqual(coupling["thickness_m"], 0.011)
        self.assertEqual(len(coupling["thickness_sources"]), 2)

    def test_coupling_mass_is_marked_implausible(self):
        coupling = self.mounting.coupling
        self.assertEqual(coupling["mass_status"], "declared_implausible")
        self.assertIn("비현실적", coupling["mass_note"])
        # 볼트 패턴·중심 보스는 여전히 확인 불가다.
        self.assertIsNone(coupling["bolt_pattern"])
        self.assertIsNone(coupling["centering_feature"])

    def test_comparison_lists_each_measured_item(self):
        items = self.mounting.comparison["items"]
        self.assertGreaterEqual(len(items), 10)
        text = json.dumps(items, ensure_ascii=False)
        for token in ("PCD", "볼트", "위치 결정 핀", "중심 맞춤", "커플링 두께",
                      "질량", "yaw"):
            with self.subTest(item=token):
                self.assertIn(token, text)

    def test_transform_chain_records_each_step_source(self):
        derivation = self.mounting.transform_derivation
        self.assertEqual(derivation["status"],
                         "translation_verified_rotation_blocked")
        chain = derivation["chain"]
        self.assertEqual(len(chain), 3)
        for step in chain:
            with self.subTest(step=step["step"]):
                self.assertTrue(step["source"])
                self.assertTrue(step["status"])
        # identity를 가정하지 않았다는 사실이 적혀 있다.
        self.assertIn("가정한 것이 아니라", chain[1]["note"])
        self.assertTrue(derivation["blocked_inputs"])

    def test_tcp_comes_from_official_fk_and_pad_measurement(self):
        self.assertEqual(self.mounting.tcp_frame, "robotiq_85_tcp")
        self.assertIsNotNone(self.mounting.tcp_xyz)
        z = self.mounting.tcp_xyz[2]
        self.assertAlmostEqual(z.value, 0.130346, places=6)
        self.assertIn("패드", z.provenance.note)

    def test_gripper_official_values_are_recorded(self):
        official = self.mounting.gripper_official
        self.assertEqual(official["command_joint"],
                         "robotiq_85_left_knuckle_joint")
        self.assertEqual(official["command_range_rad"], [0.0, 0.8])
        self.assertEqual(official["closed_position_rad"], 0.7929)
        self.assertEqual(len(official["mimic_joints"]), 5)
        self.assertAlmostEqual(official["aperture_at_open_m"], 0.084997, places=6)
        # 공식 매뉴얼 값과 모델 값을 함께 둔다(하나로 뭉개지 않는다).
        self.assertEqual(official["official_manual_mass_kg"], 0.925)
        self.assertAlmostEqual(official["total_mass_kg"], 0.920864, places=6)


class TestMoveItConfigFiles(unittest.TestCase):
    def test_group_and_named_state_are_declared_once(self):
        srdf = ET.parse(MOVEIT / "fr3wms_arm.base.srdf").getroot()
        groups = [g.get("name") for g in srdf.findall("group")]
        self.assertEqual(groups, ["fr3wms_arm"])
        chain = srdf.find("group/chain")
        self.assertEqual(chain.get("base_link"), "base_link")
        self.assertEqual(chain.get("tip_link"), "tool_Link")
        states = [s.get("name") for s in srdf.findall("group_state")]
        self.assertEqual(states, ["home"])

    def test_base_srdf_carries_no_collision_exclusions(self):
        """행렬은 자동 생성한다. 손으로 쌍을 제외하지 않는다."""
        srdf = ET.parse(MOVEIT / "fr3wms_arm.base.srdf").getroot()
        self.assertEqual(srdf.findall("disable_collisions"), [])

    def test_joint_limits_do_not_duplicate_urdf_numbers(self):
        policy = yaml.safe_load((MOVEIT / "joint_limits.yaml")
                                .read_text(encoding="utf-8"))
        self.assertEqual(policy["velocity_limits"]["source"], "urdf")
        self.assertEqual(policy["acceleration_limits"]["source"],
                         "derived_for_simulation")
        self.assertFalse(policy["acceleration_limits"]["verified"])
        self.assertFalse(policy["jerk_limits"]["enabled"])
        # 관절 이름별 수치가 이 파일에 없다.
        self.assertNotIn("joint_limits", policy)

    def test_octomap_is_not_enabled_without_sensors(self):
        scene = yaml.safe_load((MOVEIT / "planning_scene.yaml")
                               .read_text(encoding="utf-8"))
        self.assertTrue(scene["publish_planning_scene"])
        self.assertNotIn("octomap_resolution", scene)

    def test_timeout_verification_planner_is_marked_as_verification_only(self):
        text = (MOVEIT / "ompl_planning.yaml").read_text(encoding="utf-8")
        self.assertIn("RRTConnectTimeoutVerification", text)
        self.assertIn("검증 전용", text)


class TestGeneratedSelfCollisionMatrix(unittest.TestCase):
    """생성된 행렬을 검토한다. 없으면 건너뛴다(ROS 환경에서만 생성된다)."""

    def setUp(self):
        if not GENERATED_SRDF.is_file():
            self.skipTest("생성된 SRDF가 없다")
        self.root = ET.parse(GENERATED_SRDF).getroot()

    def test_every_exclusion_names_a_reason(self):
        pairs = self.root.findall("disable_collisions")
        self.assertTrue(pairs)
        for pair in pairs:
            with self.subTest(pair=(pair.get("link1"), pair.get("link2"))):
                self.assertIn(pair.get("reason"), ("Adjacent", "Never"))

    def test_no_pair_is_excluded_because_it_actually_collides(self):
        """'Default'·'Always' 쌍을 제외하지 않았는지 본다.

        `collisions_updater`는 --default/--always를 줄 때만 실제로 충돌하는
        쌍을 비활성화한다. 우리는 그 옵션을 쓰지 않는다.
        """
        reasons = {p.get("reason") for p in self.root.findall("disable_collisions")}
        self.assertNotIn("Default", reasons)
        self.assertNotIn("Always", reasons)

    def test_every_never_exclusion_is_backed_by_the_independent_review(self):
        """'Never' 제외는 독립 검토가 멀다고 확인한 쌍만 남아 있어야 한다.

        FR3-WMS에서 collisions_updater는 15쌍을 'Never'로 비활성화했지만,
        독립 검토는 그중 12쌍이 0.2 mm 안까지 접근함을 찾았다. 그 쌍은
        검사 대상으로 되살려야 한다.
        """
        report = ROOT / "reports/moveit/self_collision_review.json"
        if not report.is_file():
            self.skipTest("검토 보고서가 없다")
        review = json.loads(report.read_text(encoding="utf-8"))
        threshold = review["method"]["proximity_threshold_m"]
        distances = {frozenset(e["pair"]): e["min_distance_m"]
                     for e in review["closest_per_pair"]}
        for element in self.root.findall("disable_collisions"):
            pair = frozenset((element.get("link1"), element.get("link2")))
            if element.get("reason") == "Adjacent":
                continue
            with self.subTest(pair=sorted(pair)):
                self.assertIn(pair, distances)
                self.assertGreater(distances[pair], threshold)

    def test_review_report_is_kept_next_to_the_matrix(self):
        report = ROOT / "reports/moveit/self_collision_review.json"
        if not report.is_file():
            self.skipTest("검토 보고서가 아직 없다")
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertIn("conclusion", data)
        self.assertIn("pairs_within_threshold", data)
        # 표본 방법의 한계가 보고서에 적혀 있어야 한다.
        self.assertIn("가능성이 남는다", data["method"]["note"])


class TestSimVerificationRecordsMoveIt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = SqliteRepository(
            str(Path(self.tmp.name) / "db.sqlite3"), now=time.time())
        self.addCleanup(self.repo.close)

    def test_moveit_fields_survive_a_round_trip(self):
        self.repo.append_sim_verification(record(
            moveit_config_version="config/moveit@abc123",
            kinematics_solver="kdl_kinematics_plugin/KDLKinematicsPlugin",
            kinematics_solver_version="moveit_kinematics 2.15.0",
            planning_scene_snapshot_id="moveit:scene",
            planning_scene_snapshot_version="h0123456789ab",
            planning_scene_snapshot_hash="0123456789ab" * 4,
            planning_scene_checked_at=1789529000.5,
            collision_decision="allow",
            geometry_validator_id="moveit-planning-scene",
            geometry_validator_version="moveit 2.15.0",
            commanded_joints={"j1": 0.1}, observed_joints={"j1": 0.1002},
            target_pose={"xyz": [0.1, 0.2, 0.3]},
            observed_pose={"xyz": [0.1, 0.2, 0.31]},
            sim_fixture_version="moveit-verify-fixtures-1.0",
        ))
        stored = self.repo.sim_verifications(limit=1)[0]
        self.assertEqual(stored.moveit_config_version, "config/moveit@abc123")
        self.assertEqual(stored.kinematics_solver_version,
                         "moveit_kinematics 2.15.0")
        self.assertEqual(stored.planning_scene_snapshot_version, "h0123456789ab")
        self.assertEqual(stored.collision_decision, "allow")
        self.assertEqual(stored.commanded_joints, {"j1": 0.1})
        self.assertEqual(stored.observed_joints, {"j1": 0.1002})
        self.assertEqual(stored.sim_fixture_version,
                         "moveit-verify-fixtures-1.0")
        # 시뮬레이션 기록임이 남아 있어야 한다.
        self.assertTrue(stored.is_simulated)
        self.assertTrue(stored.arm_only)

    def test_block_decision_keeps_its_reason_code(self):
        self.repo.append_sim_verification(record(
            verification_id="simv_block",
            collision_decision="block",
            collision_reason_code=ReasonCode.GEOMETRY_COLLISION,
            planning_scene_snapshot_id="moveit:scene",
            planning_scene_snapshot_hash="f" * 64,
            planning_scene_checked_at=1789529001.0,
        ))
        stored = self.repo.sim_verifications(limit=1)[0]
        self.assertIs(stored.collision_reason_code, ReasonCode.GEOMETRY_COLLISION)

    def test_allow_without_a_scene_is_refused_by_the_contract(self):
        with self.assertRaises(ValueError):
            record(collision_decision="allow")

    def test_unknown_decision_is_refused(self):
        with self.assertRaises(ValueError):
            record(collision_decision="probably_fine")

    def test_storage_also_refuses_an_allow_without_a_scene(self):
        """계약을 우회해도 저장이 막는다(트리거)."""
        row = record(verification_id="simv_direct")
        columns = SqliteRepository._SIM_COLUMNS
        values = list(SqliteRepository._sim_values(row))
        values[columns.index("collision_decision")] = "allow"
        with self.assertRaises(Exception):
            with self.repo._tx() as conn:
                conn.execute(
                    f"INSERT INTO sim_verification_runs ({','.join(columns)})"
                    f" VALUES ({','.join('?' * len(columns))})", values)

    def test_simulation_rows_are_not_counted_as_real_executions(self):
        self.repo.append_sim_verification(record(verification_id="simv_sim"))
        self.assertEqual(len(self.repo.sim_verifications(limit=10)), 1)
        # 실행 표에는 아무것도 생기지 않는다 — 시뮬레이션 검증이 real 실행으로
        # 집계되지 않는다.
        with self.repo._tx() as conn:
            count = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        self.assertEqual(count, 0)


class TestBaselineIsPreserved(unittest.TestCase):
    """8-03~8-06 기준선 파일이 바이트 단위로 그대로인지 확인한다.

    MoveIt을 얹으면서 world·xacro·controller 설정을 덮어쓰지 않았다는 사실을
    테스트로 고정한다.
    """

    PRESERVED = (
        "config/gazebo/fr3_cell.sdf",
        "config/gazebo/fr3wms_arm.urdf.xacro",
        "config/gazebo/fr3wms_controllers.yaml",
        "scripts/run_gazebo_fr3.sh",
    )

    def setUp(self):
        path = ROOT / "reports/baseline/fr3_arm_only_2026-09-16.1.json"
        if not path.is_file():
            self.skipTest("기준선 기록이 없다")
        self.baseline = json.loads(path.read_text(encoding="utf-8"))

    def test_preserved_files_match_the_recorded_checksums(self):
        import hashlib

        for name in self.PRESERVED:
            with self.subTest(file=name):
                recorded = self.baseline["files"][name]
                raw = (ROOT / name).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(),
                                 recorded["sha256"])
                self.assertEqual(len(raw), recorded["bytes"])

    def test_the_new_baseline_records_the_moveit_state(self):
        path = ROOT / "reports/baseline/fr3_arm_only_moveit_2026-09-16.2.json"
        if not path.is_file():
            self.skipTest("MoveIt 기준선 기록이 없다")
        current = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(current["supersedes"], self.baseline["baseline_id"])
        status = current["status"]
        self.assertIn("완료", status["8-07 MoveIt2·충돌 검사"])
        for blocked in ("8-08 2F-85 결합·open/close", "8-09 pick·보유 확인",
                        "8-10 transport·place"):
            self.assertIn("미완료", status[blocked])


class TestPruningRule(unittest.TestCase):
    """제외 가지치기 규칙을 ROS 없이 검증한다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.srdf = self.dir / "robot.srdf"
        self.srdf.write_text(
            '<?xml version="1.0"?><robot name="r">'
            '<disable_collisions link1="a" link2="b" reason="Adjacent"/>'
            '<disable_collisions link1="a" link2="c" reason="Never"/>'
            '<disable_collisions link1="b" link2="d" reason="Never"/>'
            '<disable_collisions link1="c" link2="d" reason="Never"/>'
            "</robot>", encoding="utf-8")

    def run_prune(self, review: dict | None) -> dict:
        import subprocess

        args = [sys.executable, str(ROOT / "scripts/prune_self_collision_srdf.py"),
                str(self.srdf)]
        if review is not None:
            path = self.dir / "review.json"
            path.write_text(json.dumps(review), encoding="utf-8")
            args.append(str(path))
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def remaining(self) -> set[frozenset[str]]:
        root = ET.parse(self.srdf).getroot()
        return {frozenset((e.get("link1"), e.get("link2")))
                for e in root.findall("disable_collisions")}

    def test_close_pairs_are_restored_to_checking(self):
        report = self.run_prune({"closest_per_pair": [
            {"pair": ["a", "c"], "min_distance_m": 0.0002},   # 근접 → 되살린다
            {"pair": ["b", "d"], "min_distance_m": 0.4},       # 멀다 → 남긴다
            {"pair": ["c", "d"], "min_distance_m": 0.009},     # 임계값 이내
        ]})
        self.assertEqual(self.remaining(),
                         {frozenset({"a", "b"}), frozenset({"b", "d"})})
        restored = {tuple(r["pair"]) for r in report["restored_to_checked"]}
        self.assertEqual(restored, {("a", "c"), ("c", "d")})

    def test_adjacent_pairs_always_stay_disabled(self):
        self.run_prune({"closest_per_pair": [
            {"pair": ["a", "b"], "min_distance_m": 0.0},
        ]})
        self.assertIn(frozenset({"a", "b"}), self.remaining())

    def test_without_a_review_every_never_pair_is_checked_again(self):
        """근거가 없으면 검사한다 — 모르는 상태를 안전으로 바꾸지 않는다."""
        self.run_prune(None)
        self.assertEqual(self.remaining(), {frozenset({"a", "b"})})


if __name__ == "__main__":
    unittest.main()
