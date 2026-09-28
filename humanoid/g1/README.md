# G1 Gazebo 보행 검증 (시뮬레이션 전용)

FR3 작업 셀과 **별개**다. 파티션 `forstick2_humanoid`, 산출물 `/tmp/forstick2_humanoid`, 파이썬
환경 `/home/asd/external/humanoid_venv`. FR3의 config·서버·웹·파티션(`forstick2_fr3_workcell`)은
건드리지 않는다. 모든 결과는 `is_simulated: true`.

## 쓰는 공개 자료와 라이선스

| 항목 | 출처 | 라이선스 |
|---|---|---|
| 모델 `g1_12dof.urdf` + 메시 | unitree_rl_gym `resources/robots/g1_description` | BSD-3-Clause (Unitree Robotics) |
| 정책 `deploy/pre_train/g1/motion.pt` (TorchScript) | unitree_rl_gym | BSD-3-Clause |
| 배치 설정(PD 이득·기본 자세·관측 배율) | unitree_rl_gym `deploy/deploy_mujoco/configs/g1.yaml` | BSD-3-Clause |

저장소는 `/home/asd/external/unitree_rl_gym`에 받아 두고 **읽기만** 한다(복사하지 않음).

## 모델 ↔ 정책 일치 확인

- 정책 출력 12 = 다리 관절 12, 순서: 왼쪽·오른쪽 × (hip_pitch, hip_roll, hip_yaw, knee,
  ankle_pitch, ankle_roll). URDF의 가동 관절도 이 12개뿐이고 이름·순서가 배치 설정과 같다.
- 관측 47 = 각속도(골반 좌표계)·0.25 | 중력 투영 3 | 명령(vx, vy, yaw)·[2, 2, 0.25] |
  q − q0 (12) | q̇·0.05 (12) | 이전 행동 12 | sin·cos(보행 위상, 주기 0.8 s).
- 제어: 정책 50 Hz(물리 10스텝마다), PD 500 Hz: τ = kp(q* − q) − kd·q̇, q* = 행동·0.25 + q0,
  토크는 URDF effort 한계로 자름. kp/kd/q0는 g1.yaml 그대로.
- 정책은 **LSTM**(unitree_rl_gym `PolicyExporterLSTM`)이라 hidden/cell 상태를 모듈 버퍼로 들고 있다.
  TorchScript에는 `reset_memory()`가 실리지 않아, 회차 시작마다 제어기가 두 버퍼를 0으로 만든다
  (`Controller.reset_memory`). 안 비우면 다음 회차의 첫 행동부터 달라진다(MuJoCo에서 확인).
- MuJoCo 배치(원래 환경)로 먼저 확인: `mujoco_reference.py`.

## Gazebo 구성

- `build_world.py`: URDF를 읽어 메시 경로만 절대 경로로 바꾸고 Gazebo 태그(관절 상태 발행,
  관절별 `ApplyJointForce`, 골반 IMU, 발 접촉 센서, 발 마찰 1.0)를 붙여 `gz sdf -p`로 변환.
  관절 감쇠 0.001·마찰 0.1은 MJCF 기본값을 옮김. **MJCF armature 0.01은 옮기지 못함**(차이).
  DART, 물리 0.002 s, 중력 −9.81, 지면 평면(마찰 1.0).
- `run_gazebo.sh`: 멈춘 상태로 서버를 띄운다.
- `controller.py`: **스텝 맞물림(lockstep)** — 스텝마다 관측을 받아 토크를 보내고 `WorldControl.multi_step=1`로
  한 스텝 민다. 로봇을 움직이는 수단은 관절 토크뿐이다. 세계 초기화는 회차 시작에만.
- `verify_walk.py`: 반복 검증. 판정은 Gazebo `pose/info`(모델 world pose + 메시지 시각)와 발 접촉
  센서로만 한다. 기준(`CRITERIA`)은 결과를 보기 전에 정했다.

## 실행

