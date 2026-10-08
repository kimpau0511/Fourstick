#!/usr/bin/env python3
"""음성 인식률 평가 실행(2026-10-08) — 평가 자료(manifest)의 녹음을 **이 프로세스에서 따로 불러온** STT로 전사하고
사람이 확인한 정답(또는 합성 대본)과 비교해 저장한다. 운영 STT(8092)에 요청하지 않는다.

    .venv/bin/python scripts/stt_recognition_eval.py <manifest.jsonl> [--purpose functional|official]
        [--model-config examples/config/valid_stt_model_turbo_gpu.json] [--store DIR]

- purpose=official: 실제 녹음 + 사람 확인 정답 + final 자료에만 허용(화면 인식률 후보가 된다).
- purpose=functional: 합성·개선용 자료(기능·회귀 시험). 화면 표시 대상이 아니다.
- 결과 파일: 원본 STT 결과(raw)·용어 후보정 결과(corrected, 철자 보정 `correct_stt_spelling`)·정답·정규화·사례별/종합
  계산·모델/설정 키·자료 출처(manifest 해시)·실행 환경.
- 오디오는 16 kHz mono PCM16 WAV(다른 형식이면 ffmpeg로 바꿔 읽는다).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stt.command_normalization import SPELLING_VERSION, correct_stt_spelling  # noqa: E402
from stt.recognition_rate import NORMALIZATION_VERSION, aggregate, score_case  # noqa: E402
from stt.recognition_store import file_sha256, load_manifest, model_key, save_result  # noqa: E402


def read_audio(path: Path, rate: int) -> bytes:
    try:
        with wave.open(str(path)) as w:
            if (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, rate, 2):
                return w.readframes(w.getnframes())
    except wave.Error:
        pass
    out = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"],
                         check=True, capture_output=True)
    return out.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--purpose", default="functional", choices=("functional", "official"))
    ap.add_argument("--model-config", default="examples/config/valid_stt_model_turbo_gpu.json")
    ap.add_argument("--store", default=None)
    args = ap.parse_args()

    manifest = Path(args.manifest).resolve()
    rows = load_manifest(manifest)
    kinds = {(r["source_type"], r["split"]) for r in rows}
    source_type, split = next(iter(kinds))
    if args.purpose == "official" and (source_type, split) != ("real", "final"):
        sys.exit("official 평가는 실제 녹음(real)·최종(final) 자료에만 쓴다 — 합성·개선용 자료는 functional")

    from config.loader import load_stt_model_config
    from stt.faster_whisper_backend import FasterWhisperBackend

    cfg_path = ROOT / args.model_config
    cfg = load_stt_model_config(json.loads(cfg_path.read_text(encoding="utf-8")))
    t0 = time.time()
    backend = FasterWhisperBackend(cfg)
    load_sec = None
    cases = []
    for r in rows:
        audio = read_audio(manifest.parent / r["audio"], cfg.sample_rate_hz)
        t1 = time.time()
        tr = backend.transcribe(audio, cfg.sample_rate_hz)
        if load_sec is None:
            load_sec = round(t1 - t0, 2)
        sec = round(time.time() - t1, 3)
        hyp = tr.text
        corrected, changes = correct_stt_spelling(hyp)
        ref_source = r.get("reference_source")
        case = {"id": r["id"], "audio": r["audio"], "reference": r.get("reference"), "reference_source": ref_source,
                "reference_verified_by": r.get("reference_verified_by"), "tags": r.get("tags"), "noise": r.get("noise"),
                "hypothesis_raw": hyp, "hypothesis_corrected": corrected, "corrections": changes,
                "stt_confidence": tr.confidence, "audio_sec": round(len(audio) / 2 / cfg.sample_rate_hz, 3), "process_sec": sec,
                "raw": score_case(r.get("reference"), hyp, reference_source=ref_source),
                "corrected": score_case(r.get("reference"), corrected, reference_source=ref_source)}
        cases.append(case)
        c = case["raw"]["counts"]
        print(f"{r['id']:10s} {'%.1f%%' % c['rate_percent'] if c else case['raw']['status_label']:>8} | 정답: {r.get('reference')} | STT: {hyp}", flush=True)
    key = model_key(cfg)
    result = {
        "created_at": time.time(), "purpose": args.purpose, "model_key": key,
        "model_config_file": str(cfg_path.relative_to(ROOT)), "normalization_version": NORMALIZATION_VERSION,
        "correction": {"name": "correct_stt_spelling", "version": SPELLING_VERSION},
        "dataset": {"id": manifest.parent.name, "manifest": str(manifest), "manifest_sha256": file_sha256(manifest),
                    "source_type": source_type, "split": split,
                    "synth_engine": sorted({r.get("synth_engine") for r in rows if r.get("synth_engine")})},
        "model_load_sec": load_sec, "cases": cases,
        "aggregate_raw": aggregate([c["raw"] for c in cases]),
        "aggregate_corrected": aggregate([c["corrected"] for c in cases]),
    }
    store = Path(args.store) if args.store else None
    path = save_result(result, root=store)
    a, b = result["aggregate_raw"], result["aggregate_corrected"]
    print(json.dumps({"dataset": manifest.parent.name, "purpose": args.purpose,
                      "raw": {k: a[k] for k in ("cases_measured", "reference_chars", "substitutions", "deletions", "insertions", "errors", "cer", "rate_percent")},
                      "corrected": {k: b[k] for k in ("errors", "cer", "rate_percent")}}, ensure_ascii=False))
    print("저장:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
