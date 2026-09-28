# FR3 작업 셀 설치·실행(새 팀원용)

목표: Git 복제 + 이 문서 절차로 **FR3 + Robotiq 2F-85 Gazebo 작업 셀과 웹 명령**을 재현한다. G1은 맨 아래 선택 단계다.
모든 결과는 시뮬레이션이다(`is_simulated=true`). 실제 로봇 연결은 없다.

## 0. 필수 조건

| 항목 | 조건 | 개발 PC(확인된 값) |
|---|---|---|
| OS | Ubuntu 26.04 LTS | WSL2(Windows 11) 위 Ubuntu 26.04.1 |
| ROS | ROS 2 Lyrical(apt) | gz-sim 10.5.0 · MoveIt 2.15.0 · ros2_control 6.9.0 |
| 파이썬 | 시스템 python3 ≥ 3.12 | 3.14.4 |
| GPU | NVIDIA 권장(장면 카메라 렌더링). vLLM을 쓰면 8 GB 이상 | RTX 4060 Ti 8 GB |
| 메모리 | 여유 6 GB 이상 권장(Gazebo+MoveIt+웹+STT) | 7.7 GB(vLLM 동시 실행) |
| 네트워크 | GitHub·Hugging Face(설치 때만) | |

## 1. 시스템 패키지

ROS 2 Lyrical apt 저장소를 ROS 공식 설치 문서대로 설정한다(`ros2-apt-source` 패키지). 그다음:

```bash
git clone <이 저장소> forstick2 && cd forstick2
./scripts/setup/install_system.sh            # 빠진 패키지만 보여 준다
./scripts/setup/install_system.sh --install  # sudo apt-get install (목록: scripts/setup/apt-packages.txt)
```

## 2. 웹 서버 가상환경

```bash
./scripts/setup/create_venv.sh   # .venv(--system-site-packages) + requirements.txt 고정 버전
```

## 3. 외부 로봇 자산(저장소에 포함하지 않음)

```bash
FORSTICK2_ACCEPT_FR3_TERMS=1 ./scripts/setup/fetch_assets.sh   # third_party/ 에 원본에서 지정 commit으로 받고 검증
```

- FR3-WMS: FAIR-INNOVATION/frcobot_ros2 `5bed0b0263c8…` — **라이선스 파일 없음(재배포 조건 미확인)**. 그래서 저장소에 넣지 않고
  사용자가 원본에서 직접 받는다. 소속 조직의 사용 조건을 확인한 뒤 `FORSTICK2_ACCEPT_FR3_TERMS=1`로 동의해야 받는다.
- Robotiq 2F-85: robotiq/ros `v1.1.0`(commit `3ab3bef…`, BSD-3-Clause).
- 실패하면 이유 코드로 멈춘다: `asset.network` · `asset.commit_missing` · `asset.hash_mismatch` · `asset.terms_not_accepted` · `asset.missing`.
- 이미 다른 곳에 받아 뒀으면 `cp config/local.env.example config/local.env` 후 `FORSTICK2_FR3_REPO`·`FORSTICK2_ROBOTIQ_REPO`를 적고
  `./scripts/setup/fetch_assets.sh --verify-only`.

## 4. 모델

```bash
./scripts/setup/fetch_models.sh          # 음성 인식 faster-whisper small(약 470 MB). 안 해도 첫 실행 때 받는다
./scripts/setup/fetch_models.sh --qwen   # (선택) Qwen3-8B-AWQ 약 6 GB
```

vLLM(선택): 모호한 문장 해석(Qwen)과 일반 `/v1/plan` 계획 생성에 쓴다. **없어도** 정확한 시연 명령·확인 카드·STOP·3D 관측은 된다.
vLLM은 별도 가상환경에 설치한다(개발 PC: vLLM 0.28.0 · torch 2.13.0+cu130). 실행:

```bash
FORSTICK2_VLLM_BIN=/경로/venv/bin/vllm FORSTICK2_QWEN_DIR=~/models/qwen3-8b-awq ./scripts/run_vllm.sh   # :8000, 모델명 qwen3-8b-awq
```

## 5. 사전 점검 → 실행 → 종료

