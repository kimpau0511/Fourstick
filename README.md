# forstick2

FR3-WMS 로봇 팔과 Robotiq 2F-85 그리퍼로 구성한 작업 셀을, **한국어 음성·텍스트
명령**으로 움직이는 시스템이다.

말한 문장을 계획(Task Plan)으로 바꾸고, 그 계획을 실행하기 전에 도달 가능성과
충돌을 검사해서 통과한 것만 로봇에 보낸다.

- 명령 이해 — STT(faster-whisper) → 계획 생성(vLLM, Qwen3-8B-AWQ)
- 계획 검증 — MoveIt2로 단계별 도달·관절 한계·충돌 확인
- 실행 — Gazebo 안의 작업 셀(팔레트 3개, 자재 3종, 컨베이어)
- 웹 UI — 명령 입력과 판정 결과 확인, **시뮬레이션 자재 이송 시연**(아래)

**현재는 시뮬레이터에서만 동작한다.** 실제 로봇에 연결하지 않았고, 장착 각도·
커플링 질량·실제 파지 관측 같은 실측 근거를 수집하지 않았다. 그래서 Gazebo에서
이송이 끝나도 실기 검증 완료로 표시하지 않으며, 실제 pick/place 실행은 계속
막혀 있다. 시뮬레이션 결과(`simulation_e2e`)와 실기 준비 상태
(`real_hardware_ready`)는 끝까지 다른 값으로 다룬다.

## 전제

- ROS 2 lyrical · gz-sim 10.5.0 · MoveIt 2.15.0 · ros2_control 6.9.0 (WSL2에서 확인)
- 외부 저장소 두 개를 따로 받아둔다. 이 저장소에는 포함되지 않는다.
  - [FAIR-INNOVATION/frcobot_ros2](https://github.com/FAIR-INNOVATION/frcobot_ros2) `5bed0b0` — FR3-WMS 설명 파일
  - [robotiq/ros](https://github.com/robotiq/ros) `ca4bd28` (v1.1.0) — 2F-85 설명 파일
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

계획 생성에 쓰는 모델 설정은 `examples/config/valid_llm_provider_qwen3.json`이며
`base_url`이 `http://localhost:8000/v1`, 모델이 `qwen3-8b-awq`다. 다른 설정을
쓰려면 `FORSTICK2_LLM_CONFIG`로 파일 이름을 지정한다.

### 2. pick/place 시뮬레이션 시연 (명령줄)

Gazebo 안에서 팔레트 → 컨베이어 이송 전체를 돌려 본다. 개발용 설정을 명시해야
들어간다. **일반 계획 생성(`/v1/plan`)과 일반 API의 집기·놓기는 이 경로와 무관하게
계속 차단된다**(`capability.profile_incomplete`).

```bash
# 이송(끝나면 자재를 원래 자리로 되돌린다 — E2E와 같은 기본 정책)
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
아니다. 실제 로봇의 pick/place 가능 판정이 아니다. 모든 기록은
`is_simulated=true`, `real_hardware_ready`·`real_hardware_verified`는 항상 false다.

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
  컨베이어는 단일 배치 위치라 "N번 슬롯"은 지원하지 않는다.

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

### 4. 파지·놓기 자세 측정

자세는 만들지 않고 측정한다. 컨베이어 위 자재를 집는 자세와, 든 자재를 원래
슬롯에 놓는 자세는 아래로 측정해 `config/workcell/fr3_2f85_workcell_grasp.json`에
기록한다(Gazebo·MoveIt이 떠 있어야 한다).

```bash
python3 scripts/derive_grasp_poses.py --conveyor
python3 scripts/derive_grasp_poses.py --pallet-place --materials mat_a mat_c
```

유효 구간이 없으면 값을 만들지 않고 차단한다
(`geometry.grasp_pose_unavailable`).

### 5. 테스트

```bash
./scripts/run_tests.sh                              # 기본 제한 60초
FORSTICK_TEST_TIMEOUT_SEC=120 ./scripts/run_tests.sh
node --test tests/web/*.test.mjs                    # 웹 화면(JS)
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
```

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
