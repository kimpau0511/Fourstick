"""Robot Adapter 인터페이스와 호출 수명 (md/개발플랜.md 1-04).

모든 로봇은 이 인터페이스만 노출한다. 공통 코드는 제조사·로봇명으로 분기하지
않고(계획.md 27장) Adapter와 Capability Profile을 의존성으로 받는다.

호출 수명 규칙:
1. connect() 전에는 어떤 스킬도 호출하지 않는다.
2. 모든 스킬은 ExecutionResult를 돌려준다. 예외로 성공/실패를 표현하지 않는다.
3. 스킬이 요청을 수락하지 못하면 request_accepted=False로 돌려준다.
4. 결과를 계측할 수 없으면 verified=False로 돌려준다. 성공으로 바꾸지 않는다.
5. stop()은 요청 접수만으로 성공을 주장하지 않는다. confirm_stopped()가 실제
   정지를 계측으로 확인한 뒤에만 STOPPED가 된다(1-08).
6. cancel()은 진행 중인 goal의 취소를 요청한다. 취소 ACK가 오지 않으면
   확인 불가로 남기고 추적을 유지한다 — handle을 버려 추적을 잃지 않는다.
7. 시간이 필요한 대기는 호출자가 예산(timeout_sec)을 주입한다. Adapter가
   코드 안에 고정 대기값을 두지 않는다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping

from core.capability_profile import CapabilityProfile
from core.execution_result import ExecutionResult
from core.execution_state import ExecutionState


@dataclass(frozen=True)
class RobotStateSnapshot:
    """로봇 상태 관측값. 신선도를 함께 담아 낡은 값을 성공 판정에 쓰지 않게 한다."""

    state: ExecutionState
    #: 관측 시각(epoch 초). 호출자가 now와 비교해 stale을 판정한다.
    observed_at: float
    #: 관절명 -> 위치(SI). 관절명은 Capability Profile이 정한 이름이다.
    joint_positions: Mapping[str, float]
    joint_velocities: Mapping[str, float]
    #: 관측을 못 한 경우 False. 이때 joint_* 는 신뢰할 수 없다.
    valid: bool = True
    #: 지금 쥐고 있다고 관측된 물체 이름. 아무것도 쥐지 않았으면 None.
    held_object: str | None = None
    #: 파지 상태를 관측할 수 있었는가. False면 held_object는 의미가 없다 —
    #: "쥔 것이 없다"와 "확인할 수 없다"를 구별하기 위해 별도 플래그로 둔다.
    hold_observed: bool = True

    def is_stale(self, now: float, max_age_sec: float) -> bool:
        return (now - self.observed_at) > max_age_sec


class RobotAdapter(ABC):
    """로봇 한 대를 제어하는 계약. 구현체는 robots/<robot>/adapter.py에 둔다."""

    def __init__(self, robot_id: str, profile: CapabilityProfile):
        self._robot_id = robot_id
        self._profile = profile

    @property
    def robot_id(self) -> str:
        return self._robot_id

    @property
    def profile(self) -> CapabilityProfile:
        return self._profile

    # ── 연결 ────────────────────────────────────────────────────────────
    @abstractmethod
    def connect(self, timeout_sec: float) -> ExecutionResult: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    # ── 상태 ────────────────────────────────────────────────────────────
    @abstractmethod
    def state(self) -> RobotStateSnapshot:
        """현재 관측값. 관측 실패는 예외가 아니라 valid=False로 표현한다."""

    # ── 스킬 ────────────────────────────────────────────────────────────
    # args는 Task Plan의 인자(이름)이고, 이름->좌표 해석은 호출자가 Catalog로
    # 끝낸 뒤 Adapter에 프레임이 명시된 pose로 전달한다.
    @abstractmethod
    def home(self, timeout_sec: float) -> ExecutionResult: ...

    @abstractmethod
    def move(self, target: str, timeout_sec: float) -> ExecutionResult: ...

    @abstractmethod
    def pick(self, obj: str, source: str, timeout_sec: float) -> ExecutionResult: ...

    @abstractmethod
    def place(self, obj: str, destination: str, timeout_sec: float) -> ExecutionResult: ...

    # ── 정지 ────────────────────────────────────────────────────────────
    @abstractmethod
    def stop(self, timeout_sec: float) -> ExecutionResult:
        """정지를 요청한다. 반환값만으로 정지를 확정하지 않는다."""

    @abstractmethod
    def cancel(self, timeout_sec: float) -> ExecutionResult:
        """진행 중 goal의 취소를 요청한다. ACK 미수신은 확인 불가로 남긴다."""

    @abstractmethod
    def confirm_stopped(self, timeout_sec: float) -> ExecutionResult:
        """실제 정지를 계측으로 확인한다. 확인되면 STOPPED, 아니면 UNKNOWN."""

    # ── 사전 점검 ───────────────────────────────────────────────────────
    @abstractmethod
    def check(self) -> ExecutionResult:
        """실행 직전 점검(연결·컨트롤러·상태 신선도). 실패 시 이유 코드를 담는다."""

    def supports(self, skill: str) -> bool:
        return self._profile.supports(skill)
