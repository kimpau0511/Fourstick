# forstick 백업 복원/재구동 가이드

`forstick_backup_20260908.tar.gz` (프로젝트 코드/설정/로그만 포함, `venv`·`__pycache__`·설치 패키지·윈도우 메타데이터는 제외)를 다시 풀어서
전체 파이프라인(음성/텍스트 → Task Plan → Gazebo 로봇 실행)을 재구동하는 절차.

## 0. 전제 조건 (백업에 포함되지 않음 — 새 머신이면 직접 준비)

- **OS/미들웨어**: WSL2 Ubuntu 26.04 "Resolute" + ROS2 "Lyrical Luth" + Gazebo "Jetty"가 이미 설치돼 있어야 함.
  같은 배포판이 아니면 `CLAUDE.md`의 "이 배포판에서 만난 버전 고유 버그들" 섹션에 정리된 우회들이 안 맞을 수 있음.
- **LLM 모델**: `/home/asd/models/exaone-3.5-7.8b-awq` (EXAONE-3.5-7.8B-AWQ) 경로에 모델 파일이 있어야 함. 백업에는 안 들어있음.
- **Python venv**: 용량(8.3GB) 때문에 백업에서 제외함. 아래 1번 단계에서 새로 만들어야 함.
- **ROS2 패키지**: `ros-lyrical-rmw-cyclonedds-cpp`, `ros-lyrical-ur-simulation-gz`, `ros-lyrical-ur-description`, `ros-lyrical-ur-moveit-config`, `ros-lyrical-ur-controllers`, `ros-lyrical-robotiq-description` 등 apt로 설치돼 있어야 함 (gazebo_robot/launch 파일들이 참조).

## 1. 압축 해제

```bash
cd ~
tar -xzf forstick_backup_20260908.tar.gz   # ~/forstick 로 풀림
cd ~/forstick
```

## 2. venv 재생성

```bash
cd ~/forstick
python3 -m venv venv
source venv/bin/activate
pip install vllm==0.28.0 fastapi==0.136.3 uvicorn==0.52.4 \
            faster-whisper==1.2.1 torch==2.13.0 transformers==5.16.1
```

- 버전은 백업 시점(2026-09-08) 설치본 기준. `requirements.txt`가 따로 없었으므로 `pip freeze` 스냅샷이 필요하면 다음에 미리 만들어두는 게 좋음.
- `rclpy`는 pip 패키지가 아니라 ROS2 시스템 설치(`/opt/ros/lyrical`)에서 옴 — venv에 `include-system-site-packages`는 false지만 아래 3번처럼 `source /opt/ros/lyrical/setup.bash` 후 실행하면 정상 동작(원본 환경도 이 방식).

## 3. 실행 (터미널마다 공통으로 먼저)

```bash
source /opt/ros/lyrical/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
```

### 3-1. vLLM 서버 (터미널 1, `~/forstick/venv` 활성화 상태)

```bash
cd ~/forstick && source venv/bin/activate
VLLM_USE_V2_MODEL_RUNNER=0 python3 -m vllm.entrypoints.openai.api_server \
  --model /home/asd/models/exaone-3.5-7.8b-awq --quantization awq \
  --gpu-memory-utilization 0.85 --max-model-len 4096 --enforce-eager --trust-remote-code
```

### 3-2. Gazebo + MoveIt (터미널 2)

```bash
ros2 launch gazebo_robot panda_gazebo_moveit.launch.py
# UR5e로 하려면: ros2 launch gazebo_robot ur5e_robotiq_gazebo.launch.py (단, UR5e는 그리퍼 컨트롤러 버그로 pick까지는 안 됨)
```

### 3-3. API 서버 (터미널 3, `~/forstick/venv` 활성화 상태 — `--reload` 절대 쓰지 말 것)

```bash
cd ~/forstick && source venv/bin/activate
uvicorn api_server:app --host 0.0.0.0 --port 8090
```

### 3-4. 접속

- 데모 페이지: `http://localhost:8090/`
- 콘솔 UI: `http://localhost:8090/console`
- 마이크(getUserMedia)를 쓰려면 반드시 `localhost`/`127.0.0.1`로 접속 (LAN IP는 브라우저가 보안 컨텍스트로 안 봐서 막힘).

## 4. 편의 스크립트

- 로봇만 바꾸고 싶을 때 (vLLM은 이미 떠 있는 상태): `./start_robot.sh panda` 또는 `./start_robot.sh ur5e`
- Panda+UR5e 동시 실행: `./start_dual_robots.sh` (Panda `:8090`, UR5e `:8091`) / 종료: `./stop_dual_robots.sh`
- 실행 권한이 빠져 있으면: `chmod +x start_robot.sh start_dual_robots.sh stop_dual_robots.sh`

## 5. 알려진 미해결 이슈 (재구동 후 바로 마주칠 수 있음)

- **UR5e pick 안 됨**: 하강/접근 스텝에서 `joint_trajectory_controller`가 `GOAL_TOLERANCE_VIOLATED`로 abort. 원인 미확정(`gz_ros2_control` joint-limits enforcement 쪽 의심).
- **그리퍼가 마찰력만으로 붙잡음**: DetachableJoint 플러그인 미적용이라 pick 후 실제로 들어올려지지 않는 경우 있음 (`{"success":true}` 응답만으로 grasp 성공을 신뢰하면 안 됨 — `gz topic -e -t /world/pallets_world/pose/info`로 물체 위치 직접 확인 필요). Gazebo Jetty(버전 10.5.0) 문법은 확인해둠, 구현은 미착수.
- `/health`의 `robot_connected`는 최초 연결 이후 Gazebo가 죽어도 계속 true를 반환 — 로봇이 안 움직이면 `pgrep -af gz-sim-main`으로 프로세스 생존을 먼저 확인.
- 자세한 배경/원인/팀 역할 분담은 `CLAUDE.md` 참고.
