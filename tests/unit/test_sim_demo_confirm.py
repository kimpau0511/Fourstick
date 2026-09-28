"""해석 → 확인 → 실행 계약 검증 (`/v1/sim-demo/command`, `/v1/sim-demo/confirm`).

핵심 불변식 하나다. **확인을 누르기 전에는 작업이 0건이다.**

| 경우 | 기대 |
|---|---|
| 명확한 규칙 명령("A 자재를 컨베이어로 옮겨줘") | 기존대로 즉시 RUN, 확인 없음 |
| "멈춰" | 즉시 STOP — 분류기·확인을 거치지 않는다 |
| 일반 명령("안전 위치로 복귀해줘") | PASS_THROUGH — 분류기를 부르지도 않는다 |
| 모호한 자재 작업 발화 | CONFIRM — **작업 0건** |
| CONFIRM → confirm | 작업 1건 |
| CONFIRM → cancel | 작업 0건 |
| CONFIRM → 만료 | 작업 0건 |
| CONFIRM → 그 사이 상태 변경 | 작업 0건 |
| 분류기 JSON 오류·낮은 confidence·모델 서버 없음 | 작업 0건 |

프로세스를 실제로 띄우지 않는다 — Popen은 기록용 대역이고, 작업 건수는
그 기록(`popen.calls`)으로 센다.
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

from server.sim_demo_commands import looks_like_material_work  # noqa: E402
from server.sim_demo_confirm import ConfirmStore, fingerprint  # noqa: E402
from server.sim_demo_intent import IntentClassifier  # noqa: E402
from validation.simulation_demo_state import (  # noqa: E402
    POLICY_DEMO_HOLD,
    SimulationDemoState,
)
from tests.unit.test_simulation_demo_checkpoint import completed_result  # noqa: E402
from tests.unit.test_sim_demo_intent import FakeClient  # noqa: E402
from tests.unit.test_sim_demo_web import WORKCELL, JobsBase  # noqa: E402


def body(payload):
    async def read(receive):
        return payload
    return read


def answer(content):
    """분류기가 낼 JSON 한 줄.

    스키마는 **선언형 작업 다섯 칸**이다(`server/sim_demo_intent.FIELDS`).
    테스트를 읽기 쉽게 `intent`로 적어도 `action`으로 옮겨 주고, 말하지 않은
    자리는 `None`으로 채운다 — 자리를 시험하는 테스트는 직접 적는다.
    """
    payload = dict(content)
    if "intent" in payload:
        payload["action"] = payload.pop("intent")
    payload.setdefault("source_resource", None)
    payload.setdefault("destination_resource", None)
    return json.dumps(payload, ensure_ascii=False)


class ClockBase(JobsBase):
    """시계를 손으로 돌린다 — 만료를 실제 시간으로 기다리지 않는다."""

    def setUp(self):
        super().setUp()
        self.now = 1000.0
        self.store = ConfirmStore(ttl_sec=60.0, clock=lambda: self.now)
        self.runtime = types.SimpleNamespace(
            sim_demo_jobs=self.jobs,
            sim_demo_disabled_reason=None,
            sim_demo_confirm=self.store,
            sim_demo_intent=None,
            sim_demo_intent_disabled_reason="분류기를 붙이지 않았다",
            simulation_demo_status=lambda: self.jobs.state.status())

    def with_classifier(self, content=None, error=None):
        client = FakeClient(content=content, error=error)
        self.runtime.sim_demo_intent = IntentClassifier(client=client,
                                                        min_confidence=0.7)
        self.runtime.sim_demo_intent_disabled_reason = None
        return client

    def call(self, method, path, payload=None):
        from server.routes import sim_demo

        ctx = types.SimpleNamespace(runtime=self.runtime, read_body=body(payload or {}))
        status, _, raw = asyncio.run(
            sim_demo.handle(ctx, method, path, None, {}))
        return status, json.loads(raw)

    def command(self, utterance, source="text"):
        return self.call("POST", "/v1/sim-demo/command",
                         {"mode": "simulation_demo", "utterance": utterance,
                          "source": source})

    def confirm(self, token, action="confirm"):
        return self.call("POST", "/v1/sim-demo/confirm",
                         {"token": token, "action": action})

    @property
    def job_count(self):
        return len(self.popen.calls)


class VoiceFinalContractTest(ClockBase):
    """오인식 원문·정규화·해석을 분리하고 확인 전 작업 0건을 지킨다."""

    def test_korean_aliases_wait_for_confirmation(self):
        cases = (
            ("에이 자재를 벨트로 갖다 놔", "A 자재를 컨베이어로 옮겨"),
            ("비 자재를 컨베이어로 올려 놔", "B 자재를 컨베이어로 옮겨"),
            ("씨 자재를 컨베이어로 치워", "C 자재를 컨베이어로 옮겨"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                status, result = self.call("POST", "/v1/sim-demo/command", {
                    "mode": "simulation_demo", "source": "stt_final",
                    "utterance": raw, "raw_transcript": raw,
                    "stt_confidence": 0.82,
                })
                self.assertEqual((status, result["decision"]), (200, "CONFIRM"))
                self.assertEqual(result["raw_transcript"], raw)
                self.assertIn(expected, result["normalized_transcript"])
                self.assertEqual(result["stt_confidence"], 0.82)
                diagnostic = result["diagnostic"]
                self.assertEqual(diagnostic["raw_transcript"], raw)
                self.assertEqual(diagnostic["normalized_transcript"], result["normalized_transcript"])
                self.assertEqual(diagnostic["interpretation"]["material"], result["material"])
                self.assertNotIn("audio", diagnostic)
                self.assertEqual(self.job_count, 0)
                self.store.cancel(result["confirmation"]["token"])

    def test_raw_error_is_visible_separately_from_interpretation(self):
        # 의도는 B였지만 STT가 A라고 냈다. 해석기가 맞게 A를 고른 경우에도
        # 원문과 정규화 결과를 감추지 않아 사람이 확인 카드에서 발견할 수 있다.
        intended = "B 자재를 컨베이어로 옮겨"
        status, result = self.call("POST", "/v1/sim-demo/command", {
            "mode": "simulation_demo", "source": "stt_final",
            "utterance": "A 자재를 컨베이어로 옮겨", "raw_transcript": "에이 자재를 컨베이어로 옮겨",
            "stt_confidence": 0.71,
        })
        self.assertEqual((status, result["decision"]), (200, "CONFIRM"))
        self.assertEqual(result["material"], "material_a")
        self.assertNotEqual(result["normalized_transcript"], intended)
        self.assertEqual(result["raw_transcript"], "에이 자재를 컨베이어로 옮겨")
        self.assertEqual(self.job_count, 0)

    def test_ambiguous_destination_asks_without_job(self):
        for raw in ("A 자재를 저쪽 벨트로 옮겨", "A 자재를 빈자리로 옮겨"):
            with self.subTest(raw=raw):
                status, result = self.command(raw, "stt_final")
                self.assertEqual((status, result["decision"]), (200, "ASK"))
                self.assertIn("제가 이렇게 들었습니다", result["reason"])
                self.assertIsNone(result["confirmation"])
                self.assertEqual(self.job_count, 0)

    def test_pallet_return_alias_and_final_stop(self):
        status, result = self.command("에이 자재를 1번 팔렛으로 돌려놔", "stt_final")
        self.assertIn(result["decision"], ("ASK", "BLOCK"))  # 아직 복귀할 자재가 없다
        self.assertIn("1번 팔레트", result["normalized_transcript"])
        self.assertIsNone(result["job"])
        self.assertEqual(self.job_count, 0)
        status, stopped = self.command("스톱", "stt_final")
        self.assertEqual((status, stopped["decision"]), (200, "STOP"))
        self.assertEqual(stopped["normalized_transcript"], "정지")
        self.assertEqual(self.job_count, 0)

    def test_partial_source_never_creates_job_confirmation_or_stop(self):
        for raw in ("멈춰", "A 자재를 컨베이어로 옮겨"):
            status, result = self.command(raw, "stt_partial")
            self.assertEqual((status, result["decision"]), (400, "BLOCK"))
            self.assertIsNone(result["job"])
            self.assertIsNone(result["confirmation"])
            self.assertIsNone(result["stop"])
        self.assertEqual(self.job_count, 0)

    def test_correct_raw_but_wrong_qwen_material_stays_ask(self):
        # STT 원문은 A다. Qwen이 B를 제안한 경우는 해석 오류로 구분한다.
        self.with_classifier(answer({"intent": "transfer", "material_id": "material_b",
                                     "destination_resource": "loc_conveyor",
                                     "confidence": 0.99}))
        status, result = self.command("A 자재를 옮겨줘", "stt_final")
        self.assertEqual((status, result["decision"]), (200, "ASK"))
        self.assertEqual(result["raw_transcript"], "A 자재를 옮겨줘")
        self.assertEqual(result["intent_result"]["material_id"], "material_b")
        self.assertIsNone(result["confirmation"])
        self.assertEqual(self.job_count, 0)

    def test_qwen_candidate_cannot_fill_unspoken_material_or_destination(self):
        self.with_classifier(answer({"intent": "transfer", "material_id": "material_a",
                                     "destination_resource": "loc_conveyor",
                                     "confidence": 0.99}))
        for raw in ("그 자재를 컨베이어로 옮겨", "A 자재를 어디론가 옮겨"):
            with self.subTest(raw=raw):
                status, result = self.command(raw, "stt_final")
                self.assertEqual((status, result["decision"]), (200, "ASK"))
                self.assertIsNone(result["confirmation"])
                self.assertEqual(self.job_count, 0)

class RoutingTest(ClockBase):
    """어떤 발화가 분류기로 가는가. **명확한 명령과 일반 명령은 그대로다.**"""

    def test_material_work_heuristic(self):
        yes = ["그거 컨베이어로 옮겨줘", "주황 자재 올려줘", "이어서 해줘",
               "아까 그 블록 원래 자리에 놔"]
        no = ["안전 위치로 복귀해줘", "1번 팔레트로 이동", "지금 상태 알려줘",
              "홈으로 가",
              # 팔레트를 목적지로 쓴 일반 이동 명령. 분류기로 가지 않는다.
              "1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘"]
        for text in yes:
            with self.subTest(text=text):
                self.assertTrue(looks_like_material_work(text, WORKCELL))
        for text in no:
            with self.subTest(text=text):
                self.assertFalse(looks_like_material_work(text, WORKCELL))

    def test_clear_rule_command_runs_without_confirmation(self):
        client = self.with_classifier(answer({"intent": "transfer",
                                              "material_id": "material_a",
                                              "confidence": 0.99}))
        status, payload = self.command("A 자재를 컨베이어로 옮겨줘")
        self.assertEqual(status, 202)
        self.assertEqual(payload["decision"], "RUN")
        self.assertIsNone(payload["confirmation"])
        self.assertIsNotNone(payload["job"])
        self.assertEqual(self.job_count, 1)
        # 규칙이 정한 명령은 분류기를 **부르지 않는다.**
        self.assertEqual(client.calls, [])

    def test_stop_never_reaches_the_classifier_or_a_confirmation(self):
        client = self.with_classifier(answer({"intent": "transfer",
                                              "material_id": "material_a",
                                              "confidence": 0.99}))
        self.jobs.start("transfer", "material_a")
        status, payload = self.command("멈춰")
        self.assertEqual(status, 200)
        self.assertEqual(payload["decision"], "STOP")
        self.assertTrue(payload["stop"]["requested"])
        self.assertIsNone(payload["confirmation"])
        self.assertEqual(client.calls, [])

    def test_general_command_passes_through_without_calling_the_classifier(self):
        client = self.with_classifier(answer({"intent": "transfer",
                                              "material_id": "material_a",
                                              "confidence": 0.99}))
        for text in ("안전 위치로 복귀해줘", "1번 팔레트로 이동",
                     "1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘"):
            with self.subTest(text=text):
                status, payload = self.command(text)
                self.assertEqual(status, 200)
                self.assertEqual(payload["decision"], "PASS_THROUGH")
                self.assertIsNone(payload["confirmation"])
        self.assertEqual(client.calls, [])
        self.assertEqual(self.job_count, 0)


class ConfirmFlowTest(ClockBase):
    AMBIGUOUS = "팔레트에 있는 물건 하나 컨베이어로 옮겨줘"

    def ask_for_confirmation(self, confidence=0.91):
        self.with_classifier(answer({"intent": "transfer",
                                     "material_id": "material_a",
                                     "confidence": confidence}))
        status, payload = self.command(self.AMBIGUOUS)
        return status, payload

    def test_confirmation_is_offered_and_creates_no_job(self):
        status, payload = self.ask_for_confirmation()
        self.assertEqual(status, 200)
        self.assertEqual(payload["decision"], "CONFIRM")
        self.assertEqual(self.job_count, 0, "확인 전에 작업이 만들어졌다")
        self.assertIsNone(payload["job"])
        pending = payload["confirmation"]
        self.assertEqual(pending["summary"], "A자재를 컨베이어로 옮기겠습니다.")
        self.assertEqual(pending["action"], "transfer")
        self.assertEqual(pending["material"], "material_a")
        self.assertEqual(pending["ttl_sec"], 60.0)
        self.assertIs(pending["is_simulated"], True)
        # 해석 근거와 현재 자재 상태가 함께 간다.
        evidence = pending["evidence"]
        self.assertEqual(evidence["classifier"]["intent"], "transfer")
        self.assertEqual(evidence["classifier"]["confidence"], 0.91)
        self.assertEqual(evidence["classifier"]["model_id"], "qwen3-8b-awq")
        self.assertEqual(sorted(row["model"] for row in evidence["state"]),
                         ["material_a", "material_b", "material_c"])

    def test_confirm_creates_exactly_one_job(self):
        _, payload = self.ask_for_confirmation()
        status, confirmed = self.confirm(payload["confirmation"]["token"])
        self.assertEqual(status, 202)
        self.assertEqual(confirmed["decision"], "RUN")
        # job spec은 **선언형**이다 — 동작·자재·자리뿐이고 좌표·관절값이 없다.
        self.assertEqual(confirmed["job_spec"],
                         {"action": "transfer", "material": "material_a",
                          "checkpoint_id": None, "slot": None,
                          "source": None, "destination": None})
        self.assertEqual(self.job_count, 1)
        argv = self.popen.calls[0]["argv"]
        self.assertTrue(any("material_a" in str(part) for part in argv))

    def test_the_same_token_cannot_be_used_twice(self):
        _, payload = self.ask_for_confirmation()
        token = payload["confirmation"]["token"]
        self.confirm(token)
        status, again = self.confirm(token)
        self.assertEqual(status, 409)
        self.assertEqual(again["decision"], "BLOCK")
        self.assertEqual(again["confirm_rejection"], "not_found")
        self.assertEqual(self.job_count, 1)

    def test_cancel_creates_no_job(self):
        _, payload = self.ask_for_confirmation()
        status, cancelled = self.confirm(payload["confirmation"]["token"], "cancel")
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["decision"], "CANCELLED")
        self.assertEqual(self.job_count, 0)
        self.assertIsNone(self.store.current())

    def test_expiry_creates_no_job(self):
        _, payload = self.ask_for_confirmation()
        self.now += 61.0
        status, expired = self.confirm(payload["confirmation"]["token"])
        self.assertEqual(status, 409)
        self.assertEqual(expired["decision"], "BLOCK")
        self.assertEqual(expired["confirm_rejection"], "not_found")
        self.assertEqual(self.job_count, 0)

    def test_state_change_between_ask_and_press_creates_no_job(self):
        _, payload = self.ask_for_confirmation()
        # 확인을 기다리는 동안 셀 상태가 바뀌었다(다른 경로로 자재가 옮겨짐).
        SimulationDemoState(self.state_path).record_run(
            policy=POLICY_DEMO_HOLD, model="material_b", result=completed_result(),
            final_pose_m=(0.25, -0.5, 0.75), restored=None)
        status, blocked = self.confirm(payload["confirmation"]["token"])
        self.assertEqual(status, 409)
        self.assertEqual(blocked["confirm_rejection"], "state_changed")
        self.assertEqual(self.job_count, 0)

    def test_a_job_started_meanwhile_blocks_the_confirmation(self):
        _, payload = self.ask_for_confirmation()
        self.jobs.start("transfer", "material_b")
        status, blocked = self.confirm(payload["confirmation"]["token"])
        self.assertEqual(status, 409)
        self.assertEqual(blocked["confirm_rejection"], "busy")
        self.assertEqual(self.job_count, 1, "확인이 두 번째 작업을 띄웠다")

    def test_a_new_utterance_replaces_the_pending_confirmation(self):
        _, first = self.ask_for_confirmation()
        _, second = self.ask_for_confirmation()
        self.assertNotEqual(first["confirmation"]["token"],
                            second["confirmation"]["token"])
        status, stale = self.confirm(first["confirmation"]["token"])
        self.assertEqual(stale["confirm_rejection"], "not_found")
        self.assertEqual(self.job_count, 0)

    def test_pending_confirmation_is_visible_on_status_for_refresh(self):
        _, payload = self.ask_for_confirmation()
        _, status_payload = self.call("GET", "/v1/sim-demo")
        self.assertEqual(status_payload["pending_confirmation"]["token"],
                         payload["confirmation"]["token"])
        self.assertTrue(status_payload["intent_available"])


class ClassifierFailureTest(ClockBase):
    AMBIGUOUS = "팔레트에 있는 물건 하나 컨베이어로 옮겨줘"

    def assert_no_job(self, payload, decision):
        self.assertEqual(payload["decision"], decision)
        self.assertIsNone(payload["confirmation"])
        self.assertIsNone(payload["job"])
        self.assertEqual(self.job_count, 0)

    def test_low_confidence_asks_without_a_job(self):
        self.with_classifier(answer({"intent": "transfer",
                                     "material_id": "material_a",
                                     "confidence": 0.31}))
        _, payload = self.command(self.AMBIGUOUS)
        self.assert_no_job(payload, "ASK")
        self.assertIn("확신이 낮습니다", payload["reason"])
        self.assertEqual(payload["intent_result"]["failure"], "confidence")

    def test_broken_json_asks_without_a_job(self):
        self.with_classifier("A 자재를 옮기는 계획을 세웠습니다. 1) move ...")
        _, payload = self.command(self.AMBIGUOUS)
        self.assert_no_job(payload, "ASK")
        self.assertEqual(payload["intent_result"]["failure"], "schema")

    def test_a_free_plan_instead_of_the_schema_asks_without_a_job(self):
        self.with_classifier(answer({"intent": "transfer",
                                     "material_id": "material_a",
                                     "confidence": 0.99,
                                     "steps": ["move", "pick", "place"]}))
        _, payload = self.command(self.AMBIGUOUS)
        self.assert_no_job(payload, "ASK")
        self.assertEqual(payload["intent_result"]["failure"], "schema")

    def test_a_material_outside_this_cell_asks_without_a_job(self):
        self.with_classifier(answer({"intent": "transfer",
                                     "material_id": "material_z",
                                     "confidence": 0.99}))
        _, payload = self.command(self.AMBIGUOUS)
        self.assert_no_job(payload, "ASK")
        self.assertEqual(payload["intent_result"]["failure"], "candidate")

    def test_unknown_intent_asks_without_a_job(self):
        self.with_classifier(answer({"intent": "unknown", "material_id": None,
                                     "confidence": 0.99}))
        _, payload = self.command(self.AMBIGUOUS)
        self.assert_no_job(payload, "ASK")

    def test_a_dead_model_server_asks_without_a_job(self):
        self.with_classifier(error=RuntimeError("연결 실패"))
        _, payload = self.command(self.AMBIGUOUS)
        self.assert_no_job(payload, "ASK")
        self.assertEqual(payload["intent_result"]["failure"], "unavailable")

    def test_no_classifier_configured_asks_without_a_job(self):
        _, payload = self.command(self.AMBIGUOUS)
        self.assert_no_job(payload, "ASK")
        self.assertIn("분류기를 붙이지 않았다", payload["reason"])

    def test_a_state_that_forbids_the_intent_blocks_without_a_job(self):
        """해석은 통과했지만 지금 상태로 할 수 없다 — 확인 카드도 만들지 않는다.

        셀이 초기 상태라 `return`은 할 수 없다(컨베이어에 유지 중인 자재가 없다).
        """
        self.with_classifier(answer({"intent": "return",
                                     "material_id": "material_a",
                                     "confidence": 0.99}))
        _, payload = self.command(self.AMBIGUOUS)
        self.assertEqual(payload["decision"], "BLOCK")
        self.assertIsNone(payload["confirmation"])
        self.assertEqual(self.job_count, 0)
        self.assertIsNone(self.store.current())

    def test_general_commands_keep_their_old_path_when_the_classifier_fails(self):
        """분류기가 죽어도 일반 명령은 그대로 계획 생성으로 간다.

        일반 명령은 분류기를 **부르지도 않는다** — 실패해도 경로가 바뀌지 않는다.
        """
        client = self.with_classifier(error=RuntimeError("연결 실패"))
        _, payload = self.command("안전 위치로 복귀해줘")
        self.assertEqual(payload["decision"], "PASS_THROUGH")
        self.assertEqual(client.calls, [])
        self.assertEqual(self.job_count, 0)


class StoreTest(unittest.TestCase):
    """대기함 자체의 계약."""

    @staticmethod
    def status(*, row=None, checkpoint=None):
        return {
            "state": {
                "objects": {"material_a": row or {
                    "state": "held_on_target", "slot": "slot_1",
                    "pose_m": [0.1, 0.2, 0.3],
                    "recorded_at": "2026-01-02T03:04:05+0900",
                    "scenario": "pallet_1/material_a->conveyor",
                    "record_version": 2,
                }},
                "checkpoints": (["material_a"] if checkpoint else []),
                "checkpoint": checkpoint,
                # These are UI/status decorations, not persisted cell state.
                "display_label": "시뮬레이션 상태 유지 중",
                "updated_at": "2026-01-02T03:04:05+0900",
            },
            "running_job": None,
        }

    def assert_confirmation_rejects_change(self, before, after):
        store = ConfirmStore()
        pending = store.create(job_spec={"action": "transfer"},
                               status=before, summary="x")
        result = store.take(pending.token, after)
        self.assertEqual(result.code, "state_changed")

    def test_confirmation_rejects_material_pose_change(self):
        before = self.status()
        changed = self.status(row={**before["state"]["objects"]["material_a"],
                                   "pose_m": [0.1, 0.2, 0.31]})
        self.assert_confirmation_rejects_change(before, changed)

    def test_confirmation_rejects_material_recorded_at_change(self):
        before = self.status()
        changed = self.status(row={**before["state"]["objects"]["material_a"],
                                   "recorded_at": "2026-01-02T03:04:06+0900"})
        self.assert_confirmation_rejects_change(before, changed)

    def test_confirmation_rejects_material_slot_change(self):
        before = self.status()
        changed = self.status(row={**before["state"]["objects"]["material_a"],
                                   "slot": "slot_2"})
        self.assert_confirmation_rejects_change(before, changed)

    def test_confirmation_rejects_material_scenario_or_version_change(self):
        before = self.status()
        row = before["state"]["objects"]["material_a"]
        for field, value in (("scenario", "pallet_2/material_a->conveyor"),
                             ("record_version", 3)):
            with self.subTest(field=field):
                self.assert_confirmation_rejects_change(
                    before, self.status(row={**row, field: value}))

    def test_confirmation_rejects_checkpoint_content_change(self):
        checkpoint = {
            "checkpoint_id": "simckpt_1", "model": "material_a",
            "object_pose_m": [0.1, 0.2, 0.3],
            "remaining_stages": ["place_approach", "place"],
            "joint_state": {"positions": {"joint_2": 0.2, "joint_1": 0.1}},
            "scene_hash": "scene-a", "stop_execution_id": "simstop_1",
        }
        before = self.status(checkpoint=checkpoint)
        changed = self.status(checkpoint={
            **checkpoint, "joint_state": {
                "positions": {"joint_1": 0.1, "joint_2": 0.21}}})
        self.assert_confirmation_rejects_change(before, changed)

    def test_semantically_unchanged_state_preserves_confirmation(self):
        checkpoint = {
            "checkpoint_id": "simckpt_1", "model": "material_a",
            "remaining_stages": ["place_approach", "place"],
            "joint_state": {"positions": {"joint_2": 0.2, "joint_1": 0.1}},
            "note": "old display text",
        }
        before = self.status(checkpoint=checkpoint)
        after = {
            "running_job": None,
            "state": {
                "updated_at": "later display refresh",
                "display_label": "different display text",
                "checkpoint": {
                    "note": "new display text",
                    "joint_state": {"positions": {"joint_1": 0.1,
                                                    "joint_2": 0.2}},
                    "remaining_stages": ("place_approach", "place"),
                    "model": "material_a", "checkpoint_id": "simckpt_1",
                },
                "checkpoints": ["material_a"],
                "objects": {
                    "material_a": dict(reversed(list(
                        before["state"]["objects"]["material_a"].items())))
                },
            },
        }
        store = ConfirmStore()
        pending = store.create(job_spec={"action": "transfer"}, status=before,
                               summary="x")
        self.assertIs(store.take(pending.token, after), pending)

    def test_fingerprint_follows_material_records_and_checkpoints(self):
        base = {"state": {"objects": {"material_a": {"state": "held_on_target"}},
                          "checkpoints": []}, "running_job": None}
        same = {"state": {"checkpoints": [],
                          "objects": {"material_a": {"state": "held_on_target"}}},
                "running_job": None}
        moved = {"state": {"objects": {}, "checkpoints": []}, "running_job": None}
        reassigned = {"state": {"objects": {
            "material_a": {"state": "held_on_target", "slot": "slot_2"}},
            "checkpoints": []}, "running_job": None}
        self.assertEqual(fingerprint(base), fingerprint(same))
        self.assertNotEqual(fingerprint(base), fingerprint(moved))
        self.assertNotEqual(fingerprint(base), fingerprint(reassigned))

    def test_expired_entries_are_swept(self):
        now = [100.0]
        store = ConfirmStore(ttl_sec=60.0, clock=lambda: now[0])
        store.create(job_spec={"action": "transfer", "material": "material_a"},
                     status={}, summary="x")
        self.assertIsNotNone(store.current())
        now[0] += 61.0
        self.assertIsNone(store.current())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SlotAssignmentTest(ClockBase):
    """서버가 슬롯을 고르고 사용자에게 확인시킨다.

    **발화도 모델도 슬롯·좌표를 정하지 않는다.** 이송은 빈 첫 자리를 받고,
    셋이 차면 BLOCK이며 자동 복귀는 없다.
    """

    def setUp(self):
        super().setUp()
        import json as _json
        from pathlib import Path as _Path

        from server.sim_demo_jobs import SimDemoJobs

        root = _Path(__file__).resolve().parents[2]
        grasp = _json.loads((root / "config/workcell/fr3_2f85_workcell_grasp.json")
                            .read_text(encoding="utf-8"))
        # 자세 설정도 함께 준다 — 안전 위치가 자리 어휘에 들어가야 "안전 위치에
        # 놔줘"를 **지원 밖 목적지**로 답할 수 있다(없으면 모르는 자리가 된다).
        poses = _json.loads((root / "config/workcell/fr3_2f85_workcell_poses.json")
                            .read_text(encoding="utf-8"))
        self.jobs = SimDemoJobs(
            workcell=WORKCELL, grasp_config=grasp, poses_config=poses,
            state_path=self.state_path,
            jobs_dir=self.tmp / "jobs", stop_request=self.tmp / "stop.json",
            popen=self.popen, environ={"PATH": "/usr/bin"})
        self.runtime.sim_demo_jobs = self.jobs

    def finish_running(self):
        """대역 프로세스를 끝낸 것으로 만든다. 작업은 한 번에 하나뿐이다."""
        for proc in self.popen.procs:
            proc.code = 0

    def hold(self, model, slot):
        self.finish_running()
        SimulationDemoState(self.state_path).record_run(
            policy=POLICY_DEMO_HOLD, model=model, result=completed_result(),
            final_pose_m=(0.25, -0.5, 0.75), restored=None, slot=slot)

    def test_three_verified_slots_are_loaded(self):
        self.assertEqual([s.name for s in self.jobs.slots],
                         ["slot_1", "slot_2", "slot_3"])

    def test_transfers_fill_slots_in_order(self):
        for model, letter, expected in (("material_a", "A", "slot_1"),
                                        ("material_b", "B", "slot_2"),
                                        ("material_c", "C", "slot_3")):
            with self.subTest(slot=expected):
                status, payload = self.command(f"{letter} 자재를 컨베이어로 옮겨줘")
                self.assertEqual((status, payload["decision"]), (202, "RUN"))
                self.assertEqual(payload["slot"], expected)
                self.assertEqual(payload["job_spec"]["slot"], expected)
                # 스크립트에 슬롯이 그대로 전달된다.
                argv = self.popen.calls[-1]["argv"]
                self.assertIn("--slot", argv)
                self.assertEqual(argv[argv.index("--slot") + 1], expected)
                self.hold(model, expected)

    def stopped_before_grasp(self, model, slot, pose):
        """집기 전에 멈춘 기록을 그대로 넣는다(실측 모양)."""
        import json as _json
        # 정상 기록을 먼저 만들고(파일 모양을 손으로 만들지 않는다) 그 기록의
        # 상태와 pose만 **집기 전 정지**가 남기는 값으로 바꾼다.
        self.hold(model, slot)
        raw = _json.loads(self.state_path.read_text(encoding="utf-8"))
        raw["objects"][model] = {**raw["objects"][model],
                                 "pose_m": list(pose), "state": "stopped_unrestored"}
        self.state_path.write_text(_json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    def test_stopped_before_grasp_is_not_called_on_the_conveyor(self):
        """기록만 남고 자재는 팔레트에 있다 — "이미 컨베이어에 있다"고 하지 않는다.

        실측 2026-09-22: pre-grasp에서 정지하면 `stopped_unrestored` 기록과
        슬롯 배정은 남지만 자재는 원래 자리 그대로다. 예전 문구는 화면과
        실물이 어긋나 보였다.
        """
        before = self.job_count
        self.stopped_before_grasp("material_a", "slot_2", (0.500081, 0.200908, 0.839999))
        status, payload = self.command("A 자재를 컨베이어로 옮겨줘")
        self.assertEqual(payload["decision"], "BLOCK")
        self.assertNotIn("이미 컨베이어에 있습니다", payload["reason"])
        # 무엇을 해야 하는지 적는다.
        self.assertIn("복구", payload["reason"])
        self.assertIsNone(payload["job"])
        self.assertEqual(self.job_count, before)

    def test_material_really_on_the_conveyor_still_says_so(self):
        """자리에 놓인 자재는 예전처럼 "이미 컨베이어에 있습니다"다."""
        self.hold("material_a", "slot_1")
        status, payload = self.command("A 자재를 컨베이어로 옮겨줘")
        self.assertEqual(payload["decision"], "BLOCK")
        self.assertIn("이미 컨베이어에 있습니다", payload["reason"])
        self.assertIn("컨베이어 1번 위치", payload["reason"])

    def test_fourth_transfer_is_blocked_without_touching_anyone(self):
        for model, slot in (("material_a", "slot_1"), ("material_b", "slot_2"),
                            ("material_c", "slot_3")):
            self.hold(model, slot)
        before = self.job_count
        status, payload = self.command("A 자재를 컨베이어로 옮겨줘")
        self.assertEqual(payload["decision"], "BLOCK")
        self.assertIn("모두 찼습니다", payload["reason"])
        self.assertIsNone(payload["job"])
        self.assertEqual(self.job_count, before, "만차인데 작업이 생겼다")
        # 아무도 자동으로 되돌리지 않았다.
        objects = SimulationDemoState(self.state_path).status()["objects"]
        self.assertEqual(sorted(objects), ["material_a", "material_b", "material_c"])

    def test_return_uses_the_slot_the_material_was_given(self):
        self.hold("material_b", "slot_2")
        status, payload = self.command("B 자재를 원래 자리로 돌려놔")
        self.assertEqual((status, payload["decision"]), (202, "RUN"))
        self.assertEqual(payload["slot"], "slot_2")
        argv = self.popen.calls[-1]["argv"]
        self.assertEqual(argv[argv.index("--slot") + 1], "slot_2")

    def test_status_reports_slot_occupancy(self):
        self.hold("material_a", "slot_1")
        _, status = self.call("GET", "/v1/sim-demo")
        conveyor = status["conveyor"]
        self.assertTrue(conveyor["enabled"])
        self.assertEqual(conveyor["slot_count"], 3)
        self.assertEqual(conveyor["next_slot"], "slot_2")
        self.assertFalse(conveyor["full"])
        rows = {r["slot"]: r for r in conveyor["slots"]}
        self.assertTrue(rows["slot_1"]["occupied"])
        self.assertEqual(rows["slot_1"]["korean"], "A자재")
        self.assertFalse(rows["slot_3"]["occupied"])

    def test_confirm_card_shows_the_server_chosen_slot(self):
        self.with_classifier(answer({"intent": "transfer",
                                     "material_id": "material_a",
                                     "confidence": 0.95}))
        _, payload = self.command("팔레트에 있는 물건 하나 컨베이어로 옮겨줘")
        self.assertEqual(payload["decision"], "CONFIRM")
        self.assertEqual(self.job_count, 0)
        pending = payload["confirmation"]
        self.assertEqual(pending["summary"],
                         "A자재를 컨베이어 1번 위치로 옮기겠습니다.")
        self.assertEqual(pending["evidence"]["slot"], "slot_1")
        self.assertEqual(pending["evidence"]["slot_label"], "컨베이어 1번 위치")
        # 모델은 슬롯을 내지 않았다 — 서버가 골랐다.
        self.assertNotIn("slot", payload["intent_result"])

    def test_confirming_starts_the_job_on_that_slot(self):
        self.with_classifier(answer({"intent": "transfer",
                                     "material_id": "material_a",
                                     "confidence": 0.95}))
        _, payload = self.command("팔레트에 있는 물건 하나 컨베이어로 옮겨줘")
        _, confirmed = self.confirm(payload["confirmation"]["token"])
        self.assertEqual(confirmed["decision"], "RUN")
        self.assertEqual(confirmed["slot"], "slot_1")
        argv = self.popen.calls[-1]["argv"]
        self.assertEqual(argv[argv.index("--slot") + 1], "slot_1")


class NaturalPlaceTest(SlotAssignmentTest):
    """자연어 pick/place — 모델이 **선언형 작업**을 내고 서버가 자리를 검증한다.

    모델은 자재·동작·출발·도착만 말한다. 관절값·좌표를 낼 칸이 없고, 자리는
    셀이 선언한 것만 받는다. 확인을 누르기 전에는 작업이 0건이다.
    """

    def task(self, utterance, *, action="transfer", material="material_a",
             source=None, destination=None, confidence=0.93):
        self.with_classifier(answer({
            "action": action, "material_id": material,
            "source_resource": source, "destination_resource": destination,
            "confidence": confidence}))
        return self.command(utterance)

    def test_a_named_slot_from_speech_reaches_the_job(self):
        before = self.job_count
        status, payload = self.task(
            "A자재를 1번 팔레트에서 컨베이어 2번 위치에 놓아줘",
            source="loc_pallet_1", destination="slot_2")
        self.assertEqual((status, payload["decision"]), (200, "CONFIRM"))
        self.assertEqual(payload["slot"], "slot_2")
        self.assertEqual(self.job_count, before, "확인 전에 작업이 생겼다")
        readiness = payload["confirmation"]["evidence"]["readiness"]
        self.assertEqual(readiness["source_resource"], "loc_pallet_1")
        self.assertEqual(readiness["destination_resource"], "slot_2")
        self.assertEqual(readiness["origin"], "1번 팔레트")
        self.assertEqual(readiness["target"], "컨베이어 2번 위치")
        # 확인을 눌러야 **그제서야** 기존 시연 작업이 만들어진다.
        status, done = self.confirm(payload["confirmation"]["token"])
        self.assertEqual((status, done["decision"]), (202, "RUN"))
        self.assertEqual(self.job_count, before + 1)
        argv = self.popen.calls[-1]["argv"]
        self.assertEqual(argv[argv.index("--slot") + 1], "slot_2")
        # 좌표·관절값은 명령줄 어디에도 없다.
        for banned in ("--joint", "--pose", "--xyz"):
            self.assertNotIn(banned, argv)

    def test_every_verified_slot_can_be_named_from_speech(self):
        for name in ("slot_1", "slot_2", "slot_3"):
            with self.subTest(slot=name):
                status, payload = self.task("저 자리에 올려줘", destination=name)
                self.assertEqual(payload["decision"], "CONFIRM")
                self.assertEqual(payload["slot"], name)
                self.confirm(payload["confirmation"]["token"], "cancel")

    def test_an_occupied_slot_plans_the_prerequisite_and_makes_no_job(self):
        """2026-09-25: 점유된 목적지는 비우는 선행 작업을 계획해 확인 카드로 보인다
        (예전: BLOCK). "B를"은 B자재로 읽는다(해석 근거에 남는다)."""
        from server.sim_demo_goals import SimDemoGoals
        self.runtime.sim_demo_goals = SimDemoGoals(self.jobs, auto_run=False)
        self.hold("material_a", "slot_1")
        before = self.job_count
        status, payload = self.task("B를 컨베이어 1번 위치에 놔줘",
                                    material="material_b", destination="slot_1")
        self.assertEqual((status, payload["decision"]), (200, "CONFIRM_GOAL"))
        self.assertEqual([(s["action"], s["material"]) for s in payload["goal"]["plan"]],
                         [("return", "material_a"), ("transfer", "material_b")])
        self.assertTrue(payload["interpretation"]["evidence"])
        self.assertEqual(self.job_count, before)

    def test_a_place_this_cell_does_not_have_is_refused(self):
        """스키마 열거를 빠져나온 값도 서버가 다시 막는다."""
        before = self.job_count
        status, payload = self.task("저 작업대 자리에 올려줘",
                                    destination="loc_workbench")
        self.assertIn(payload["decision"], ("ASK", "BLOCK"))
        self.assertIsNone(payload["confirmation"])
        self.assertEqual(self.job_count, before)

    def test_the_safe_pose_is_not_a_material_destination(self):
        before = self.job_count
        status, payload = self.task("A자재를 안전 위치에 놔줘",
                                    destination="loc_safe_home")
        self.assertEqual(payload["decision"], "BLOCK")
        self.assertIn("안전 위치", payload["reason"])
        self.assertEqual(self.job_count, before)

    def test_a_wrong_source_pallet_is_blocked(self):
        before = self.job_count
        status, payload = self.task("A자재를 3번 팔레트에서 컨베이어로 옮겨줘",
                                    source="loc_pallet_3",
                                    destination="loc_conveyor")
        self.assertEqual(payload["decision"], "BLOCK")
        self.assertIn("3번 팔레트", payload["reason"])
        self.assertEqual(self.job_count, before)

    def test_return_from_the_conveyor_to_the_origin_pallet(self):
        self.hold("material_a", "slot_2")
        # 규칙이 못 푸는 말이라 분류기로 간다("돌려놔" 같은 확정 낱말이 없다).
        status, payload = self.task("컨베이어 2번 위치의 A자재 회수해줘",
                                    action="return", source="slot_2",
                                    destination="loc_pallet_1")
        self.assertEqual(payload["decision"], "CONFIRM")
        readiness = payload["confirmation"]["evidence"]["readiness"]
        self.assertEqual(readiness["target"], "1번 팔레트")
        status, done = self.confirm(payload["confirmation"]["token"])
        self.assertEqual(done["decision"], "RUN")
        self.assertIn("--return-held-to-origin", self.popen.calls[-1]["argv"])

    def test_an_unsure_reading_never_becomes_a_job(self):
        before = self.job_count
        status, payload = self.task("A자재 좀 어떻게 해줘", destination=None,
                                    confidence=0.4)
        self.assertEqual((status, payload["decision"]), (200, "ASK"))
        self.assertIsNone(payload["confirmation"])
        self.assertEqual(self.job_count, before)

    def test_a_destination_the_speaker_never_named_is_asked_about(self):
        """모델도 발화도 목적지를 말하지 않았다 — 서버가 골라 주지 않는다."""
        before = self.job_count
        status, payload = self.task("A자재 좀 저기로 치워줘", destination=None)
        self.assertEqual((status, payload["decision"]), (200, "ASK"))
        self.assertEqual(self.job_count, before)


class ReadinessTest(SlotAssignmentTest):
    """"안전 판단 및 실행" 카드가 읽는 실행 준비 근거.

    **확인된 것과 실행 직전에 다시 볼 것을 구분해서 적는지**가 핵심이다.
    검사하지 않은 것을 통과로 보여 주면 근거 없이 확인을 누르게 된다.
    """

    def ready_for(self, utterance="팔레트에 있는 물건 하나 컨베이어로 옮겨줘", material="material_a"):
        self.with_classifier(answer({"intent": "transfer",
                                     "material_id": material, "confidence": 0.95}))
        _, payload = self.command(utterance)
        self.assertEqual(payload["decision"], "CONFIRM")
        return payload["confirmation"]["evidence"]["readiness"]

    def test_readiness_names_material_route_and_slot(self):
        r = self.ready_for()
        self.assertEqual(r["action"], "transfer")
        self.assertEqual(r["material"], "material_a")
        self.assertEqual(r["material_korean"], "A자재")
        self.assertEqual(r["origin"], "원래 팔레트")
        self.assertEqual(r["target"], "컨베이어 1번 위치")
        self.assertEqual(r["slot"], "slot_1")
        self.assertEqual(r["status"], "ready")
        self.assertEqual(r["status_label"], "실행 준비됨")

    def test_checks_separate_verified_from_recheck(self):
        keys = {c["key"]: c for c in self.ready_for()["checks"]}
        self.assertEqual(sorted(keys), ["geometry", "occupancy", "scene"])
        # geometry는 MoveIt으로 **이미 검증된 값**이다.
        self.assertIs(keys["geometry"]["ok"], True)
        self.assertIs(keys["geometry"]["recheck"], False)
        # 점유는 지금 확인했고, 실행 직전에 또 본다.
        self.assertIs(keys["occupancy"]["ok"], True)
        self.assertIs(keys["occupancy"]["recheck"], True)
        # scene은 **아직 보지 않았다** — 통과로 적지 않는다.
        self.assertIsNone(keys["scene"]["ok"])
        self.assertIs(keys["scene"]["recheck"], True)

    def test_return_reverses_the_route_and_keeps_the_slot(self):
        """분류기를 타는 복귀 발화. ("돌려놔"는 규칙이 바로 알아들어 확인 없이
        실행되는 기존 경로다 — 그쪽은 확인 카드를 만들지 않는다.)"""
        self.hold("material_b", "slot_2")
        self.with_classifier(answer({"intent": "return",
                                     "material_id": "material_b", "confidence": 0.95}))
        _, payload = self.command("파란 블록 팔레트로 갖다놔")
        self.assertEqual(payload["decision"], "CONFIRM")
        r = payload["confirmation"]["evidence"]["readiness"]
        self.assertEqual(r["action"], "return")
        self.assertEqual(r["origin"], "컨베이어 2번 위치")
        self.assertEqual(r["target"], "원래 팔레트")
        self.assertEqual(r["slot"], "slot_2")

    def test_readiness_does_not_create_a_job(self):
        before = self.job_count
        self.ready_for()
        self.assertEqual(self.job_count, before, "준비 표시가 작업을 만들었다")

    def test_full_conveyor_blocks_without_readiness(self):
        for model, slot in (("material_a", "slot_1"), ("material_b", "slot_2"),
                            ("material_c", "slot_3")):
            self.hold(model, slot)
        self.with_classifier(answer({"intent": "transfer",
                                     "material_id": "material_a", "confidence": 0.95}))
        _, payload = self.command("팔레트에 있는 물건 하나 컨베이어로 옮겨줘")
        self.assertEqual(payload["decision"], "BLOCK")
        self.assertIsNone(payload["confirmation"])
        self.assertIn("모두 찼습니다", payload["reason"])
        self.assertEqual(self.job_count, 0)


class DestinationSubstitutionTest(SlotAssignmentTest):
    """분류기는 **자재만** 정한다. 목적지를 대신 골라 주지 않는다.

    실측 결함: "1번 팔레트에서 A자재를 집어서 **작업대**에 올려줘"가
    "A자재를 **컨베이어 1번 위치**로 옮기겠습니다"로 바뀌어 확인 카드가 떴다.
    목적지가 조용히 치환된 것이다 — 지원 밖 작업은 ASK로 끝나야 한다.
    """

    def classified_transfer(self, material="material_a", confidence=0.95):
        self.with_classifier(answer({"intent": "transfer",
                                     "material_id": material,
                                     "confidence": confidence}))

    def test_unsupported_destination_asks_with_guidance(self):
        self.classified_transfer()
        _, payload = self.command("1번 팔레트에서 A자재를 집어서 작업대에 올려줘")
        self.assertEqual(payload["decision"], "ASK")
        self.assertIsNone(payload["confirmation"])
        self.assertIn("A/B/C 자재와 대상 위치를 지정하세요", payload["reason"])
        self.assertIn("팔레트↔컨베이어", payload["reason"])
        self.assertEqual(self.job_count, 0)

    def test_transfer_needs_the_declared_conveyor_name(self):
        """transfer는 컨베이어로만 간다 — 발화가 컨베이어를 말해야 받는다.

        컨베이어를 말하지 않은 발화의 결말은 둘 중 하나다. 분류기까지 온 것은
        ASK, 자재 작업처럼 보이지도 않은 것은 PASS_THROUGH(기존 계획 생성).
        **어느 쪽도 확인 카드를 만들지 않는다.**
        """
        # "A자재를 2번 팔레트로"는 2026-09-25부터 팔레트 이송(계획기)이라 여기서 뺐다.
        for utterance in ("주황색 거 저기에 올려줘",
                          "A자재를 작업대로 갖다놔"):
            with self.subTest(utterance=utterance):
                self.classified_transfer()
                _, payload = self.command(utterance)
                self.assertIn(payload["decision"], ("ASK", "PASS_THROUGH"),
                              payload.get("reason"))
                self.assertIsNone(payload["confirmation"])
                self.assertIsNone(payload["job"])
        self.assertEqual(self.job_count, 0)

    def test_naming_the_conveyor_still_works(self):
        """검증된 발화는 그대로 통과한다 — 회귀 방지."""
        for utterance in ("주황색 거 컨베이어에 올려줘",
                          "팔레트에 있는 물건 하나 컨베이어로 옮겨줘",
                          "아직 안 옮긴 주황 물건 컨베이어 쪽에 갖다 놔줘"):
            with self.subTest(utterance=utterance):
                self.classified_transfer()
                _, payload = self.command(utterance)
                self.assertEqual(payload["decision"], "CONFIRM", payload.get("reason"))
                self.assertEqual(payload["confirmation"]["evidence"]["slot"], "slot_1")
        self.assertEqual(self.job_count, 0)

    def test_wrong_pallet_is_checked_on_the_classifier_path_too(self):
        """규칙 경로에만 있던 "엉뚱한 팔레트" 검사가 분류기 경로에도 걸린다."""
        self.hold("material_a", "slot_1")
        self.with_classifier(answer({"intent": "return",
                                     "material_id": "material_a",
                                     "confidence": 0.95}))
        # A자재의 원래 자리는 1번 팔레트다. 2번을 부르면 받아 주지 않는다.
        _, payload = self.command("주황 블록 2번 팔레트에 갖다놔")
        self.assertIn(payload["decision"], ("BLOCK", "ASK"))
        self.assertIsNone(payload["confirmation"])
        self.assertEqual(self.job_count, 0)

    def test_declared_vocabulary_only(self):
        """컨베이어 이름은 셀 설정 선언에서만 온다 — 코드가 만들지 않는다."""
        from server.sim_demo_commands import conveyor_names, mentions_conveyor

        self.assertEqual(conveyor_names(WORKCELL), ("컨베이어",))
        self.assertTrue(mentions_conveyor("컨베이어에 올려", WORKCELL))
        self.assertFalse(mentions_conveyor("작업대에 올려", WORKCELL))
        # 선언이 없으면 아무 이름도 만들어 내지 않는다.
        self.assertEqual(conveyor_names({}), ())
        self.assertFalse(mentions_conveyor("컨베이어에 올려", {}))
