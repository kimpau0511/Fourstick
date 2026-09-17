"""실기 FR3 어댑터 **경계** (8-12).

**실제 로봇에 명령을 보내지 않는다.** 이 클래스는 `RobotAdapter` 계약을
만족하는 자리를 잡아 두고, 설정과 준비 근거가 없으면 **생성·연결 단계에서
막는다.**

## 계약은 Gazebo 어댑터와 같다

결과는 `core.execution_result.ExecutionResult`다 — 시뮬레이터 어댑터와 같은
다섯 축(state / request_accepted / task_succeeded / verified / reason)을 쓴다.
그래서 상위 계층(판정·저장·화면)이 두 어댑터를 구분해 다룰 필요가 없다.

## 다른 점은 두 가지다

1. `adapter_kind = "real"`, `is_simulated_adapter = False` — 이 어댑터의
   결과는 시뮬레이션으로 집계되지 않는다.
2. **준비 근거가 없으면 아무것도 하지 않는다.** 생성 시 실기 준비 판정을
   받아 `real_hardware_ready`가 아니면 `connect`가 거절한다.

## 시뮬레이터 결과를 재사용하지 않는다

`state()`는 실기 장치 관측만 쓴다. Gazebo 이송 시연 결과(`simulation_e2e`)를
관측값으로 넣는 경로가 없다 — 생성자가 그런 인자를 받지 않고, 테스트가
모듈에 `simulation` 관련 참조가 없는지 확인한다.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from core.capability_profile import CapabilityProfile
from core.execution_result import ExecutionResult, rejected
from core.execution_state import ExecutionState
from core.reason_codes import ReasonCode
from robots.base.robot_adapter import RobotAdapter, RobotStateSnapshot
from robots.hardware.config import HardwareConfigError, HardwareConnection


class FR3HardwareAdapter(RobotAdapter):
    """실기 FR3 어댑터. **연결 구현이 아직 없다 — 경계만 있다.**"""

    #: 어댑터 종류. 시뮬레이터가 아니다.
    adapter_kind = "real"
    #: 이 어댑터는 시뮬레이션이 아니다. 연결 확인은 별도로 한다.
    is_simulated_adapter = False

    def __init__(
        self,
        robot_id: str,
        profile: CapabilityProfile,
        *,
        connection: HardwareConnection,
        readiness: Any,
        now: Callable[[], float] = time.time,
    ):
        """설정과 준비 근거가 모두 있어야 만들 수 있다.

        `readiness`는 `validation.hardware_readiness.HardwareReadinessResult`
        형태여야 한다(`real_hardware_ready` 속성을 본다). 준비되지 않은 상태로
        어댑터를 만들 수는 있지만, 그 사실이 객체에 남고 `connect`가 거절한다 —
        **"만들어졌으니 쓸 수 있다"는 착각을 막는다.**
        """
        super().__init__(robot_id, profile)
        if connection is None:
            raise HardwareConfigError(
                ReasonCode.HARDWARE_ADAPTER_UNCONFIGURED,
                "실기 접속 설정 없이 어댑터를 만들 수 없다")
        if not connection.complete:
            raise HardwareConfigError(
                ReasonCode.HARDWARE_ADAPTER_UNCONFIGURED,
                "관절·힘·속도 한계 설정이 없다 — 한계를 모르는 채로 명령을"
                " 보내지 않는다")
        if readiness is None or not hasattr(readiness, "real_hardware_ready"):
            raise HardwareConfigError(
                ReasonCode.HARDWARE_INPUT_MISSING,
                "실기 준비 판정 없이 어댑터를 만들 수 없다"
                " (validation/hardware_readiness.py)")
        self._connection = connection
        self._readiness = readiness
        self._now = now
        self._connected = False

    # ── 준비 상태 ───────────────────────────────────────────────────────
    @property
    def ready(self) -> bool:
        return bool(getattr(self._readiness, "real_hardware_ready", False))

    def readiness_dict(self) -> dict:
        to_dict = getattr(self._readiness, "to_dict", None)
        return to_dict() if callable(to_dict) else {}

    def _blocked(self, detail: str) -> ExecutionResult:
        """준비되지 않은 상태의 모든 요청에 같은 답을 준다."""
        missing = tuple(getattr(self._readiness, "missing_labels", ()) or ())
        return rejected(ReasonCode.HARDWARE_INPUT_MISSING, {
            "detail": detail,
            "missing": list(missing[:12]),
            "missing_count": len(missing),
            "gate": "validation/hardware_readiness.py",
            "note": "실기 준비 근거가 채워지기 전에는 실제 로봇에 명령을"
                    " 보내지 않는다",
        })

    # ── 연결 ────────────────────────────────────────────────────────────
    def connect(self, timeout_sec: float) -> ExecutionResult:
        """준비 근거가 없으면 **연결을 시도조차 하지 않는다.**"""
        if not self.ready:
            self._connected = False
            return self._blocked(
                "실기 준비 판정이 real_hardware_ready=false다 —"
                " 연결을 시도하지 않는다")
        # 준비가 끝난 뒤 실제 연결을 구현한다. 지금은 구현이 없다는 사실을
        # 그대로 돌려준다 — 없는 연결을 있는 것처럼 두지 않는다.
        self._connected = False
        return rejected(ReasonCode.HARDWARE_NOT_CONNECTED, {
            "detail": "실기 연결 구현이 아직 없다 —"
                      " 이 단계에서는 실제 로봇에 명령을 보내지 않는다",
            "arm_kind": self._connection.arm_kind,
            "gripper_kind": self._connection.gripper_kind,
            "next": "실기 준비가 끝난 뒤 접속 구현을 붙인다",
        })

    def disconnect(self) -> None:
        self._connected = False

    def state(self) -> RobotStateSnapshot:
        """**실기 장치 관측만 쓴다.** 관측이 없으면 없다고 말한다."""
        return RobotStateSnapshot(
            state=ExecutionState.UNKNOWN,
            observed_at=0.0,
            joint_positions={},
            joint_velocities={},
            # 관측하지 못했다. 이 자리에 시뮬레이터 값을 넣지 않는다.
            valid=False,
            held_object=None,
            hold_observed=False,
        )

    def check(self) -> ExecutionResult:
        return self._blocked("실기 점검은 준비 근거가 채워진 뒤에 한다")

    # ── 스킬 ────────────────────────────────────────────────────────────
    def home(self, timeout_sec: float) -> ExecutionResult:
        return self._blocked("home은 실기 준비가 끝난 뒤에만 보낸다")

    def move(self, target: str, timeout_sec: float) -> ExecutionResult:
        return self._blocked(f"move({target})는 실기 준비가 끝난 뒤에만 보낸다")

    def pick(self, obj: str, source: str, timeout_sec: float) -> ExecutionResult:
        return self._blocked(
            f"pick({obj}, {source})는 실기 파지 관측이 확보된 뒤에만 보낸다")

    def place(self, obj: str, destination: str,
              timeout_sec: float) -> ExecutionResult:
        return self._blocked(
            f"place({obj}, {destination})는 실기 파지 관측이 확보된 뒤에만"
            " 보낸다")

    def stop(self, timeout_sec: float) -> ExecutionResult:
        """정지는 **연결됐을 때만** 의미가 있다.

        연결이 없으면 "정지시켰다"고 말할 수 없다 — 움직이는 것이 없다.
        그 사실을 그대로 돌려준다.
        """
        return rejected(ReasonCode.HARDWARE_NOT_CONNECTED, {
            "detail": "실기에 연결되지 않았다 — 정지시킬 동작이 없다",
            "note": "연결 구현이 붙은 뒤 정지는 관측으로 확인한다"
                    " (요청 수락만으로 정지로 적지 않는다)",
        })

    def cancel(self, timeout_sec: float) -> ExecutionResult:
        return self.stop(timeout_sec)

    def confirm_stopped(self, timeout_sec: float) -> ExecutionResult:
        return rejected(ReasonCode.HARDWARE_NOT_CONNECTED, {
            "detail": "실기 관측이 없어 정지를 확인할 수 없다",
        })

    def supports(self, skill: str) -> bool:
        """준비되지 않은 어댑터는 **어떤 스킬도 지원하지 않는다.**"""
        if not self.ready:
            return False
        return skill in tuple(self.profile.supported_skills)

    def status(self) -> dict:
        """화면·기록용 요약. 접속 대상 값을 담지 않는다."""
        return {
            "robot_id": self.robot_id,
            "adapter": f"{type(self).__module__}.{type(self).__name__}",
            "adapter_kind": self.adapter_kind,
            "is_simulated": self.is_simulated_adapter,
            "real_hardware_ready": self.ready,
            "connected": self._connected,
            "connection": self._connection.to_dict(),
            "note": "실기 어댑터 경계다. 연결 구현이 아직 없고 실제 로봇에"
                    " 명령을 보내지 않는다",
        }
