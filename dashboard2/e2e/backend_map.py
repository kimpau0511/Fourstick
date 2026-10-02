"""백엔드 코드를 고치지 않고 실행만 한다, 요구사항 추적표(문서/QA_요구사항_추적표.md)의 BE 항목용.

요구사항 ID -> 백엔드 unittest 모듈 대응표를 실행해 마크다운 표로 보고한다.
사용: python dashboard2/e2e/backend_map.py [출력파일.md]
"""
import io
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

MAP = {
    "SFR-001": ["unit.test_stt_session", "unit.test_stt_protocol", "unit.test_stt_command_normalization", "unit.test_stt_runtime_budget", "unit.test_stt_measurement"],
    "SFR-002": ["unit.test_slot_extractor", "unit.test_sim_demo_commands"],
    "SFR-003": ["unit.test_material_color_names", "unit.test_material_colors", "unit.test_sim_demo_natural_place", "unit.test_place_intent", "unit.test_sim_demo_context"],
    "SFR-004": ["unit.test_prompt_generation", "unit.test_plan_provider_contract", "unit.test_vllm_provider", "integration.test_planning_flow"],
    "SFR-005": ["unit.test_schema_conformance", "unit.test_boundary_schemas", "unit.test_examples"],
    "SFR-006": ["unit.test_safety_validator", "unit.test_request_plan_consistency", "unit.test_capability_precheck"],
    "SFR-007": ["unit.test_geometry_check", "unit.test_moveit_geometry", "unit.test_scene_sync"],
    "SFR-008": ["unit.test_execution_permit", "integration.test_session_isolation", "integration.test_planning_to_permit", "integration.test_validation_gate"],
    "SFR-009": ["contract.test_stop_contract", "unit.test_api_stop_order", "unit.test_api_stop_release", "unit.test_demo_external_stop", "unit.test_stop_diagnostics"],
    "SFR-010": ["unit.test_public_payload", "integration.test_web_api"],
    "SFR-011": ["integration.test_storage_roundtrip", "integration.test_session_clients"],
    "SFR-013": ["contract.test_adapter_conformance", "contract.test_fake_adapter", "unit.test_fr3_gazebo_adapter", "unit.test_hardware_adapters"],
    "SFR-014": ["integration.test_stage7_conditions"],
    "NFR-006": ["integration.test_storage_roundtrip", "integration.test_validation_gate"],
    "TER-001": ["unit.test_safety_validator"],
    "TER-002": ["unit.test_schema_conformance", "unit.test_boundary_schemas"],
    "TER-004": ["contract.test_stop_contract"],
    "TER-006": ["integration.test_stage7_conditions"],
}


def flat(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from flat(t)
        else:
            yield t


def run(req, mods):
    loader = unittest.TestLoader()
    suite, missing = unittest.TestSuite(), 0
    for m in mods:
        t = loader.loadTestsFromName(m)
        fl = [x for x in flat(t) if type(x).__name__ == "_FailedTest"]
        # 해당 모듈 자체가 없는 경우만 "모듈 없음"(그 안의 의존 모듈 누락은 오류로 센다)
        if fl and f"No module named '{m}'" in str(getattr(fl[0], "_exception", "")):
            missing += 1
            continue
        suite.addTests(t)
    res = unittest.TextTestRunner(stream=io.StringIO(), buffer=True, verbosity=0).run(suite)
    return res, missing, 0


def main():
    rows = ["| 요구사항 | 모듈 수 | 테스트 수 | 통과 | 실패 | 오류 | 건너뜀 | 판정 | 실패한 테스트(최대 3개) |",
            "|---|---|---|---|---|---|---|---|---|"]
    for req, mods in MAP.items():
        res, missing, _ = run(req, mods)
        n, f, e, s = res.testsRun, len(res.failures), len(res.errors), len(res.skipped)
        bad = [t.id() if hasattr(t, "id") else str(t) for t, _ in res.failures + res.errors][:3]
        if missing == len(mods):
            verdict = "모듈 없음"
        else:
            verdict = "통과" if f + e == 0 and n >= 1 else "실패"
        note = f"모듈 없음 {missing}개" if 0 < missing < len(mods) else ""
        rows.append(f"| {req} | {len(mods)} | {n} | {n - f - e - s} | {f} | {e} | {s} | {verdict} | "
                    + "<br>".join(x.replace("|", "/") for x in bad + ([note] if note else [])) + " |")
    out = "\n".join(rows)
    print(out)
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(out + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
