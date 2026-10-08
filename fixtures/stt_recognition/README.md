# 음성 인식률 평가 자료 (실제 녹음) — 준비 형식

**현재 실제 녹음·사람 확인 정답이 없다 → 화면 표시는 '음성 인식률 미측정'.** 아래 형식으로 녹음과 정답만 넣으면 평가할 수 있다.

## 넣는 방법
1. 폴더 하나 = 자료 하나. 예: `real-final-20261010/`
2. 녹음 파일(16 kHz mono WAV 권장, 다른 형식은 자동 변환)을 그 폴더에 둔다. 오디오는 git에 넣지 않는다.
3. `manifest.jsonl` 한 줄 = 녹음 하나:
   ```json
   {"id": "r001", "audio": "r001.wav", "reference": "A자재를 컨베이어로 옮겨줘",
    "reference_source": "human", "reference_verified_by": "확인한 사람", "source_type": "real", "split": "final",
    "tags": ["현장", "마이크A"], "note": "녹음 환경·화자 등"}
   ```
   - `reference`: **사람이 녹음을 직접 듣고 확인한 실제 발화 내용**(대본이 아니라 실제로 말한 대로). STT 결과를 정답으로 쓰지 않는다.
   - 정답을 아직 못 정했으면 `"reference": null`(평가 대기), 말소리가 없으면 `""`(평가 불가).
   - `split`: 규칙·용어 보정 개선에 쓸 자료는 `dev`, 최종 평가는 `final`(final은 개선에 쓰지 않는다). 한 폴더에 섞지 않는다.
4. 평가(격리 STT — 운영 STT에 보내지 않는다):
   ```bash
   C=$HOME/.cache/forstick2_stt_cudalib/nvidia; export LD_LIBRARY_PATH=$C/cuda_nvrtc/lib:$C/cudnn/lib:$C/cublas/lib
   .venv/bin/python scripts/stt_recognition_eval.py fixtures/stt_recognition/real-final-20261010/manifest.jsonl --purpose official
   ```
   결과는 `reports/stt_recognition/<모델키>/<자료>/<시각>.json`(또는 `FORSTICK2_STT_RECOGNITION_DIR`).
5. 화면: 서버가 **현재 STT 모델·설정과 같은** official·real·final·사람 확인 결과 중 가장 최근 것 하나를 보인다(`GET /v1/stt/recognition-rate`).

## 계산 기준(정규화 `ko-cer-1`)
NFKC → 라틴 소문자 → 공백 제거 → 문장부호·기호 제거. 숫자↔한글 읽기, 라틴↔한글 읽기는 바꾸지 않는다(다르면 오류).
CER = (치환+삭제+삽입)/정답 글자 수, 인식률 = max(0, 1−CER)×100, 종합 = 전체 오류 / 전체 정답 글자(단순 평균 아님).
