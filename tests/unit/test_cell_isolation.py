"""작업 셀 설정과 프로세스 환경(Gazebo 파티션·ROS 도메인)이 다른 셀을 가리키면 시작하지 않는다.

2026-10-07 실측: 복제 셀에서 팔은 환경의 ROS 도메인으로 복제 셀을 움직였는데, 고정 장치와 위치
관측은 설정의 파티션으로 라이브 셀에 붙었다(상자가 따라오지 않은 것처럼 보였다).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from robots.fr3_gazebo.adapter import WorkcellConfigError, check_cell_isolation  # noqa: E402

CELL = json.loads((ROOT / "config/workcell/fr3_2f85_workcell.json").read_text(encoding="utf-8"))


class CellIsolationTest(unittest.TestCase):
    def test_matching_or_absent_environment_passes(self):
        check_cell_isolation(CELL, {})
        check_cell_isolation(CELL, {"GZ_PARTITION": CELL["gz_partition"],
                                    "ROS_DOMAIN_ID": str(CELL["ros_domain_id"])})
        check_cell_isolation(CELL, {"GZ_PARTITION": "", "ROS_DOMAIN_ID": ""})

    def test_other_partition_or_domain_refuses(self):
        for env in ({"GZ_PARTITION": "f2clone"}, {"ROS_DOMAIN_ID": "61"},
                    {"GZ_PARTITION": "f2clone", "ROS_DOMAIN_ID": str(CELL["ros_domain_id"])}):
            with self.subTest(env=env), self.assertRaises(WorkcellConfigError) as caught:
                check_cell_isolation(CELL, env)
            self.assertIn("시작하지 않는다", str(caught.exception))

    def test_clone_config_with_its_own_partition_passes(self):
        clone = {**CELL, "gz_partition": "f2clone", "ros_domain_id": 61}
        check_cell_isolation(clone, {"GZ_PARTITION": "f2clone", "ROS_DOMAIN_ID": "61"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