```bash
humanoid/g1/run_gazebo.sh
humanoid/g1/run_controller.sh humanoid/g1/controller.py --plan stand:3,walk:0.5:0:0:4,turn:0:0:0.6:5,stop:4
humanoid/g1/run_controller.sh humanoid/g1/verify_walk.py --repeats 5   # → reports/humanoid/g1_gazebo_walk.json
```

`run_controller.sh`는 ROS(gz 파이썬 바인딩)를 소스하고, protobuf는 gz-msgs 생성 코드와 맞는
시스템 것(4.21)을 `/home/asd/external/humanoid_syslink`로 링크해 쓴다(pip 4.21 휠은 Python 3.14에서
C 확장이 실패).

## 알려진 한계

- 이 정책에는 **정지 자세 모드가 없다.** 명령 0이면 제자리 걸음으로 균형을 잡는다. 기본 자세 PD만으로는
  MuJoCo·Gazebo 모두 약 1.5 s에 앞으로 넘어진다 → "서기"·"정지"는 제자리 걸음 균형이다.
- 파이썬 gz-transport는 `WorldControl` 서비스의 응답을 받지 못한다(요청은 처리됨). 스텝 확인은 새 시각이
  찍힌 관절 관측으로 한다. 토크 메시지와 스텝 요청은 다른 통로라 한 스텝(2 ms) 늦게 적용될 수 있다.
- 실시간이 아니다(스텝 맞물림 제어, 시뮬레이션 19 s에 벽시계 약 47 s). 실시간 제어 루프는 검증하지 않았다.
- 12자유도 다리 모델(팔·허리 고정). 평지·마찰 1.0에서만. 외란·경사·계단 없음.

## 연속 제어 · 컨베이어 앞 왕복 (`--nav` 세계)

- 세계 `forstick2_humanoid_nav`(파티션 `forstick2_humanoid_nav`): G1 + 정적 컨베이어(`config/site.json`).
  안전 도착 지점 `conveyor_front` = (2.15, 0.0), 컨베이어 앞면(x 2.75)에서 0.60 m, 방향 0.
  골반–앞면 최소 거리 0.35 m 아래로 들어가면 제어기가 전진 목표를 취소한다.
- PD는 Gazebo 플러그인(`plugin/G1PdController.cc`, 500 Hz, 식은 같음). 파이썬 제어기는 50 Hz로 목표만 보낸다.
  목표 메시지에 seq를 붙이고 플러그인의 확인 응답을 받은 뒤에 스텝을 민다(순서 보장).
- `nav_controller.py`: 제어 주기 = 시뮬레이션 0.02 s. 벽시계에 맞춰 돈다(`--rtf 1.0`). 명령은 `/g1/command`
  (JSON), 상태는 `/g1/status`. `g1cmd.py`로 보낸다. 세계 초기화는 `--reset-world`로 **시작 때만**.
- 이동 목표: 멀면 방향 맞추고 걷기, 0.5 m 안에서는 몸 좌표계 (vx, vy)+회전, 도착 뒤 자리 지키기.
  선속도 명령은 0 또는 0.25 m/s 이상 — 학습에서 0.2 m/s 이하 선속도 명령은 0으로 바뀌어 학습되지 않았다.
- STOP: 이동 목표 취소 + 명령 0. 정책·PD는 계속 돈다(제자리 걸음 균형). 토크를 끄는 경로는 없다.
- 정책 기억: 속도·회전·정지 전환 중에는 유지. 초기화는 제어기 시작 · 세계 시각 되돌아감 감지 때만.
  넘어짐을 감지하면 정책을 멈추고 기본 자세 PD만 유지한다(세계 초기화 필요 — 자동으로 하지 않음).