```bash
./scripts/doctor.sh      # OK / WARN(선택 기능 없음) / FAIL(실행 막힘)
./scripts/fr3_up.sh      # Gazebo → MoveIt → 웹 서버 순서로 띄우고 준비될 때까지 기다린다 → http://127.0.0.1:8092
./scripts/fr3_down.sh    # 웹 → MoveIt·브리지·rsp(SIGINT로 정상 종료) → Gazebo
```

- `FORSTICK2_STT=0` 음성 입력 없이 · `FORSTICK2_LAN=1` 같은 망 기기에서 접속(0.0.0.0, **인증 없음**) · 포트 `FORSTICK2_PORT`.
- 개별 실행(기존 방식): `FORSTICK2_ASSEMBLY_YAW_RAD=0 ./scripts/run_gazebo_fr3_2f85_workcell_gui.sh` → `./scripts/run_moveit_workcell.sh`
  → `./scripts/run_web_workcell.sh`.
- 로그: `/tmp/forstick2_workcell/`(`FORSTICK2_WORKCELL_LOG_DIR`). 한 PC에서 두 번째 셀을 따로 띄우려면 `config/local.env.example`의 분리 항목
  (로그 폴더·Gazebo 파티션·ROS 도메인·포트)을 모두 바꾼다.
- 실제 Gazebo 반복 검증(이송→복귀 N회, 자재 위치를 Gazebo에서 직접 확인):
  `python3 scripts/verify_instance_isolation.py --base http://127.0.0.1:8092 --partition forstick2_fr3_workcell --cycles 3`
  같은 PC에 다른 셀이 떠 있으면 `--other-partition <그 파티션>`을 더해 그 셀의 자재·로봇 링크가 그대로인지도 본다.
- 웹 확인: 명령 칸에 "주황 자재 컨베이어로 옮겨줘" → 확인 카드 → 확인 → 3D 화면에서 자재가 컨베이어로 간다. "멈춰"는 확인 없이 정지.

## 6. 경로 규칙

실행 경로는 환경변수 → `config/local.env`(Git 제외) → 저장소 상대 기본값(`third_party/…`, `/tmp/forstick2_*`) 순이다
(`scripts/lib/env.sh`, `core/paths.py`, `robots/fr3_gazebo/paths.py`). `config/profiles/*.json`·`config/assets/third_party_assets.json` 안의
`/home/asd/...`는 **측정 출처 기록**이라 바꾸지 않았다(실행에 쓰지 않는다). MoveIt 자기충돌·루프 폐쇄 근거와 개발 PC에서 쓰던 SRDF는
`config/evidence/`에 스냅숏으로 들어 있다.

## 7. (선택) G1 휴머노이드

FR3 설치와 무관하다. README 7장의 외부 자산(unitree_rl_gym, GR00T-WholeBodyControl)과 별도 가상환경이 필요하며, 이 설치 자동화에는
포함하지 않았다. 웹 서버는 G1 제어기가 없으면 G1 명령만 이유와 함께 막고 FR3에는 영향이 없다.

## 8. 팀원 PC 확인 절차(이 브랜치)

```bash
git clone -b fr3-reproducible-setup https://github.com/kimpau0511/Fourstick forstick2 && cd forstick2
./scripts/setup/install_system.sh --install && ./scripts/setup/create_venv.sh
FORSTICK2_ACCEPT_FR3_TERMS=1 ./scripts/setup/fetch_assets.sh && ./scripts/doctor.sh
FORSTICK2_STT=0 ./scripts/fr3_up.sh
python3 scripts/verify_instance_isolation.py --base http://127.0.0.1:8092 --partition forstick2_fr3_workcell --cycles 1
```

- 마지막 줄이 `결과: PASS · 작업 2/2`면 이송·복귀 통과(자재 위치는 Gazebo에서 직접 읽는다).
- 3D 화면 FPS: 브라우저로 http://127.0.0.1:8092 → "작업 셀 화면"의 **3D (관측)** 탭. 왼쪽 위 `● 관측 · N FPS`의 N은
  **브라우저 렌더링 FPS**(requestAnimationFrame 횟수)다. 관측 데이터 수신 빈도가 아니다(개발 PC 측정: 관측 약 29 메시지/s).
  "연결 중…"에서 멈추면 브라우저 개발자 도구 콘솔에 `WebGL context` 오류가 있는지 본다(하드웨어 가속이 꺼진 브라우저).
- 결과(PASS 줄, FPS 값, 브라우저·GPU 이름)를 공유한다. 끄기: `./scripts/fr3_down.sh`
