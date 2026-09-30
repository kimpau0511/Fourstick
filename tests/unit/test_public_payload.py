"""공개 응답에 실기(실제 하드웨어) 상태가 새지 않는지 검증.

이 서비스는 지금 **Gazebo 시뮬레이션 전용**이다. 실기 준비도·검증 상태를
사용자에게 보여 주면 실제 로봇을 고르거나 돌릴 수 있는 것처럼 읽힌다.
그래서 화면과 공개 JSON에서만 뺀다.

**지우는 것이 아니라 내보내지 않는 것이다.** 판정 코드·어댑터·설정·기준
데이터는 그대로 있고 계속 계산된다 — 아래 `PreservedInternalsTest`가 그 보존을
지킨다. 두 축을 한 파일에서 함께 본다: 새는지, 그리고 남아 있는지.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.routes.common import (  # noqa: E402
    HIDDEN_HARDWARE_KEYS,
    HIDDEN_LIST_VALUES,
    json_response,
    public_payload,
)


class PublicPayloadTest(unittest.TestCase):
    """`public_payload`는 실기 상태 키만, 깊이에 상관없이 뺀다."""

    def test_hides_hardware_keys_at_any_depth(self):
        payload = {
            "is_simulated": True,
            "real_hardware_ready": False,
            "robot": {
                "hardware_readiness": {"available": True, "blocking": 11},
                "workcell": {"registered": True, "real_hardware_verified": False},
                "simulation_e2e": {"completed": True, "real_hardware_ready": False,
                                   "real_hardware_verified": False},
                "stop_diagnostics": {"stop_latch_active": False,
                                     "real_hardware": False},
            },
            "rows": [{"real_hardware_connected": False, "korean": "A자재"}],
        }
        out = public_payload(payload)
        self.assertEqual(out, {
            "is_simulated": True,
            "robot": {
                "workcell": {"registered": True},
                "simulation_e2e": {"completed": True},
                "stop_diagnostics": {"stop_latch_active": False},
            },
            "rows": [{"korean": "A자재"}],
        })

    def test_keeps_everything_else_untouched(self):
        payload = {"decision": "CONFIRM", "is_simulated": True,
                   "confirmation": {"token": "t", "summary": "A자재를 옮기겠습니다."},
                   "materials": [{"model": "material_a", "korean": "A자재"}],
                   "count": 0, "flag": False, "none": None}
        self.assertEqual(public_payload(payload), payload)

    def test_does_not_mutate_the_caller_object(self):
        """내부 모델을 건드리지 않는다 — 사본만 깎는다."""
        payload = {"real_hardware_ready": False, "keep": 1,
                   "unverified_items": ["real_hardware_activation", "other"]}
        public_payload(payload)
        self.assertIn("real_hardware_ready", payload)
        self.assertIn("real_hardware_activation", payload["unverified_items"])

    def test_every_response_goes_through_the_filter(self):
        """라우트가 아니라 `json_response` 한 곳에서 걸러진다."""
        _, _, body = json_response({"a": 1, "real_hardware_ready": False,
                                    "hardware_readiness": {"x": 1}})
        self.assertEqual(json.loads(body), {"a": 1})

    def test_hides_exact_list_values_only(self):
        """리스트에서는 **정확히 같은 문자열**만 뺀다. 부분 일치는 건드리지 않는다."""
        payload = {"unverified_items": [
            "pad_surface_aperture_at_close",
            "gazebo_mimic_behaviour",
            "real_hardware_activation",
            "coupling_height_GRP-CPL-062",
        ]}
        self.assertEqual(public_payload(payload)["unverified_items"], [
            "pad_surface_aperture_at_close",
            "gazebo_mimic_behaviour",
            "coupling_height_GRP-CPL-062",
        ])

    def test_substring_matches_are_kept(self):
        """낱말이 들어 있다는 이유로 다른 항목을 지우지 않는다."""
        payload = {"items": ["real_hardware_activation_notes",
                             "pre_real_hardware_activation",
                             "REAL_HARDWARE_ACTIVATION"]}
        self.assertEqual(public_payload(payload)["items"], payload["items"])

    def test_list_value_filter_does_not_touch_dict_keys_of_the_same_name(self):
        """키와 값을 구분한다 — 같은 이름이 키로 오면 리스트 규칙이 아니라 키 규칙이다."""
        payload = {"real_hardware_activation": True, "keep": 1}
        # 키 목록에 없으므로 키로 쓰인 것은 그대로 남는다.
        self.assertIn("real_hardware_activation", public_payload(payload))

    def test_the_list_value_set_is_exactly_one_entry(self):
        self.assertEqual(HIDDEN_LIST_VALUES, frozenset({"real_hardware_activation"}))

    def test_the_hidden_set_is_exactly_the_hardware_axis(self):
        """시뮬레이션 축(`is_simulated` 등)은 **가리지 않는다.**"""
        self.assertEqual(HIDDEN_HARDWARE_KEYS, frozenset({
            "real_hardware", "real_hardware_ready", "real_hardware_verified",
            "real_hardware_connected", "real_hardware_ready_at",
            "hardware_readiness"}))
        for keep in ("is_simulated", "simulation_e2e", "simulation_demo",
                     "simulation_notice", "workcell"):
            self.assertNotIn(keep, HIDDEN_HARDWARE_KEYS)


class RouteResponsesTest(unittest.TestCase):
    """라우트 응답 **모양**의 예시 dict가 필터를 거치면 실기 상태가 없는지 본다.

    실제 라우트를 호출하지 않는다. 라우트가 필터 **전**에 금지 키를 만들지 않는지는
    `test_sim_demo_commands.py`의 `test_routes_do_not_build_keys_the_public_filter_strips`가
    실제 응답으로 본다 — 필터가 지운 것이 신호가 되려면 라우트가 스스로 만들지 않아야 한다.
    """

    def assert_clean(self, payload, where):
        blob = json.dumps(public_payload(payload), ensure_ascii=False)
        for key in HIDDEN_HARDWARE_KEYS:
            self.assertNotIn(f'"{key}"', blob, f"{where}에 {key}가 남아 있다")

    def test_sim_demo_command_base_has_no_hardware_state(self):
        # 라우트가 스스로 넣지 않는 키를 다른 코드가 실수로 넣는 경우를 흉내 낸다.
        base = {"is_simulated": True, "real_hardware_verified": False,
                "simulation_notice": "Gazebo 시뮬레이션 · 실제 로봇 아님",
                "decision": "ASK", "job": None}
        out = public_payload(base)
        self.assert_clean(base, "/v1/sim-demo/command")
        # 시뮬레이션 축은 그대로 남는다.
        self.assertIs(out["is_simulated"], True)
        self.assertIn("simulation_notice", out)

    def test_sim_demo_status_has_no_hardware_state(self):
        from server.sim_demo_jobs import ACTION_LABELS  # noqa: F401 — 모듈 로드 확인

        status = {"is_simulated": True, "real_hardware_ready": False,
                  "real_hardware_verified": False, "state": {"objects": {}},
                  "materials": [], "running_job": None}
        out = public_payload(status)
        self.assert_clean(status, "/v1/sim-demo")
        self.assertIs(out["is_simulated"], True)
        self.assertIn("state", out)


class PreservedInternalsTest(unittest.TestCase):
    """실기 코드·설정·기준 데이터가 **지워지지 않았는지** 본다.

    UI/API에서 감춘 것이지 삭제한 것이 아니다. 여기가 깨지면 보존 약속이 깨진 것이다.
    """

    def test_hardware_readiness_module_still_evaluates(self):
        from validation import hardware_readiness

        for name in ("evaluate", "load_input_set", "load_checklist",
                     "pinned_state", "state_lines", "collection_guide"):
            self.assertTrue(hasattr(hardware_readiness, name), name)

    def test_runtime_still_computes_readiness_internally(self):
        """내부 모델에는 남는다 — 라우트가 내보내지 않을 뿐이다."""
        from server import runtime as runtime_module

        self.assertTrue(hasattr(runtime_module, "_load_hardware_readiness"))
        payload = runtime_module._load_hardware_readiness()
        self.assertIsInstance(payload, dict)
        # 판정은 계속 돈다. 그리고 시뮬레이션을 실기로 승격하지 않는다.
        self.assertIs(payload.get("real_hardware_ready", False), False)
        self.assertIs(payload.get("real_hardware_verified", False), False)

    def test_simulation_demo_state_keeps_the_hardware_axis(self):
        """기록 파일의 불변식은 이 필드에 기대고 있다 — 빼면 안 된다."""
        import inspect

        from validation import simulation_demo_state

        source = inspect.getsource(simulation_demo_state)
        self.assertIn("real_hardware_verified", source)

    def test_gripper_profile_still_lists_the_unverified_item(self):
        """공개 응답에서만 뺀다 — 기준 데이터 파일은 그대로다."""
        profile = json.loads(
            (ROOT / "config/profiles/robotiq_2f85_gripper.json").read_text(
                encoding="utf-8"))
        items = profile["unverified_items"]
        self.assertIn("real_hardware_activation", items)
        # 목록의 다른 항목도 그대로다.
        self.assertIn("pad_surface_aperture_at_close", items)

    def test_hardware_config_and_adapters_are_present(self):
        self.assertTrue((ROOT / "validation/hardware_readiness.py").is_file())
        self.assertTrue((ROOT / "config/hardware").is_dir())
        self.assertTrue((ROOT / "tests/unit/test_hardware_adapters.py").is_file())
        self.assertTrue((ROOT / "tests/unit/test_hardware_readiness.py").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
