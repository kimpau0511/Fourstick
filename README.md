# forstick2

FR3-WMS 로봇 팔과 Robotiq 2F-85 그리퍼로 구성한 **Gazebo 작업 셀**을, 한국어
음성·텍스트 명령으로 움직이는 시스템이다.

말한 문장을 계획(Task Plan)으로 바꾸고, 그 계획을 실행하기 전에 도달 가능성과
충돌을 검사해서 통과한 것만 시뮬레이터에 보낸다.

- 명령 이해 — STT(faster-whisper) → 계획 생성(vLLM, Qwen3-8B-AWQ)
- 계획 검증 — MoveIt2로 단계별 도달·관절 한계·충돌 확인
- 실행 — Gazebo 안의 작업 셀(팔레트 3개, 자재 3종, 컨베이어, 장면 카메라)
- 웹 UI — 명령 입력, 안전 판단, **Qwen 해석 확인 → 시뮬레이션 작업 실행**(아래),
  Gazebo 관측만 그리는 **Three.js 3D 화면**
- **G1 휴머노이드(별도 시뮬레이션)** — Unitree G1 보행·연속 제어·컨베이어 앞 왕복을 같은
  웹에서 로봇 선택으로 쓴다. 팔·손 조작은 실험 단계다(아래 7장)

> **FR3 실제 하드웨어 기준 데이터는 향후 확장을 위해 내부에 보존되어 있으며,
> 현재 서비스는 Gazebo 시뮬레이션 전용입니다.**

화면에는 `SIM` 배지가 항상 떠 있고(배지에 마우스를 올리면 뜻이 나온다), 모든 실행
기록은 `is_simulated=true`다. 화면과 공개 API에는 실기 준비도나 실기 실행 선택지가
없다.

위 기준 데이터는 **FR3에만 해당한다.** 2F-85 그리퍼, 작업 셀(받침대·작업대·
팔레트·자재·컨베이어), 장면 카메라, MoveIt2·Gazebo 구성은 **현재 시뮬레이션 기준
기능**이며, 실기 지원 대상이 아니다.

## 전체 흐름

```
음성(마이크) ─ 실시간 STT ─┐
                          ├─ 명령 해석 ─ 계획 생성 ─ 안전 검증 ─ 사용자 승인 ─ 시뮬레이션 실행 ─ 관측으로 결과 판정
텍스트 입력 ──────────────┘
```

1. **입력** — 관제 대시보드(`dashboard2`)의 입력칸이나 마이크. 음성은 서버의 실시간 STT
   (faster-whisper)가 받아 중간 결과를 보이고, 발화가 끝나면 확정 문장 하나를 만든다.
2. **명령 해석** — 정지 표현은 먼저 규칙으로 본다(모델을 거치지 않고 바로 전체 정지).
   자재 이송·복귀 문장은 Qwen이 의도(자재·출발지·목적지)를 읽고, 서버가 작업 셀 기록·
   등록 정보로 다시 확인한다. 애매하면 되묻고(ASK), 할 수 없으면 차단(BLOCK), 이미 그 상태면
   할 일 없음(NOOP)으로 끝내며 계획을 만들지 않는다.
3. **계획·안전 검증** — 계획(move·pick·place·home 단계)을 만든 뒤 스킬·Profile·안전 규칙,
   요청–계획 일치, MoveIt 기하(도달·충돌), 공통 이송 계약, 자재 기록과 Gazebo 관측(위치·부착)을
   함께 본다. 판정은 허용·확인 필요·차단 셋이다.
4. **사용자 승인** — 허용된 계획만 화면에 '실행 승인' 버튼이 생긴다. 승인은 그 세션의 그 계획에
   **한 번만** 쓰인다. 재개·복구도 승인 카드를 거친다.
5. **시뮬레이션 실행** — 실행 직전 승인·관문·실행 허가(환경 버전·세션·관측 신선도)를 다시 본 뒤
   Gazebo 작업 셀에서 움직인다. 도착·놓기는 Gazebo 관측으로만 판정해 기록한다.

## 현재 기능 (관제 대시보드 기준)

- **음성 입력** — 실시간 STT 중간 결과 표시, 발화가 끝나면 **음성 인식 신뢰도**(아래 '제한' 참고)를
  보이고 확정 문장을 **자동으로 계획 요청**한다(한 번만). 신뢰도가 기준(0.6) 미만이거나 빈 문장이면
  보내지 않고 다시 말하게 한다. 녹음 중 입력칸을 고치면 자동으로 보내지 않는다. 승인은 언제나 사람이
  버튼으로 한다.
- **이송·복귀** — "A자재를 컨베이어로 옮겨줘", "컨베이어에 있는 B자재를 2번 팔레트로 돌려놔",
  색 이름("초록자재…")·원래 자리·팔레트 간 이송. 컨베이어 칸(3개)은 서버가 배정한다. 자재 버튼
  (A·B·C → 컨베이어 / 원래 자리)도 같은 계획 경로를 쓴다.
- **반복 작업** — 자재와 횟수를 고르면 확인 뒤 이송·복귀를 번갈아 반복한다. 회차 끝내기·일시정지·
  재개·취소, 서버 재시작으로 중단된 반복의 상태 확인 후 잠금 해제.
