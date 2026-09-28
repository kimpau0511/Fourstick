"""웹 서버 런타임 조립 (md/개발플랜.md 7단계).

설정·카탈로그·정책·저장소·로봇 Registry·계획 공급자를 한 곳에서 묶는다.
HTTP 처리(`server/asgi.py`)는 이 객체만 본다.

원칙:
- **없는 기능을 있는 것처럼 만들지 않는다.** LLM 서버가 없으면 계획 생성은
  `available=False`로 보고하고, STT 모델이 없으면 STT를 사용할 수 없는 상태로
  둔다. Mock 성공 응답을 대신 넣지 않는다.
- **실제 로봇 Profile이 없으면 '로봇 미설정'** 이다. 개발용 Fake Adapter는
  등록하되 `configured=False`로 구분해 내려보낸다.
- **브라우저에 모델 서버 주소·인증값을 내려보내지 않는다.** `/v1/config`는
  버전과 이름만 담는다.
"""

from __future__ import annotations

import importlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from config.loader import (
    load_arm_profile,
    load_asset_manifest,
    load_composite_profile,
    load_gripper_profile,
    load_mounting_profile,
    load_capability_profile,
    load_freshness_policy,
    load_llm_provider_config,
    load_planning_policy,
    load_resource_catalog,
    load_safety_policy,
    load_skill_catalog,
    load_stop_policy,
    load_stt_model_config,
    load_stt_policy,
)
from core.capability_profile import CapabilityProfile
from core.constants import TASK_PLAN_SCHEMA_VERSION
from core.policy import (
    FreshnessPolicy,
    LlmProviderConfig,
    PlanningPolicy,
    SafetyPolicy,
    StopPolicy,
    SttModelConfig,
    SttPolicy,
)
from core.frames import FrameKind
from core.resource_catalog import ResourceCatalog
from core.skill_catalog import SkillCatalog
from core.termination import (
    ApproachRequirement,
    TerminationRequirement,
    approach_requirement,
    termination_requirement,
)
from planning.openai_compat import OpenAiCompatClient
from planning.plan_provider import PlanProvider
from planning.vllm_provider import VllmPlanProvider
import json

from core.asset_manifest import AssetManifest
from core.robot_profile import CompositeRobotProfile, Environment
from core.stop_contract import JointSample
from robots.fake.geometry import DevFakeGeometryValidator, FakeCell
from validation import pick_place_gate
from storage.records import FAKE_ADAPTER_KIND, RobotProfileRecord
from robots.fake.adapter import FakeRobotAdapter
from robots.registry import RobotRegistry
from server.config import ROOT, ServerConfig
from storage.sqlite.repository import SqliteRepository

#: 개발용 Fake Adapter의 robot_id. 실제 로봇이 아니라는 뜻을 이름에 남긴다.
FAKE_ROBOT_ID = "fake_dev"


class DevFakeAdapter(FakeRobotAdapter):
    """개발용 Fake Adapter. **실제 로봇이 아니다.**

    `FakeWorld`는 테스트가 시각을 주입하도록 설계돼 있어서 `observed_at`이
    고정값이다. 웹 서버는 실제 시계로 신선도를 판정하므로 그대로 쓰면 모든
    관측이 stale이 된다.

    그래서 이 어댑터는 상태를 물을 때 **관측 시각을 현재로 갱신하고 정지 표본을
    합성한다.** 시뮬레이터가 하는 일과 같다 — 센서가 없는 대신 세계를 계산한다.
    UI는 이 로봇을 `로봇 미설정 (Fake Adapter / 개발용)`으로 표시하므로, 여기서
    나온 "정지 확인"을 실제 로봇의 확인으로 읽지 않는다.

    실제 로봇 Adapter(8단계)는 자기 타임스탬프와 실제 표본을 보고한다.
    """

    def __init__(self, robot_id, profile, *, stop_policy, clock=None, **kwargs):
        super().__init__(robot_id, profile, stop_policy=stop_policy, **kwargs)
        self._clock = clock or time.time
        self._hold_sec = stop_policy.hold_sec
        self.world.connected = True
        self.world.joint_positions = {name: 0.0 for name in self._joint_names}
        self.world.joint_velocities = {name: 0.0 for name in self._joint_names}
        self._refresh()

    def _refresh(self) -> None:
        now = self._clock()
        self.world.now = now
        self.world.observed_at = now
        # 정지 확인 표본: 같은 위치에 hold_sec 동안 머문 관측을 합성한다.
        # 실제 센서가 없으므로 계산된 세계 상태를 표본으로 쓴다.
        positions = dict(self.world.joint_positions)
        span = max(self._hold_sec, 0.05)
        self.world.samples = [
            JointSample(at=now - span, positions=positions),
            JointSample(at=now - span / 2, positions=positions),
            JointSample(at=now, positions=positions),
        ]

    def state(self):
        self._refresh()
        return super().state()

    def confirm_stopped(self, timeout_sec: float):
        self._refresh()
        return super().confirm_stopped(timeout_sec)


@dataclass
class FeatureState:
    """기능 하나의 사용 가능 여부. 못 쓰는 이유를 함께 담는다."""

    available: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return {"available": self.available, "detail": self.detail}


