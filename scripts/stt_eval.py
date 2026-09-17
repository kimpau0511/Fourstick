"""실제 모델 측정 실행기. `scripts/run_stt_eval.sh`가 호출한다.

측정 정의 (md/STT_모델_구성.md):
- 발화 사례(정답 문장 있음) → WER·CER
- 비발화 사례(침묵·잡음·순음) → VAD 오탐 / transcript 발생 / final 요청 생성
  / 환각 발생률
- 속도는 RTF(처리÷음성)와 처리 배수(음성÷처리)를 함께 기록하고, 음성 길이·
  처리 시간·모델 최초 로딩 시간을 각각 남긴다.

요구정의서에 수치 기준이 없으므로 **합격 판정을 하지 않는다.**

  ./scripts/run_stt_eval.sh                            기본 Profile로 측정
  STT_PROFILE=turbo-auto-int8 ./scripts/run_stt_eval.sh  특정 Profile로 측정
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.loader import load_stt_profile_catalog
from core.policy import SttVerification
from stt.evaluation import EvalReport, evaluate_non_speech, evaluate_speech, load_cases
from stt.faster_whisper_backend import FasterWhisperBackend, TranscriberUnavailable
from stt.silero_vad_backend import SileroVadBackend, VadUnavailable

FIXTURE = ROOT / "fixtures" / "stt" / "eval_ko_robot_commands.jsonl"
CATALOG = ROOT / "examples" / "config" / "valid_stt_profiles.json"
#: final 확정 신뢰도 기준. 정책 파일에서 읽는다 — 여기 숫자를 두지 않는다.
POLICY = ROOT / "examples" / "config" / "valid_stt_policy.json"


def fmt(value, spec="{:.4f}") -> str:
    return "없음" if value is None else spec.format(value)


def main() -> int:
    catalog_path = Path(sys.argv[1]) if len(sys.argv) > 1 else CATALOG
    if not catalog_path.is_absolute():
        catalog_path = ROOT / catalog_path
    if not catalog_path.exists():
        print(f"Profile 목록을 찾을 수 없다: {catalog_path}", file=sys.stderr)
        return 2
    catalog = load_stt_profile_catalog(
        json.loads(catalog_path.read_text(encoding="utf-8"))
    )
    profile_id = os.environ.get("STT_PROFILE") or catalog.default_profile_id
    config = catalog.get(profile_id)

    from config.loader import load_stt_policy
    policy = load_stt_policy(json.loads(POLICY.read_text(encoding="utf-8")))

    print(f"Profile {config.profile_id} ({config.verification.value})")
    print(
        f"  모델 {config.model_name} / device {config.device} / "
        f"compute {config.compute_type} / lang {config.language} "
        f"(profile_version {config.config_version})"
    )
    if config.verification is not SttVerification.VERIFIED:
        print("  주의: 이 환경에서 아직 실행을 확인하지 않은 후보 구성이다.")
    print(f"  검증된 Profile {catalog.verified_ids()} / 후보 {catalog.candidate_ids()}")

    backend = FasterWhisperBackend(config)
    try:
        load = backend.load()
    except TranscriberUnavailable as exc:
        print(f"모델 로딩 실패: {exc}", file=sys.stderr)
        return 1
    print(f"\n모델 최초 로딩 {load.load_sec:.2f}s (사례별 처리 시간과 분리해 기록한다)")

    vad = SileroVadBackend(config)
    try:
        vad.load()
    except VadUnavailable as exc:
        print(f"VAD 로딩 실패 — VAD 오탐은 미측정으로 남는다: {exc}", file=sys.stderr)
        vad = None

    cases = load_cases(FIXTURE)
    report = EvalReport(
        model_load_sec=load.load_sec,
        speech=evaluate_speech(
            cases, backend, sample_rate_hz=config.sample_rate_hz, fixture_dir=FIXTURE.parent
        ),
        non_speech=evaluate_non_speech(
            cases, backend, vad, sample_rate_hz=config.sample_rate_hz,
            fixture_dir=FIXTURE.parent,
            min_final_confidence=policy.min_final_confidence,
        ),
    )

    # ── 발화 사례 ──────────────────────────────────────────────────────
    print(f"\n[발화 사례] 정답 문장이 있는 음성만 WER·CER로 본다 ({len(report.speech)}건)")
    print(f"{'id':<14}{'WER':>7}{'CER':>7}{'음성(s)':>9}{'처리(s)':>9}{'RTF':>7}{'배수':>7}  전사")
    for r in report.speech:
        if not r.measured:
            print(f"{r.case_id:<14}{'미측정':>7}  {r.skip.reason.value} — {r.skip.detail[:44]}")
            continue
        s = r.speed
        print(
            f"{r.case_id:<14}{r.wer:>7.3f}{r.cer:>7.3f}{s.audio_sec:>9.2f}"
            f"{s.process_sec:>9.2f}{s.rtf:>7.2f}{s.throughput:>7.3f}  {r.hypothesis!r}"
        )

    # ── 비발화 사례 ────────────────────────────────────────────────────
    print(f"\n[비발화 사례] 침묵·잡음·순음 ({len(report.non_speech)}건)")
    print(
        f"{'id':<14}{'태그':<8}{'VAD오탐':>8}{'VAD최대':>8}{'전사발생':>9}"
        f"{'final생성':>10}{'처리(s)':>9}{'RTF':>7}  환각 문장"
    )
    for r in report.non_speech:
        if not r.measured:
            print(f"{r.case_id:<14}{'미측정':>8}  {r.skip.reason.value} — {r.skip.detail[:40]}")
            continue
        def yn(v):
            return "미측정" if v is None else ("예" if v else "아니오")
        s = r.speed
        print(
            f"{r.case_id:<14}{','.join(r.tags):<8}{yn(r.vad_false_positive):>8}"
            f"{fmt(r.vad_max_probability, '{:.3f}'):>8}{yn(r.transcript_emitted):>9}"
            f"{yn(r.final_request_created):>10}{s.process_sec:>9.2f}{s.rtf:>7.2f}"
            f"  {r.hallucinated_text!r}"
        )

    d = report.to_dict()
    print("\n[요약]  (실행 경로: evaluation — Transcriber 직접 호출, 운영 경로 아님)")
    print(f"  모델 최초 로딩(s)      {fmt(d['model_load_sec'], '{:.2f}')}")
    print(f"  발화 측정/미측정       {d['speech']['measured']} / {d['speech']['skipped']}")
    print(f"  발화 평균 WER          {fmt(d['speech']['mean_wer'])}")
    print(f"  발화 평균 CER          {fmt(d['speech']['mean_cer'])}")
    print(f"  비발화 VAD 오탐률      {fmt(d['non_speech']['vad_false_positive_rate'])}")
    print(f"  비발화 환각 발생률     {fmt(d['non_speech']['hallucination_rate'])}")
    print(f"  비발화 final 생성률    {fmt(d['non_speech']['final_request_rate'])}")
    for label, block in (("발화", d["speech"]), ("비발화", d["non_speech"])):
        speed = block["speed"]
        if speed is None:
            print(f"  {label} 속도            없음")
            continue
        rep = speed["representative"]
        print(
            f"  {label} 대표 속도       RTF {rep['rtf']:.2f} / 처리 배수 "
            f"{rep['throughput']:.3f}  (전체 합계: 음성 "
            f"{rep['audio_sec_total']:.2f}s / 처리 {rep['process_sec_total']:.2f}s)"
        )
        if rep["rtf"] > 1.0:
            print("    -> RTF > 1 이므로 이 구성은 실시간보다 느리다.")
        dist = speed["per_case_reference"]["rtf_distribution"]
        mean = speed["per_case_reference"]["mean"]
        print(
            f"  {label} 사례별 RTF 분포 n={dist['n']} min {dist['min']:.2f} / "
            f"median {dist['median']:.2f} / p90 {dist['p90']:.2f} / "
            f"max {dist['max']:.2f}"
        )
        print(
            f"    (참고: 사례별 RTF 평균 {mean['rtf']:.2f} — 대표값으로 쓰지"
            " 않는다. 역수의 평균은 평균의 역수가 아니다)"
        )

    print(
        "\n요구정의서에 WER·CER·지연 수치 기준이 없다. 합격 판정을 하지 않고 "
        "측정값만 기록한다."
    )

    out = ROOT / "reports" / f"stt_eval_{config.profile_id}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "profile": {
                    "profile_id": config.profile_id,
                    "profile_version": config.config_version,
                    "verification": config.verification.value,
                    "model_name": config.model_name,
                    "device": config.device,
                    "compute_type": config.compute_type,
                    "language": config.language,
                    "sample_rate_hz": config.sample_rate_hz,
                    "vad_threshold": config.vad_threshold,
                },
                "catalog": {
                    "catalog_version": catalog.catalog_version,
                    "default_profile_id": catalog.default_profile_id,
                    "verified": list(catalog.verified_ids()),
                    "candidate": list(catalog.candidate_ids()),
                },
                "min_final_confidence": policy.min_final_confidence,
                **d,
            },
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"결과 저장: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
