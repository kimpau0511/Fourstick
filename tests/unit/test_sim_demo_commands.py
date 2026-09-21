"""시뮬레이션 시연 전용 명령(텍스트·STT final) 단위 검증.

- 텍스트와 STT final은 **같은 입구**로 가고 같은 job spec이 된다
- 원래 자리 복귀는 held_on_target일 때만, resume은 체크포인트가 하나일 때만
- "멈춰/정지/스톱"은 LLM·계획을 거치지 않고 즉시 시연 정지를 요청한다
- STT partial · 모드 꺼짐 · 시뮬레이션 아님 · 모호 발화 · 슬롯 발화 → 작업 0건
- 모든 응답에 "Gazebo 시뮬레이션 · 실제 로봇 아님"과 `is_simulated=true`

**프로세스를 띄우지 않는다.** Popen은 기록용 대역이다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.routes import sim_demo as sim_demo_route  # noqa: E402
from server.sim_demo_commands import (  # noqa: E402
    ASK,
    BLOCK,
    PASS_THROUGH,
    RUN,
    SIMULATION_NOTICE,
    STOP,
    decide,
    parse_command,
)
from validation.simulation_demo_state import (  # noqa: E402
    POLICY_DEMO_HOLD,
    SimulationDemoState,
)
from tests.unit.test_sim_demo_web import WORKCELL, JobsBase  # noqa: E402
from tests.unit.test_simulation_demo_checkpoint import (  # noqa: E402
    build,
    completed_result,
    stopped_result,
)


class ParseTest(unittest.TestCase):
    def parse(self, text):
        return parse_command(text, WORKCELL)

    def test_supported_transfer_and_return_utterances(self):
        for letter, model in (("A", "material_a"), ("B", "material_b"),
                              ("C", "material_c")):
            with self.subTest(letter=letter):
                transfer = self.parse(f"{letter} 자재를 컨베이어로 옮겨줘")
                self.assertEqual((transfer["decision"], transfer["intent"],
                                  transfer["material"]), (RUN, "transfer", model))
                back = self.parse(f"{letter} 자재를 원래 자리로 돌려놔")
                self.assertEqual((back["decision"], back["intent"], back["material"]),
                                 (RUN, "return", model))

    def test_stop_words_take_precedence(self):
        for text in ("멈춰", "정지", "스톱", "지금 멈춰줘", "A 자재 멈춰"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text)["decision"], STOP)

    def test_resume_utterance(self):
        parsed = self.parse("이어서 해줘")
        self.assertEqual((parsed["decision"], parsed["intent"]), (RUN, "resume"))

    def test_stt_style_korean_letter_readings(self):
        parsed = self.parse("에이 자재를 컨베이어로 옮겨 줘")
        self.assertEqual(parsed["material"], "material_a")

    def test_slot_utterances_ask(self):
        for text in ("A 자재를 컨베이어 2번 슬롯에 놔줘",
                     "A 자재를 컨베이어 3번 자리에 올려줘"):
            with self.subTest(text=text):
                parsed = self.parse(text)
                self.assertEqual(parsed["decision"], ASK)
                self.assertIn("단일 배치 위치", parsed["reason"])

    def test_ambiguous_material_utterances_ask_without_fallback(self):
        """자재를 가리키는데 불완전하면 계획 생성으로 넘기지 않는다."""
        for text in ("자재를 컨베이어로 옮겨줘", "A자재랑 B자재 옮겨줘",
                     "A 자재 어떻게 해줘"):
            with self.subTest(text=text):
                parsed = self.parse(text)
                self.assertEqual(parsed["decision"], ASK)
                self.assertIsNone(parsed["intent"])

    def test_non_simulation_utterances_pass_through_to_plan(self):
        for text in ("안전 위치로 복귀해줘", "1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘",
                     "홈으로 가", "그거 저기로 옮겨줘", "로봇 상태 확인해줘", ""):
            with self.subTest(text=text):
                parsed = self.parse(text)
                self.assertEqual(parsed["decision"], PASS_THROUGH)
                self.assertIsNone(parsed["intent"])

    def test_bare_return_without_a_material(self):
        """자재를 말하지 않은 복귀. 어느 자재인지는 상태가 정한다."""
        for text in ("돌려놔", "원래 자리로 돌려놔", "제자리로 되돌려줘", "원위치로"):
            with self.subTest(text=text):
                parsed = self.parse(text)
                self.assertEqual((parsed["decision"], parsed["intent"],
                                  parsed["material"]), (RUN, "return", None))

    def test_general_return_words_still_pass_through(self):
        """"안전 위치로 복귀해줘"는 일반 명령이다 — 자재 복귀로 가지 않는다."""
        for text in ("안전 위치로 복귀해줘", "홈으로 복귀", "복귀해줘"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text)["decision"], PASS_THROUGH)

    def test_pallet_number_is_kept_for_cross_check(self):
        parsed = self.parse("1번 팔레트에서 A자재를 컨베이어로 옮겨줘")
        self.assertEqual(parsed["mentioned_pallets"], {"1": "pallet_1"})


class CommandEndpointTest(JobsBase):
    """`POST /v1/sim-demo/command` — 텍스트·STT final 공통 입구."""

    def setUp(self):
        super().setUp()
        self.state = SimulationDemoState(self.state_path)
        self.runtime = types.SimpleNamespace(
            sim_demo_jobs=self.jobs, sim_demo_disabled_reason=None,
            simulation_demo_status=lambda: self.state.status())

    def send(self, utterance, *, source="text", mode="simulation_demo",
             runtime=None):
        payload = {"utterance": utterance, "source": source}
        if mode is not None:
            payload["mode"] = mode

        async def read(_receive):
            return payload

        ctx = types.SimpleNamespace(
            runtime=runtime or self.runtime, read_body=read,
            # 계획·LLM 경로는 쓰지 않는다. 쓰면 테스트가 깨진다.
            api=_NoApi())
        status, _, raw = asyncio.run(
            sim_demo_route.handle(ctx, "POST", "/v1/sim-demo/command", None, {}))
        return status, json.loads(raw)

    def assert_simulated(self, payload):
        self.assertIs(payload["is_simulated"], True)
        self.assertEqual(payload["simulation_notice"], SIMULATION_NOTICE)

    def test_text_and_stt_final_produce_the_same_job_spec(self):
        status, text = self.send("A 자재를 컨베이어로 옮겨줘", source="text")
        self.assertEqual(status, 202)
        self.assertEqual(text["decision"], RUN)
        self.assertEqual(text["job_spec"], {"action": "transfer",
                                            "material": "material_a",
                                            "checkpoint_id": None})
        self.assert_simulated(text)
        first_argv = self.popen.calls[0]["argv"]
        # 작업이 끝난 뒤 같은 발화를 STT final로 보내면 같은 spec이 나온다.
        self.popen.procs[0].code = 0
        _, voice = self.send("에이 자재를 컨베이어로 옮겨줘", source="stt_final")
        self.assertEqual(voice["job_spec"], text["job_spec"])
        self.assertEqual(self.popen.calls[1]["argv"], first_argv[:-1] + [
            self.popen.calls[1]["argv"][-1]])
        self.assertEqual(len(self.popen.calls), 2)

    def test_return_only_when_held_on_target(self):
        status, blocked = self.send("A 자재를 원래 자리로 돌려놔")
        self.assertEqual((status, blocked["decision"]), (409, BLOCK))
        self.assertEqual(self.popen.calls, [])
        self.state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                              result=completed_result(),
                              final_pose_m=(0.25, -0.5, 0.75), restored=None)
        status, ok = self.send("A 자재를 원래 자리로 돌려놔")
        self.assertEqual((status, ok["decision"]), (202, RUN))
        self.assertEqual(ok["job_spec"]["action"], "return")
        self.assertIn("--return-held-to-origin", self.popen.calls[0]["argv"])

    def test_bare_return_uses_the_only_held_material(self):
        self.state.record_run(policy=POLICY_DEMO_HOLD, model="material_b",
                              result=completed_result(),
                              final_pose_m=(0.25, -0.5, 0.75), restored=None)
        status, ok = self.send("돌려놔", source="stt_final")
        self.assertEqual((status, ok["decision"]), (202, RUN))
        self.assertEqual(ok["job_spec"], {"action": "return",
                                          "material": "material_b",
                                          "checkpoint_id": None})
        self.assertIn("--return-held-to-origin", self.popen.calls[0]["argv"])
        self.assertIn("pallet_2", self.popen.calls[0]["argv"])

    def test_bare_return_with_nothing_held_is_blocked(self):
        status, payload = self.send("돌려놔")
        self.assertEqual((status, payload["decision"]), (409, BLOCK))
        self.assertIn("유지 중인 자재가 없습니다", payload["reason"])
        self.assertEqual(self.popen.calls, [])

    def test_bare_return_with_two_held_materials_asks(self):
        for model, pose in (("material_a", (0.25, -0.5, 0.75)),
                            ("material_b", (0.25, -0.45, 0.75))):
            self.state.record_run(policy=POLICY_DEMO_HOLD, model=model,
                                  result=completed_result(), final_pose_m=pose,
                                  restored=None)
        status, payload = self.send("돌려놔")
        self.assertEqual((status, payload["decision"]), (200, ASK))
        self.assertIn("어느 것을", payload["reason"])
        self.assertEqual(self.popen.calls, [])

    def test_resume_only_with_exactly_one_checkpoint(self):
        status, none = self.send("이어서 해줘")
        self.assertEqual((status, none["decision"]), (409, BLOCK))
        self.state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                              result=stopped_result(), final_pose_m=(0.4, 0, 1),
                              restored=None)
        checkpoint = self.state.record_checkpoint(build()[0])
        status, ok = self.send("이어서 해줘", source="stt_final")
        self.assertEqual((status, ok["decision"]), (202, RUN))
        self.assertEqual(ok["job_spec"], {"action": "resume",
                                          "material": "material_a",
                                          "checkpoint_id": checkpoint["checkpoint_id"]})
        argv = self.popen.calls[0]["argv"]
        self.assertEqual(argv[argv.index("--resume-checkpoint") + 1],
                         checkpoint["checkpoint_id"])

    def test_resume_with_two_checkpoints_asks(self):
        for model, pose in (("material_a", (0.4, 0, 1)), ("material_b", (0.4, 0.1, 1))):
            self.state.record_run(policy=POLICY_DEMO_HOLD, model=model,
                                  result=stopped_result(), final_pose_m=pose,
                                  restored=None)
            self.state.record_checkpoint(build(model=model)[0])
        status, payload = self.send("이어서 해줘")
        self.assertEqual((status, payload["decision"]), (200, ASK))
        self.assertEqual(self.popen.calls, [])

    def test_stop_is_immediate_without_plan_or_llm(self):
        self.jobs.start("transfer", "material_a")
        calls_before = len(self.popen.calls)
        status, payload = self.send("멈춰", source="stt_final")
        self.assertEqual((status, payload["decision"]), (200, STOP))
        self.assertTrue(payload["stop"]["requested"])
        self.assertIsNone(payload["job"])
        self.assertEqual(len(self.popen.calls), calls_before)   # 새 작업 없음
        record = json.loads((self.tmp / "stop.json").read_text(encoding="utf-8"))
        self.assertEqual(record["reason"], "sim_demo_command_stop")
        self.assert_simulated(payload)

    def test_stop_without_running_job_reports_nothing_to_stop(self):
        status, payload = self.send("정지")
        self.assertEqual((status, payload["decision"]), (200, STOP))
        self.assertFalse(payload["stop"]["requested"])

    def test_partial_transcript_makes_no_job(self):
        status, payload = self.send("A 자재를 컨베이어로 옮겨줘", source="stt_partial")
        self.assertEqual((status, payload["decision"]), (400, BLOCK))
        self.assertIn("partial", payload["reason"])
        self.assertEqual(self.popen.calls, [])

    def test_general_command_passes_through_without_job(self):
        status, payload = self.send("1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘")
        self.assertEqual((status, payload["decision"]), (200, PASS_THROUGH))
        self.assertIsNone(payload["job"])
        self.assertEqual(self.popen.calls, [])

    def test_mode_off_makes_no_job(self):
        status, payload = self.send("A 자재를 컨베이어로 옮겨줘", mode=None)
        self.assertEqual((status, payload["decision"]), (400, BLOCK))
        self.assertEqual(self.popen.calls, [])

    def test_disabled_runtime_makes_no_job(self):
        runtime = types.SimpleNamespace(sim_demo_jobs=None,
                                        sim_demo_disabled_reason="활성 작업 셀이 시뮬레이션이 아니다",
                                        simulation_demo_status=lambda: {})
        status, payload = self.send("A 자재를 컨베이어로 옮겨줘", runtime=runtime)
        self.assertEqual((status, payload["decision"]), (403, BLOCK))
        self.assertIn("시뮬레이션이 아니다", payload["reason"])
        self.assertEqual(self.popen.calls, [])

    def test_ambiguous_and_slot_utterances_make_no_job(self):
        for text in ("자재를 컨베이어로 옮겨줘", "A 자재를 컨베이어 2번 슬롯에 놔줘",
                     "A자재랑 B자재 옮겨줘"):
            with self.subTest(text=text):
                status, payload = self.send(text)
                self.assertEqual((status, payload["decision"]), (200, ASK))
                self.assertTrue(payload["reason"])
        self.assertEqual(self.popen.calls, [])

    def test_transfer_blocked_when_cell_is_not_clean(self):
        self.state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                              result=completed_result(),
                              final_pose_m=(0.25, -0.5, 0.75), restored=None)
        status, payload = self.send("B 자재를 컨베이어로 옮겨줘")
        self.assertEqual((status, payload["decision"]), (409, BLOCK))
        self.assertEqual(self.popen.calls, [])

    def test_wrong_pallet_in_utterance_is_blocked(self):
        status, payload = self.send("2번 팔레트에서 A자재를 컨베이어로 옮겨줘")
        self.assertEqual((status, payload["decision"]), (409, BLOCK))
        self.assertIn("2번 팔레트", payload["reason"])
        self.assertEqual(self.popen.calls, [])

    def test_running_job_blocks_new_command(self):
        self.jobs.start("transfer", "material_a")
        status, payload = self.send("B 자재를 컨베이어로 옮겨줘")
        self.assertEqual((status, payload["decision"]), (409, BLOCK))
        self.assertIn("실행 중", payload["reason"])


class _NoApi:
    """계획 생성·LLM 경로를 쓰면 즉시 실패하게 만드는 대역."""

    def __getattr__(self, name):
        raise AssertionError(f"시연 명령이 일반 API를 썼다: {name}")


class DecideStateTest(unittest.TestCase):
    def test_decide_needs_no_llm_and_keeps_reasons(self):
        parsed = parse_command("A 자재를 컨베이어로 옮겨줘", WORKCELL)
        status = {"state": {"available": True, "objects": {}, "checkpoints": []},
                  "running_job": None}
        materials = {"material_a": {"model": "material_a", "korean": "A자재",
                                    "support_model": "pallet_1"}}
        decision = decide(parsed, status, materials)
        self.assertEqual(decision["decision"], RUN)
        self.assertEqual(decision["job_spec"]["action"], "transfer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