@dataclass
class Runtime:
    config: ServerConfig
    repository: SqliteRepository
    resource_catalog: ResourceCatalog
    skill_catalog: SkillCatalog
    safety_policy: SafetyPolicy
    freshness_policy: FreshnessPolicy
    planning_policy: PlanningPolicy
    stop_policy: StopPolicy
    stt_policy: SttPolicy
    registry: RobotRegistry
    termination: TerminationRequirement
    approach: ApproachRequirement
    #: 실제 로봇 Profile이 등록됐는가. Fake만 있으면 False다.
    robot_configured: bool
    robot_id: str | None
    profile: CapabilityProfile | None
    llm_config: LlmProviderConfig | None = None
    stt_model_config: SttModelConfig | None = None
    planning: FeatureState = field(default_factory=lambda: FeatureState(False, "미초기화"))
    stt: FeatureState = field(default_factory=lambda: FeatureState(False, "미초기화"))
    #: 계획 공급자. 없으면 계획 생성을 쓸 수 없다.
    provider: PlanProvider | None = None
    #: STT 백엔드 생성자. 세션마다 새 인스턴스를 만든다.
    stt_factory: Callable[[], Any] | None = None
    #: 공유 어댑터. **로봇은 하나다** — 세션마다 새 어댑터를 만들면 같은 로봇에
    #: 여러 연결이 붙어 STOP이 다른 인스턴스의 동작을 멈추지 못한다.
    _adapter: Any = field(default=None, repr=False)
    #: 실행 직렬화. 한 로봇에 동시에 두 실행을 보내지 않는다.
    execution_lock: Any = field(default_factory=threading.Lock, repr=False)
    #: 기하 검사 구현체. **없으면 기하 검사는 ASK다** — 검사하지 않은 상태를
    #: 안전으로 보지 않는다(6-05). 개발용 Fake 셀에서만 개발용 구현체를 붙인다.
    geometry_validator: Any = field(default=None, repr=False)
    geometry_cell: Any = field(default=None, repr=False)
    geometry_timeout_sec: float = 2.0
    #: 제3자 자산 manifest. 없으면 None(자산 상태를 알 수 없다).
    asset_manifest: AssetManifest | None = None
    #: 선언된 로봇 구성. **완성·검증된 것만 Registry에 등록된다.**
    composite_profiles: tuple = ()
    #: 그리퍼 조립 검증 기록(reports/gripper/verify_gripper.json). 없으면 None —
    #: 관문이 관측 조건을 미충족으로 본다.
    gripper_verification: dict | None = None
    #: FR3 Gazebo 작업 셀 연결 상태(8-08). 붙지 않았으면 이유를 담는다.
    workcell: dict | None = None
    #: 어댑터를 아직 만들지 않았을 때의 종류. 등록된 factory가 기준이다.
    declared_adapter_kind: str = FAKE_ADAPTER_KIND
    #: 기호 계획을 수치 관절값으로 바꾸는 함수. 작업 셀 Adapter가 준다.
    #: 없으면 수치가 없고, 기하 검사는 ASK다(통과시키지 않는다).
    motion_resolver: Any = None
    #: planning scene 클라이언트를 만드는 함수(지연 생성). 작업 셀이 준다.
    scene_client_factory: Any = None
    #: 만들어진 planning scene 클라이언트. 실패했으면 False로 표시해 다시
    #: 시도하지 않는다(매 요청마다 20초씩 기다리지 않게).
    scene_client: Any = None
    #: 작업 셀 장면 영상 공급자. 장면 카메라가 꺼져 있으면 None이다.
    scene_viewer: Any = None
    #: 기하 검사기를 만드는 함수(scene client를 받는다). 작업 셀이 준다.
    geometry_validator_factory: Any = None
    #: pick/place 계획 검증에 넘길 셀 선언을 만드는 함수. 작업 셀이 준다.
    #: 없으면 계획 검증을 하지 않고 그 사실을 이유 코드로 남긴다.
    pick_place_bindings_factory: Any = None
    #: 만들어진 셀 선언(한 번만 만든다). 실패했으면 False다.
    pick_place_bindings: Any = None
    #: 파지 측정에서 **측정할 수 없었던 것**. 화면·보고서에 그대로 남긴다.
    grasp_limitations: tuple = ()
    #: 실기 전환 준비 판정(8-12). 읽기 전용이며 실행 경로를 열지 않는다.
    #: 만들지 못했으면 이유를 담은 dict다.
    hardware_readiness: dict | None = None
    #: Gazebo 이송 시연 요약(8-11). **실기 준비와 다른 축이다** — 화면이
    #: 나란히 보여주되 하나의 배지로 합치지 않는다.
    simulation_e2e: dict | None = None
    #: 시뮬레이션 사용자 시연의 셀 상태 기록 경로. None이면 기본 경로다.
    #: 시연은 별도 프로세스라 조회 때마다 새로 읽는다(`simulation_demo_status`).
    simulation_demo_state_path: Any = None
    #: 웹 시뮬레이션 시연 작업 실행기(`server/sim_demo_jobs.py`). 꺼져 있으면 None.
    sim_demo_jobs: Any = None
    #: 시연 작업을 쓸 수 없는 이유(켜져 있으면 None).
    sim_demo_disabled_reason: str | None = "시뮬레이션 작업 셀이 아니다"
    #: 이 어댑터 종류가 **항상 시뮬레이션**인가. 시뮬레이터에 붙는 어댑터는
    #: 개발용 Fake가 아니지만 실하드웨어도 아니다 — 두 축을 따로 둔다.
    #: None이면 모름(연결 확인 뒤에 정한다).
    declared_is_simulated: bool | None = None

    # ── 어댑터 ──────────────────────────────────────────────────────────
    @property
    def adapter_kind(self) -> str:
        """어댑터 종류. **Profile이 아니라 어댑터가 기준이다.**

        실제 로봇 Profile이 등록돼 있어도 실행을 보내는 어댑터가 개발용 Fake면
        그 실행은 시뮬레이션이다. 이전 구현은 Profile 등록 여부로 'real'을
        판단해, Fake 어댑터 실행이 real로 기록될 수 있었다.
        """
        adapter = self._adapter
        if adapter is not None:
            declared = getattr(adapter, "adapter_kind", "")
            if declared:
                return str(declared)
            if isinstance(adapter, FakeRobotAdapter):
                return FAKE_ADAPTER_KIND
            return "real"
        return self.declared_adapter_kind

    @property
    def is_simulated(self) -> bool | None:
        """시뮬레이션 실행인가.

        - Fake 어댑터: 항상 True
        - 실제 어댑터: **연결이 확인된 뒤에만** False다. 그전까지는 모름(None)
        - 어댑터 없음: 모름(None)

        실행 기록에 쓰는 값은 `execution_environment()`가 연결 확인 여부와 함께
        정한다. 이 속성은 화면 표시용 요약이다.
        """
        kind = self.adapter_kind
        if not kind:
            return None
        if kind == FAKE_ADAPTER_KIND:
            return True
        adapter = self._adapter
        declared = getattr(adapter, "is_simulated_adapter", None)
        if declared is None:
            declared = self.declared_is_simulated
        # 시뮬레이터에 붙는 어댑터는 연결 여부와 무관하게 시뮬레이션이다.
        return True if declared is True else None

    def execution_environment(
        self, *, connected: bool, now: float
    ) -> tuple[str | None, bool | None, float | None]:
        """실행 기록에 남길 (어댑터 종류, 시뮬레이션 여부, 연결 확인 시각).

        모순된 조합을 만들지 않는다 — 같은 규칙을 레코드 계약과 SQLite 트리거도
        강제한다(마이그레이션 0011).
        """
        kind = self.adapter_kind
        if not kind:
            return (None, None, None)
        if kind == FAKE_ADAPTER_KIND:
            return (kind, True, None)
        adapter = self._adapter
        declared = getattr(adapter, "is_simulated_adapter", None)
        if declared is None:
            declared = self.declared_is_simulated
        if declared is True:
            # 시뮬레이터 어댑터다. **real로 기록하지 않는다.** 연결이 확인돼도
            # 그것은 시뮬레이터와의 연결이다.
            return (kind, True, now if connected else None)
        if connected:
            return (kind, False, now)
        return (kind, None, None)

    def adapter(self):
        """공유 어댑터를 돌려준다. 없으면 만들고 연결한다."""
        if self.robot_id is None:
            return None
        if self._adapter is None:
            adapter = self.registry.create(self.robot_id)
            adapter.connect(10.0)
            self._adapter = adapter
        return self._adapter

    def simulation_demo_status(self) -> dict:
        """시뮬레이션 시연 상태 유지·수동 reset 필요 여부. **실기 상태가 아니다.**

        파일만 읽는다. 어댑터를 만들거나 연결하지 않는다.
        """
        from validation.simulation_demo_state import (
            DEFAULT_PATH,
            SimulationDemoState,
        )

        return SimulationDemoState(
            self.simulation_demo_state_path or DEFAULT_PATH).status()

    def reset_stop_latch(self) -> tuple[bool, str]:
        """새 계획 수락 시 어댑터의 정지 래치를 푼다.

        해제 지점은 계약이 정한다 — `core/stop_contract.GoalTracker`의
        `reset_for_new_plan`은 추적 중인 goal이 남아 있으면 해제를 거부한다.
        여기서 그 계약을 그대로 호출하고, 결과를 감추지 않는다.

        돌려주는 값: (풀렸는가, 사유). 실패를 성공으로 바꾸지 않는다.
        """
        adapter = self._adapter
        if adapter is None:
            return (True, "어댑터가 아직 만들어지지 않았다(정지 래치 없음)")
        tracker = getattr(adapter, "tracker", None)
        reset = getattr(tracker, "reset_for_new_plan", None)
        if reset is None:
            return (False, "어댑터가 정지 래치 해제를 제공하지 않는다")
        try:
            reset()
        except Exception as exc:  # noqa: BLE001 — 해제 실패를 숨기지 않는다
            return (False, f"{type(exc).__name__}: {exc}"[:200])
        return (True, "정지 래치를 풀었다")

    # ── 검증 입력 ───────────────────────────────────────────────────────
    @property
    def policy_binding(self) -> tuple[str, str]:
        """검증·승인을 묶을 Policy 식별자와 버전.

        안전 검증이 판정의 주 정책이므로 그 버전을 쓴다. 신선도 정책은 실행
        허가에서 따로 재검사되고, 기록에는 실행 행이 자기 값을 남긴다.
        """
        return ("safety", self.safety_policy.policy_version)

    @property
    def frame_id(self) -> str:
        """계획이 전제하는 좌표계 이름. **Profile이 유일한 출처다.**"""
        if self.profile is None:
            return ""
        try:
            return self.profile.frame_name(FrameKind.BASE)
        except Exception:  # noqa: BLE001 — 이름이 없으면 "좌표계 불명"이다
            return ""

    def environment_snapshot(self):
        """지금 관측된 환경 snapshot. 없으면 None이다.

        개발용 Fake 셀에서는 어댑터 관측 시각을 `captured_at`으로 쓴다.
        `content_hash`에는 **관측 시각을 넣지 않는다** — 내용이 그대로인데
        지문이 매번 달라지면 승인을 환경에 묶을 수 없다. 오래된 관측은 지문이
        아니라 만료(`ttl_sec`)로 걸러진다.
        """
        # 작업 셀에서는 **MoveIt planning scene**이 환경의 출처다.
        client = self.planning_scene_client()
        if client is not None:
            try:
                return client.snapshot().to_environment()
            except Exception:  # noqa: BLE001 — 관측 실패는 "환경 정보 없음"이다
                return None
        cell = self.geometry_cell
        if cell is None:
            return None
        adapter = self.adapter()
        if adapter is None:
            return None
        try:
            state = adapter.state()
        except Exception:  # noqa: BLE001 — 관측 실패는 "환경 정보 없음"이다
            return None
        captured_at = getattr(state, "observed_at", 0.0) or 0.0
        if captured_at <= 0:
            return None
        return cell.snapshot(
            captured_at=captured_at,
            ttl_sec=self.freshness_policy.environment_max_age_sec,
            source=f"{self.adapter_kind}:{self.robot_id}",
            extra=(self.resource_catalog.catalog_version,),
        )

    def _raw_motion(self, plan=None) -> dict:
        """해석기가 준 원본 관절값. `{스텝: {"joints": {...}, "pose": 이름}}`."""
        if plan is None or self.motion_resolver is None:
            return {}
        try:
            return dict(self.motion_resolver(plan))
        except Exception:  # noqa: BLE001 — 해석 실패는 "수치 없음"이다
            return {}

    def resolved_motion(self, plan=None) -> dict:
        """Capability 사전 검사가 쓰는 해석된 모션(`MotionRequest`).

        해석기가 붙어 있으면(작업 셀 Adapter가 제공) 기호 계획을 **검증된
        관절값**으로 바꿔 돌려준다. 없으면 비어 있고, 그것은 "검사할 수치가
        계약에 존재하지 않는다"는 뜻이다 — Adapter 내부 해석값에 대한 보증이
        아니다(6-04 문서의 미검증 항목).

        단위를 함께 선언한다. 단위 없이 비교하면 조용히 틀린다(6-04).
        """
        from core.motion import MotionRequest, Quantity

        out: dict[int, MotionRequest] = {}
        for index, item in self._raw_motion(plan).items():
            out[index] = MotionRequest(
                joint_targets=dict(item["joints"]),
                units={Quantity.JOINT_POSITION: "rad"},
                source=f"workcell_pose:{item.get('pose', '')}",
            )
        return out

    def geometry_motion(self, plan=None) -> dict:
        """기하 검사기가 쓰는 해석된 모션. 키 이름은 검사기 계약이 정한다."""
        from robots.moveit.validator import joint_motion

        return {index: joint_motion(item["joints"])
                for index, item in self._raw_motion(plan).items()}

    def validator(self):
        """기하 검사기. 작업 셀이면 planning scene에서 지연 생성한다.

        검사기를 만들지 못하면 None이고, 기하 검사는 ASK가 된다 —
        **검사하지 않은 상태를 안전으로 보지 않는다.**
        """
        if self.geometry_validator is not None:
            return self.geometry_validator
        if self.geometry_validator_factory is None:
            return None
        client = self.planning_scene_client()
        if client is None:
            return None
        try:
            self.geometry_validator = self.geometry_validator_factory(client)
        except Exception:  # noqa: BLE001 — 만들 수 없으면 검사기 없음이다
            return None
        return self.geometry_validator

    def planning_scene_client(self):
        """planning scene 클라이언트. 한 번 실패하면 다시 만들지 않는다."""
        if self.scene_client is False:
            return None
        if self.scene_client is not None:
            return self.scene_client
        if self.scene_client_factory is None:
            return None
        try:
            client = self.scene_client_factory()
        except Exception:  # noqa: BLE001 — 붙지 못하면 기하 검사는 ASK다
            client = None
        self.scene_client = client if client is not None else False
        return client

    # ── pick/place 계획 검증 (8-10) ─────────────────────────────────────
    def cell_bindings(self):
        """pick/place 계획 검증용 셀 선언. 만들 수 없으면 None이다."""
        if self.pick_place_bindings is False:
            return None
        if self.pick_place_bindings is not None:
            return self.pick_place_bindings
        if self.pick_place_bindings_factory is None:
            return None
        try:
            bindings = self.pick_place_bindings_factory()
        except Exception:  # noqa: BLE001 — 만들 수 없으면 선언 없음이다
            bindings = None
        self.pick_place_bindings = bindings if bindings is not None else False
        return bindings

    def pick_place_validation(self, steps, *, slots=None, utterance: str = ""):
        """pick/place 계획 초안을 단계로 펼쳐 사전 검증한다.

        **실행 허가를 만들지 않는다.** 결과의 `execution_allowed`는 항상
        False이고, 실행 차단은 `validation/pick_place_gate.py`가 판정한다.
        선언이나 planning scene이 없으면 그 사실을 이유 코드로 남긴다 —
        검사하지 않은 상태를 통과로 쓰지 않는다.
        """
        from validation.pick_place_plan import validate

        bindings = self.cell_bindings()
        if bindings is None:
            return None
        return validate(
            steps, slots=slots, bindings=bindings,
            client=self.planning_scene_client(),
            grasp_observation=self.grasp_observation().to_dict(),
            utterance=utterance,
            limitations=self.grasp_limitations,
        )

    def grasp_observation(self):
        """`grasp.object_held` 관측. **이 셀에는 수단이 없다.**

        어댑터가 관측자를 제공하면 그것을 쓰고, 없으면 `unavailable`이다.
        개구가 목표와 맞는 것을 파지 근거로 쓰지 않는다.
        """
        from core.grasp_observation import GraspObservation, unavailable

        adapter = None
        try:
            adapter = self.adapter()
        except Exception:  # noqa: BLE001 — 관측자를 못 얻으면 수단 없음이다
            adapter = None
        observer = getattr(adapter, "grasp_observer", None)
        if observer is None:
            return unavailable(
                detail="이 셀에 파지 상태 관측 수단이 없다 —"
                       " 개구가 목표와 맞는 것을 파지 근거로 쓰지 않는다",
                source=f"{self.adapter_kind}:{self.robot_id}")
        try:
            observed = observer.observe(None)
        except Exception as exc:  # noqa: BLE001
            return unavailable(
                detail=f"관측 호출이 실패했다: {type(exc).__name__}"[:120],
                source=f"{self.adapter_kind}:{self.robot_id}")
        if not isinstance(observed, GraspObservation):
            return unavailable(
                detail="관측자가 계약 형식이 아닌 값을 돌려줬다",
                source=f"{self.adapter_kind}:{self.robot_id}")
        return observed

    # ── 선언된 로봇 구성 ────────────────────────────────────────────────
    def declared_robots(self) -> list[dict]:
        """선언된 로봇 구성 목록.

        완성·검증되지 않은 구성도 **보여준다** — 무엇이 막혀 있는지 화면에서
        알 수 있어야 한다. 대신 Registry에는 등록되지 않아 실행 대상이 아니다.
        """
        out = []
        for profile in self.composite_profiles:
            payload = profile.to_dict()
            payload["registered"] = profile.composite_profile_id in self.registry.catalog()
            payload["executable"] = profile.complete and profile.verified
            # pick·place는 7개 조건 관문을 지나야 열린다(8-08). 조건과 이유를
            # 그대로 내려보낸다 — 화면이 무엇이 막혔는지 보여줄 수 있어야 한다.
            payload["pick_place_gate"] = pick_place_gate.evaluate(
                mounting=profile.mounting,
                verification=self.gripper_verification,
                geometry_decision=None,
                revalidated=False,
                environment=profile.environment.value,
                # 파지 관측 기록을 관문 근거에 붙인다. 지금은 `unavailable`이다.
                grasp_observation=self.grasp_observation().to_dict(),
            ).to_dict()
            out.append(payload)
        return out

    def asset_status_payload(self) -> dict:
        """자산 상태. 없거나 라이선스 미확인이면 그 이유를 그대로 내려보낸다."""
        manifest = self.asset_manifest
        if manifest is None:
            return {
                "manifest_version": None,
                "detail": "자산 manifest를 읽지 못했다",
                "assets": [],
            }
        return {
            "manifest_version": manifest.manifest_version,
            "detail": manifest.note,
            "unverified_licenses": list(manifest.unverified_licenses()),
            "assets": [
                {**entry.to_dict(), **manifest.resolve(entry.asset_id).to_dict()}
                for entry in manifest.entries
            ],
        }

    # ── 조회 ────────────────────────────────────────────────────────────
    def config_payload(self) -> dict:
        """브라우저에 내려보낼 설정. **주소·인증값을 담지 않는다.**"""
        profile = self.profile
        return {
            "schema_version": TASK_PLAN_SCHEMA_VERSION,
            "robot": {
                "configured": self.robot_configured,
                "robot_id": self.robot_id,
                "kind": self.adapter_kind,
                "is_simulated": self.is_simulated,
                "profile_id": None if profile is None else profile.profile_id,
                "profile_version": None if profile is None else profile.profile_version,
                "dof": None if profile is None else profile.dof,
                "payload_kg": None if profile is None else profile.payload_kg,
                "work_radius_m": None if profile is None else profile.work_radius_m,
                "supported_skills": [] if profile is None else list(profile.supported_skills),
                "has_gripper": None if profile is None else profile.gripper is not None,
                # 시뮬레이션 작업 셀 연결 상태(8-08). 붙지 않았으면 이유를 담는다.
                # 화면이 **실하드웨어가 아니라는 사실**을 보여줄 수 있어야 한다.
                "workcell": self.workcell,
            },
            "declared_robots": self.declared_robots(),
            "assets": {
                "manifest_version": (
                    None if self.asset_manifest is None
                    else self.asset_manifest.manifest_version
                ),
                "unverified_licenses": (
                    [] if self.asset_manifest is None
                    else list(self.asset_manifest.unverified_licenses())
                ),
            },
            "policies": {
                "safety": {
                    "policy_version": self.safety_policy.policy_version,
                    "max_steps": self.safety_policy.max_steps,
                    "required_final_skill": self.safety_policy.required_final_skill,
                    "approach_skill": self.safety_policy.approach_skill,
                },
                "freshness": {
                    "policy_version": self.freshness_policy.policy_version,
                    "robot_state_max_age_sec": self.freshness_policy.robot_state_max_age_sec,
                    "environment_max_age_sec": self.freshness_policy.environment_max_age_sec,
                },
                "planning": {
                    "policy_version": self.planning_policy.policy_version,
                    "max_attempts": self.planning_policy.max_attempts,
                    "request_timeout_sec": self.planning_policy.request_timeout_sec,
                },
                "stop": {
                    "policy_version": self.stop_policy.policy_version,
                    "hold_sec": self.stop_policy.hold_sec,
                    "cancel_ack_timeout_sec": self.stop_policy.cancel_ack_timeout_sec,
                },
                "stt": {
                    "policy_version": self.stt_policy.policy_version,
                    "max_audio_sec": self.stt_policy.max_audio_sec,
                    "min_final_confidence": self.stt_policy.min_final_confidence,
                    "stop_keywords": list(self.stt_policy.stop_keywords),
                    "transcribe_deadline_sec": self.stt_policy.transcribe_deadline_sec,
                    "max_concurrent_transcriptions": (
                        self.stt_policy.max_concurrent_transcriptions
                    ),
                },
            },
            "catalogs": {
                "resource_catalog_version": self.resource_catalog.catalog_version,
                "skill_catalog_version": self.skill_catalog.catalog_version,
                "locations": [
                    {"resource_id": e.resource_id, "display_name": e.display_name}
                    for e in self.resource_catalog.entries
                    if e.kind.value == "location"
                ],
                "objects": [
                    {"resource_id": e.resource_id, "display_name": e.display_name}
                    for e in self.resource_catalog.entries
                    if e.kind.value == "object"
                ],
                "skills": [
                    {"skill": e.skill, "description": e.description}
                    for e in self.skill_catalog.entries
                ],
            },
            "termination": {
                "final_skill": self.termination.final_skill,
                "max_steps": self.termination.max_steps,
                "hold_possible": self.termination.hold_possible,
                "conflicts": list(self.termination.conflicts),
            },
            "audio": {
                # STT 계약. 브라우저가 이 값으로 AudioWorklet을 맞춘다.
                "sample_rate_hz": (
                    None if self.stt_model_config is None
                    else self.stt_model_config.sample_rate_hz
                ),
                "encoding": "pcm_s16le",
                "channels": 1,
            },
            "session": {
                "idle_timeout_sec": self.config.session_idle_timeout_sec,
                "plan_ttl_sec": self.config.plan_ttl_sec,
                # 탭(클라이언트) 유예. 브라우저가 이 값을 받아 쓴다 —
                # 화면 코드에 숫자를 중복하지 않는다.
                "client_grace_sec": self.config.client_grace_sec,
                # 인증이 없다. 세션은 작업 묶음 구분자다.
                "auth": "none",
                "binding": f"{self.config.host}:{self.config.port}",
            },
            "features": {
                "planning": self.planning.to_dict(),
                "stt": self.stt.to_dict(),
                "robot": FeatureState(
                    self.robot_id is not None,
                    "" if self.robot_id else "등록된 로봇이 없다",
                ).to_dict(),
            },
            "model": {
                # 이름과 버전만. 주소·인증값은 내려보내지 않는다.
                "model_id": None if self.llm_config is None else self.llm_config.model_id,
                "quantization": (
                    None if self.llm_config is None else self.llm_config.quantization
                ),
                "thinking_mode": (
                    None if self.llm_config is None
                    else self.llm_config.thinking_mode.value
                ),
                "structured_output": (
                    None if self.llm_config is None
                    else self.llm_config.structured_output.value
                ),
            },
        }


