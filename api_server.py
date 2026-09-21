"""
포스틱 백엔드 API 서버.

실행 순서:
  1) vLLM 서버가 이미 8000번 포트에서 떠 있어야 함 (별도 터미널)
  2) (별도 /v1/execute 승인 후 로봇 실행까지 하려면) ur5e_robotiq_gazebo.launch.py가
     이미 떠 있어야 함 (별도 터미널) — 그 터미널과 동일하게 아래 env가
     이 서버를 실행하는 터미널에도 필요:
       source /opt/ros/lyrical/setup.bash
       export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
  3) 로봇을 실제로 움직이는 API라 인증 토큰이 필수임 — 토큰 없이는 서버가
     기동조차 안 됨:
       export FORSTICK_API_TOKEN=<임의의 긴 무작위 문자열>
  4) 이 서버 실행: uvicorn api_server:app --host 0.0.0.0 --port 8080 --reload
     (--reload는 rclpy 노드를 파일 변경마다 재시작시켜 문제가 될 수 있으니
     로봇 실행 기능을 테스트할 때는 --reload 빼고 실행 권장)

동작 확인 (계획만 생성, 로봇 실행 안 함 — 기존과 동일. 계획/실행/정지/로봇전환
POST 엔드포인트는 모두 Authorization: Bearer <FORSTICK_API_TOKEN> 헤더 필요):
  curl -X POST http://localhost:8080/v1/task-plan \
       -H "Content-Type: application/json" \
       -H "Authorization: Bearer $FORSTICK_API_TOKEN" \
       -d '{"text": "2번 팔레트에서 A자재를 집어서 컨베이어에 올려줘"}'

동작 확인 (서버가 발급한 plan_id로 실제 Gazebo 로봇 실행):
  curl -X POST http://localhost:8080/v1/execute \
       -H "Content-Type: application/json" \
       -H "Authorization: Bearer $FORSTICK_API_TOKEN" \
       -d '{"plan_id": "<계획 생성 응답의 plan_id>"}'

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
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from PIL import Image as PILImage
from sensor_msgs.msg import Image as RosImage

from pipeline import (
    AUDIT_LOG_PATH,
    CAPABILITY_PROFILE,
    transcribe_text_stub,
    transcribe_audio,
    process_utterance,
    validate_task_plan_for_execution,
    write_audit_log,
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

# 콘솔 UI(forstick_ex.html)는 로봇을 여러 대 동시에 띄우는 듀얼 모드에서
# "다른 로봇 포트가 이미 떠 있는지" 확인하려고 자기 origin이 아닌 다른
# 포트로 fetch를 날린다(예: 8090 페이지에서 8091/health 확인) — 기본적으로
# 브라우저 CORS에 막히므로 허용할 포트를 명시적으로 열어준다.
# GET 몇 개만 열어주는 수준이라 위험도는 낮지만, POST 계열은 어차피 토큰
# 인증(require_api_token)으로 막혀 있음. Panda+UR5e 듀얼 구성(8090/8091)은
# Panda 제거로 현재는 안 쓰지만, 나중에 두 번째 로봇(FR3-WMS 등)을 추가하면
# 그대로 재사용할 수 있게 포트는 하드코딩 대신 환경변수로 남겨둔다.
_CORS_PORTS = os.environ.get("FORSTICK_CORS_PORTS", "8090|8091")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=rf"^https?://[^/]+:({_CORS_PORTS})$",
    allow_methods=["GET"],
    allow_headers=["*"],
)

# 실행할 로봇 선택 — FORSTICK_ROBOT 환경변수(기본값이자 현재 유일한 지원
# 로봇은 ur5e — 2026-09-09, 기획서가 명시한 실제 검증 대상 로봇. 개발 초기에
# 먼저 만들었던 Panda는 스코프 재정렬 후 제거함, robot_config.py 참고).
# Gazebo 쪽은 로봇마다
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

# 실제 배포에서는 인증 없이 로봇을 움직이거나 멈출 수 있으면 안 되므로,
# FORSTICK_ROBOT과 동일한 fail-fast 패턴으로 토큰을 필수 환경변수로 요구한다.
# 로컬 개발 편의를 위한 기본값은 일부러 두지 않는다 — 빈 값이면 시작 자체를 막는다.
_API_TOKEN = os.environ.get("FORSTICK_API_TOKEN", "")
if not _API_TOKEN:
    raise RuntimeError(
        "FORSTICK_API_TOKEN 환경변수가 설정되지 않았습니다. "
        "로봇을 실제로 움직이는 API이므로 인증 토큰 없이는 기동하지 않습니다."
    )


def require_api_token(authorization: str | None = Header(default=None)):
    """POST 엔드포인트(계획 생성/실행/정지/로봇 전환)에 붙는 인증 의존성.
    'Authorization: Bearer <token>' 형식만 허용하고, secrets.compare_digest로
    비교해 타이밍 사이드채널로 토큰이 조금씩 새는 걸 방지한다."""
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="인증 토큰이 필요합니다.")
    token = authorization[len(prefix):]
    if not secrets.compare_digest(token, _API_TOKEN):
        raise HTTPException(status_code=401, detail="유효하지 않은 인증 토큰입니다.")


require_auth = Depends(require_api_token)

# 파이프라인 결과의 status를 HTTP 상태 코드로 매핑.
# gated는 클라이언트 잘못이 아니라 "정보가 더 필요하다"는 뜻이라 422(Unprocessable Entity)로,
# error는 우리 쪽/LLM 쪽 문제라 502(Bad Gateway)로 구분한다.
STATUS_TO_HTTP = {
    "success": 200,
    "gated": 422,    # 정보 부족/미등록 — 클라이언트가 다시 물어봐야 함
    "rejected": 422, # 완성된 계획이지만 L3 Safety Guard 규칙 위반
    "stop_requested": 200,
    "error": 502,    # 백엔드/LLM 쪽 문제
}

# ============================================================
# 로봇 실행기 — 서버 생명주기에 맞춰 한 번만 생성/정리.
# 여러 요청이 동시에 로봇을 움직이려 들지 않도록 락으로 직렬화한다.
# ============================================================

_robot_executor: TaskPlanExecutor | None = None
_executor_lock = threading.Lock()

# ponytail: 단일 프로세스 데모용 저장소. 다중 인스턴스/재시작 보존이 필요하면 DB로 교체.
_issued_plans: dict[str, dict] = {}
_issued_plans_lock = threading.Lock()
_active_plan_id: str | None = None
# STOP은 "그 순간의 동작 하나만 멈춤"이 아니라 산업용 비상정지처럼 래칭된다 —
# 한 번 세팅되면 /v1/resume으로 운영자가 명시적으로 풀기 전까지 그 어떤
# plan_id claim도 통과하지 못한다. 대기 중이던 실행 요청이 STOP 직후
# 자동으로 이어받는 걸 막기 위한 장치(_issued_plans_lock으로 claim과
# 같은 락을 공유해 경쟁조건 없이 동기화됨).
_global_stopped: bool = False

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
        print(f"[api_server] 초기화 실패 (실행 API / 카메라 스트리밍 요청은 실패할 것): {e}")
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


def _register_issued_plan(task_plan: dict):
    with _issued_plans_lock:
        _issued_plans[task_plan["plan_id"]] = {"task_plan": task_plan, "status": "issued"}


def _claim_issued_plan(plan_id: str) -> tuple[dict | None, dict | None]:
    global _active_plan_id
    with _issued_plans_lock:
        if _global_stopped:
            return None, {"success": False, "reason_code": "E-EXEC-STOPPED-LATCHED",
                          "message": "정지 상태입니다. /v1/resume으로 재개한 뒤 다시 시도하세요."}
        record = _issued_plans.get(plan_id)
        if record is None:
            return None, {"success": False, "reason_code": "E-EXEC-UNKNOWN",
                          "message": "서버가 발급한 Task Plan을 찾을 수 없음"}
        if record["status"] != "issued":
            return None, {"success": False, "reason_code": "E-EXEC-REPLAY",
                          "message": f"이미 처리된 Task Plan임: {record['status']}"}

        task_plan = record["task_plan"]
        violations = validate_task_plan_for_execution(task_plan, _ROBOT_CONFIG.robot_id)
        if violations:
            record["status"] = "rejected"
            return None, {"success": False, "reason_code": violations[0]["code"],
                          "message": violations[0]["message"], "violations": violations}

        record["status"] = "executing"
        _active_plan_id = plan_id
        return task_plan, None


def _finish_plan(plan_id: str, status: str):
    global _active_plan_id
    with _issued_plans_lock:
        record = _issued_plans.get(plan_id)
        if record is not None and record["status"] != "stopped":
            record["status"] = status
        if _active_plan_id == plan_id:
            _active_plan_id = None


def _plan_status(plan_id: str) -> str | None:
    with _issued_plans_lock:
        record = _issued_plans.get(plan_id)
        return record["status"] if record is not None else None


def _execute_task_plan(plan_id: str) -> dict:
    if _robot_executor is None:
        return {
            "success": False,
            "reason_code": "E-EXEC-NOT-READY",
            "message": "로봇 실행기가 초기화되지 않음 — 서버가 ROS2 환경에서 "
                       "시작되지 않았거나 Gazebo 시뮬레이션이 안 떠 있을 수 있음",
        }

    with _executor_lock:
        # clear_stop()은 여기서 자동으로 부르지 않는다 — STOP은 래칭되므로
        # /v1/resume이 명시적으로 풀어주기 전까지는 애초에 _claim_issued_plan에서
        # 걸러진다(아래 참고). 여기서 자동으로 풀면 "정지했는데 다음 계획이
        # 바로 이어받는" 원래 버그가 다시 생긴다.
        task_plan, error = _claim_issued_plan(plan_id)
        if error:
            return error

        utterance = task_plan.get("utterance", "")
        confidence = (task_plan.get("audit") or {}).get("stt_confidence", 1.0)
        write_audit_log("EXECUTION_STARTED", None, utterance, confidence, {"plan_id": plan_id})
        try:
            ok = _robot_executor.run_plan(task_plan)
        except Exception as exc:
            _finish_plan(plan_id, "failed")
            write_audit_log("EXECUTION_FAILED", "E-EXEC-ERROR", utterance, confidence,
                            {"plan_id": plan_id, "error": str(exc)})
            return {"success": False, "reason_code": "E-EXEC-ERROR", "message": str(exc)}

    stopped = _plan_status(plan_id) == "stopped"
    _finish_plan(plan_id, "completed" if ok else "failed")
    event = "EXECUTION_COMPLETED" if ok else ("EXECUTION_STOPPED" if stopped else "EXECUTION_FAILED")
    reason_code = None if ok else ("E-EXEC-STOPPED" if stopped else "E-EXEC-FAILED")
    write_audit_log(event, reason_code, utterance, confidence,
                    {"plan_id": plan_id})
    return {"success": ok, "plan_id": plan_id, "reason_code": reason_code,
            "message": "실행 완료" if ok else ("정지 요청으로 실행 중단" if stopped else "실행 실패")}


def _request_stop(source: str, utterance: str = "", confidence: float = 1.0) -> dict:
    global _global_stopped
    if _robot_executor is None:
        return {"success": False, "reason_code": "E-STOP-NOT-READY",
                "message": "로봇 실행기가 초기화되지 않음"}

    # 래칭: 실행 중인 계획이 있든 없든 항상 먼저 세운다 — 이 시점에 다른
    # 요청이 아직 claim하지 않은 계획을 들고 _executor_lock을 기다리고
    # 있었다면, 그 claim은 (같은 _issued_plans_lock으로 직렬화되는)
    # _claim_issued_plan에서 이 플래그를 보고 반드시 거부된다.
    with _issued_plans_lock:
        _global_stopped = True
        plan_id = _active_plan_id

    goal_cancelled = None
    if plan_id is not None:
        goal_cancelled = _robot_executor.request_stop()
        with _issued_plans_lock:
            if plan_id in _issued_plans:
                _issued_plans[plan_id]["status"] = "stopped"

    write_audit_log("STOP_REQUESTED", None, utterance, confidence,
                    {"plan_id": plan_id, "source": source,
                     "active_goal_cancelled": goal_cancelled, "latched": True})
    return {
        "success": True,
        "plan_id": plan_id,
        "latched": True,
        "active_goal_cancel_confirmed": goal_cancelled,
        "message": "정지 상태로 전환됨 — /v1/resume으로 재개하기 전까지 새 계획을 실행할 수 없습니다."
                   if plan_id is not None else
                   "정지 상태로 전환됨(실행 중인 계획 없음) — 대기 중이던 실행 요청도 차단됩니다.",
    }


def _request_resume(source: str = "ui") -> dict:
    global _global_stopped
    if _robot_executor is None:
        return {"success": False, "reason_code": "E-RESUME-NOT-READY",
                "message": "로봇 실행기가 초기화되지 않음"}
    with _issued_plans_lock:
        was_stopped = _global_stopped
        _global_stopped = False
    _robot_executor.clear_stop()
    write_audit_log("RESUMED", None, "", 1.0, {"source": source, "was_stopped": was_stopped})
    return {"success": True, "was_stopped": was_stopped,
            "message": "정지 상태를 해제했습니다. 이제 새 계획을 실행할 수 있습니다."}


def _finalize_pipeline_result(result: dict, utterance: str, confidence: float) -> dict:
    if result["status"] == "success":
        _register_issued_plan(result["task_plan"])
    elif result["status"] == "stop_requested":
        result["stop"] = _request_stop("voice", utterance, confidence)
    return result


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
    model_config = ConfigDict(extra="forbid")
    text: str


@app.get("/health")
def health():
    return {
        "status": "ok",
        "robot_connected": _robot_executor is not None,
        "robot_type": _ROBOT_CONFIG.robot_id,   # "ur5e" (등록된 로봇, robot_config.ROBOT_CONFIGS 참고)
        "robot_label": _ROBOT_CONFIG.label,     # "UR5e + Robotiq 2F-85"
        "stopped": _global_stopped,             # true면 /v1/resume 전까지 실행 전부 거부됨
    }


class SwitchRobotRequest(BaseModel):
    robot: str


_switch_in_progress = False
_switch_lock = threading.Lock()

LOG_RETENTION_COUNT = int(os.environ.get("FORSTICK_LOG_RETENTION_COUNT", 20))


def _prune_old_logs(log_dir: Path, pattern: str, keep: int = LOG_RETENTION_COUNT):
    """이벤트별로 새 파일이 생기는 로그(전환/기동 로그 등)는 회전이 아니라
    개수 상한이 맞다 — 오래된 파일부터 지워서 최근 keep개만 남긴다."""
    files = sorted(log_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
    for old in files[:-keep] if keep > 0 else files:
        old.unlink(missing_ok=True)


@app.post("/v1/robot/switch", dependencies=[require_auth])
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
    with _issued_plans_lock:
        if _active_plan_id is not None:
            return JSONResponse(
                status_code=409,
                content={"success": False, "message": "계획 실행 중에는 로봇을 전환할 수 없습니다."},
            )
    with _switch_lock:
        if _switch_in_progress:
            return JSONResponse(status_code=409, content={"success": False, "message": "이미 다른 전환이 진행 중입니다."})
        _switch_in_progress = True  # 이 프로세스는 곧 죽으므로 리셋할 필요 없음 — 새 프로세스는 False로 시작.

    forstick_dir = Path(__file__).parent
    script = forstick_dir / "start_robot.sh"
    log_dir = forstick_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    _prune_old_logs(log_dir, "switch_*.log")
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


@app.get("/v1/audit-log", dependencies=[require_auth])
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
    model_config = ConfigDict(extra="forbid")
    plan_id: str


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = "ui"


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = "ui"


@app.post("/v1/execute", dependencies=[require_auth])
def execute_existing_plan(req: ExecuteRequest):
    """서버가 발급·보관한 계획만 실행하고 실행 직전 전체 검증을 다시 수행한다."""
    result = _execute_task_plan(req.plan_id)
    return JSONResponse(status_code=200 if result["success"] else 409, content=result)


@app.post("/v1/stop", dependencies=[require_auth])
def stop_active_execution(req: StopRequest):
    """정지 상태는 래칭된다 — /v1/resume 전까지 어떤 plan_id claim도 통과 못 함."""
    result = _request_stop(req.source)
    return JSONResponse(status_code=200 if result["success"] else 409, content=result)


@app.post("/v1/resume", dependencies=[require_auth])
def resume_from_stop(req: ResumeRequest):
    """운영자가 명시적으로 정지 래치를 해제한다. 이 호출 전까지는 /v1/execute가
    E-EXEC-STOPPED-LATCHED로 전부 거부된다."""
    result = _request_resume(req.source)
    return JSONResponse(status_code=200 if result["success"] else 409, content=result)


@app.post("/v1/task-plan", dependencies=[require_auth])
def create_task_plan_from_text(req: TextRequest):
    raw_text, confidence = transcribe_text_stub(req.text)
    result = process_utterance(raw_text, confidence, robot_id=_ROBOT_CONFIG.robot_id)
    result = _finalize_pipeline_result(result, raw_text, confidence)
    status_code = STATUS_TO_HTTP.get(result["status"], 500)
    return JSONResponse(status_code=status_code, content=result)


@app.post("/v1/task-plan/audio", dependencies=[require_auth])
def create_task_plan_from_audio(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        raw_text, confidence = transcribe_audio(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    result = process_utterance(raw_text, confidence, robot_id=_ROBOT_CONFIG.robot_id)
    result = _finalize_pipeline_result(result, raw_text, confidence)
    status_code = STATUS_TO_HTTP.get(result["status"], 500)
    return JSONResponse(status_code=status_code, content={**result, "utterance": raw_text})
