"""실기 2F-85 **파지 관측** 어댑터 경계 (8-12).

**실제 그리퍼에 접속하지 않는다.** 이 클래스는 파지 관측이 어디서 어떤 형태로
들어와야 하는지를 고정한다.

## 무엇을 요구하는가

`observe(object_id)`는 `core.grasp_observation.GraspObservation`을 돌려주고,
실기 관측으로 인정받으려면 다음이 **모두** 있어야 한다.

- 관측 시각 · 로봇 id · 그리퍼 id · 대상 물체 id
- 관측 방식(어떤 신호로 판정했는가)
- 장치가 준 **원본 상태**(레지스터·비트 값)

## 무엇을 거부하는가

- **개구 일치만으로 `object_held=true`를 만들지 않는다.** 개구를 넘겨도
  원본 장치 상태가 없으면 관측이 아니다.
- **대상 물체 id가 없으면 파지 성공으로 판정하지 않는다.**
- 시뮬레이터 관측(`simulated_observation`)을 실기 관측으로 바꾸지 않는다 —
  `from_simulation()`은 예외를 던진다.
- 설정이 없으면 관측기를 만들 수 없다.

## 레지스터 이름을 코드에 박지 않는다

어떤 레지스터·비트에서 어떤 값으로 오는지는 **설정**이 정한다
(`grasp_observation_interface` 입력). 이 파일은 "원본 상태가 있어야 한다"만
요구한다.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from core.grasp_observation import (
    GraspObservation,
    GraspObservationError,
    measured_from_device,
    unavailable,
)
from core.reason_codes import ReasonCode
from robots.hardware.config import HardwareConfigError, HardwareConnection


class Robotiq2F85HardwareObservationAdapter:
    """실기 2F-85 파지 관측 경계. **접속 구현이 아직 없다.**"""

    #: 이 관측기가 만드는 관측의 종류. 시뮬레이터가 아니다.
    observation_kind = "measured"

    def __init__(
        self,
        *,
        robot_id: str,
        gripper_id: str,
        connection: HardwareConnection,
        #: 장치 원본 상태를 읽는 함수. 없으면 관측 수단이 없는 것이다.
        read_device_state: Callable[[], Mapping[str, Any]] | None = None,
        #: 원본 상태에서 파지 여부를 뽑는 함수. **설정이 정한다.**
        interpret: Callable[[Mapping[str, Any]], bool | None] | None = None,
        now: Callable[[], float] = time.time,
    ):
        if not robot_id or not gripper_id:
            raise HardwareConfigError(
                ReasonCode.HARDWARE_ADAPTER_UNCONFIGURED,
                "관측기에는 로봇 id와 그리퍼 id가 있어야 한다 —"
                " 어느 장비에서 읽었는지 없이 관측을 남기지 않는다")
        if connection is None or not connection.gripper_endpoint:
            raise HardwareConfigError(
                ReasonCode.HARDWARE_ADAPTER_UNCONFIGURED,
                "그리퍼 접속 설정이 없다 — 관측기를 만들 수 없다")
        self.robot_id = robot_id
        self.gripper_id = gripper_id
        self._connection = connection
        self._read = read_device_state
        self._interpret = interpret
        self._now = now

    @property
    def available(self) -> bool:
        """원본 상태를 읽고 해석할 수 있는가. 둘 다 있어야 한다."""
        return self._read is not None and self._interpret is not None

    # ── 관측 ────────────────────────────────────────────────────────────
    def observe(self, object_id: str | None) -> GraspObservation:
        """파지 상태를 관측한다. **못 하면 `unavailable`을 돌려준다.**

        예외를 던져 호출자가 "모름"을 성공으로 오해하게 두지 않는다.
        """
        if not self.available:
            return unavailable(
                detail="실기 파지 관측 경로가 없다 —"
                       " 장치 상태 읽기와 해석 규칙이 설정에 없다",
                source=f"{self.observation_kind}:{self.gripper_id}")
        if not object_id:
            # 무엇을 쥐었다고 말할 수 없으면 파지 관측이 아니다.
            return unavailable(
                detail="대상 물체 id가 없다 — 무엇을 쥐었는지 없이 파지"
                       " 성공으로 판정하지 않는다",
                source=f"{self.observation_kind}:{self.gripper_id}")
        try:
            raw = dict(self._read() or {})
        except Exception as exc:  # noqa: BLE001 — 읽기 실패는 관측 없음이다
            return unavailable(
                detail=f"장치 상태를 읽지 못했다: {type(exc).__name__}"[:120],
                source=f"{self.observation_kind}:{self.gripper_id}")
        if not raw:
            return unavailable(
                detail="장치가 준 원본 상태가 비어 있다 —"
                       " 원본 값 없이 파지를 주장하지 않는다",
                source=f"{self.observation_kind}:{self.gripper_id}")
        held = self._interpret(raw)
        if not isinstance(held, bool):
            return unavailable(
                detail="원본 상태에서 파지 여부를 판정할 수 없다"
                       f" (해석 결과: {held!r})",
                source=f"{self.observation_kind}:{self.gripper_id}")
        try:
            return measured_from_device(
                held=held, object_id=object_id, observed_at=self._now(),
                source=f"{self.observation_kind}:{self.gripper_id}",
                method="gripper_object_detection_status",
                robot_id=self.robot_id, gripper_id=self.gripper_id,
                raw_state=raw,
                detail="장치의 물체 감지 상태로 판정했다")
        except GraspObservationError as exc:
            return unavailable(
                detail=f"관측 계약을 만족하지 못했다: {exc}"[:200],
                source=f"{self.observation_kind}:{self.gripper_id}")

    # ── 거부하는 경로 ───────────────────────────────────────────────────
    @staticmethod
    def from_aperture(*_args, **_kwargs) -> GraspObservation:
        """**호출하면 예외다.** 개구로 파지를 만들지 않는다."""
        raise GraspObservationError(
            "개구 일치로 object_held를 만들 수 없다. 빈손으로도 같은 개구를"
            " 만들 수 있으므로 개구는 파지 근거가 아니다 —"
            " 장치의 물체 감지 상태가 필요하다")

    @staticmethod
    def from_simulation(*_args, **_kwargs) -> GraspObservation:
        """**호출하면 예외다.** 시뮬레이터 관측을 실기 관측으로 바꾸지 않는다."""
        raise GraspObservationError(
            "시뮬레이터 관측(simulated_observation)을 실기 관측(measured)으로"
            " 바꿀 수 없다. Gazebo 고정 장치는 물체 감지가 아니다")

    def status(self) -> dict:
        """화면·기록용 요약. 접속 대상 값을 담지 않는다."""
        return {
            "robot_id": self.robot_id,
            "gripper_id": self.gripper_id,
            "adapter": f"{type(self).__module__}.{type(self).__name__}",
            "observation_kind": self.observation_kind,
            "available": self.available,
            "reads_device_state": self._read is not None,
            "has_interpreter": self._interpret is not None,
            "note": "실기 파지 관측 경계다. 접속 구현이 아직 없고 개구로"
                    " 파지를 유도하지 않는다",
        }
