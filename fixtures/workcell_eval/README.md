# 작업 셀 명령 평가셋 (8-13)

팔레트 3개 · 자재 3개 · 컨베이어 1개 작업 셀에서 한국어 텍스트 명령이
계획·안전 판단·자원 대조를 올바르게 수행하는지 재기 위한 발화 200문장.

| 파일 | 내용 |
|---|---|
| `scenarios.json` | 시나리오 37개. 분류·설명·기본 정답(`defaults`) |
| `dev_set.jsonl` | 개발셋 100문장. 프롬프트·규칙을 고칠 때 보고 돌린다 |
| `sealed_set.jsonl` | 봉인 평가셋 100문장. 8-13에서 1회 평가 → **evaluated_holdout**. 튜닝·프롬프트·규칙에 쓰지 않는다 |
| `sealed2_set.jsonl` | 8-14에서 만든 holdout 100문장. 기존 두 세트와 겹치지 않는다. 8-14에서 실제 LLM으로 1회 실행 → **evaluated_holdout**. 튜닝·프롬프트·규칙에 쓰지 않는다 |
| `sealed3_set.jsonl` | 8-15에서 만든 holdout 100문장. 기존 세 세트와 겹치지 않는다. 8-15에서 실제 LLM으로 1회 실행 → **evaluated_holdout**. 튜닝·프롬프트·규칙에 쓰지 않는다 |

## 발화 한 줄의 형식

```json
{"id": "dev_001", "scenario": "S02", "utterance": "…", "expect": {…}, "note": ""}
```

`expect`는 시나리오 `defaults` 위에 덮어쓴다. 합쳐진 정답의 필드:

| 필드 | 뜻 |
|---|---|
| `intent` | `home` / `move` / `stop` / `transfer` / `hold` / `clarify` / `unsupported` |
| `resources` | 발화에서 추출돼야 하는 자원 id 집합 (별칭 해석 결과) |
| `steps` | 정확히 일치해야 하는 계획 (스킬·인자 순서, `terminal_hold`) |
| `core_steps` | 순서대로 포함돼야 하는 핵심 스텝 (pick/place 초안용) |
| `decision` | 허용 판정 목록. `PASS` / `BLOCK` / `ASK` / `STOP`. **첫 항목이 기대 판정**(혼동 행렬 행) |
| `reason_codes` | 허용 이유 코드 목록. 비어 있으면 "이유 코드 없음"(PASS)이 정답 |
| `executable` | 실행 가능 정답. `null`이면 "판정이 PASS일 때만 실행 가능" |
| `stop_bypass` | 정지 키워드로 모델을 거치지 않아야 하는가 |
| `plan_validation` | pick/place 초안의 계획 검증 기대: `verified` / `not_verified` / `null`(채점 안 함). **실행 차단과 별도 정답이다** |

## 별칭

번호 별칭은 카탈로그(`config/workcell/fr3_2f85_workcell_resource_catalog.json`)에 있다.

- 1번 자재 / A자재 / 에이자재 → `mat_a` · 2번 자재 / B자재 → `mat_b` · 3번 자재 / C자재 → `mat_c`
- 1번 팔레트 / 일번 팔레트 / 첫번째 팔레트 → `loc_pallet_1` (2·3번도 같다)
- 컨베이어 / 벨트 / 컨베어 → `loc_conveyor`

별칭에 없는 표현("1번, 2번, 3번 팔레트"의 "1번,")은 **추출되지 않는다.** 그런 발화는
resource mismatch 시나리오(S29)에 두고 정답도 그렇게 적었다.

## 검사

```
python3 scripts/check_workcell_eval_dataset.py
```

schema · 중복 · 개발/봉인 겹침 · 자원 id ↔ 셀 설정 · 추출 정답 대조 · 봉인 누출을 본다.

`sealed2_set.jsonl`·`sealed3_set.jsonl`은 각각 `--set sealed2`/`--set sealed3`로만 실행되며 `--set all`에는 포함되지 않는다.
