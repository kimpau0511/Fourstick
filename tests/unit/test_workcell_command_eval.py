"""작업 셀 명령 평가 (8-13) — 평가셋과 채점 규칙.

봉인 발화를 이 파일에 적지 않는다. 파일에서 읽어 검사만 한다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.loader import load_resource_catalog  # noqa: E402
from core.resource_catalog import ResourceKind  # noqa: E402
from core.termination import TerminationRequirement  # noqa: E402
from planning import workcell_command_eval as ev  # noqa: E402
from planning.output_parser import ClarificationNeeded  # noqa: E402
from planning.plan_provider import PlanningContext  # noqa: E402
from planning.slot_extractor import extract_slots  # noqa: E402

FIXTURES = ROOT / "fixtures/workcell_eval"
MANIFEST = ROOT / "config/workcell/active.json"
STOP_KEYWORDS = tuple(json.loads(
    (ROOT / "examples/config/valid_stt_policy.json").read_text(encoding="utf-8"))["stop_keywords"])


def _catalog():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return load_resource_catalog(json.loads(
        (MANIFEST.parent / manifest["resource_catalog"]).read_text(encoding="utf-8")))


def _holdout2():
    scenarios = ev.load_scenarios(FIXTURES / "scenarios.json")
    out = []
    for name in ("sealed2_set.jsonl", "sealed3_set.jsonl"):
        path = FIXTURES / name
        if path.is_file():
            out += list(ev.load_set(path, scenarios, "sealed"))
    return out


def _sets():
    scenarios = ev.load_scenarios(FIXTURES / "scenarios.json")
    dev = ev.load_set(FIXTURES / "dev_set.jsonl", scenarios, "dev")
    sealed = ev.load_set(FIXTURES / "sealed_set.jsonl", scenarios, "sealed")
    return scenarios, dev, sealed


def _step(skill, **args):
    return ev.ExpectedStep(skill=skill, args=args)


def _expect(**over):
    base = dict(intent="move", resources=("loc_pallet_1",),
                steps=(_step("move", to="loc_pallet_1"), _step("home")), core_steps=None,
                decisions=("PASS",), reason_codes=(), executable=True, stop_bypass=False,
                plan_validation=None, terminal_hold=None)
    base.update(over)
    return ev.Expectation(**base)


def _case(expect, utterance="x", scenario="S02", category="팔레트 접근 move"):
    return ev.EvalUtterance(id="t_001", scenario=scenario, category=category, split="dev",
                            utterance=utterance, expect=expect)


def _obs(**over):
    base = dict(ok=True, bypassed_model=False, decision="PASS", reason_code=None,
                executable=True, slots=("loc_pallet_1",),
                steps=(_step("move", to="loc_pallet_1"), _step("home")), terminal_hold=None,
                clarification=None, consistency=None, plan_validation=None,
                geometry_decision="allow", planning_ms=100.0, gate_ms=10.0, total_ms=120.0,
                provider_id="vllm", model_id="m", served_model_id="m", is_mock=False,
                plan_hash="h")
    base.update(over)
    return ev.Observation(**base)


class TestDataset(unittest.TestCase):
    def test_dataset_has_no_defects(self):
        scenarios, dev, sealed = _sets()
        catalog = _catalog()
        problems = ev.validate_dataset(scenarios, dev, sealed,
                                       catalog_ids={e.resource_id for e in catalog.entries})
        self.assertEqual(problems, [])
        self.assertGreaterEqual(len(scenarios), 30)
        self.assertEqual(len(dev) + len(sealed), 200)

    def test_required_categories_present_in_both_splits(self):
        _, dev, sealed = _sets()
        required = ("home", "팔레트 접근 move", "컨베이어 접근 move", "STOP", "정상 자원 인식",
                    "자재·팔레트 불일치", "존재하지 않는 자원", "모호한 발화",
                    "복수 자재·복수 위치", "pick/place 실행 차단", "resource mismatch",
                    "STOP 키워드 변형")
        for rows in (dev, sealed):
            present = {r.category for r in rows}
            for name in required:
                self.assertIn(name, present)

    def test_resource_ids_match_active_workcell_config(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cell = json.loads((MANIFEST.parent / manifest["workcell_config"]).read_text(encoding="utf-8"))
        cell_ids = {row["resource_id"] for row in cell["resource_map"]}
        _, dev, sealed = _sets()
        for row in dev + sealed:
            self.assertTrue(set(row.expect.resources) <= cell_ids, row.id)
            for steps in (row.expect.steps, row.expect.core_steps):
                for step in steps or ():
                    for value in step.args.values():
                        self.assertIn(value, cell_ids, row.id)

    def test_expected_resources_equal_catalog_extraction(self):
        """자원 추출은 결정적이다. 정답이 카탈로그와 어긋나면 평가셋 결함이다."""
        catalog = _catalog()
        _, dev, sealed = _sets()
        for row in dev + sealed:
            got = extract_slots(row.utterance, catalog, stop_keywords=STOP_KEYWORDS)
            self.assertEqual({m.resource_id for m in got.matches}, set(row.expect.resources), row.id)
            self.assertEqual(got.stop_keyword_hit, row.expect.stop_bypass, row.id)

    def test_sealed_set_not_in_dev_prompt_catalog_tests_or_ui(self):
        _, dev, sealed = _sets()
        holdouts = list(sealed) + _holdout2()
        dev_text = {"".join(r.utterance.split()) for r in dev}
        for row in holdouts:
            self.assertNotIn("".join(row.utterance.split()), dev_text, row.id)
        roots = [ROOT / p for p in ("planning", "tests", "scripts", "html", "examples",
                                    "config", "fixtures", "md", "server", "core", "validation")]
        hits = ev.leak_scan(
            holdouts, roots,
            exclude=[(FIXTURES / "sealed_set.jsonl").resolve(),
                     (FIXTURES / "sealed2_set.jsonl").resolve(),
                     (FIXTURES / "sealed3_set.jsonl").resolve()])
        self.assertEqual(hits, [])


class TestDecisionAndIntent(unittest.TestCase):
    def test_observed_decision_mapping(self):
        self.assertEqual(ev.observed_decision({"ok": True, "validation": {"decision": "allow"}},
                                              bypassed=False), "PASS")
        self.assertEqual(ev.observed_decision({"ok": True, "validation": {"decision": "ask"}},
                                              bypassed=False), "ASK")
        self.assertEqual(ev.observed_decision({"ok": True, "validation": {"decision": "block"}},
                                              bypassed=False), "BLOCK")
        self.assertEqual(ev.observed_decision(
            {"ok": False, "reason_code": "plan.clarification_required"}, bypassed=False), "ASK")
        self.assertEqual(ev.observed_decision(
            {"ok": False, "reason_code": "capability.profile_incomplete"}, bypassed=False), "BLOCK")
        self.assertEqual(ev.observed_decision({"ok": True}, bypassed=True), "STOP")

    def test_derive_intent(self):
        self.assertEqual(ev.derive_intent(_obs(bypassed_model=True)), "stop")
        self.assertEqual(ev.derive_intent(_obs()), "move")
        self.assertEqual(ev.derive_intent(_obs(steps=(_step("home"),))), "home")
        self.assertEqual(ev.derive_intent(_obs(steps=(
            _step("move", to="loc_pallet_1"),
            _step("pick", object="mat_a", **{"from": "loc_pallet_1"}),
            _step("move", to="loc_conveyor"),
            _step("place", object="mat_a", to="loc_conveyor")))), "transfer")
        self.assertEqual(ev.derive_intent(_obs(steps=(
            _step("move", to="loc_pallet_1"),
            _step("pick", object="mat_a", **{"from": "loc_pallet_1"})), terminal_hold="mat_a")), "hold")
        self.assertEqual(ev.derive_intent(_obs(
            ok=False, steps=(), reason_code="plan.clarification_required")), "clarify")

    def test_observation_from_failed_payload_reads_draft_and_slots(self):
        payload = {
            "ok": False, "reason_code": "capability.profile_incomplete", "detail": "x",
            "slots": {"matches": [{"resource_id": "loc_pallet_1"}, {"resource_id": "mat_a"},
                                  {"resource_id": "loc_conveyor"}, {"resource_id": "loc_pallet_1"}]},
            "draft_steps": [{"no": 1, "skill": "move", "args": {"to": "loc_pallet_1"}},
                            {"no": 2, "skill": "pick", "args": {"object": "mat_a", "from": "loc_pallet_1"}}],
            "plan_validation": {"available": True, "plan_verified": False,
                                "execution_allowed": False, "reason_codes": ["plan.slot_incomplete"]},
        }
        obs = ev.observation_from_payload(payload, bypassed=False, planning_ms=1.0,
                                          gate_ms=None, total_ms=2.0, attempt=None)
        self.assertEqual(obs.decision, "BLOCK")
        self.assertEqual(obs.slots, ("loc_pallet_1", "mat_a", "loc_conveyor"))
        self.assertEqual(len(obs.steps), 2)
        self.assertFalse(obs.executable)
        self.assertFalse(obs.mismatch_observed)


    def test_observation_from_success_payload_reads_slots_from_consistency(self):
        payload = {
            "ok": True, "executable": True,
            "plan": {"plan_hash": "h", "terminal_hold": None,
                     "steps": [{"index": 1, "skill": "move", "args": {"to": "loc_pallet_1"}},
                               {"index": 2, "skill": "home", "args": {}}]},
            "consistency": {"status": "consistent", "only_in_plan": [],
                            "request_resources": [
                                {"surface": "1번팔레트", "resource_id": "loc_pallet_1", "kind": "location"},
                                {"surface": "일번팔레트", "resource_id": "loc_pallet_1", "kind": "location"}]},
            "validation": {"decision": "allow", "reason_code": None, "detail": "",
                           "geometry": {"decision": "allow"}},
        }
        obs = ev.observation_from_payload(payload, bypassed=False, planning_ms=1.0,
                                          gate_ms=2.0, total_ms=3.0, attempt=None)
        self.assertEqual(obs.decision, "PASS")
        self.assertEqual(obs.slots, ("loc_pallet_1",))
        self.assertFalse(obs.mismatch_observed)
        self.assertEqual(obs.geometry_decision, "allow")
        self.assertTrue(obs.executable)


class TestScoring(unittest.TestCase):
    def test_pass_move_case_all_ok(self):
        r = ev.CaseResult(_case(_expect()), _obs())
        self.assertTrue(r.all_ok)
        self.assertEqual(r.problems(), [])

    def test_wrong_decision_and_reason_are_reported(self):
        r = ev.CaseResult(_case(_expect()), _obs(decision="BLOCK", reason_code="safety.sequence_invalid",
                                                 executable=False))
        self.assertFalse(r.decision_ok)
        self.assertFalse(r.reason_ok)
        self.assertFalse(r.executable_ok)
        self.assertFalse(r.all_ok)

    def test_clarify_counts_as_intent_ok_only_when_ask_allowed(self):
        clarify = _obs(ok=False, decision="ASK", reason_code="plan.clarification_required",
                       executable=False, steps=(), slots=())
        allowed = _case(_expect(intent="move", resources=(), steps=None,
                                decisions=("ASK", "BLOCK"),
                                reason_codes=("plan.clarification_required",), executable=False))
        self.assertTrue(ev.CaseResult(allowed, clarify).intent_ok)
        strict = _case(_expect())
        self.assertFalse(ev.CaseResult(strict, clarify).intent_ok)

    def test_executable_none_follows_observed_decision(self):
        e = _expect(decisions=("PASS", "ASK"), reason_codes=("plan.clarification_required",),
                    executable=None)
        self.assertTrue(ev.CaseResult(_case(e), _obs()).executable_ok)
        asked = _obs(ok=False, decision="ASK", reason_code="plan.clarification_required",
                     executable=False, steps=())
        self.assertTrue(ev.CaseResult(_case(e), asked).executable_ok)
        self.assertFalse(ev.CaseResult(_case(e), _obs(executable=False)).executable_ok)

    def test_mismatch_observed_and_blocked(self):
        obs = _obs(decision="BLOCK", reason_code="plan.resource_mismatch", executable=False,
                   slots=(), steps=(_step("move", to="loc_pallet_1"), _step("home")))
        r = ev.CaseResult(_case(_expect(resources=(), steps=None, decisions=("ASK", "BLOCK"),
                                        reason_codes=("plan.resource_mismatch",), executable=False)), obs)
        self.assertTrue(obs.mismatch_observed)
        self.assertTrue(r.mismatch_blocked)
        leaked = _obs(slots=(), steps=(_step("move", to="loc_pallet_1"), _step("home")))
        self.assertFalse(ev.CaseResult(_case(_expect()), leaked).mismatch_blocked)

    def test_pick_place_dual_scoring(self):
        steps = (_step("move", to="loc_pallet_1"),
                 _step("pick", object="mat_a", **{"from": "loc_pallet_1"}),
                 _step("move", to="loc_conveyor"),
                 _step("place", object="mat_a", to="loc_conveyor"))
        e = _expect(intent="transfer", resources=("loc_pallet_1", "mat_a", "loc_conveyor"),
                    steps=None, core_steps=steps[1:2] + steps[3:4], decisions=("BLOCK",),
                    reason_codes=("capability.profile_incomplete",), executable=False,
                    plan_validation="verified")
        verified = _obs(ok=False, decision="BLOCK", reason_code="capability.profile_incomplete",
                        executable=False, slots=("loc_pallet_1", "mat_a", "loc_conveyor"), steps=steps,
                        plan_validation={"available": True, "plan_verified": True,
                                         "execution_allowed": False})
        r = ev.CaseResult(_case(e), verified)
        self.assertTrue(r.structure_ok)
        self.assertTrue(r.plan_validation_ok)
        self.assertTrue(r.execution_block_ok)
        self.assertTrue(r.all_ok)
        unverified = _obs(ok=False, decision="BLOCK", reason_code="capability.profile_incomplete",
                          executable=False, slots=("loc_pallet_1", "mat_a", "loc_conveyor"), steps=steps,
                          plan_validation={"available": True, "plan_verified": False,
                                           "execution_allowed": False})
        self.assertFalse(ev.CaseResult(_case(e), unverified).plan_validation_ok)
        # 계획 검증이 통과해도 실행 가능이 되면 안 된다 — 별도 정답
        opened = _obs(ok=True, decision="PASS", executable=True,
                      slots=("loc_pallet_1", "mat_a", "loc_conveyor"), steps=steps,
                      plan_validation={"available": True, "plan_verified": True,
                                       "execution_allowed": True})
        self.assertFalse(ev.CaseResult(_case(e), opened).execution_block_ok)

    def test_no_draft_skips_plan_validation_scoring(self):
        e = _expect(intent="transfer", resources=("mat_a", "loc_conveyor"), steps=None,
                    decisions=("ASK", "BLOCK"), reason_codes=("plan.clarification_required",),
                    executable=False, plan_validation="not_verified")
        asked = _obs(ok=False, decision="ASK", reason_code="plan.clarification_required",
                     executable=False, steps=(), slots=("mat_a", "loc_conveyor"))
        r = ev.CaseResult(_case(e), asked)
        self.assertIsNone(r.plan_validation_ok)
        self.assertTrue(r.all_ok)

    def test_move_only_plan_is_not_scored_for_plan_validation(self):
        e = _expect(intent="transfer", resources=("loc_conveyor",), steps=None,
                    decisions=("ASK", "BLOCK"), reason_codes=("plan.clarification_required",),
                    executable=False, plan_validation="not_verified")
        moved = _obs(slots=("loc_conveyor",), steps=(_step("move", to="loc_conveyor"), _step("home")))
        r = ev.CaseResult(_case(e), moved)
        self.assertIsNone(r.plan_validation_ok)
        self.assertIsNone(r.execution_block_ok)
        self.assertFalse(r.decision_ok)

    def test_stop_case(self):
        e = _expect(intent="stop", resources=(), steps=None, decisions=("STOP",),
                    reason_codes=(), executable=False, stop_bypass=True)
        r = ev.CaseResult(_case(e), _obs(bypassed_model=True, decision="STOP", executable=False,
                                         slots=(), steps=(_step("stop"),)))
        self.assertTrue(r.all_ok)
        via_model = _obs(decision="PASS", steps=(_step("home"),), slots=())
        self.assertFalse(ev.CaseResult(_case(e), via_model).stop_ok)


class TestSummary(unittest.TestCase):
    def test_metrics_confusion_and_failures(self):
        s = ev.RunSummary(provider_label="t", is_mock=False, split="dev")
        s.add(ev.CaseResult(_case(_expect()), _obs()))
        s.add(ev.CaseResult(_case(_expect()), _obs(decision="ASK", reason_code="geometry.validator_unavailable",
                                                   executable=False)))
        m = s.metrics()
        self.assertEqual(m["case_count"], 2)
        self.assertAlmostEqual(m["decision_accuracy"], 0.5)
        self.assertEqual(s.confusion()["PASS"]["ASK"], 1)
        self.assertEqual(s.confusion()["PASS"]["PASS"], 1)
        fails = s.failures(repro_prefix="cmd")
        self.assertEqual(len(fails), 1)
        self.assertIn("--only t_001", fails[0]["repro"])
        self.assertIsNone(s.timings()["gazebo_execution_ms"])

    def test_repeat_agreement(self):
        a = [ev.CaseResult(_case(_expect()), _obs())]
        b = [ev.CaseResult(_case(_expect()), _obs(decision="ASK", reason_code="x", executable=False))]
        r = ev.repeat_agreement(a, b)
        self.assertEqual(r["compared"], 1)
        self.assertEqual(r["decision_agreement"], 0.0)
        self.assertEqual(r["plan_agreement"], 1.0)

    def test_markdown_marks_double_and_never_claims_hardware(self):
        s = ev.RunSummary(provider_label="double-rule", is_mock=True, split="dev")
        s.add(ev.CaseResult(_case(_expect()), _obs(is_mock=True)))
        report = {"run_id": "r", "versions": {"model_id": "m"},
                  "runs": [s.to_dict(repro_prefix="cmd")], "repeat": {}, "voice": None}
        text = ev.render_markdown(report)
        self.assertIn("test double", text)
        self.assertIn("실제 모델 성능으로 읽지 않는다", text)
        self.assertNotIn("실기 완료", text)
        self.assertNotIn("pick/place 지원 완료", text)
        self.assertIn("real_hardware_ready=false", text)


class TestDoubles(unittest.TestCase):
    def _context(self, utterance):
        catalog = _catalog()
        return PlanningContext(
            utterance=utterance,
            slots=extract_slots(utterance, catalog, stop_keywords=STOP_KEYWORDS),
            allowed_skills=("home", "move", "stop"),
            allowed_locations=catalog.ids_of_kind(ResourceKind.LOCATION),
            allowed_objects=catalog.ids_of_kind(ResourceKind.OBJECT),
            robot_id="r", profile_id="p", profile_version="1",
            catalog_version=catalog.catalog_version, schema_version="1",
        )

    def test_rule_double_is_mock_and_deterministic(self):
        term = TerminationRequirement(final_skill="home", max_steps=12, hold_possible=True,
                                      sources=("test",), conflicts=())
        p = ev.RuleDoubleProvider(termination=term)
        self.assertTrue(p.is_mock)
        a = p.generate(self._context("1번 팔레트 앞으로"))
        b = p.generate(self._context("1번 팔레트 앞으로"))
        self.assertTrue(a.is_mock)
        self.assertEqual([(s.skill, dict(s.args)) for s in a.steps],
                         [(s.skill, dict(s.args)) for s in b.steps])
        self.assertEqual([s.skill for s in a.steps], ["move", "home"])
        with self.assertRaises(ClarificationNeeded):
            p.generate(self._context("저쪽으로 가"))

    def test_substituting_double_uses_resource_absent_from_utterance(self):
        p = ev.SubstitutingDoubleProvider(termination=None)
        self.assertTrue(p.is_mock)
        ctx = self._context("저쪽으로 가")
        draft = p.generate(ctx)
        used = {v for s in draft.steps for v in s.args.values()}
        self.assertTrue(used)
        self.assertTrue(used.isdisjoint(set(ctx.slots.resource_ids)))


if __name__ == "__main__":
    unittest.main()