```bash
humanoid/g1/run_gazebo.sh --nav
humanoid/g1/run_nav.sh nav_controller.py --rtf 1.0 --reset-world &
humanoid/g1/run_nav.sh g1cmd.py save_start ; humanoid/g1/run_nav.sh g1cmd.py goto conveyor_front
humanoid/g1/run_nav.sh g1cmd.py stop | return | velocity 0.3 0 0 | status | shutdown
humanoid/g1/run_nav.sh verify_roundtrip.py --trips 5 --stops 5   # → reports/humanoid/g1_roundtrip.json
```

실시간 한계: 이 PC(12코어, FR3 Gazebo가 약 4.4코어 사용 중)에서 G1 물리 스텝이 벽시계로 약 2.2 ms/스텝
(0.002 s 스텝) — 물리만으로 실시간을 못 넘는다. 제어기는 가능한 만큼 돌아 실측 실시간 배율(RTF) 0.81–0.82.

## 정지·대기 중 자리 유지 · 금지선 앞 여유 (2026-09-27)

- 명령 0의 제자리 걸음은 표류한다(실측: 대기 0.03 m/s, 걷다 멈춘 뒤 0.1–0.15 m/s → 20 s에 2 m 넘게).
  목표가 없으면 `HoldKeeper`가 감속이 끝난 관측 위치를 기준으로 0.15 m를 넘을 때만 되돌린다(0.06 m 안이면 멈춤).
  이동 목표가 아니다 — 새 목적지로 가지 않는다. 상태 `balance`: stepping_in_place / correcting_drift / walking / fallen.
- 수동 속도·자리 유지는 금지선 앞 0.25 m부터 컨베이어 쪽(+x) 선속도를 막는다(걷던 관성으로 선을 넘은 뒤 막으면
  앞면 0.13 m까지 갔다 — 실측). 이동 목표(goto)는 선언된 안전 지점까지만 가고, 선을 넘으면 목표를 취소한다.
- 검증(`verify_roundtrip.py`): 시작 전 컨베이어를 등지고 2 m 이상 떨어진 곳으로 옮긴 뒤 명령 전환 · 대기 20 s ·
  왕복 5 · STOP 5(정지 뒤 10 s 관찰).

## 웹 연결(`/v1/humanoid/*`)

- 웹 실행기(`scripts/run_web_workcell.sh`)가 `FORSTICK2_HUMANOID_WEB=1`로 켠다. 서버는 휴머노이드 파티션에
  별도 gz 노드를 연다(`robots/g1_gazebo/bridge.py`). 명령·확인·작업: `server/humanoid_service.py`,
  해석: `server/humanoid_commands.py`, 3D: `server/humanoid_view.py` + `html/static/js/humanoid*.js`.
- 제어기·Gazebo는 웹이 띄우지 않는다. 먼저 `run_gazebo.sh --nav`와 `nav_controller.py`를 띄운다.

## 팔·손 조작 실험(`--manip`, 2026-09-27) — 접근 가능성까지. 잡기·들기·운반 없음

- 모델: `g1_29dof_with_hand_rev_1_0.urdf`(Dex3-1 손, 손가락 7×2 — 열기·닫기 가능, BSD-3-Clause). 다리 관절 이름·
  원점·질량·질량 중심은 보행 정책 모델(`g1_12dof`)과 같다. 상체는 +2.28 kg, 질량 중심 앞 +13 mm·위 +12 mm.
  `g1_29dof_with_hand`(rev 아님)는 다리 매개변수가 달라 제외. `g1_23dof_rev_1_0`은 질량이 같지만 손가락이 없다.
- **발목 관성**: 정책 모델(`g1_12dof`)의 발목 링크 관성은 0.01로 키워져 있고 상체 URDF들은 실제 값(8.4e-6 ~
  1.6e-3)이다. 실제 값으로는 발목 PD가 발산해 1.3 s 만에 넘어졌다(23dof도 같음). 조작 세계는 다리 링크 관성을
  정책 모델 값으로 쓴다(`g1_manip_leg_inertia_from_policy_model.json` — 발목 4개, 질량·질량 중심은 원래 같음).
