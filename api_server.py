"""
포스틱 백엔드 API 서버.

실행 순서:
  1) vLLM 서버가 이미 8000번 포트에서 떠 있어야 함 (별도 터미널)
  2) (execute=true로 로봇 실행까지 하려면) panda_gazebo_moveit.launch.py가
     이미 떠 있어야 함 (별도 터미널) — 그 터미널과 동일하게 아래 env가
     이 서버를 실행하는 터미널에도 필요:
       source /opt/ros/lyrical/setup.bash
       export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
  3) 이 서버 실행: uvicorn api_server:app --host 0.0.0.0 --port 8080 --reload
     (--reload는 rclpy 노드를 파일 변경마다 재시작시켜 문제가 될 수 있으니
     로봇 실행 기능을 테스트할 때는 --reload 빼고 실행 권장)

동작 확인 (계획만 생성, 로봇 실행 안 함 — 기존과 동일):
  curl -X POST http://localhost:8080/v1/task-plan \
       -H "Content-Type: application/json" \
       -d '{"text": "2번 팔레트에서 A자재를 집어서 컨베이어에 올려줘"}'

동작 확인 (계획 생성 + 실제 Gazebo 로봇 실행까지, 풀 루프):
  curl -X POST http://localhost:8080/v1/task-plan \
       -H "Content-Type: application/json" \
       -d '{"text": "1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘", "execute": true}'

Gazebo 화면 실시간 스트리밍 (MJPEG):
  WSLg 화면(x11grab)을 직접 캡처하는 방식은 WSLg가 창을 Windows 쪽으로
  원격 렌더링하는 구조상 항상 검은 화면만 나와서 포기했다. 대신
  pallets_world.sdf에 추가한 scene_camera 센서가 렌더링한 영상을
  ROS2 토픽(/scene_camera/image)으로 직접 구독해서 스트리밍한다.
  Pillow가 필요함: pip install Pillow
  브라우저에서 http://localhost:8080/gazebo-stream.mjpeg 로 직접 확인 가능.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel
from PIL import Image as PILImage
from sensor_msgs.msg import Image as RosImage

from pipeline import (
    AUDIT_LOG_PATH,
    CAPABILITY_PROFILE,
    transcribe_text_stub,
    transcribe_audio,
    process_utterance,
)

# gazebo_robot/taskplan_bridge.py의 TaskPlanExecutor를 재사용 (코드 중복 방지).
# taskplan_bridge.py가 rclpy를 import하므로, 이 서버도 ROS2가 source된
# 환경(+ RMW_IMPLEMENTATION=rmw_cyclonedds_cpp)에서 실행되어야 함.
sys.path.insert(0, str(Path(__file__).parent / "gazebo_robot"))
import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from taskplan_bridge import TaskPlanExecutor
from robot_config import ROBOT_CONFIGS

app = FastAPI(title="포스틱 Task Plan API", version="1.1")

# 콘솔 UI(forstick_ex.html)가 듀얼 로봇 모드(start_dual_robots.sh, Panda=8090/
# UR5e=8091)에서 "다른 로봇 포트가 이미 떠 있는지" 확인하려고 자기 origin이
# 아닌 다른 포트로 fetch를 날린다(예: 8090 페이지에서 8091/health 확인) —
# 기본적으로 브라우저 CORS에 막히므로 두 포트 origin을 명시적으로 허용한다.
# 인증/쿠키가 없는 로컬 데모 API라 자격증명 없는 GET 몇 개만 열어주는 수준의
# 위험도임. LAN IP로 접속하는 경우도 있어서(README 참고) 호스트는 와일드카드,
# 포트만 8090/8091로 고정.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://[^/]+:(8090|8091)$",
    allow_methods=["GET"],
    allow_headers=["*"],
)

# 실행할 로봇 선택 — FORSTICK_ROBOT 환경변수(기본값 ur5e — 2026-09-09, 기획서가
# 명시한 실제 검증 대상 로봇으로 재정렬. Panda는 이번 스코프 재정렬 전 먼저
# 만들어져 있던 로봇이라 여전히 지원은 하되 기본값은 아님). Gazebo 쪽은 로봇마다
# 독립된 Gazebo 서버 + 네임스페이스 없는 동일 이름 노드(move_group 등)를 띄우므로
# 한 번에 하나의 로봇 스택만 가동 가능하다 — 그래서 로봇 선택은 요청 단위가 아니라
# 서버 기동 시점에 결정된다. 오타로 조용히 엉뚱한 로봇으로 도는 걸 막기 위해
# 잘못된 값이면 기동 시점에 바로 죽는다(fail-fast).
_ROBOT_TYPE = os.environ.get("FORSTICK_ROBOT", "ur5e")
if _ROBOT_TYPE not in ROBOT_CONFIGS:
    raise RuntimeError(
        f"FORSTICK_ROBOT={_ROBOT_TYPE!r} 은 지원하지 않는 값입니다. "
        f"가능한 값: {list(ROBOT_CONFIGS)}"
    )
_ROBOT_CONFIG = ROBOT_CONFIGS[_ROBOT_TYPE]

# 파이프라인 결과의 status를 HTTP 상태 코드로 매핑.
# gated는 클라이언트 잘못이 아니라 "정보가 더 필요하다"는 뜻이라 422(Unprocessable Entity)로,
# error는 우리 쪽/LLM 쪽 문제라 502(Bad Gateway)로 구분한다.
STATUS_TO_HTTP = {
    "success": 200,
    "gated": 422,    # 정보 부족/미등록 — 클라이언트가 다시 물어봐야 함
    "rejected": 422, # 완성된 계획이지만 L3 Safety Guard 규칙 위반
    "error": 502,    # 백엔드/LLM 쪽 문제
}

# ============================================================
# 로봇 실행기 — 서버 생명주기에 맞춰 한 번만 생성/정리.
# 여러 요청이 동시에 로봇을 움직이려 들지 않도록 락으로 직렬화한다.
# ============================================================

_robot_executor: TaskPlanExecutor | None = None
_executor_lock = threading.Lock()

# ============================================================
# 카메라 뷰어 — scene_camera 토픽을 구독하는 전용 노드.
# _robot_executor와는 별개 노드/별개 스레드에서 계속 spin한다
# (로봇 실행기는 요청 들어올 때만 spin_until_future_complete로
# 짧게 도는 방식이라, 카메라 프레임을 실시간으로 계속 받으려면
# 독립적으로 백그라운드에서 계속 spin하는 노드가 따로 필요함).
# ============================================================

_camera_frame_lock = threading.Lock()
_camera_frame_jpeg: bytes | None = None
_camera_node = None
_camera_executor = None
_camera_spin_thread = None


class CameraViewer(Node):
    def __init__(self):
        super().__init__("api_server_camera_viewer")
        self.create_subscription(RosImage, "/scene_camera/image", self._on_image, 5)

    def _on_image(self, msg: RosImage):
        global _camera_frame_jpeg
        try:
            # gz_ros2_bridge의 카메라 이미지는 보통 rgb8로 옴 (패딩 없음 가정).
            img = PILImage.frombytes("RGB", (msg.width, msg.height), bytes(msg.data))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            with _camera_frame_lock:
                _camera_frame_jpeg = buf.getvalue()
        except Exception as e:
            print(f"[api_server] 카메라 프레임 변환 실패: {e}")


@app.on_event("startup")
def startup_ros():
    global _robot_executor, _camera_node, _camera_executor, _camera_spin_thread
    try:
        rclpy.init()

        # 로봇 실행기: 이 노드 전용 SingleThreadedExecutor를 별도 스레드에서
        # 계속 spin시킨다 (enable_background_spin). 이렇게 해야 각 요청 스레드가
        # compute_ik 등에서 rclpy 전역 암묵 executor를 공유해서 충돌하는 문제
        # (RuntimeError: Executor is already spinning)가 안 생긴다.
        _robot_executor = TaskPlanExecutor(_ROBOT_CONFIG)
        _robot_executor.enable_background_spin()

        # 카메라 뷰어도 마찬가지로 자기 전용 executor를 쓴다 (bare rclpy.spin()은
        # 로봇 실행기와 같은 암묵적 전역 executor를 공유해서 위 문제를 일으킨다).
        _camera_node = CameraViewer()
        _camera_executor = SingleThreadedExecutor()
        _camera_executor.add_node(_camera_node)
        _camera_spin_thread = threading.Thread(
            target=_camera_executor.spin, daemon=True
        )
        _camera_spin_thread.start()

        print("[api_server] 로봇 실행기 + 카메라 뷰어(rclpy) 초기화 완료 (각자 전용 executor)")
    except Exception as e:
        # ROS2가 source 안 된 환경이거나 rclpy 관련 문제일 경우, 서버 자체는
        # 계속 뜨게 하되(계획 생성 기능은 로봇 없이도 유효) 실행/스트리밍
        # 요청만 실패시킨다.
        print(f"[api_server] 초기화 실패 (execute=true / 카메라 스트리밍 요청은 실패할 것): {e}")
        _robot_executor = None
        _camera_node = None
        _camera_executor = None


@app.on_event("shutdown")
def shutdown_ros():
    global _robot_executor, _camera_node, _camera_executor
    if _camera_executor is not None:
        _camera_executor.shutdown()
    if _camera_node is not None:
        _camera_node.destroy_node()
    if _robot_executor is not None:
        _robot_executor.destroy_node()
    if _robot_executor is not None or _camera_node is not None:
        rclpy.shutdown()


def _execute_task_plan(task_plan: dict) -> dict:
    if _robot_executor is None:
        return {
            "success": False,
            "message": "로봇 실행기가 초기화되지 않음 — 서버가 ROS2 환경에서 "
                       "시작되지 않았거나 Gazebo 시뮬레이션이 안 떠 있을 수 있음",
        }
    with _executor_lock:
        ok = _robot_executor.run_plan(task_plan)
    return {"success": ok}


# ============================================================
# Gazebo 화면 MJPEG 스트리밍 — CameraViewer가 구독한 /scene_camera/image
# 프레임을 multipart/x-mixed-replace로 흘려보낸다.
# <img src="/gazebo-stream.mjpeg"> 태그로 브라우저에서 바로 볼 수 있는
# 표준 MJPEG-over-HTTP 방식.
# ============================================================

def _gazebo_mjpeg_frames():
    """/scene_camera/image 구독 콜백이 채워주는 _camera_frame_jpeg를
    폴링하면서, 새 프레임이 도착할 때마다 클라이언트로 흘려보낸다."""
    last_sent = None
    waited_without_frame = 0
    while True:
        with _camera_frame_lock:
            frame = _camera_frame_jpeg
        if frame is not None:
            if frame is not last_sent:
                last_sent = frame
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
        else:
            waited_without_frame += 1
            if waited_without_frame == 1:
                print("[api_server] 아직 /scene_camera/image 프레임을 못 받음 — "
                      "Gazebo가 떠 있는지, scene_camera 센서가 포함된 world를 "
                      "쓰고 있는지 확인 필요")
        time.sleep(1 / 15)


@app.get("/gazebo-stream.mjpeg")
def gazebo_stream():
    return StreamingResponse(
        _gazebo_mjpeg_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


class TextRequest(BaseModel):
    text: str
    execute: bool = False  # true면 계획 생성 후 바로 Gazebo 로봇 실행까지 수행


@app.get("/health")
def health():
    return {
        "status": "ok",
        "robot_connected": _robot_executor is not None,
        "robot_type": _ROBOT_CONFIG.robot_id,   # "panda" | "ur5e"
        "robot_label": _ROBOT_CONFIG.label,     # "Panda" | "UR5e + Robotiq 2F-85"
    }


class SwitchRobotRequest(BaseModel):
    robot: str


_switch_in_progress = False
_switch_lock = threading.Lock()


@app.post("/v1/robot/switch")
def switch_robot(req: SwitchRobotRequest):
    """웹 UI에서 로봇을 바꾸는 엔드포인트. 실제로는 start_robot.sh를 백그라운드로
    실행시킬 뿐이다 — start_robot.sh가 지금 떠 있는 Gazebo+MoveIt/이 API 서버
    프로세스 자체를 내리고 새 로봇으로 다시 띄운다. 그래서 이 요청을 처리 중인
    프로세스가 스크립트에 의해 곧 죽는다 — subprocess를 start_new_session=True로
    띄워서 이 프로세스가 죽어도 스크립트는 계속 살아남아 전환을 끝까지 진행한다.
    클라이언트는 응답을 받은 뒤 /health를 폴링해서 전환 완료를 확인해야 한다
    (그 사이엔 서버가 잠깐 내려가 있으므로 연결 실패가 정상)."""
    global _switch_in_progress
    if req.robot not in ROBOT_CONFIGS:
        return JSONResponse(
            status_code=422,
            content={"success": False, "message": f"알 수 없는 로봇: {req.robot!r} (가능한 값: {list(ROBOT_CONFIGS)})"},
        )
    if req.robot == _ROBOT_TYPE:
        return {"success": True, "already": True, "message": f"이미 {_ROBOT_CONFIG.label}로 떠 있습니다."}
    with _switch_lock:
        if _switch_in_progress:
            return JSONResponse(status_code=409, content={"success": False, "message": "이미 다른 전환이 진행 중입니다."})
        _switch_in_progress = True  # 이 프로세스는 곧 죽으므로 리셋할 필요 없음 — 새 프로세스는 False로 시작.

    forstick_dir = Path(__file__).parent
    script = forstick_dir / "start_robot.sh"
    log_dir = forstick_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    switch_log = log_dir / f"switch_{req.robot}_{int(time.time())}.log"
    with open(switch_log, "w") as logf:
        subprocess.Popen(
            [str(script), req.robot],
            stdout=logf, stderr=subprocess.STDOUT,
            cwd=str(forstick_dir),
            start_new_session=True,
        )
    return {
        "success": True,
        "message": f"{ROBOT_CONFIGS[req.robot].label}로 전환을 시작했습니다. "
                    f"서버가 재기동되니 /health를 몇 초 간격으로 폴링하세요 (최대 2~3분).",
        "log": str(switch_log),
    }


@app.get("/", response_class=HTMLResponse)
def demo_page():
    """데모 페이지를 API 서버와 같은 origin(localhost:8080)에서 서빙한다.
    마이크(getUserMedia)는 브라우저가 https 또는 localhost에서만 허용하므로,
    파일을 그냥 열지 말고 반드시 이 경로(http://localhost:8080/)로 접속해야 한다."""
    demo_path = Path(__file__).parent / "demo.html"
    return demo_path.read_text(encoding="utf-8")


@app.get("/console", response_class=HTMLResponse)
def console_page():
    """forstick_ex.html(작업 콘솔/Task Plan/안전 검증/판정/시뮬레이션/감사 로그
    풀 화면 UI)을 같은 origin에서 서빙한다. demo_page()와 동일한 이유로 마이크를
    쓰려면 파일을 직접 열지 말고 반드시 이 경로로 접속해야 한다."""
    console_path = Path(__file__).parent / "forstick_ex.html"
    return console_path.read_text(encoding="utf-8")


@app.get("/v1/capability-profile")
def get_capability_profile():
    """현재 로봇이 알고 있는 위치/물건 목록. 다른 팀원 쪽 UI나 미들웨어에서
    선택지를 보여줄 때 참고용으로 쓸 수 있음."""
    return CAPABILITY_PROFILE


@app.get("/v1/audit-log")
def get_audit_log(limit: int = 200):
    """pipeline.write_audit_log()가 쌓아온 audit_log.jsonl을 최신순으로 반환.
    파일 자체가 진실의 원천 — 여기서는 그대로 읽어서 파싱만 한다."""
    path = Path(AUDIT_LOG_PATH)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    entries.reverse()
    return entries[:limit]


class ExecuteRequest(BaseModel):
    task_plan: dict


@app.post("/v1/execute")
def execute_existing_plan(req: ExecuteRequest):
    """/v1/task-plan(execute=false)로 이미 만들어둔 task_plan을 그대로 실행.
    새로 계획을 생성하지 않으므로 plan_id/plan_hash가 그대로 유지된다."""
    return _execute_task_plan(req.task_plan)


@app.post("/v1/task-plan")
def create_task_plan_from_text(req: TextRequest):
    raw_text, confidence = transcribe_text_stub(req.text)
    result = process_utterance(raw_text, confidence)
    if req.execute and result["status"] == "success":
        result["execution"] = _execute_task_plan(result["task_plan"])
    status_code = STATUS_TO_HTTP.get(result["status"], 500)
    return JSONResponse(status_code=status_code, content=result)


@app.post("/v1/task-plan/audio")
def create_task_plan_from_audio(file: UploadFile = File(...), execute: bool = Form(False)):
    suffix = Path(file.filename).suffix or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        raw_text, confidence = transcribe_audio(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    result = process_utterance(raw_text, confidence)
    if execute and result["status"] == "success":
        result["execution"] = _execute_task_plan(result["task_plan"])
    status_code = STATUS_TO_HTTP.get(result["status"], 500)
    return JSONResponse(status_code=status_code, content={**result, "utterance": raw_text})