def _load(config_dir, name: str) -> dict:
    return json.loads((config_dir / name).read_text(encoding="utf-8"))


def _load_hardware_readiness() -> dict:
    """실기 준비 판정을 계산해 담는다. **시뮬레이터 결과를 넣지 않는다.**

    설정 파일이 없거나 읽히지 않으면 그 사실을 이유 코드와 함께 남긴다 —
    판정을 못 했는데 준비된 것처럼 두지 않는다.
    """
    from core.reason_codes import ReasonCode
    from robots.hardware.config import connection_state
    from validation import hardware_readiness

    # 설정 파일 이름은 **매니페스트에만** 있다 — 공통 코드에 로봇·그리퍼
    # 이름을 두지 않는다(계획.md 27장, 작업 셀 매니페스트와 같은 방식).
    manifest_path = ROOT / "config/hardware/active.json"
    if not manifest_path.is_file():
        return {
            "available": False,
            "real_hardware_ready": False,
            "real_hardware_verified": False,
            "detail": "실기 준비 매니페스트가 없다",
            "reason_code": ReasonCode.CONFIG_MISSING.value,
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "available": False,
            "real_hardware_ready": False,
            "real_hardware_verified": False,
            "detail": f"실기 준비 매니페스트를 읽지 못했다: {type(exc).__name__}",
            "reason_code": ReasonCode.CONFIG_INVALID.value,
        }
    if not manifest.get("enabled"):
        return {
            "available": False,
            "real_hardware_ready": False,
            "real_hardware_verified": False,
            "detail": "실기 준비 매니페스트가 꺼져 있다",
            "reason_code": ReasonCode.CONFIG_MISSING.value,
        }
    inputs_path = ROOT / str(manifest.get("inputs") or "")
    checklist_path = ROOT / str(manifest.get("checklist") or "")
    for path in (inputs_path, checklist_path):
        if not path.is_file():
            return {
                "available": False,
                "real_hardware_ready": False,
                "real_hardware_verified": False,
                "detail": f"실기 준비 설정이 없다: {path.name}",
                "reason_code": ReasonCode.CONFIG_MISSING.value,
            }
    try:
        inputs = hardware_readiness.load_input_set(inputs_path)
        checklist = hardware_readiness.load_checklist(checklist_path)
        adapter = connection_state()
        result = hardware_readiness.evaluate(
            inputs=inputs, checklist=checklist,
            adapter_config=(None if not adapter.get("configured") else {
                "arm": adapter.get("arm_kind"),
                "gripper": adapter.get("gripper_kind"),
            }),
            # 파지 관측은 실기 관측기가 있을 때만 들어온다. 지금은 없다.
            grasp_observation=None,
            hardware_connected=False)
    except Exception as exc:  # noqa: BLE001 — 판정 실패를 준비로 쓰지 않는다
        return {
            "available": False,
            "real_hardware_ready": False,
            "real_hardware_verified": False,
            "detail": f"실기 준비 판정을 만들지 못했다:"
                      f" {type(exc).__name__}: {exc}"[:300],
            "reason_code": ReasonCode.CONFIG_INVALID.value,
        }
    payload = {"available": True, **result.to_dict()}
    payload["collection_guide"] = list(
        hardware_readiness.collection_guide(result))
    payload["adapter_state"] = adapter
    # 고정 상태 다섯 줄. **한 곳에서만 만든다**(validation/hardware_readiness).
    # 시뮬레이터 축은 표시용으로만 함께 적고, 실기 값은 판정에서만 온다.
    simulation = _load_simulation_e2e()
    state = hardware_readiness.pinned_state(
        result,
        simulation_e2e=(bool(simulation.get("completed"))
                        if simulation.get("available") else None))
    payload["state"] = state
    payload["state_lines"] = list(hardware_readiness.state_lines(state))
    return payload


