"""파지 상태 관측 계약 `grasp.object_held` (md/개발플랜.md 8-10).

확인하는 것:

- 관측 수단이 없으면 `held`는 None이고, 값·시각·물체를 붙일 수 없다
- 관측했다면 **어느 물체를 쥐었는지**가 따라온다 — 물체 없는 값은 거부한다
  (개구 측정에는 물체 식별자가 없으므로 이 형식을 만들 수 없다)
- 개구(aperture)를 방법으로 적은 관측은 거부한다
- 시뮬레이터 관측은 `simulated_observation`으로 구분되고 **관문 조건을
  충족시키지 않는다**
- 실기 관측만 관문 조건을 충족시킨다
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.grasp_observation import (
    OBSERVATION_KEY,
    GraspAvailability,
    GraspObservation,
    GraspObservationError,
    GraspObserver,
    measured,
    simulated,
    unavailable,
)


class TestUnavailable(unittest.TestCase):
    def test_no_means_no_value(self):
        item = unavailable(detail="수단이 없다", source="sim:cell")
        self.assertIsNone(item.held)
        self.assertIsNone(item.object_id)
        self.assertIsNone(item.observed_at)
        self.assertFalse(item.available)
        self.assertFalse(item.counts_for_gate)
        self.assertEqual(item.to_dict()["key"], OBSERVATION_KEY)

    def test_cannot_claim_a_grasp_without_observation(self):
        with self.assertRaises(GraspObservationError):
            GraspObservation(availability=GraspAvailability.UNAVAILABLE, held=True)

    def test_cannot_timestamp_an_observation_that_did_not_happen(self):
        with self.assertRaises(GraspObservationError):
            GraspObservation(availability=GraspAvailability.UNAVAILABLE,
                             observed_at=5.0)

    def test_cannot_name_an_object_without_observing(self):
        with self.assertRaises(GraspObservationError):
            GraspObservation(availability=GraspAvailability.UNAVAILABLE,
                             object_id="mat_a")


class TestObservationInvariants(unittest.TestCase):
    def test_observation_requires_the_object_identity(self):
        """물체 식별자 없는 값은 파지 관측이 아니다 — 개구 측정과 구분된다."""
        with self.assertRaises(GraspObservationError):
            GraspObservation(
                availability=GraspAvailability.MEASURED, held=True,
                observed_at=1.0, source="sensor", method="object_presence")

    def test_observation_requires_a_timestamp(self):
        with self.assertRaises(GraspObservationError):
            GraspObservation(
                availability=GraspAvailability.MEASURED, held=True,
                object_id="mat_a", source="sensor", method="object_presence")

    def test_observation_requires_source_and_method(self):
        with self.assertRaises(GraspObservationError):
            GraspObservation(
                availability=GraspAvailability.MEASURED, held=True,
                object_id="mat_a", observed_at=1.0, source="", method="x")

    def test_held_must_be_a_boolean(self):
        with self.assertRaises(GraspObservationError):
            GraspObservation(
                availability=GraspAvailability.MEASURED, held="yes",
                object_id="mat_a", observed_at=1.0, source="s",
                method="object_presence")

    def test_aperture_cannot_be_the_method(self):
        with self.assertRaises(GraspObservationError):
            measured(held=True, object_id="mat_a", observed_at=1.0,
                     source="gripper", method="aperture_within_tolerance")

    def test_aperture_mentioned_with_object_sensing_is_allowed(self):
        item = measured(
            held=True, object_id="mat_a", observed_at=1.0, source="gripper",
            method="object_detection_plus_aperture_check")
        self.assertTrue(item.counts_for_gate)


class TestGateWeight(unittest.TestCase):
    def test_simulated_observation_does_not_satisfy_the_gate(self):
        item = simulated(held=True, object_id="mat_a", observed_at=1.0,
                         source="gz", method="simulator_attached_state")
        self.assertTrue(item.available)
        self.assertTrue(item.simulated)
        self.assertFalse(item.counts_for_gate)
        self.assertEqual(item.to_dict()["availability"], "simulated_observation")

    def test_measured_observation_satisfies_the_gate(self):
        item = measured(held=True, object_id="mat_a", observed_at=1.0,
                        source="sensor", method="object_presence_sensor")
        self.assertTrue(item.counts_for_gate)
        self.assertFalse(item.simulated)

    def test_false_measurement_is_still_an_observation(self):
        item = measured(held=False, object_id="mat_a", observed_at=1.0,
                        source="sensor", method="object_presence_sensor")
        self.assertTrue(item.counts_for_gate)
        self.assertIs(item.held, False)


class TestObserverProtocol(unittest.TestCase):
    def test_an_object_with_observe_satisfies_the_protocol(self):
        class Observer:
            def observe(self, object_id):
                return unavailable(detail="없다")

        self.assertIsInstance(Observer(), GraspObserver)

    def test_an_object_without_observe_does_not(self):
        class NotAnObserver:
            pass

        self.assertNotIsInstance(NotAnObserver(), GraspObserver)


if __name__ == "__main__":
    unittest.main(verbosity=2)