- 상체 PD: 두 번째 플러그인 인스턴스(`/g1/upper/target`). 이득 기본값에 **명시 PD 수치 안정 한계**(kd·dt/I ≤ 0.5,
  kp·dt²/I ≤ 0.1, 관절 유효 관성으로 계산)를 적용 — 손가락 말단은 kd 0.02에서 발산했다(kd·dt/I=2.23). 손가락 관절
  마찰 0(다리 값 0.1 N·m를 주자 말단이 움직이지 않았다), 감쇠 0.05(암묵). 팔 중력 보상은 목표를 τ_중력/kp만큼 옮겨
  보낸다(관측 골반 방향).
- 수신 폭주: 멈춘 세계에서도 관절 상태·pose/info가 같은 시각으로 ~2 ms마다 다시 발행돼 43관절 메시지가 파이썬
  수신을 밀어 PD 확인 응답을 잃었다 → 조작 세계는 관측을 50 Hz로, 제어기 구독은 200/s로 줄였다(`subscribe_hz`).
- 접근: 잡기 점(엄지·검지 끝 가운데)을 물체 중심에서 몸 방향 뒤로 d, 손바닥은 기본 자세 방향을 몸 방향으로(위치+방향
  역기구학). 충돌 예측은 팔·손 충돌 메시 경계 상자 점(링크마다 26점)과 받침대·물체·몸통(torso 메시 경계) 사이 거리.
  필요 여유 = 기준(물체·받침대 3 cm, 몸통 2 cm) + **측정한 손 흔들림**(팔이 멈춘 동안 잡기 점이 앞뒤 0.8 s 평균에서
  벗어난 최대값 — 제자리 걸음 주기의 흔들림, 약 5 cm). d는 경로 전체가 여유를 지키는 가장 작은 값.
- 안전: 서기 지점(x −0.16 m) 0.10 m 안에서만 접근, 추적 중 0.14 m 벗어나면 팔 멈춤. 움직이는 중 기울기 12.8° 넘거나
  팔·손이 받침대·물체에 닿으면 기본 자세로 물린다(가까워지지 않는 경로일 때, 아니면 멈춤). 팔을 뻗은 채 걷는 명령
  (goto·velocity·return)은 거절. STOP은 팔·손을 그 자리에 멈추고 다리 균형 제어는 계속.

```bash
humanoid/g1/run_gazebo.sh --manip
humanoid/g1/run_manip.sh manip_controller.py --rtf 1.0 --reset-world &
humanoid/g1/run_manip.sh verify_manip.py                 # → reports/humanoid/g1_manip.json
```

## GR00T-WBC Balance 균형 정책 시험(`--wbc`, 2026-09-27)

- 정책: NVlabs/GR00T-WholeBodyControl `decoupled_wbc/sim2mujoco` Balance.onnx(sha256 f645da59…, 가중치 NVIDIA Open Model
  License · 코드 Apache-2.0). 받은 곳: `/home/asd/external/gr00t_wbc`(희소 복제 b042411), 가중치 `/home/asd/external/gr00t_weights`
  (LFS 원본 해시 확인). MuJoCo용 MJCF는 `/home/asd/external/gr00t_work`(메시 경로만 unitree_rl_gym으로 — 해시 동일 확인).
- 하체 15관절(다리 + 허리)을 정책이, 팔·손은 `manip_controller`가 같은 규칙으로. 걷기 명령은 거절(서기 지점에서 시작).
- 물리 차이: armature 없음 → 발목 링크 관성 대체, PD 500 Hz(학습 200 Hz).

```bash
humanoid/g1/run_gazebo.sh --wbc
humanoid/g1/run_wbc.sh gr00t_controller.py --rtf 1.0 --reset-world &
humanoid/g1/run_wbc.sh verify_manip.py --approaches 5 --out reports/humanoid/g1_manip_gr00t.json
/home/asd/external/humanoid_venv/bin/python humanoid/g1/gr00t_mujoco_reference.py --reach 0 0.33 0.40   # MuJoCo 참조
```