def _attach_sim_demo_jobs(runtime, config, manifest, workcell_path) -> None:
    """웹 시연 작업 실행기를 붙인다. **시뮬레이션 셀일 때만.**"""
    if not config.enable_sim_demo_web:
        runtime.sim_demo_disabled_reason = "FORSTICK2_SIM_DEMO_WEB=0으로 꺼져 있다"
        return
    if not config.enable_workcell_robot or not manifest or not manifest.get("enabled"):
        runtime.sim_demo_disabled_reason = "작업 셀 Adapter가 켜져 있지 않다"
        return
    if manifest.get("is_simulated") is not True:
        runtime.sim_demo_disabled_reason = "활성 작업 셀이 시뮬레이션이 아니다"
        return
    try:
        workcell = json.loads(workcell_path("workcell_config").read_text(
            encoding="utf-8"))
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        runtime.sim_demo_disabled_reason = (
            f"작업 셀 설정을 읽지 못했다: {type(exc).__name__}")
        return
    if workcell.get("is_simulated") is not True:
        runtime.sim_demo_disabled_reason = "작업 셀 설정이 시뮬레이션이 아니다"
        return
    from server.sim_demo_jobs import SimDemoJobs

    kwargs = {}
    if runtime.simulation_demo_state_path is not None:
        kwargs["state_path"] = runtime.simulation_demo_state_path
    runtime.sim_demo_jobs = SimDemoJobs(workcell=workcell, **kwargs)
    runtime.sim_demo_disabled_reason = None


