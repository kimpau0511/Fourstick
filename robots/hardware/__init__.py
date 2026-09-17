"""실기(실제 하드웨어) 어댑터 **경계만** 준비한다 (md/개발플랜.md 8-12).

이 패키지에는 **연결 코드가 없다.** 실제 FR3와 실제 2F-85에 명령을 보내지
않는다. 여기 있는 것은

- 실기 어댑터가 만족해야 하는 인터페이스(`RobotAdapter` 계약 그대로)
- 설정이 없으면 **생성·연결 단계에서 막는** 규칙
- 실기 파지 관측 어댑터의 인터페이스

## 왜 미리 만드는가

실기 전환 때 급하게 어댑터를 만들면 주소·한계값을 코드에 박게 된다. 경계를
먼저 고정해 두면 그 자리에 **설정만** 들어온다.

## 여기에 없는 것 (있으면 안 되는 것)

- 접속 주소·포트·호스트 이름
- 인증값·토큰·비밀번호
- controller endpoint 경로
- 관절값·힘·속도 한계

전부 실행 환경 설정(`HardwareConnection`)으로 들어오고, 없으면 어댑터를 만들
수 없다. 테스트가 이 파일들에 그런 값이 없는지 확인한다.
"""

from robots.hardware.config import (
    HardwareConfigError,
    HardwareConnection,
    HardwareLimits,
    load_hardware_connection,
)
from robots.hardware.fr3 import FR3HardwareAdapter
from robots.hardware.robotiq import Robotiq2F85HardwareObservationAdapter

__all__ = [
    "FR3HardwareAdapter",
    "HardwareConfigError",
    "HardwareConnection",
    "HardwareLimits",
    "Robotiq2F85HardwareObservationAdapter",
    "load_hardware_connection",
]
