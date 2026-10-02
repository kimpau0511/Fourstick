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

웹 페이지에서 "1번 팔레트로 가"처럼 입력하면 계획이 생성되고, 검증 결과와
판정(허용·확인·차단)이 화면에 표시된다. 포트는 `FORSTICK2_PORT`로 바꾼다.

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
들어간다. **일반 계획 생성(`/v1/plan`)과 일반 API의 집기·놓기는 이 경로와 무관하게
계속 차단된다**(`capability.profile_incomplete`).

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

### 3. 웹에서 쓰는 시뮬레이션 명령

작업 셀(시뮬레이터)에 붙어 있으면 웹에서도 같은 시연을 돌릴 수 있다. 서버는
활성 작업 셀이 시뮬레이션일 때만 이 경로를 연다(`FORSTICK2_SIM_DEMO_WEB=0`으로
끈다). **일반 명령의 집기·놓기 차단은 그대로다.**

- **텍스트·음성(최종 전사)** — 아래 발화는 시뮬레이션 명령으로 간다. 그 밖의
  발화("안전 위치로 복귀해줘" 등)는 기존 계획 생성으로 간다. 음성의 중간 전사
  (partial)는 표시만 하고 실행하지 않는다.

  | 발화 | 동작 |
  |---|---|
  | "A 자재를 컨베이어로 옮겨줘" | 이송(자재를 컨베이어에 둔다) |
  | "A 자재를 원래 자리로 돌려놔" · "돌려놔" | 원래 슬롯 복귀(생략하면 컨베이어에 하나일 때) |
  | "멈춰" · "정지" · "스톱" | 즉시 정지 — 계획·LLM을 거치지 않는다 |
  | "이어서 해줘" | STOP 체크포인트에서 이어서(체크포인트가 하나일 때) |

  자재가 모호하거나 지금 할 수 없으면 ASK/BLOCK만 돌려주고 작업을 만들지 않는다.

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

- **시연 카드** — 자재별로 이송·복귀·resume 사전검증·resume·복구 버튼과 진행
  단계·체크포인트·마지막 결과를 보여준다. 로봇이 움직이는 동작은 한 번 더 확인을
  받는다.

- **정지** — 시연 정지와 헤더의 전체 정지는 실행 중인 시연에 **정지 요청**을
  보낸다. 프로세스를 죽이지 않는다. 시연은 기존 STOP 절차(취소 → 정지 확인 →
  자재 수렴 → 체크포인트 → 래치)로 멈춘다.

- **체크포인트와 resume** — 확인된 STOP이고 자재가 도구 위치에 수렴(0.005 m,
  연속 2표본)했을 때만 체크포인트를 만든다. resume은 저장된 사전검증을 믿지 않고
  실행 직전에 scene·자재 pose·고정·관절·남은 goal·래치 소유권을 다시 관측해
  통과할 때만 움직이며, 중단된 궤적을 재생하지 않고 현재 자세에서 새 접근을
  만든다.

### 4. 웹 화면에서 보이는 것

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
./scripts/run_tests.sh                              # 기본 제한 60초
FORSTICK_TEST_TIMEOUT_SEC=120 ./scripts/run_tests.sh
node --test tests/web/*.test.mjs                    # 웹 화면(JS)
```

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
Node.js가 필요하다(확인한 환경: Node 26.7.0 · npm 11.19.0, Windows).

```bash
cd dashboard2
npm install                 # 처음 한 번, 의존성이 바뀐 뒤에도
npm run dev                 # http://localhost:5175
```

- **백엔드 주소** — 기본값은 작업 셀 PC `https://192.168.0.175:8443`이다. 다른 서버에 붙이려면
  `FORSTICK2_BACKEND_URL=https://주소:포트 npm run dev`. 개발 서버가 `/v1/sim-view`·`/v1/scene`·
  `/v1/sim-demo`를 그 주소로 넘기므로 백엔드에 CORS를 열 필요가 없다(자체 서명 인증서 허용).
- **이 PC에서만 열린다(`localhost`).** 백엔드에 인증이 없고 대시보드가 명령·확인·정지 API를
  넘기기 때문이다. LAN이나 외부 데모로 열려면 인증부터 붙여야 한다.
- **실제 서버 값을 쓰는 곳**
  - 시뮬레이션 창: 3D 관측 화면(`/v1/sim-view/*`). 3D 모델을 받지 못하면 Gazebo 영상
    (`/v1/scene/stream`)으로 대신한다. 화면 아래 DEMO 바의 "안전 검사 중"을 누르면 열린다.
  - 명령 패널: 시연 명령 API(`/v1/sim-demo/*`)
  - 나머지 카드(지표·로봇 목록·이력·알림 등)는 아직 목업 데이터다(`dashboard2/src/data.js`).
- **검사·빌드**

  ```bash
  npm run lint                # ESLint
  npm run build               # dist/ 생성
  ```

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