def _load_simulation_e2e() -> dict:
    """Gazebo 이송 시연 확인 기록 요약. **실기 결과가 아니다.**

    보고서 파일만 읽는다(실행 DB에는 들어가지 않는다). 없으면 없다고 말한다.
    """
    path = ROOT / "reports/workcell/pick_place_sim_e2e_verify.json"
    if not path.is_file():
        return {"available": False, "detail": "시뮬레이션 E2E 기록이 없다",
                "is_simulated": True, "real_hardware_ready": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False,
                "detail": f"기록을 읽지 못했다: {type(exc).__name__}",
                "is_simulated": True, "real_hardware_ready": False}
    transfers = [
        row for row in payload.get("checks", ())
        if str(row.get("key", "")).startswith(("01_e2e", "02_e2e", "03_e2e"))
    ]
    return {
        "available": True,
        "is_simulated": True,
        # 이 축에서는 True일 수 있다. **실기 준비와 같은 값이 아니다.**
        "completed": bool(transfers) and all(row.get("passed")
                                            for row in transfers),
        "scenarios_passed": sum(1 for row in transfers if row.get("passed")),
        "scenarios_total": len(transfers),
        "passed_count": payload.get("passed_count"),
        "total_count": payload.get("total_count"),
        "checked_at": payload.get("checked_at"),
        # 시뮬레이터 결과는 실기 축에서 **항상** false다.
        "real_hardware_ready": False,
        "real_hardware_verified": False,
        "grasp_observation_kind": payload.get("grasp_observation_kind"),
        "source": str(path.relative_to(ROOT)),
        "note": payload.get("note"),
    }


