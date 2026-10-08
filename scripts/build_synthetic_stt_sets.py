#!/usr/bin/env python3
"""합성 음성 평가 자료 만들기(2026-10-08) — **기능·회귀 시험용**. 실제 사용자 인식률이 아니다.

    python3 scripts/build_synthetic_stt_sets.py [출력 폴더(기본 ~/voiceeval-run/datasets)]

- Windows 내장 한국어 음성(SAPI 'Microsoft Heami', 설치·네트워크 없음)으로 문장을 읽혀 16 kHz mono PCM16 WAV를 만든다.
  문장: `fixtures/voice_free_speech/{dev,final}.jsonl`의 발화(자유 발화 시험과 같은 문장).
- 조건마다 **자료를 따로** 만든다(결과가 섞이지 않게): dev 깨끗 / final 깨끗 / final 백색잡음 SNR 10·5 dB / final 기계 소음(험) SNR 10 dB.
  잡음은 고정 시드로 섞는다(재현 가능). 잡음 종류·SNR은 manifest에 적는다.
- 기존 gTTS 합성 녹음(`fixtures/stt/voice_commands`, `voice_context`)은 별도 자료(dev — 모델 선택에 이미 쓰였다).
- 정답 = 합성에 넣은 **대본**(reference_source=tts_script). 합성 엔진이 대본과 다르게 발음할 수 있다 — 사람이 들어 확인한
  정답이 아니므로 실제 인식률로 쓰지 않는다(화면 표시 대상 아님).
"""
from __future__ import annotations

import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "voiceeval-run" / "datasets"
WIN_TMP_WSL = Path("/mnt/c/Users/smhrd/AppData/Local/Temp/voiceeval_tts")
WIN_TMP_WIN = r"C:\Users\smhrd\AppData\Local\Temp\voiceeval_tts"
VOICE = "Microsoft Heami Desktop"
ENGINE = "windows-sapi:Microsoft Heami Desktop"
RATE = 16000


def utterances(split: str) -> list[tuple[str, str]]:
    rows = [json.loads(l) for l in (ROOT / "fixtures/voice_free_speech" / f"{split}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    return [(r["id"], r["utterance"]) for r in rows]


def synthesize(items: list[tuple[str, str]]) -> dict[str, Path]:
    """한 번의 PowerShell 호출로 모두 합성한다. 대본은 UTF-8 파일로 넘긴다(명령줄 인코딩 문제 회피)."""
    WIN_TMP_WSL.mkdir(parents=True, exist_ok=True)
    (WIN_TMP_WSL / "script.tsv").write_text("\n".join(f"{i}\t{t}" for i, t in items), encoding="utf-8")
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        f"$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.SelectVoice('{VOICE}'); "
        "$f=New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,[System.Speech.AudioFormat.AudioChannel]::Mono); "
        f"Get-Content -Encoding UTF8 '{WIN_TMP_WIN}\\script.tsv' | ForEach-Object {{ $p=$_.Split([char]9); "
        f"$s.SetOutputToWaveFile('{WIN_TMP_WIN}\\' + $p[0] + '.wav', $f); $s.Speak($p[1]) }}; $s.Dispose()")
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], check=True, timeout=600)
    return {i: WIN_TMP_WSL / f"{i}.wav" for i, _ in items}


def read_pcm(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, RATE, 2), path
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)


def write_pcm(path: Path, x: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(np.clip(np.round(x), -32768, 32767).astype(np.int16).tobytes())


def mix(x: np.ndarray, kind: str, snr_db: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = len(x)
    if kind == "white":
        noise = rng.standard_normal(n)
    elif kind == "hum":   # 기계 소음 모사: 120 Hz와 배음 + 약한 백색잡음
        t = np.arange(n) / RATE
        noise = sum(np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28)) / k for k, f in enumerate((120, 240, 360, 480), 1))
        noise = noise + 0.3 * rng.standard_normal(n)
    else:
        raise ValueError(kind)
    speech = x[np.abs(x) > 0.02 * np.abs(x).max()] if np.abs(x).max() > 0 else x
    p_s = float(np.mean(speech ** 2)) if len(speech) else 1.0
    p_n = float(np.mean(noise ** 2))
    return x + noise * np.sqrt(p_s / (p_n * 10 ** (snr_db / 10)))


def write_dataset(ds_id: str, rows: list[dict]) -> Path:
    d = OUT / ds_id
    d.mkdir(parents=True, exist_ok=True)
    m = d / "manifest.jsonl"
    m.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    print(ds_id, len(rows), "건 →", m)
    return m


def main() -> int:
    import os
    for split in (os.environ.get("SPLITS") or "dev,final").split(","):
        items = utterances(split)
        wavs = synthesize(items)
        conds = [("clean", None, None)] if split == "dev" else [("clean", None, None), ("white-snr10", "white", 10.0),
                                                              ("white-snr5", "white", 5.0), ("hum-snr10", "hum", 10.0)]
        split_tag = "final" if split.startswith("final") else split   # manifest split은 dev|final, 자료 이름은 final2 그대로
        for cond, kind, snr in conds:
            ds_id = f"synth-sapi-{split}-{cond}"
            rows = []
            for k, (cid, text) in enumerate(items):
                x = read_pcm(wavs[cid])
                if kind:
                    x = mix(x, kind, snr, seed=1000 + k)
                write_pcm(OUT / ds_id / f"{cid}.wav", x)
                rows.append({"id": cid, "audio": f"{cid}.wav", "reference": text, "reference_source": "tts_script",
                             "source_type": "synthetic", "synth_engine": ENGINE, "split": split_tag, "tags": [cond, split],
                             "noise": None if not kind else {"kind": kind, "snr_db": snr, "seed": 1000 + k},
                             "note": "합성 대본을 정답으로 씀 — 실제 발음과 다를 수 있음"})
            write_dataset(ds_id, rows)
    if os.environ.get("SPLITS"):
        return 0
    # 기존 gTTS 합성(mp3 → 16 kHz mono WAV)
    rows = []
    for folder in ("voice_commands", "voice_context"):
        man = json.loads((ROOT / "fixtures/stt" / folder / "manifest.json").read_text(encoding="utf-8"))
        for c in man["cases"]:
            dst = OUT / "synth-gtts-dev" / f"{c['id']}.wav"
            dst.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(ROOT / "fixtures/stt" / folder / c["audio"]),
                            "-ac", "1", "-ar", str(RATE), "-sample_fmt", "s16", str(dst)], check=True)
            rows.append({"id": c["id"], "audio": dst.name, "reference": c["text"], "reference_source": "tts_script",
                         "source_type": "synthetic", "synth_engine": "gtts", "split": "dev", "tags": [folder],
                         "note": "gTTS(ko) 합성 — 2026-09 모델 선택에 이미 사용(dev)"})
    write_dataset("synth-gtts-dev", rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
