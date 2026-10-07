"""관절 상태 패널의 허용 범위(/v1/sim-view/model joint_limits) — 현재 Profile 한계 그대로, 없으면 없다고(2026-10-07)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.capability_profile import JointLimit  # noqa: E402
from server.routes.sim_view import _joint_limits  # noqa: E402


class JointLimitsTest(unittest.TestCase):
    def test_profile_limits_are_passed_through_in_rad(self):
        profile = SimpleNamespace(profile_id="fr3wms", profile_version="1.2", joint_limits=(
            JointLimit("j1", -3.0543, 3.0543, 3.14, "rad", "revolute"),
            JointLimit("j2", -4.6251, 1.4835, 3.14, "rad", "revolute")))
        out = _joint_limits(SimpleNamespace(profile=profile))
        self.assertTrue(out["available"])
        self.assertEqual(out["source"], "capability profile fr3wms 1.2")
        self.assertEqual(out["joints"]["j2"], {"lower": -4.6251, "upper": 1.4835, "unit": "rad", "kind": "revolute"})

    def test_no_profile_means_no_limits(self):
        out = _joint_limits(SimpleNamespace(profile=None))
        self.assertEqual((out["available"], out["joints"]), (False, {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