def _load_robot_configs(
    config: ServerConfig, *, repository: SqliteRepository, now: float,
) -> tuple[AssetManifest | None, tuple]:
    """자산 manifest와 조합형 로봇 Profile을 읽고 DB에 기록한다.

    읽지 못하면 **조용히 비우지 않고** 빈 목록과 None을 돌려준다. 화면·API는
    "manifest를 읽지 못했다"를 그대로 표시한다.

    완성·검증되지 않은 구성은 Registry에 등록하지 않는다 — 선언은 보이지만
    실행 대상이 아니다.
    """
    root = config.robot_config_dir
    manifest: AssetManifest | None = None
    manifest_path = root / "assets" / "third_party_assets.json"
    if manifest_path.is_file():
        try:
            manifest = load_asset_manifest(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
        except Exception:  # noqa: BLE001 — 실패를 성공으로 바꾸지 않는다
            manifest = None

    profile_dir = root / "profiles"
    if not profile_dir.is_dir():
        return (manifest, ())

    arms: dict = {}
    grippers: dict = {}
    mountings: dict = {}
    composites_raw: list[dict] = []
    for path in sorted(profile_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        try:
            if "arm_profile_id" in payload:
                profile = load_arm_profile(payload)
                arms[profile.arm_profile_id] = profile
            elif "gripper_profile_id" in payload:
                profile = load_gripper_profile(payload)
                grippers[profile.gripper_profile_id] = profile
            elif "mounting_profile_id" in payload:
                profile = load_mounting_profile(payload)
                mountings[profile.mounting_profile_id] = profile
            elif "composite_profile_id" in payload:
                composites_raw.append(payload)
        except Exception:  # noqa: BLE001 — 잘못된 설정은 건너뛰고 기록도 하지 않는다
            continue

    composites: list = []
    for payload in composites_raw:
        try:
            composite = load_composite_profile(
                payload, arms=arms, grippers=grippers, mountings=mountings
            )
        except Exception:  # noqa: BLE001
            continue
        composites.append(composite)
        _record_profile(repository, composite, manifest, now)
    return (manifest, tuple(composites))


def _record_profile(
    repository: SqliteRepository, composite, manifest: AssetManifest | None,
    now: float,
) -> None:
    """Profile 한 버전을 append-only로 남긴다. 실패는 조용히 넘기지 않는다."""
    commit = ""
    checksum = ""
    if manifest is not None:
        # 자산 manifest에서 이 구성이 참조하는 출처 중 첫 commit·checksum을 남긴다.
        for entry in manifest.entries:
            if entry.commit and not commit:
                commit = entry.commit
            if entry.checksum and not checksum:
                checksum = entry.checksum
    record = RobotProfileRecord(
        composite_profile_id=composite.composite_profile_id,
        composite_profile_version=composite.composite_profile_version,
        display_name=composite.display_name,
        arm_profile_id=composite.arm.arm_profile_id,
        arm_profile_version=composite.arm.arm_profile_version,
        gripper_profile_id=(
            None if composite.gripper is None
            else composite.gripper.gripper_profile_id
        ),
        gripper_profile_version=(
            None if composite.gripper is None
            else composite.gripper.gripper_profile_version
        ),
        mounting_profile_id=(
            None if composite.mounting is None
            else composite.mounting.mounting_profile_id
        ),
        mounting_profile_version=(
            None if composite.mounting is None
            else composite.mounting.mounting_profile_version
        ),
        asset_manifest_version=composite.asset_manifest_version or "unknown",
        source_commit=commit, source_checksum=checksum,
        environment=composite.environment.value,
        verified=composite.verified,
        supported_skills=composite.supported_skills,
        unverified_items=composite.blocking_items,
        recorded_at=now,
        schema_version=TASK_PLAN_SCHEMA_VERSION,
    )
    try:
        repository.record_robot_profile(record)
    except Exception:  # noqa: BLE001 — 기록 실패는 조회로 드러난다
        pass


def build_runtime(config: ServerConfig) -> Runtime:
    """설정을 읽어 런타임을 만든다. 실패한 기능은 사용 불가로 표시한다."""
    cfg = config.config_dir
    # 작업 셀에 붙을 때는 **작업 셀 자원 카탈로그**를 쓴다. 발화에 등장할 수
    # 있는 자원이 씬에 실제로 있는 것과 같아야 한다(8-08 우선순위 6).
    workcell_manifest: dict | None = None
    if config.enable_workcell_robot and config.workcell_manifest.is_file():
        try:
            workcell_manifest = json.loads(
                config.workcell_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            workcell_manifest = None

    def _workcell_path(key: str) -> Path:
        """매니페스트 기준 상대 경로를 절대 경로로 만든다."""
        return (config.workcell_manifest.parent / workcell_manifest[key]).resolve()

    # 작업 셀에 붙을 때는 **그 셀의 자원 카탈로그**를 쓴다. 발화에 등장할 수
    # 있는 자원이 씬에 실제로 있는 것과 같아야 한다(8-08 우선순위 6).
    if workcell_manifest and workcell_manifest.get("enabled"):
        resource_catalog = load_resource_catalog(
            json.loads(_workcell_path("resource_catalog").read_text(encoding="utf-8")))
    else:
        resource_catalog = load_resource_catalog(
            _load(cfg, "valid_resource_catalog.json"))
    skill_catalog = load_skill_catalog(_load(cfg, "valid_skill_catalog.json"))
    safety_policy = load_safety_policy(_load(cfg, "valid_safety_policy.json"))
    freshness_policy = load_freshness_policy(_load(cfg, "valid_freshness_policy.json"))
    planning_policy = load_planning_policy(_load(cfg, "valid_planning_policy.json"))
    stt_policy = load_stt_policy(_load(cfg, "valid_stt_policy.json"))
    stop_policy = load_stop_policy(_load(cfg, "valid_stop_policy.json"))

    # ── 로봇 ────────────────────────────────────────────────────────────
    registry = RobotRegistry()
    robot_configured = False
    robot_id: str | None = None
    profile: CapabilityProfile | None = None
    def fake_factory(rid: str, prof: CapabilityProfile) -> FakeRobotAdapter:
        # 정지 판정 허용치는 정책에서 온다. 코드에 수치를 두지 않는다.
        return DevFakeAdapter(rid, prof, stop_policy=stop_policy)

    # ── 시뮬레이션 작업 셀 Adapter (8-08 우선순위 6) ─────────────────────
    # 실제 하드웨어가 아니다. 시뮬레이터 작업 셀에 붙는다.
    # **Adapter 모듈 경로는 설정에서 온다** — 공통 코드는 로봇·제조사 이름을
    # 알지 않는다(계획.md 27장). 붙지 못하면 등록하지 않고 이유를 남긴다.
    workcell_status: dict[str, object] | None = None
    runtime_declared_kind: list[str] = []
    workcell_extras: dict[str, Any] = {}
    if config.enable_workcell_robot:
        workcell_status = {
            "enabled": True, "registered": False, "is_simulated": True,
            "real_hardware_verified": False,
            "manifest": str(config.workcell_manifest),
            "adapter_module": (None if workcell_manifest is None
                               else workcell_manifest.get("adapter_module")),
        }
        try:
            if workcell_manifest is None:
                raise ValueError(
                    f"작업 셀 매니페스트를 읽을 수 없다: {config.workcell_manifest}")
            if not workcell_manifest.get("enabled"):
                raise ValueError("매니페스트의 enabled가 꺼져 있다")
            adapter_module = workcell_manifest.get("adapter_module")
            if not adapter_module:
                raise ValueError("매니페스트에 adapter_module이 없다")
            module = importlib.import_module(adapter_module)
            entry = getattr(module, module.ADAPTER_ENTRY_POINT)
            workcell_profile = load_capability_profile(json.loads(
                _workcell_path("capability_profile").read_text(encoding="utf-8")))
            built = entry(
                robot_id=workcell_profile.profile_id,
                profile=workcell_profile,
                workcell_config=_workcell_path("workcell_config"),
                workcell_poses=_workcell_path("poses"),
                now=time.time,
                stop_velocity_rad_s=stop_policy.position_tolerance.get("arm", 0.01),
                max_sample_gap_sec=stop_policy.max_sample_gap_sec,
            )
            factory = built["factory"]
            status = dict(built["status"])
            profile = workcell_profile
            robot_id = workcell_profile.profile_id
            robot_configured = True
            registry.register(robot_id, workcell_profile, factory)
            workcell_extras = {
                "motion_resolver": built.get("motion_resolver"),
                "scene_client_factory": built.get("scene_client_factory"),
                "geometry_validator_factory": built.get("geometry_validator_factory"),
                "pick_place_bindings": built.get("pick_place_bindings"),
                "grasp_limitations": built.get("grasp_limitations") or (),
            }
            workcell_status.update(status)
            workcell_status["registered"] = True
            # 어댑터 종류·시뮬레이션 여부를 선언값으로 남긴다. 어댑터를 만들기
            # 전에도 화면이 "Fake가 아니지만 실하드웨어도 아니다"를 보여준다.
            declared_kind = str(status.get("adapter_kind_label") or "")
            if declared_kind:
                runtime_declared_kind.append(declared_kind)
        except Exception as exc:  # noqa: BLE001 — 이유를 화면에 남긴다
            workcell_status.update({
                "registered": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "reason_code": "config.invalid",
            })

    if robot_configured:
        pass
    elif config.robot_profile_name:
        profile = load_capability_profile(_load(cfg, config.robot_profile_name))
        robot_id = profile.profile_id
        robot_configured = True
        registry.register(robot_id, profile, fake_factory)
    elif config.enable_fake_robot:
        # 실제 로봇이 아니다. '로봇 미설정'으로 표시하되 흐름은 시험할 수 있게 한다.
        profile = load_capability_profile(_load(cfg, "valid_capability_profile.json"))
        robot_id = FAKE_ROBOT_ID
        registry.register(FAKE_ROBOT_ID, profile, fake_factory)

    termination = (
        termination_requirement(profile=profile, safety_policy=safety_policy)
        if profile is not None
        else TerminationRequirement(
            final_skill=None, max_steps=safety_policy.max_steps,
            hold_possible=False, sources=("로봇 미설정",),
            conflicts=("등록된 로봇 Profile이 없다",),
        )
    )
    approach = (
        approach_requirement(
            skill_catalog=skill_catalog, safety_policy=safety_policy, profile=profile
        )
        if profile is not None
        else ApproachRequirement(approach_skill=None)
    )

    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    repository = SqliteRepository(str(config.db_path), now=time.time())

    runtime = Runtime(
        config=config, repository=repository, resource_catalog=resource_catalog,
        skill_catalog=skill_catalog, safety_policy=safety_policy,
        freshness_policy=freshness_policy, planning_policy=planning_policy,
        stop_policy=stop_policy, stt_policy=stt_policy, registry=registry,
        termination=termination,
        approach=approach, robot_configured=robot_configured, robot_id=robot_id,
        profile=profile,
    )
    runtime.workcell = workcell_status
    # 장면 영상: 작업 셀이 붙었고 장면 카메라가 켜져 있을 때만 둔다.
    if workcell_manifest and workcell_manifest.get("enabled"):
        from server.scene_view import SceneViewer

        viewer = SceneViewer(_workcell_path("workcell_config"))
        available, _ = viewer.available()
        runtime.scene_viewer = viewer if available else None
        if available:
            # 첫 요청 전에 구독을 시작해 버퍼를 채워 둔다(백그라운드, 막지 않는다).
            viewer.start_live()
    runtime.motion_resolver = workcell_extras.get("motion_resolver")
    runtime.scene_client_factory = workcell_extras.get("scene_client_factory")
    runtime.geometry_validator_factory = workcell_extras.get(
        "geometry_validator_factory")
    runtime.pick_place_bindings_factory = workcell_extras.get("pick_place_bindings")
    runtime.grasp_limitations = tuple(workcell_extras.get("grasp_limitations") or ())
    if runtime_declared_kind:
        runtime.declared_adapter_kind = runtime_declared_kind[0]
        runtime.declared_is_simulated = True

    # ── 선언된 로봇 구성과 제3자 자산 (8-01·8-02) ───────────────────────
    # 그리퍼 조립 검증 기록. 있으면 관문이 관측 조건을 그 기록으로 판정한다.
    # 작업 셀 보고서가 있으면 **그것이 현재 검증 결과다**(8-08에서 그리퍼
    # 컨트롤러를 고친 뒤의 관측). 없으면 단순 조립 셀 보고서로 떨어진다.
    verification_path = ROOT / "reports/workcell/gripper_control.json"
    if not verification_path.is_file():
        verification_path = ROOT / "reports/gripper/verify_gripper.json"
    if verification_path.is_file():
        try:
            runtime.gripper_verification = json.loads(
                verification_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            runtime.gripper_verification = None

    # ── 실기 전환 준비 (8-12) ────────────────────────────────────────────
    # **읽기 전용이다.** 이 판정은 실행 경로를 열지 않는다 — 실기 근거가
    # 무엇까지 모였는지 화면과 보고서가 보여줄 수 있게 싣기만 한다.
    runtime.hardware_readiness = _load_hardware_readiness()
    runtime.simulation_e2e = _load_simulation_e2e()
    _attach_sim_demo_jobs(runtime, config, workcell_manifest, _workcell_path)

    runtime.asset_manifest, runtime.composite_profiles = _load_robot_configs(
        config, repository=repository, now=time.time()
    )

    # ── 기하 검사 (6-05) ─────────────────────────────────────────────────
    # 개발용 Fake 셀에서만 개발용 구현체를 붙인다. 실제 로봇 Profile이 등록되면
    # 그 셀의 기하 자료(공식 URDF·MoveIt)가 확보될 때까지 구현체가 없고,
    # 기하 검사는 ASK다 — 검사하지 않은 상태를 안전으로 보지 않는다.
    if profile is not None and not robot_configured:
        cell = FakeCell.from_catalog(resource_catalog, frame_id=runtime.frame_id)
        runtime.geometry_cell = cell
        runtime.geometry_validator = DevFakeGeometryValidator(cell)

    # ── 계획 공급자 ─────────────────────────────────────────────────────
    try:
        llm_config = load_llm_provider_config(_load(cfg, config.llm_config_name))
    except Exception as exc:  # noqa: BLE001 — 설정이 없으면 기능을 끈다
        runtime.planning = FeatureState(False, f"LLM 설정을 읽을 수 없다: {exc}"[:200])
    else:
        runtime.llm_config = llm_config
        provider = VllmPlanProvider(
            config=llm_config,
            client=OpenAiCompatClient(config=llm_config),
            resource_catalog=resource_catalog, skill_catalog=skill_catalog,
            termination=termination, approach=approach,
        )
        try:
            info = provider.verify_server()
        except Exception as exc:  # noqa: BLE001 — 서버가 없으면 기능을 끈다
            runtime.planning = FeatureState(
                False, f"모델 서버를 확인할 수 없다: {exc}"[:200]
            )
        else:
            runtime.provider = provider
            runtime.planning = FeatureState(
                True,
                f"{llm_config.model_id} / {info.server_version or '버전 불명'}",
            )

    # ── STT ────────────────────────────────────────────────────────────
    if not config.enable_stt:
        runtime.stt = FeatureState(False, "설정에서 꺼져 있다")
    else:
        try:
            stt_model_config = load_stt_model_config(
                _load(cfg, "valid_stt_model_lowspec.json")
            )
            from stt.faster_whisper_backend import FasterWhisperBackend
            from stt.silero_vad_backend import SileroVadBackend

            transcriber = FasterWhisperBackend(stt_model_config)
            report = transcriber.load()
            vad_probe = SileroVadBackend(stt_model_config)
            vad_probe.load()
        except Exception as exc:  # noqa: BLE001 — 모델이 없으면 기능을 끈다
            runtime.stt = FeatureState(False, f"STT 모델을 올릴 수 없다: {exc}"[:200])
        else:
            runtime.stt_model_config = stt_model_config
            runtime.stt = FeatureState(
                True,
                f"{stt_model_config.model_name}/{stt_model_config.device}"
                f"/{stt_model_config.compute_type} (로딩 {report.load_sec:.1f}s)",
            )

            def make_stt() -> dict:
                return {
                    "transcriber": transcriber,
                    "vad": SileroVadBackend(stt_model_config),
                    "config": stt_model_config,
                    "load_sec": report.load_sec,
                }

            runtime.stt_factory = make_stt

    return runtime