- **정지·일시정지·재개·복구**
  - 헤더 **즉시 정지**: 확인 없이 바로 전체 정지. 정지 표현("정지", "멈춰", "긴급 정지", "작업을 중지해",
    "그만해" 등)도 같다. 정지는 **헤더의 정지 해제로만** 풀린다(새 명령으로 풀리지 않는다).
  - 시뮬레이션 보기의 **일시정지**: 지금 실행만 멈추고 정지 지점(체크포인트)을 남긴다.
  - **재개**: 놓기 접근 중에 자재를 든 채 멈췄을 때만. 승인 카드를 거친다.
  - **복구**: 재개할 수 없는 지점이면 자재를 원래 자리로 되돌린다(고정 장치 — 아래 '제한'). 승인 카드를 거친다.
- **진행 표시** — 실행기가 시작 전에 남긴 단계 목록(12단계 등)대로 가로 스테퍼가 넘어간다. 진행 중 단계는
  완료로 보이지 않고, 최종 확인 전에는 완료로 바꾸지 않는다. 통신이 끊기면 마지막 확인 상태를 둔다.
- **관절 상태** — 관절별 현재 각도와 한계 범위(Gazebo 관측).
- **시뮬레이션 보기** — Gazebo 관측만 그리는 3D 화면(전체화면 가능). 3D 모델을 못 받으면 Gazebo 영상.
- **오류 안내** — 서버가 준 사유 그대로 원인을 적고, 사유 코드나 실행기가 남긴 다음 조치를 보인다.
  실행기가 시작 전에 거절하면 "이송 시작 안 함 · 로봇 이동 없음"으로 구분한다.
- **기록·진단** — 기록 및 로그, 실시간 진단, 로봇 관리, 관제 설정 화면(서버 값).
- **로그인** — 구글 로그인(서버 설정 `FORSTICK2_REQUIRE_LOGIN`, 아래 1장).

## 전제

