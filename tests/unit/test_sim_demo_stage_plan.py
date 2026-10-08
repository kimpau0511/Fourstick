"""시뮬레이션 진행 표시용 단계 계획 줄(2026-10-08) — 실행기가 남기고 서버가 읽는다(읽기 전용).

지키는 것: 단계 **완료**는 진행 줄(도달=)만 근거다. 단계 계획 줄은 이름표일 뿐이고, 진행 줄로 읽히지 않는다.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.sim_demo_jobs import STAGE_PLAN_PREFIX, parse_progress, parse_stage_plan  # noqa: E402

SCRIPT = ROOT / "scripts" / "demo_workcell_pick_place.py"


def plan_line(labels):
    rows = [{"no": i, "label": label} for i, label in enumerate(labels, 1)]
    return f"{STAGE_PLAN_PREFIX} " + json.dumps({"of": len(rows), "stages": rows}, ensure_ascii=False)


class StagePlanTest(unittest.TestCase):
    def test_executor_and_server_use_the_same_prefix(self):
        text = SCRIPT.read_text(encoding="utf-8")
        hit = re.search(r'^STAGE_PLAN_PREFIX = "([^"]+)"', text, re.MULTILINE)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.group(1), STAGE_PLAN_PREFIX)
        # 세 실행 경로(이송·든 자재 경로·재개) 모두 단계 계획을 남긴다.
        self.assertEqual(text.count("print_stage_plan("), 4)          # 정의 1 + 호출 3

    def test_plan_line_is_parsed_and_last_one_wins(self):
        console = "\n".join(["[INFO] start", plan_line(["안전 home", "팔레트 접근"]),
                             "  [ 1/2] 안전 home         오차 0.00100 rad · 도달=True",
                             plan_line(["복구 접근", "lift", "안전 home 복귀"])])
        self.assertEqual(parse_stage_plan(console),
                         {"of": 3, "stages": [{"no": 1, "label": "복구 접근"}, {"no": 2, "label": "lift"},
                                              {"no": 3, "label": "안전 home 복귀"}]})

    def test_plan_line_is_not_progress(self):
        console = plan_line(["안전 home", "팔레트 접근"])
        self.assertEqual(parse_progress(console), [])                   # 계획만으로는 완료된 단계가 없다

    def test_missing_or_broken_plan_is_none(self):
        self.assertIsNone(parse_stage_plan(""))
        self.assertIsNone(parse_stage_plan(f"{STAGE_PLAN_PREFIX} {{not json"))
        self.assertIsNone(parse_stage_plan(f'{STAGE_PLAN_PREFIX} {{"of": 3, "stages": [{{"no": 1, "label": "a"}}]}}'))

    def test_executor_helper_prints_the_line(self):
        import contextlib
        import io

        text = SCRIPT.read_text(encoding="utf-8")
        start = text.index("def print_stage_plan(")
        end = text.index("\ndef ", start + 1)
        scope = {"json": json, "STAGE_PLAN_PREFIX": STAGE_PLAN_PREFIX}
        exec(text[start:end], scope)  # noqa: S102 — 실행기 전체(ROS 의존)를 불러오지 않고 이 함수만 본다
        stages = [SimpleNamespace(no=0, label="복구 접근(현재 → 정지 단계 목표)"), SimpleNamespace(no=9, label="place 접근")]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            scope["print_stage_plan"](stages, numbered=True)
        self.assertEqual(parse_stage_plan(out.getvalue())["stages"],
                         [{"no": 1, "label": "복구 접근(현재 → 정지 단계 목표)"}, {"no": 2, "label": "place 접근"}])


if __name__ == "__main__":
    unittest.main()
