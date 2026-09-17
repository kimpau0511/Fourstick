# 한국어 로봇 명령 STT 평가 자료

`eval_ko_robot_commands.jsonl` 한 줄이 한 사례다.

| 필드 | 의미 |
|---|---|
| `id` | 사례 식별자 |
| `reference` | 정답 문장. 빈 문자열이면 "전사할 음성이 없음"이 정답이다 |
| `tags` | 짧은명령 / 중간발화 / STOP / 유사용어 / 침묵 / 잡음 |
| `audio` | 오디오 출처. `generated:...`는 합성, 그 외는 `fixtures/stt/` 기준 상대 경로 |
| `note` | 사례를 넣은 이유 |

## audio 출처 표기

- `generated:silence:<초>` — 완전 무음
- `generated:noise:<초>:<진폭 0~1>` — 백색잡음
- `generated:tone:<초>:<Hz>` — 순음(기계 소음 모사)
- 상대 경로 — 실제 녹음 파일(PCM16 mono WAV)

합성 사례는 결정적이다(고정 시드). 실제 녹음 사례는 **현재 저장소에 없다** —
이 환경에 TTS도 마이크도 없다. 녹음을 넣으면 같은 측정 도구가 그대로 평가한다.

## 녹음을 추가하는 방법

1. PCM16 mono 16kHz WAV로 녹음해 `fixtures/stt/audio/<id>.wav`에 둔다.
2. 측정: `./scripts/run_stt_eval.sh`
3. 오디오 파일은 Git에 넣지 않는다(`.gitignore`의 `fixtures/stt/audio/`).

## 이 자료로 측정하는 것

- WER(단어 오류율)·CER(문자 오류율) — 정답 문장과 비교
- 처리 지연(초)과 실시간 배수(오디오 길이 / 처리 시간)
- 침묵·잡음에서 빈 전사가 나오는지

요구정의서에는 WER·CER·지연의 수치 기준이 없다. 따라서 **측정값만 보고하고
임의의 합격선을 만들지 않는다.**
