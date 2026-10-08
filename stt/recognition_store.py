"""음성 인식률 평가 자료·결과(파일) — 저장·조회·화면 표시값 선택 (2026-10-08).

평가 자료(manifest, JSONL 한 줄 = 사례 하나)
  id                     사례 식별자
  audio                  manifest 기준 상대 경로(16 kHz mono PCM16 WAV 권장, 다른 형식은 ffmpeg로 변환)
  reference              정답 전사. null = 평가 대기, "" = 평가 불가
  reference_source       human(사람이 실제 발화를 듣고 확인) | tts_script(합성에 쓴 대본 — 실제 발음과 다를 수 있다)
                         'stt'(평가 대상 STT 출력)는 정답으로 받지 않는다
  reference_verified_by  사람 확인자(human일 때 필수)
  source_type            real(실제 녹음) | synthetic(합성 음성)
  synth_engine           합성일 때 엔진(예: windows-sapi:Microsoft Heami, gtts)
  split                  dev(개선에 사용) | final(최종 평가 — 개선에 쓰지 않는다)
  tags, noise, note      설명용

평가 결과(JSON 파일 하나 = 실행 하나): `<store>/<model_key>/<dataset_id>/<run_id>.json`
  원본 STT 결과·용어 후보정 결과·정답·모델/설정 키·정규화 버전·자료 출처·사례별/종합 계산을 모두 담는다.
  purpose: official(실제 평가) | functional(기능·회귀 시험). 모의 표시용 숫자는 **저장하지 않는다**(화면 시험은 응답을 가짜로 바꾼다).

화면 표시값(`display_summary`) — 1순위: 아래를 **모두** 만족하는 가장 최근 실행 하나(실제 음성).
  - 현재 모델·설정 키와 같다(모델·프로필·설정 버전·장치·연산 형식·언어·VAD 임계값)
  - 정규화 버전이 같다
  - purpose=official, source_type=real, split=final
  - 평가된 사례의 정답이 모두 human이고 확인자가 있다
  - 평가된 사례가 1개 이상
  여러 실행·자료를 합치지 않는다(서로 다른 자료·조건의 결과를 섞지 않는다).
  2순위(2026-10-08 사용자 결정): 실제 음성 결과가 없으면 **합성 음성** 결과 하나 — 같은 모델·설정·정규화, final 자료,
  잡음 없는 사례만, 가장 최근 실행. 라벨은 '음성 인식률 N%' 그대로(사용자 결정), 합성이라는 사실·합성 엔진·문장 수는
  설명(note)과 source='synthetic'에 남긴다.
  둘 다 없으면 '미측정'.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from stt.recognition_rate import MEASURED, NORMALIZATION_VERSION

SCHEMA = "forstick2.stt_recognition/1"
MODEL_KEY_FIELDS = ("model_name", "profile_id", "config_version", "device", "compute_type", "language", "vad_threshold")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STORE = ROOT / "reports" / "stt_recognition"
DISPLAY_NOTE = "글자 기준 평가 결과이며 현재 발화의 정확도를 뜻하지 않습니다."


def store_dir() -> Path:
    return Path(os.environ.get("FORSTICK2_STT_RECOGNITION_DIR") or DEFAULT_STORE)


def model_key(config: Any) -> dict:
    """STT 모델·설정 키. config는 SttModelConfig(또는 같은 필드를 가진 dict)."""
    get = (lambda k: config.get(k)) if isinstance(config, Mapping) else (lambda k: getattr(config, k, None))
    return {k: get(k) for k in MODEL_KEY_FIELDS}


def model_key_id(key: Mapping[str, Any]) -> str:
    """파일 경로용 짧은 식별자: 사람이 읽는 앞부분 + 키 전체의 해시."""
    head = re.sub(r"[^a-zA-Z0-9.-]+", "_", f"{key.get('model_name')}__{key.get('profile_id')}__{key.get('config_version')}")
    digest = hashlib.sha256(json.dumps(dict(key), sort_keys=True).encode()).hexdigest()[:10]
    return f"{head}__{digest}"


class InvalidDataset(ValueError):
    pass


def load_manifest(path: Path) -> list[dict]:
    """평가 자료를 읽고 규칙을 확인한다. 어기면 InvalidDataset(평가하지 않는다)."""
    rows = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        for k in ("id", "audio", "source_type", "split"):
            if not row.get(k):
                raise InvalidDataset(f"{path}:{n} '{k}'가 없다")
        if row["source_type"] not in ("real", "synthetic"):
            raise InvalidDataset(f"{path}:{n} source_type은 real|synthetic")
        if row["split"] not in ("dev", "final"):
            raise InvalidDataset(f"{path}:{n} split은 dev|final")
        src = row.get("reference_source")
        if src == "stt":
            raise InvalidDataset(f"{path}:{n} 평가 대상 STT 출력은 정답이 될 수 없다")
        if row.get("reference") is not None:
            if src not in ("human", "tts_script"):
                raise InvalidDataset(f"{path}:{n} reference_source는 human|tts_script")
            if src == "human" and not row.get("reference_verified_by"):
                raise InvalidDataset(f"{path}:{n} 사람이 확인한 정답에는 reference_verified_by가 필요하다")
            if row["source_type"] == "real" and src != "human":
                raise InvalidDataset(f"{path}:{n} 실제 녹음의 정답은 사람이 확인한 전사(human)여야 한다")
        if row["source_type"] == "synthetic" and not row.get("synth_engine"):
            raise InvalidDataset(f"{path}:{n} 합성 음성에는 synth_engine이 필요하다")
        rows.append(row)
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise InvalidDataset(f"{path}: 사례 id가 겹친다")
    kinds = {(r["source_type"], r["split"]) for r in rows}
    if len(kinds) > 1:
        # 실제·합성, 개선용·최종 자료를 한 자료에 섞지 않는다(결과도 섞이지 않게).
        raise InvalidDataset(f"{path}: 한 자료에 source_type·split이 섞여 있다 {sorted(kinds)}")
    return rows


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_result(result: dict, *, root: Path | None = None) -> Path:
    for k in ("model_key", "dataset", "cases", "aggregate_raw", "normalization_version", "purpose"):
        if k not in result:
            raise ValueError(f"결과에 {k}가 없다")
    if result["purpose"] not in ("official", "functional"):
        raise ValueError("purpose는 official|functional(모의 표시값은 저장하지 않는다)")
    root = root or store_dir()
    run_id = result.get("run_id") or time.strftime("%Y%m%d-%H%M%S")
    result = {"schema": SCHEMA, "run_id": run_id, **result}
    path = root / model_key_id(result["model_key"]) / result["dataset"]["id"] / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def iter_results(root: Path | None = None) -> Iterable[tuple[Path, dict]]:
    root = root or store_dir()
    if not root.exists():
        return
    for p in sorted(root.rglob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("schema") == SCHEMA:
            yield p, d


def official_real(result: Mapping[str, Any], key: Mapping[str, Any]) -> tuple[bool, str]:
    """화면에 낼 수 있는 결과인가. (가능, 이유)"""
    ds = result.get("dataset") or {}
    if dict(result.get("model_key") or {}) != dict(key):
        return False, "다른 모델·설정"
    if result.get("normalization_version") != NORMALIZATION_VERSION:
        return False, "다른 정규화 버전"
    if result.get("purpose") != "official":
        return False, "기능 시험 결과"
    if ds.get("source_type") != "real":
        return False, "합성 음성 결과"
    if ds.get("split") != "final":
        return False, "개선용(dev) 자료"
    measured = [c for c in result.get("cases") or () if (c.get("raw") or {}).get("status") == MEASURED]
    if not measured:
        return False, "평가된 사례 없음"
    if any(c.get("reference_source") != "human" or not c.get("reference_verified_by") for c in measured):
        return False, "사람이 확인하지 않은 정답 포함"
    return True, ""


def synthetic_clean_final(result: Mapping[str, Any], key: Mapping[str, Any]) -> bool:
    """2순위 표시 후보: 같은 모델·설정·정규화의 합성 음성 final 자료, 잡음 없는 사례만, 평가된 사례 있음."""
    ds = result.get("dataset") or {}
    cases = result.get("cases") or ()
    return (dict(result.get("model_key") or {}) == dict(key)
            and result.get("normalization_version") == NORMALIZATION_VERSION
            and ds.get("source_type") == "synthetic" and ds.get("split") == "final"
            and bool(cases) and not any(c.get("noise") for c in cases)
            and any((c.get("raw") or {}).get("status") == MEASURED for c in cases))


def display_summary(key: Mapping[str, Any], *, root: Path | None = None) -> dict:
    """화면 표시값. 현재 모델·설정의 실제 녹음·최종 평가가 없으면 unmeasured."""
    best = synth = None
    skipped: dict[str, int] = {}
    for path, d in iter_results(root):
        ok, why = official_real(d, key)
        if not ok:
            skipped[why] = skipped.get(why, 0) + 1
            if synthetic_clean_final(d, key) and (synth is None or (d.get("created_at") or 0) > (synth[1].get("created_at") or 0)):
                synth = (path, d)
            continue
        if best is None or (d.get("created_at") or 0) > (best[1].get("created_at") or 0):
            best = (path, d)
    kind = "real"
    if best is None and synth is not None:
        best, kind = synth, "synthetic"
    base = {"model_key": dict(key), "normalization_version": NORMALIZATION_VERSION, "note": DISPLAY_NOTE,
            "skipped_results": skipped}
    if best is None:
        return {**base, "status": "unmeasured", "label": "음성 인식률 미측정", "rate_percent": None,
                "reason": "현재 모델·설정의 실제 녹음·사람 확인 정답 평가 결과가 없습니다"}
    path, d = best
    agg = d["aggregate_raw"]
    rate = agg.get("rate_percent")
    # 라벨은 실제·합성 모두 '음성 인식률 N%'(2026-10-08 사용자 결정). 합성이면 설명(마우스를 올리면 보임)과 source에 밝힌다.
    label = f"음성 인식률 {rate:.1f}%"
    if kind == "synthetic":
        engines = ", ".join(d["dataset"].get("synth_engine") or ()) or "합성 엔진 미상"
        base["note"] = (f"합성 음성({engines}) {agg.get('cases_measured')}문장, 글자 기준 평가 결과입니다."
                        " 실제 사용자 음성·현재 발화의 정확도가 아닙니다. 실제 녹음 평가가 생기면 그 결과로 바뀝니다.")
    return {**base, "status": "measured", "source": kind, "rate_percent": rate, "label": label,
            "basis": {"run_id": d.get("run_id"), "created_at": d.get("created_at"), "dataset_id": d["dataset"]["id"],
                      "cases_measured": agg.get("cases_measured"), "reference_chars": agg.get("reference_chars"),
                      "errors": agg.get("errors"), "substitutions": agg.get("substitutions"),
                      "deletions": agg.get("deletions"), "insertions": agg.get("insertions"), "cer": agg.get("cer"),
                      "file": str(path.relative_to(root or store_dir()))}}
