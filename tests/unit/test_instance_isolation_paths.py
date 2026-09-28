"""같은 PC의 독립 인스턴스가 기존 셀을 건드리지 않게: 파티션·도메인·상태 파일 경로가 환경변수를 따른다."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from robots.fr3_gazebo.paths import gz_partition, ros_domain_id  # noqa: E402

WORKCELL = json.loads((ROOT / "config/workcell/fr3_2f85_workcell.json").read_text(encoding="utf-8"))


class PartitionDomainTest(unittest.TestCase):
    def test_config_values_when_env_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GZ_PARTITION", None)
            os.environ.pop("ROS_DOMAIN_ID", None)
            self.assertEqual(gz_partition(WORKCELL), WORKCELL["gz_partition"])
            self.assertEqual(ros_domain_id(WORKCELL), int(WORKCELL["ros_domain_id"]))

    def test_env_wins_over_config(self):
        with mock.patch.dict(os.environ, {"GZ_PARTITION": "iso_b", "ROS_DOMAIN_ID": "54"}):
            self.assertEqual(gz_partition(WORKCELL), "iso_b")
            self.assertEqual(ros_domain_id(WORKCELL), 54)

    def test_adapter_resources_follow_env(self):
        from robots.fr3_gazebo.adapter import load_workcell_resources
        poses = ROOT / "config/workcell/fr3_2f85_workcell_poses.json"
        with mock.patch.dict(os.environ, {"GZ_PARTITION": "iso_b", "ROS_DOMAIN_ID": "54"}):
            res = load_workcell_resources(ROOT / "config/workcell/fr3_2f85_workcell.json", poses,
                                          state_max_age_sec=1.0)
        self.assertEqual((res.gz_partition, res.ros_domain_id), ("iso_b", 54))


class StateFilesFollowLogDirTest(unittest.TestCase):
    def test_stop_request_and_state_paths(self):
        code = ("import sys; sys.path.insert(0, %r)\n"
                "from server.sim_demo_jobs import STOP_REQUEST\n"
                "from validation.simulation_demo_state import DEFAULT_PATH\n"
                "from scripts.demo_workcell_pick_place import LOG_DIR, URDF\n"
                "print(STOP_REQUEST, DEFAULT_PATH, LOG_DIR, URDF)") % str(ROOT)
        env = dict(os.environ, FORSTICK2_WORKCELL_LOG_DIR="/tmp/iso_b_wc")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                             cwd=ROOT, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-800:])
        for part in out.stdout.split():
            self.assertTrue(part.startswith("/tmp/iso_b_wc"), part)


if __name__ == "__main__":
    unittest.main()