- ROS 2 lyrical · gz-sim 10.5.0 · MoveIt 2.15.0 · ros2_control 6.9.0 (WSL2에서 확인)
- 외부 저장소 두 개를 따로 받아둔다. 이 저장소에는 포함되지 않는다.
  - [FAIR-INNOVATION/frcobot_ros2](https://github.com/FAIR-INNOVATION/frcobot_ros2) `5bed0b0` — FR3-WMS 설명 파일
  - [robotiq/ros](https://github.com/robotiq/ros) `ca4bd28` (v1.1.0) — 2F-85 설명 파일
- G1 휴머노이드를 쓸 때만(7장) — 역시 저장소 밖에 받아둔다.
  - [unitreerobotics/unitree_rl_gym](https://github.com/unitreerobotics/unitree_rl_gym) `276801e`
    (BSD-3-Clause) — G1 URDF·메시·사전학습 보행 정책 `deploy/pre_train/g1/motion.pt`
  - [NVlabs/GR00T-WholeBodyControl](https://github.com/NVlabs/GR00T-WholeBodyControl) `b042411`
    (코드 Apache-2.0, 가중치 NVIDIA Open Model License) — 조작 실험의 균형 정책(`decoupled_wbc`
    Balance.onnx, Git LFS). 희소 복제 + LFS 파일 직접 받기, sha256 확인
  - 파이썬 환경(별도 venv): torch(CPU)·mujoco·numpy·pyyaml·onnxruntime. gz 파이썬 바인딩은 ROS에서,
    protobuf는 시스템 4.21을 링크해 쓴다(`humanoid/g1/run_controller.sh` 주석)
- 파이썬 3.12 이상(확인: 3.14). 시스템 파이썬은 PEP 668로 보호되므로 저장소의 `.venv`를
  `--system-site-packages`로 만들고, 서버 의존성(uvicorn·wsproto·pydantic·jsonschema·numpy·pytest)은
  배포판 패키지를 쓴다. 실시간 STT 의존성만 venv에 넣는다([`md/STT_모델_구성.md`](md/STT_모델_구성.md)).

  ```bash
  python3 -m venv --system-site-packages .venv
  .venv/bin/pip install -r requirements-stt.txt     # faster-whisper·ctranslate2·onnxruntime (torch 불필요)
  ```

  정적 검사 도구는 `requirements-dev.txt`(ruff·pyright, `scripts/check.sh`)다.
- 관제 대시보드는 Node.js가 필요하다(확인: Node 22.22.1, WSL). 8장 참고.
- 계획 생성을 쓰려면 vLLM 서버가 필요하다 (기본 `localhost:8000`)
  - WSL2에서는 두 가지를 맞춰야 뜬다(실측). pinned memory가 꺼져 있으면
    `UVA is not available`로 엔진이 죽고, venv의 `bin`이 PATH에 없으면 컴파일
    단계에서 `ninja`를 찾지 못한다.

    ```bash
    PATH="/경로/venv/bin:$PATH" VLLM_WSL2_ENABLE_PIN_MEMORY=1 \
      vllm serve /경로/qwen3-8b-awq --served-model-name qwen3-8b-awq \
      --max-model-len 3072 --gpu-memory-utilization 0.86 --port 8000
    ```

## 실행 방법

### 1. 작업 셀과 웹 UI 띄우기

순서대로 실행한다. 각각 별도 터미널에서 띄운다.

```bash
# Gazebo 작업 셀
FORSTICK2_ASSEMBLY_YAW_RAD=0 ./scripts/run_gazebo_fr3_2f85_workcell_gui.sh

# MoveIt2 (Gazebo가 올라온 뒤)
./scripts/run_moveit_workcell.sh

# 웹 UI — http://localhost:8092
./scripts/run_web_workcell.sh
```

웹 서버(8092)는 API와 함께 `html/`을 서빙한다. `html/index.html`은 관제 대시보드(`dashboard2`)
빌드이고(8장), 예전 화면은 `/static/legacy.html`에 남아 있다. "1번 팔레트로 가"처럼 입력하면
계획이 생성되고, 검증 결과와 판정(허용·확인·차단)이 화면에 표시된다. 포트는 `FORSTICK2_PORT`로 바꾼다.

`scripts/run_web.sh`는 `config/local.env`가 있으면 먼저 읽는다(이 PC 전용 값 — 저장소에 올리지 않는다).
주요 설정(환경변수):

| 환경변수 | 기본값 | 뜻 |
|---|---|---|
| `FORSTICK2_REQUIRE_LOGIN` | `1` | 구글 로그인 필수. 구글 설정 전 개발 PC에서만 `0` |
| `FORSTICK2_GOOGLE_CLIENT_ID` · `FORSTICK2_GOOGLE_CLIENT_SECRET` | (없음) | 구글 OAuth. 비밀값은 저장소·문서에 넣지 않는다 |
| `FORSTICK2_PUBLIC_ORIGIN` · `FORSTICK2_ADDITIONAL_PUBLIC_ORIGINS` | (없음) | 로그인 허용 출처([`docs/login-origins.md`](docs/login-origins.md)) |
| `FORSTICK2_LLM_BASE_URL` | 설정 파일 값 | 계획 모델 서버 주소 덮어쓰기 |
| `FORSTICK2_STT` | `1` | 실시간 STT 사용 |
| `FORSTICK2_STT_MODEL_CONFIG` | `valid_stt_model_lowspec.json` | STT 모델(small·CPU). GPU는 `valid_stt_model_turbo_gpu.json`(large-v3-turbo) + `FORSTICK2_CUDA_LIB_DIR`(cuBLAS·cuDNN·NVRTC 폴더) |
| `FORSTICK2_DB` | `reports/web.sqlite3` | 요청·계획·승인·실행 기록 DB(시작할 때 스키마를 올린다) |
| `FORSTICK2_PLAN_TTL_SEC` | `600` | 계획 유효 시간 |

같은 네트워크의 다른 기기에서 쓰려면(인증 없음 — 믿을 수 있는 사설망에서만):

```bash
./scripts/run_web_lan.sh     # 0.0.0.0:8094, WSL2 portproxy 명령을 출력한다
./scripts/run_web_tls.sh     # 그 앞에 HTTPS(8443, 자체 서명) — 사설 IP에서 마이크를 쓰려면 필요
```

웹 서버는 G1 휴머노이드 연결도 켠다(`FORSTICK2_HUMANOID_WEB=1`, 기본). G1 제어기가 꺼져
있으면 G1 명령은 이유와 함께 차단될 뿐 FR3에는 영향이 없다.

계획 생성에 쓰는 모델 설정은 `examples/config/valid_llm_provider_qwen3.json`이며
`base_url`이 `http://localhost:8000/v1`, 모델이 `qwen3-8b-awq`다. 다른 설정을
쓰려면 `FORSTICK2_LLM_CONFIG`로 파일 이름을 지정한다.

### 2. pick/place 시뮬레이션 시연 (명령줄)

Gazebo 안에서 팔레트 → 컨베이어 이송 전체를 돌려 본다. 개발용 설정을 명시해야
들어간다. 웹의 일반 계획(`/v1/plan`)에 든 집기·놓기도 이 실행기가 맡지만, **시뮬레이션 작업 셀에서
시뮬레이션 Profile·적재 범위·공통 이송 계약을 통과할 때만** 열린다(`server/sim_pick_place.py`).
그 밖에서는 계속 차단된다(`capability.profile_incomplete`).

```bash
# 이송(끝나면 자재를 원래 자리로 되돌린다 — 종단 간 시험과 같은 기본 정책)
FORSTICK2_SIM_PICK_PLACE_DEMO=1 ./scripts/demo_workcell_pick_place.sh pallet_1 mat_a

# 시연용: 이송 뒤 자재를 컨베이어에 그대로 둔다
... --cell-policy simulation_demo_hold

# 컨베이어의 자재를 원래 팔레트 슬롯으로 되돌린다(로봇이 실제로 집어 옮긴다)
FORSTICK2_SIM_PICK_PLACE_DEMO=1 ./scripts/demo_workcell_pick_place.sh \
    pallet_1 mat_a --return-held-to-origin

# STOP 체크포인트에서 이어서 하기 — 사전검증만 / 실행
... - material_a --resume-preflight
... - material_a --resume-checkpoint simckpt_xxxxxxxx

# 고정 장치로 원래 자리에 맞추고 기록·정지 래치를 정리한다(로봇은 움직이지 않는다)
... pallet_1 material_a --restore-only
```

실행마다 stdout·stderr 원본이 `reports/workcell/sim_demo_logs/`에 남는다.

**시뮬레이터 시연이다.** 물체는 시뮬레이션 고정 장치로 움직이며 마찰 파지가
아니다. 모든 기록은 `is_simulated=true`다.

### 3. 시연 명령 경로 (`/v1/sim-demo/*`)

관제 대시보드의 명령 경로는 둘이다. `VITE_COMMAND_MODE=general`이면 위 '전체 흐름'의 일반 경로
(계획·안전 검증·승인 — 현재 공개 화면), 지정하지 않으면 이 시연 명령 경로다. 작업 셀(시뮬레이터)에
붙어 있을 때만 서버가 이 경로를 연다(`FORSTICK2_SIM_DEMO_WEB=0`으로 끈다). 움직이는 동작은
**확인 카드만** 만들고, 같은 세션이 확인해야 작업이 시작된다(세션 필수, 한 번만, 만료·상태 변경·정지
재검사).

- **텍스트·음성(최종 전사)** — 아래 발화는 시뮬레이션 명령으로 간다. 그 밖의
  발화("안전 위치로 복귀해줘" 등)는 기존 계획 생성으로 간다. 음성의 중간 전사
  (partial)는 표시만 하고 실행하지 않는다.

  | 발화 | 동작 |
  |---|---|
  | "A 자재를 컨베이어로 옮겨줘" | 이송(자재를 컨베이어에 둔다) |
  | "A 자재를 원래 자리로 돌려놔" · "돌려놔" | 원래 슬롯 복귀(생략하면 컨베이어에 하나일 때) |
  | "멈춰" · "정지" · "스톱" · "작업을 중지해" · "그만해" | 즉시 정지 — 계획·LLM을 거치지 않는다 |
  | "이어서 해줘" | STOP 체크포인트에서 이어서(체크포인트가 하나일 때) |

  자재가 모호하거나 지금 할 수 없으면 ASK/BLOCK만 돌려주고 작업을 만들지 않는다.
  정지 판정은 낱말 경계·부정·인용을 본다(`planning/stop_intent.py`): "스톱워치"·"정지하지 마"·
  "'정지'라는 말이 뭐야?"는 정지가 아니고, "정지 버튼 어디 있어?"처럼 애매하면 이동 계획 없이 되묻는다.
  "A 옮겨, 아니 멈춰"처럼 정지가 하나라도 분명하면 정지가 먼저다.

  이후 추가된 것:
  - **색 이름으로 자재 지정** — "초록자재를 컨베이어로 옮겨줘"(색은 작업 셀 설정의 자재 색에서만).
  - **팔레트 간 이송** — 비어 있는 팔레트 자리로 옮긴다. 경로가 다른 자재 자리를 스치면
    그 자리가 차 있을 때 막고, 계획기가 순서·우회를 고른다(측정한 경로 여유 표).
  - **"초록자재 다시 팔레트로 가져다놔"** — 번호 없는 팔레트는 그 자재의 원래 자리.
  - **"원상복귀"** — 모든 자재를 원래 자리로(목표 배치). 직전 작업 하나를 취소하라는 뜻으로
    읽힐 맥락이면 되묻고, 이미 원래 자리면 움직이지 않는다(NOOP).
  - **대화 맥락은 브라우저 세션별**이다. 다른 탭·세션의 "그거"와 섞이지 않는다.

- **파지 고정·충돌 검사** — 든 자재는 Gazebo 분리 관절(DetachableJoint)로 손에 붙는다(순간
  이동 추종 없음). 실행 전 MoveIt 장면을 Gazebo 관측과 맞추고(장면 동기화), 관절 공간 경로를
  0.02 rad 간격으로 검사한다. 파지 자세는 자재–너클 여유(±4 mm) 기준으로 재측정한 값이며,
  이전 값은 `config/workcell/backup/`에 보관했다.

- **모호한 자재 작업 발화 — 해석 → 확인 → 실행** — 위 규칙이 ASK/PASS_THROUGH로
  끝났고 발화가 자재 작업처럼 보이면(예: "그거 컨베이어로 좀 옮겨줘") Qwen
  분류기가 한 번 본다. 모델은 **계획을 만들지 않고** 아래 JSON만 낸다.

  ```json
  {"intent": "transfer|return|resume|restore|unknown",
   "material_id": "material_a|material_b|material_c|null",
   "confidence": 0.0}
  ```

  서버가 스키마·자재 후보·confidence(기본 0.7 이상)·지금 시연 상태를 다시
  검증한다. 통과해도 **작업을 만들지 않는다** — 화면의 명령 결과 영역에 확인
  카드("A자재를 컨베이어로 옮기겠습니다." + 확인/취소 + 해석 근거 + 현재 자재
  상태)를 띄우고, 확인을 누른 뒤에야 기존 시연 작업이 만들어진다. 확인 만료
  (기본 60초)·취소·상태 변경·낮은 confidence·JSON 오류는 모두 **작업 0건**이다.
  "멈춰"는 분류기와 확인을 거치지 않고 즉시 정지한다. 일반 명령(home/move)은
  분류기를 부르지도 않고 기존 계획 생성으로 간다.

  | 환경변수 | 기본값 | 뜻 |
  |---|---|---|
  | `FORSTICK2_SIM_DEMO_INTENT` | `1` | 분류기 사용 여부(`0`이면 규칙 해석만) |
  | `FORSTICK2_SIM_DEMO_INTENT_MIN_CONFIDENCE` | `0.7` | 실행 후보로 받는 최소 confidence |
  | `FORSTICK2_SIM_DEMO_CONFIRM_TTL_SEC` | `60` | 확인 카드 만료(초) |

  분류기는 계획 생성과 **같은 모델 서버**(vLLM, `qwen3-8b-awq`)를 쓴다. 서버가
  없으면 분류기가 붙지 않고 ASK로 끝난다 — 추측해서 실행하지 않는다.

- **시연 카드(예전 화면)** — 자재별로 이송·복귀·resume 사전검증·resume·복구 버튼과 진행
  단계·체크포인트·마지막 결과를 보여준다. 직접 작업 요청(`POST /v1/sim-demo/jobs`)은 이제 확인 카드만
  돌려주므로, 확인 카드를 그리지 못하는 예전 화면의 이 버튼으로는 작업이 시작되지 않는다. 관제
  대시보드를 쓴다.

- **정지** — 시연 정지와 헤더의 전체 정지는 실행 중인 시연에 **정지 요청**을
  보낸다. 프로세스를 죽이지 않는다. 시연은 기존 STOP 절차(취소 → 정지 확인 →
  자재 수렴 → 체크포인트 → 래치)로 멈춘다.

- **체크포인트와 resume** — 확인된 STOP이고 자재가 도구 위치에 수렴(0.005 m,
  연속 2표본)했을 때만 체크포인트를 만든다. resume은 저장된 사전검증을 믿지 않고
  실행 직전에 scene·자재 pose·고정·관절·남은 goal·래치 소유권을 다시 관측해
  통과할 때만 움직이며, 중단된 궤적을 재생하지 않고 현재 자세에서 새 접근을
  만든다.

### 4. 예전 웹 화면(`/static/legacy.html`)에서 보이는 것

지금 기본 화면은 관제 대시보드(8장)다. 아래는 보존한 예전 화면의 설명이다(음성 안내 TTS는 이
화면에만 있다).

- **`SIM` 배지** — 장면 영상 위와 명령 카드 머리말에 **항상** 있다. 마우스를
  올리면 "Gazebo 시뮬레이션 · 실제 로봇 아님"이 나온다. 화면에는 실기 준비도나
  실기 실행 선택지가 없다 — 이 서비스는 Gazebo 시뮬레이션 전용이다.
- **작업 셀 화면(3D · 관측)** — Three.js로 Gazebo가 관측한 관절·그리퍼·자재 위치만 그린다
  (`/v1/sim-view/*`, 읽기 전용). 관측 사이만 보간하고, 끊기거나 오래되면 마지막 상태로 멈추고
  그렇다고 적는다. "Gazebo 영상" 탭으로 서버 렌더링 카메라 화면과 바꿔 본다.
- **가제보 화면** — 서버가 렌더링한 작업 셀 장면을 WebSocket으로 받는다. Gazebo
  GUI 창과 무관하다. 프레임을 받지 못하면 **빈 그림을 그리지 않고** 이유를 적는다.
- **로봇 선택** — FR3 / G1. 선택한 로봇의 경로로만 명령이 간다(요청·확인 카드·맥락 분리).
- **현재 자재 상태** — 장면 아래에 A·B·C 자재가 원래 자리인지 컨베이어에
  유지 중인지 띠로 보인다.
- **명령 칸** — 텍스트 입력, 마이크 on/off, 명령 보내기·지우기, TTS 음성 on/off.
- **명령 결과 영역** — 규칙 판단(RUN/STOP/ASK/BLOCK)과 **Qwen 확인 카드**가 뜬다.
- **오른쪽 칸** — 생성된 작업 계획 · 안전 판단 및 실행 · 차단된 요청.
  진행 중인 시뮬레이션 작업과 STOP 체크포인트도 여기 나온다.

#### 음성 안내 (TTS)

브라우저 내장 `SpeechSynthesis`로 **신뢰된 결과만 짧게** 읽는다. 기본은 꺼짐이고,
켜면 그 브라우저에 설정이 남는다. 브라우저가 음성 합성을 지원하지 않으면 토글이
잠기고 그 사실을 적는다.

| 읽는 것 | 읽지 않는 것 |
|---|---|
| 안전 판단 ASK·BLOCK | 안전 판단 PASS(실행 버튼을 누르는 자리라 방해가 된다) |
| 시뮬레이션 명령 ASK·BLOCK | 모델(LLM) 원문 |
| Qwen 확인 카드가 떴을 때 | STT 중간 전사(partial) |
| 확인을 눌러 작업이 시작됐을 때 | 단계별 진행 로그 |
| 작업 완료·실패, resume 결과 | |
| STOP 요청과 확인·미확인 결과 | |

읽는 문장은 서버의 **결정·상태값에서만** 만든다. 새 명령·새 작업·STOP이 생기면
읽던 음성을 즉시 끊는다.

### 5. 파지·놓기 자세 측정

자세는 만들지 않고 측정한다. 컨베이어 위 자재를 집는 자세와, 든 자재를 원래
슬롯에 놓는 자세는 아래로 측정해 `config/workcell/fr3_2f85_workcell_grasp.json`에
기록한다(Gazebo·MoveIt이 떠 있어야 한다).

```bash
python3 scripts/derive_grasp_poses.py --conveyor
python3 scripts/derive_grasp_poses.py --pallet-place --materials mat_a mat_c
```

유효 구간이 없으면 값을 만들지 않고 차단한다
(`geometry.grasp_pose_unavailable`).

### 6. 테스트

```bash
FORSTICK_TEST_TIMEOUT_SEC=120 ./scripts/run_tests.sh   # 파이썬 전체(약 2,470건, 약 1분 — 기본 제한 60초는 빠듯하다)
.venv/bin/python -m pytest -q tests                    # 같은 시험을 pytest로
node --test tests/web/*.test.mjs                       # 예전 웹 화면(JS)
cd dashboard2 && npm run lint && npm run qa:ui && npm run qa:general   # 관제 대시보드(가짜 응답, 서버·로봇 없이)
```

- 시험은 가짜 로봇·가짜 계획 공급자·임시 DB로 돈다. Gazebo·vLLM 없이 돈다.
- 알려진 실패(d1d89b0 기준): `tests/integration/test_web_screen.py`의 목업 원본 확인 1건(`html/목업/src`가
  저장소에 없음), `test_sim_pick_place`의 속도 스냅숏 기대값 1건, `tests/web`의 2건(`html/index.html`이
  관제 대시보드 빌드로 바뀌어 예전 화면 컨테이너가 없다).
- 명령 해석 평가: `scripts/run_workcell_command_eval.sh`(개발·봉인 평가셋, 봉인 문장은 시험·프롬프트에
  옮겨 적지 않는다), STT 평가: `scripts/run_stt_eval.sh`.

실제 브라우저 종단 간 확인(Playwright, 화면 없는 Chromium — 프로젝트 의존성이 아니다):
`scripts/verify_web_fr3_e2e.py`(일반 계획·이송·복귀), `scripts/verify_web_g1_e2e.py`
(G1 확인 카드·왕복·이동 중 STOP·FR3 색 지정 이송까지 같은 브라우저에서, 로봇별 요청 격리 확인),
`scripts/verify_sim_view.py`(3D 화면), `scripts/verify_arrangement_e2e.py`(이송·복구 스위트).

### 7. G1 휴머노이드 (시뮬레이션 전용)

FR3 작업 셀과 **별개**다 — Gazebo 파티션·세계·파이썬 환경이 따로다. 자세한 구성·측정값은
[`humanoid/g1/README.md`](humanoid/g1/README.md).

| 세계(`run_gazebo.sh` 인자) | 쓰임 | 제어기 |
|---|---|---|
| (없음) `forstick2_humanoid` | 보행 검증(서기·걷기·회전·정지) | `verify_walk.py`(스텝 맞물림 제어) |
| `--nav` `forstick2_humanoid_nav` | 연속 제어·컨베이어 앞 왕복·웹 연결 | `nav_controller.py` |
| `--manip` `forstick2_humanoid_manip` | 팔·손(Dex3-1) 조작 실험 | `manip_controller.py` |
| `--wbc` `forstick2_humanoid_wbc` | 조작용 균형 정책(GR00T-WBC Balance) 실험 | `gr00t_controller.py` |

```bash
humanoid/g1/run_gazebo.sh --nav                                        # 멈춘 세계, PD 플러그인 빌드 포함
humanoid/g1/run_nav.sh nav_controller.py --rtf 1.0 --reset-world &      # 세계 초기화는 시작 때만
humanoid/g1/run_nav.sh g1cmd.py goto conveyor_front | stop | return | status
humanoid/g1/run_nav.sh verify_roundtrip.py --trips 5 --stops 5
```

- 보행: unitree_rl_gym G1 12자유도 사전학습 정책(LSTM). 관절 토크만으로 움직이고 판정은 Gazebo 관측.
- 웹: 로봇 선택에서 G1 → "컨베이어 앞에 가" · "컨베이어 한번 찍고 와" · "출발 위치로 돌아와" · "멈춰".
  이동은 확인 카드 승인 뒤, "멈춰"는 즉시. 도착·복귀는 Gazebo 관측으로 판단, 선언된 지점만 쓴다.
  정지·대기 중에는 제자리 걸음 표류를 자리 유지로 묶는다(정지 자세가 아니다).
- **실시간이 아니다** — 제어기가 물리 스텝을 직접 밀어 한 걸음씩 맞물려 돈다(lockstep). 실시간 배율(RTF)은 약 0.8이고, CPU 부하에 따라 더 낮다.
- **팔·손 조작은 실험 단계**다. 보행 정책(팔 고정 학습)으로는 팔·손 제어만 확인했고 **안정적인 근접
  접근 기준은 충족하지 못했다**(손 흔들림 4–5 cm, 접근 1/3). GR00T-WBC Balance 정책으로 바꾸면 접근 6/6,
  손 흔들림 0.2–0.4 mm, 몸 이동 ≤ 5 mm였지만, 접근 거리는 기존 안전 규칙(질량 중심 이동 한계) 때문에
  물체 중심에서 0.39 m에 머문다. 잡기·들기·운반은 아직 하지 않는다.
- 물리 모델 차이: Gazebo(DART)에는 관절 armature가 없어 발목 링크 관성을 보행 정책 모델 값으로
  바꿔 쓴다(실제 값으로는 발목 PD가 발산). 상체 PD는 관절 유효 관성으로 수치 안정 한계를 적용한다.

### 8. 관제 대시보드 (`dashboard2`, React + Vite)

피그마 디자인으로 만든 관제 대시보드다. 백엔드(위 1장의 웹 서버)에 붙어서 쓴다.
백엔드 코드는 가져다 쓰지 않고 HTTP·WebSocket API만 부른다. 다만 3D 화면이 옛 웹 화면의
보간 계산 파일 `html/static/js/sim-view-core.js`를 그대로 import하므로, `dashboard2`만 따로
떼어 내면 빌드되지 않는다(저장소 전체가 필요하다).
Node.js가 필요하다(확인한 환경: Node 26.7.0 · npm 11.19.0, Windows / Node 22.22.1, WSL).

```bash
cd dashboard2
npm install                 # 처음 한 번, 의존성이 바뀐 뒤에도
npm run dev                 # http://localhost:5175
```

- **백엔드 주소** — 기본값은 같은 PC의 `http://127.0.0.1:8092`(1장의 웹 서버)다. 다른 서버에 붙이려면
  `FORSTICK2_BACKEND_URL=https://주소:포트 npm run dev`. 개발 서버가 `/v1/*`를 그 주소로 넘기므로
  백엔드에 CORS를 열 필요가 없다(자체 서명 인증서 허용).
- **설정 파일** — `dashboard2/.env`(팀 공통, 공개 값: 구글 클라이언트 ID 등), `dashboard2/.env.local`(이 PC
  전용, 저장소에 올리지 않는다). `VITE_COMMAND_MODE=general`이면 일반 경로(계획·승인·실행), 없으면 시연
  명령 경로(3장).
- **개발 서버는 이 PC에서만 열린다(`localhost`).** 외부 공개는 로그인을 켠 8092가 빌드한 화면을
  서빙하는 방식으로 한다(아래 '서버가 서빙하는 화면 빌드').
- **서버 값을 쓰는 곳** — 명령 패널(일반 경로 `/v1/plan`·`/v1/decision`·`/v1/execute`·`/v1/stop`, 또는
  시연 명령 `/v1/sim-demo/*`), 실시간 STT(`/v1/stt`), 반복 작업(`/v1/sim-demo/repeat`), 시뮬레이션 창
  (3D 관측 `/v1/sim-view/*`, 대신 Gazebo 영상 `/v1/scene/stream`), 관절 상태, 기록·진단·로봇 관리·설정 화면.
- **검사·빌드**

  ```bash
  npm run lint                # ESLint
  npm run build               # dist/ 생성
  npm run qa                  # 요구사항 QA(Playwright): 화면(가짜 응답) + 서버 API(읽기·취소만)
  npm run qa:report           # 요구사항 ID별 결과 표
  QA_ROBOT=1 npm run qa:api   # 로봇을 실제로 움직이는 검사(E2E·정지 반응) — 작업 셀 PC 담당 팀원 허락 뒤에만
  ```

  화면 QA의 개발 서버는 5181·5182 포트를 쓰고, 가짜 응답을 빠뜨린 요청이 실제 서버로 새지 않게 백엔드를
  닫힌 포트로 둔다(`playwright.config.js`).

- **서버가 서빙하는 화면 빌드** — 8092가 내보내는 `html/index.html`·`html/static/app/`을 만든다.

  ```bash
  cd dashboard2
  npx vite build --base=/static/app/ --outDir ../html/static/app --emptyOutDir
  cp ../html/static/app/index.html ../html/index.html
  ```

## 주요 폴더

| 폴더 | 내용 |
|---|---|
| `server/` | 웹·API 서버(ASGI). `api.py`(계획·승인·실행·정지), `routes/`(경로별 처리), `plan_intent.py`(이송 의도 검증), `sim_demo_*`(시연 작업·확인 카드·목표), `sim_pick_place.py`(일반 경로 이송), `material_check.py`(기록·관측 확인), `repeat_runs.py`(반복), `sim_view.py`(3D 관측) |
| `planning/` | 계획 생성(vLLM·Qwen 공급자, 프롬프트, 출력 해석), 슬롯 추출, 정지 표현 판정(`stop_intent.py`), 의도 해석기 |
| `validation/` | 안전 검증·실행 허가·이송 단계·시연 상태 기록·컨베이어 칸 |
| `core/` | 계획·스킬·Profile·사유 코드 등 공통 계약 |
| `robots/` | 로봇 어댑터 — `fr3_gazebo/`(작업 셀: ROS·Gazebo 전송, 고정 장치, 붙임 관절 관측), `moveit/`, `fake/`(개발·시험용) |
| `stt/` | 실시간 STT(faster-whisper·Silero VAD), 세션, 평가 |
| `storage/` | SQLite 기록(요청·계획·승인·실행, 스키마 마이그레이션) |
| `config/` | 작업 셀·Profile·MoveIt·Gazebo 설정(`config/workcell/active.json`이 활성 셀) |
| `examples/config/` | 서버가 읽는 정책·모델 설정(`valid_*.json`)과 잘못된 설정 예(시험용) |
| `scripts/` | 실행·시연·측정·평가·검증 스크립트 |
| `dashboard2/` | 관제 대시보드(React + Vite)와 화면 QA(`e2e/`) |
| `html/` | 8092가 서빙하는 화면(대시보드 빌드, 예전 화면 `static/legacy.html`) |
| `tests/` | `unit`·`integration`·`contract`·`model`, `web`(예전 화면 JS) |
| `fixtures/` | 평가셋(명령 해석·STT) |
| `md/`, `docs/` | 설계·작업 기록·실행 계획 |

## 시뮬레이션의 제한

- **Gazebo 시뮬레이션 전용**이다. 모든 실행 기록은 `is_simulated=true`이고 실기 실행 경로는 없다.
- **강제 부착 방식** — 집은 자재는 마찰 파지가 아니라 Gazebo 분리 관절(DetachableJoint)로 그리퍼에
  고정된다. 붙임·떼기는 Gazebo의 붙임 상태 알림으로 확인해 `fixture_joints.json`에 남긴다. 이미 떨어진
  관절에 떼기를 보내면 알림이 없어 기록이 `unknown`이 될 수 있는데, 이때는 월드 상태의 붙임 관절을
  (별도 프로세스에서) 관측해 확인한다. 관측으로도 확인되지 않으면 그 자재의 이송을 시작하지 않는다.
- **복구는 순간 이동**이다 — 고정 장치가 자재를 원래 자리에 옮겨 놓고 로봇은 움직이지 않는다. 그리퍼를
  닫는 중에 멈췄다 복구하면 팔이 집기 자세에 남아, 다음 이송이 첫 경로(→ 안전 home) 충돌 검사에서
  시작되지 않을 수 있다. 화면이 알려 주는 대로 "로봇을 홈 위치로 이동해"를 보낸 뒤 다시 요청한다.
- **재개 조건** — 자재를 든 채 놓기 접근 중에 확인된 정지로 멈춘 경우만 재개한다. 그 밖은 복구 대상이다.
- **기록과 관측을 함께 본다** — 이송·복귀 시작 전에 서버 기록, Gazebo 위치(허용 0.02 m), 부착 상태를
  같이 확인한다. 둘이 다르면 차단하고 정합 확인(복구)을 안내하며, 관측이 없거나 오래됐으면 시작하지 않는다.
- **음성 인식 신뢰도 ≠ 실제 인식률** — 화면의 '음성 인식 신뢰도'는 STT 모델이 그 발화에 대해 낸 추정치
  (세그먼트 평균 로그확률의 지수를 길이로 가중 평균, 0~1)다. 맞게 알아들었다는 뜻이 아니며, 기준값 0.6은
  실제 분포를 보고 다시 정할 값이다. 글자 기준 인식률은 평가 도구로만 재며(`scripts/run_stt_eval.sh`,
  합성 음성 위주), 실제 마이크 녹음과의 비교는 아직 하지 않았다. 화면에 인식률은 보이지 않는다.
- **정지** — 정지 요청은 실행 중인 작업에 바로 전달되고, 정지 확인(어댑터·실행기)은 그 뒤에 따로 보고된다.
  실행 중인 것이 없을 때 응답은 확인을 기다려 수 초 걸릴 수 있다. 정지 표현 판정은 규칙 기반이라 등록되지
  않은 말투는 되묻거나 일반 명령으로 처리될 수 있다.
- **입력 상한** — 명령 200자, HTTP 본문 64 KB, STT 오디오 프레임 32,000바이트. 넘으면 이유와 함께 거절한다.
- **계획 모델 의존** — 이송 의도 해석과 계획 생성은 vLLM 서버가 있어야 한다. 없으면 이유와 함께 실행하지
  않는다(추측해서 실행하지 않는다).
- **승인 범위** — 승인은 그 세션·그 계획에 한 번만 쓰이고, 실행 직전 환경이 바뀌었거나 관측이 오래됐으면
  다시 승인해야 한다.

## 경로 설정 — 본인 환경에 맞게 바꿔야 한다

스크립트와 설정에 들어 있는 `/home/asd/...` 는 **개발 당시 로컬 경로이며 예시다.**
비밀 정보는 아니지만 그대로는 다른 환경에서 동작하지 않는다. 세 종류로 나뉜다.

**환경변수로 덮을 수 있는 것** — 실행 스크립트 6개(`run_gazebo_fr3.sh`,
`run_gazebo_fr3_gripper.sh`, `run_gazebo_fr3_2f85_workcell_gui.sh`,
`run_moveit_fr3.sh`, `run_moveit_workcell.sh`, `attach_gazebo_gui.sh`)는 절대경로를
기본값으로만 쓴다. 파일을 고치지 말고 환경변수를 준다.

```bash
export FORSTICK2_FR3_REPO=/원하는/경로/frcobot_ros2
export FORSTICK2_ROBOTIQ_REPO=/원하는/경로/robotiq_ros
# G1 휴머노이드(7장)
export FORSTICK2_G1_DESC=/원하는/경로/unitree_rl_gym/resources/robots/g1_description
export FORSTICK2_G1_POLICY=/원하는/경로/unitree_rl_gym/deploy/pre_train/g1/motion.pt
export FORSTICK2_HUMANOID_VENV=/원하는/경로/humanoid_venv
export FORSTICK2_HUMANOID_SYSLINK=/원하는/경로/humanoid_syslink        # 시스템 protobuf 링크 폴더
export FORSTICK2_GR00T_WORK=/원하는/경로/gr00t_work FORSTICK2_GR00T_WEIGHTS=/원하는/경로/gr00t_weights
```

`humanoid/g1/config/manip_gr00t_site.json`의 `policy.onnx`는 가중치 파일 절대경로다(설정 파일에서 바꾼다).

**아직 하드코딩된 것** — 분석·산출 스크립트 6개는 파일 상단 상수를 직접 고쳐야
한다. 환경변수를 보지 않는다. 환경변수로 바꾸는 편이 낫고, 그렇게 정리할 예정이다.

| 파일 | 위치 |
|---|---|
| `scripts/derive_fr3_profile.py` | 33행 `REPO` |
| `scripts/analyze_fr3_reach.py` | 29행 `REPO` |
| `scripts/review_self_collision.py` | 44행 `DEFAULT_REPO` |
| `scripts/derive_2f85_kinematics.py` | 29행 `DEFAULT_REPO` |
| `scripts/analyze_mounting_interface.py` | 26행 `ROBOTIQ_REPO` |
| `scripts/measure_flange_interface.py` | 417·425행 — `--fr3-repo` / `--robotiq-repo` 인자로 넘길 수 있다 |

**고치지 않는 것** — `config/profiles/*.json` 과 `config/assets/third_party_assets.json`
안의 경로는 **측정값이 어디서 나왔는지 적은 출처 기록**이다. 실행에 쓰이지 않으며,
바꾸면 근거 추적이 끊긴다. 그대로 둔다.
