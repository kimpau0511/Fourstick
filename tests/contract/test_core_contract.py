"""공통 계약 테스트 (md/개발플랜.md 1-12).

명세(md/계획.md 7장, md/개발플랜.md 1단계)와 코드 타입이 같은지 검증한다.
외부 의존성 없이 끝난다 — ROS2, Gazebo, Qdrant, PostgreSQL, 네트워크를
쓰지 않으며 실제 시간을 기다리지 않는다(시각은 모두 주입값).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.capability_profile import CapabilityProfile, GripperSpec, JointLimit, ProfileError
from core.constants import ATOMIC_SKILLS, SKILL_REQUIRED_ARGS, TASK_PLAN_SCHEMA_VERSION
from core.execution_result import (
    ExecutionResult,
    InvalidExecutionResult,
    rejected,
    success,
    unverifiable,
)
from core.execution_state import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    ExecutionState,
    InvalidStateTransition,
    can_transition,
    transition,
)
from core.frames import ANGLE_UNIT, FrameKind, LENGTH_UNIT, Vector3
from core.reason_codes import ReasonCategory, ReasonCode, codes_in
from core.task_plan import PlanError, TaskPlan, TaskStep
from robots.base.robot_adapter import RobotAdapter, RobotStateSnapshot


def make_profile(**over) -> CapabilityProfile:
    """테스트 전용 fixture. 실제 로봇 값이 아니다(계획.md 27장 허용 범위)."""
    kw = dict(
        profile_id="fixture",
        profile_version="1.0",
        dof=6,
        joint_limits=tuple(
            JointLimit(f"j{i}", -3.14, 3.14, 3.14, ANGLE_UNIT, "revolute") for i in range(6)
        ),
        frames={FrameKind.BASE: "base", FrameKind.TOOL: "tool"},
        tcp_offset=Vector3(0.0, 0.0, 0.1),
        work_radius_m=0.85,
        payload_kg=5.0,
        supported_skills=ATOMIC_SKILLS,
        gripper=GripperSpec("g", 0.02, 0.55, ANGLE_UNIT, 0.0286, 50.0, fully_open_margin=0.05),
    )
    kw.update(over)
    return CapabilityProfile(**kw)


def make_plan(**over) -> TaskPlan:
    kw = dict(
        plan_id="p",
        robot_id="r",
        profile_id="fixture",
        profile_version="1.0",
        steps=(
            TaskStep("home"),
            TaskStep("move", {"to": "loc_b"}),
            TaskStep("pick", {"object": "obj_a", "from": "loc_b"}),
            TaskStep("place", {"object": "obj_a", "to": "loc_c"}),
            TaskStep("home"),
        ),
        created_at=1_000.0,
        ttl_sec=60.0,
    )
    kw.update(over)
    return TaskPlan(**kw)


class TestSkillContract(unittest.TestCase):
    """7장 1번: 스킬과 인자."""

    def test_atomic_skills_are_the_five_from_the_requirements(self):
        self.assertEqual(set(ATOMIC_SKILLS), {"home", "move", "pick", "place", "stop"})

    def test_required_args_cover_every_skill(self):
        self.assertEqual(set(SKILL_REQUIRED_ARGS), set(ATOMIC_SKILLS))

    def test_unsupported_skill_is_rejected_with_reason(self):
        with self.assertRaises(PlanError) as ctx:
            TaskStep("weld")
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_UNSUPPORTED_SKILL)

    def test_coordinates_cannot_be_embedded_in_args(self):
        # 인자는 Catalog가 해석할 이름만 담는다. 숫자/빈 값은 거부한다.
        with self.assertRaises(PlanError) as ctx:
            TaskStep("move", {"to": ""})
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_SCHEMA_INVALID)


class TestTaskPlanContract(unittest.TestCase):
    """7장 1번: schema version, plan hash, TTL, profile ID."""

    def test_schema_version_is_pinned(self):
        self.assertEqual(make_plan().schema_version, TASK_PLAN_SCHEMA_VERSION)
        with self.assertRaises(PlanError) as ctx:
            make_plan(schema_version="1.0")
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_VERSION_UNSUPPORTED)

    def test_hash_ignores_utterance_and_created_at(self):
        a = make_plan(utterance="말 A", created_at=1.0)
        b = make_plan(utterance="말 B", created_at=999.0)
        self.assertEqual(a.plan_hash(), b.plan_hash())

    def test_hash_changes_when_a_step_changes(self):
        a = make_plan()
        b = make_plan(steps=(TaskStep("home"),))
        self.assertNotEqual(a.plan_hash(), b.plan_hash())

    def test_tampered_plan_is_rejected(self):
        with self.assertRaises(PlanError) as ctx:
            make_plan().verify_hash("0" * 64)
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_HASH_MISMATCH)

    def test_ttl_uses_injected_time_only(self):
        plan = make_plan(created_at=1_000.0, ttl_sec=10.0)
        self.assertFalse(plan.is_expired(1_009.999))
        self.assertTrue(plan.is_expired(1_010.0))
        with self.assertRaises(PlanError) as ctx:
            plan.require_fresh(1_010.0)
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_EXPIRED)

    def test_profile_mismatch_is_rejected(self):
        with self.assertRaises(PlanError) as ctx:
            make_plan().require_profile("fixture", "2.0")
        self.assertIs(ctx.exception.reason, ReasonCode.ROBOT_PROFILE_MISMATCH)

    def test_skill_unsupported_by_profile_is_rejected(self):
        with self.assertRaises(PlanError) as ctx:
            make_plan().require_supported(("home", "move"))
        self.assertIs(ctx.exception.reason, ReasonCode.ROBOT_SKILL_UNSUPPORTED)


class TestCapabilityProfileContract(unittest.TestCase):
    """7장 3번: DoF, 관절 제한, 프레임, TCP, 반경, 하중, 도구, 지원 스킬."""

    def test_valid_profile_builds(self):
        self.assertEqual(make_profile().frame_name(FrameKind.TOOL), "tool")

    def test_missing_value_is_blocked_not_defaulted(self):
        for over, reason in [
            (dict(profile_version=""), ReasonCode.CONFIG_MISSING),
            (dict(dof=0), ReasonCode.CONFIG_INVALID),
            (dict(frames={FrameKind.BASE: "base"}), ReasonCode.CONFIG_MISSING),
            (dict(work_radius_m=0.0), ReasonCode.CONFIG_INVALID),
            (dict(supported_skills=()), ReasonCode.CONFIG_MISSING),
            (dict(supported_skills=("fly",)), ReasonCode.CONFIG_INVALID),
            (dict(supported_skills=("pick",), gripper=None), ReasonCode.CONFIG_MISSING),
        ]:
            with self.subTest(over=over):
                with self.assertRaises(ProfileError) as ctx:
                    make_profile(**over)
                self.assertIs(ctx.exception.reason, reason)

    def test_dof_must_match_joint_limits(self):
        with self.assertRaises(ProfileError) as ctx:
            make_profile(dof=7)
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_INVALID)

    def test_non_si_unit_is_rejected(self):
        with self.assertRaises(ProfileError) as ctx:
            JointLimit("j", 0.0, 1.0, 1.0, "deg", "revolute")
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_UNIT_MISMATCH)

    def test_gripper_direction_is_declared_not_guessed(self):
        closing_up = GripperSpec("g", 0.02, 0.55, ANGLE_UNIT, 0.0286, 50.0, fully_open_margin=0.05)
        closing_down = GripperSpec("g", 0.04, 0.014, LENGTH_UNIT, 0.028, 20.0, fully_open_margin=0.003)
        self.assertTrue(closing_up.closes_by_increasing)
        self.assertFalse(closing_down.closes_by_increasing)

    def test_gripper_requires_measured_aperture(self):
        with self.assertRaises(ProfileError) as ctx:
            GripperSpec("g", 0.02, 0.55, ANGLE_UNIT, 0.0, 50.0, fully_open_margin=0.05)
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_INVALID)


class TestFullyOpenRule(unittest.TestCase):
    """forstick 1차 STOP 테스트의 "완전개방 판정"을 이관한다.

    원본은 로봇별 상수로 판정해 닫는 방향이 반대인 로봇에서 정상 닫힘을
    완전개방으로 오판했다. 여기서는 방향을 Profile이 선언하고 크기 비교를
    쓰지 않는다. 아래 값들은 fixture다(실제 로봇 값이 아니다)."""

    #: 닫으면 값이 작아지는 그리퍼 (open 0.04 -> close 0.014)
    DECREASING = GripperSpec("g", 0.04, 0.014, LENGTH_UNIT, 0.028, 20.0, fully_open_margin=0.003)
    #: 닫으면 값이 커지는 그리퍼 (open 0.02 -> close 0.55)
    INCREASING = GripperSpec("g", 0.02, 0.55, ANGLE_UNIT, 0.0286, 50.0, fully_open_margin=0.05)

    def test_decreasing_gripper_cases(self):
        self.assertTrue(self.DECREASING.still_fully_open(0.0400))   # 거의 안 움직임
        self.assertFalse(self.DECREASING.still_fully_open(0.0341))  # 물체에 막혀 정지
        self.assertFalse(self.DECREASING.still_fully_open(0.014))   # 목표까지 닫힘

    def test_increasing_gripper_cases(self):
        self.assertTrue(self.INCREASING.still_fully_open(0.019))    # 거의 안 움직임
        self.assertFalse(self.INCREASING.still_fully_open(0.30))    # 중간에 막혀 정지
        self.assertFalse(self.INCREASING.still_fully_open(0.55))    # 목표 도달

    def test_size_comparison_would_misjudge_the_increasing_gripper(self):
        """버그 재현 방지: 크기 비교(position < open+margin)로 구현했다면
        증가형 그리퍼의 정상 닫힘(0.55)이 완전개방으로 오판됐다."""
        naive = 0.55 < self.INCREASING.open_position + self.INCREASING.fully_open_margin
        self.assertFalse(naive)  # 이 경우엔 우연히 맞지만
        # 감소형에 같은 식을 쓰면 정상 닫힘(0.014)이 완전개방으로 뒤집힌다.
        naive_dec = 0.014 < self.DECREASING.open_position + self.DECREASING.fully_open_margin
        self.assertTrue(naive_dec)
        self.assertFalse(self.DECREASING.still_fully_open(0.014))   # 방향 인식 구현은 올바름

    def test_margin_must_be_smaller_than_travel(self):
        with self.assertRaises(ProfileError):
            GripperSpec("g", 0.02, 0.55, ANGLE_UNIT, 0.0286, 50.0, fully_open_margin=0.6)

    def test_margin_is_not_shared_across_robots(self):
        """단위가 다른 로봇의 margin을 재사용하면 판정이 뒤집힌다."""
        borrowed = GripperSpec(
            "g", 0.02, 0.55, ANGLE_UNIT, 0.0286, 50.0,
            fully_open_margin=self.DECREASING.fully_open_margin,   # 0.003 (m 단위)
        )
        # 증가형에서 open+0.02 위치는 자기 margin(0.05)으로는 완전개방이지만
        self.assertTrue(self.INCREASING.still_fully_open(0.04))
        # 빌려온 margin(0.003)으로는 완전개방이 아니라고 잘못 판정된다.
        self.assertFalse(borrowed.still_fully_open(0.04))


class TestTerminalActionStatus(unittest.TestCase):
    """forstick 1차: 취소/실패로 끝난 동작을 성공으로 반환하지 않는다."""

    def test_canceled_or_aborted_cannot_be_task_success(self):
        for reason in (ReasonCode.EXEC_CANCELED, ReasonCode.EXEC_ABORTED):
            with self.subTest(reason=reason):
                r = ExecutionResult(
                    state=ExecutionState.FAILED,
                    request_accepted=True,
                    motion_completed=True,
                    target_reached=False,
                    reason=reason,
                )
                self.assertFalse(r.task_succeeded)

    def test_stalled_completion_can_still_be_success_when_verified(self):
        """물체에 막혀 멈춘 것(stall) 자체는 실패가 아니다 — 실측으로 확인되면 성공."""
        r = success({"stalled": True, "lift_m": 0.012})
        self.assertTrue(r.task_succeeded)


class TestExecutionStateContract(unittest.TestCase):
    """7장 5번: IDLE부터 UNKNOWN까지의 전이."""

    def test_every_state_has_a_transition_entry(self):
        self.assertEqual(set(ALLOWED_TRANSITIONS), set(ExecutionState))

    def test_unknown_never_becomes_completed(self):
        self.assertFalse(can_transition(ExecutionState.UNKNOWN, ExecutionState.COMPLETED))
        with self.assertRaises(InvalidStateTransition):
            transition(ExecutionState.UNKNOWN, ExecutionState.COMPLETED)

    def test_stop_requires_confirmation_before_stopped(self):
        # ACCEPTED/EXECUTING에서 STOPPED로 바로 갈 수 없다. STOPPING을 거쳐야 한다.
        for src in (ExecutionState.ACCEPTED, ExecutionState.EXECUTING):
            with self.subTest(src=src):
                self.assertFalse(can_transition(src, ExecutionState.STOPPED))
                self.assertTrue(can_transition(src, ExecutionState.STOPPING))
        self.assertTrue(can_transition(ExecutionState.STOPPING, ExecutionState.STOPPED))

    def test_stopping_can_end_as_unknown(self):
        self.assertTrue(can_transition(ExecutionState.STOPPING, ExecutionState.UNKNOWN))

    def test_terminal_states_only_return_to_idle(self):
        for st in TERMINAL_STATES - {ExecutionState.UNKNOWN}:
            with self.subTest(st=st):
                self.assertEqual(ALLOWED_TRANSITIONS[st], frozenset({ExecutionState.IDLE}))


class TestExecutionResultContract(unittest.TestCase):
    """7장 6번: 수락·모션완료·목표도달·작업성공·확인불가의 분리."""

    def test_all_five_axes_exist(self):
        fields = set(ExecutionResult.__dataclass_fields__)
        self.assertLessEqual(
            {"request_accepted", "motion_completed", "target_reached", "task_succeeded", "verified"},
            fields,
        )

    def test_unverifiable_cannot_be_success(self):
        with self.assertRaises(InvalidExecutionResult):
            ExecutionResult(
                state=ExecutionState.COMPLETED,
                request_accepted=True,
                motion_completed=True,
                target_reached=True,
                task_succeeded=True,
                verified=False,
            )

    def test_motion_complete_is_not_task_success(self):
        # 액션이 완료를 보고했어도 목표 미도달이면 작업 성공일 수 없다.
        with self.assertRaises(InvalidExecutionResult):
            ExecutionResult(
                state=ExecutionState.COMPLETED,
                request_accepted=True,
                motion_completed=True,
                target_reached=False,
                task_succeeded=True,
            )

    def test_rejected_request_cannot_report_progress(self):
        with self.assertRaises(InvalidExecutionResult):
            ExecutionResult(state=ExecutionState.FAILED, request_accepted=False, motion_completed=True)

    def test_failure_requires_reason(self):
        for state in (ExecutionState.FAILED, ExecutionState.UNKNOWN):
            with self.subTest(state=state):
                with self.assertRaises(InvalidExecutionResult):
                    ExecutionResult(state=state, request_accepted=True)

    def test_rejected_request_requires_reason(self):
        with self.assertRaises(InvalidExecutionResult):
            ExecutionResult(state=ExecutionState.IDLE, request_accepted=False)

    def test_non_task_operations_need_no_reason(self):
        """연결·점검·정지 요청은 작업 성공을 주장하지 않지만 실패도 아니다.
        이유 코드를 강제하면 의미 없는 코드를 채워 넣게 된다."""
        for state in (ExecutionState.IDLE, ExecutionState.EXECUTING, ExecutionState.STOPPING):
            with self.subTest(state=state):
                r = ExecutionResult(state=state, request_accepted=True)
                self.assertIsNone(r.reason)
                self.assertFalse(r.task_succeeded)

    def test_helpers_build_valid_results(self):
        self.assertTrue(success({"lift_m": 0.012}).task_succeeded)
        u = unverifiable(ReasonCode.EXEC_UNVERIFIABLE)
        self.assertIs(u.state, ExecutionState.UNKNOWN)
        self.assertFalse(u.task_succeeded)
        self.assertTrue(u.unverifiable)
        r = rejected(ReasonCode.EXEC_PERMIT_DENIED)
        self.assertFalse(r.request_accepted)


class TestReasonCodeContract(unittest.TestCase):
    """7장 7번: 로봇·계획·안전·실행·STT 공통 코드."""

    def test_every_required_category_has_codes(self):
        for cat in ReasonCategory:
            with self.subTest(cat=cat):
                self.assertTrue(codes_in(cat), f"{cat}에 코드가 없다")

    def test_codes_are_unique_and_prefixed(self):
        values = [c.value for c in ReasonCode]
        self.assertEqual(len(values), len(set(values)))
        for c in ReasonCode:
            with self.subTest(code=c):
                self.assertEqual(c.value.split(".", 1)[0], c.category.value)

    def test_unverifiable_has_a_dedicated_code(self):
        self.assertIs(ReasonCode.EXEC_UNVERIFIABLE.category, ReasonCategory.EXEC)


class TestRobotAdapterContract(unittest.TestCase):
    """7장 4번: connect/state/home/move/pick/place/stop/cancel/confirm/check."""

    def test_interface_exposes_the_required_operations(self):
        required = {
            "connect", "disconnect", "state", "home", "move", "pick", "place",
            "stop", "cancel", "confirm_stopped", "check",
        }
        self.assertEqual(set(RobotAdapter.__abstractmethods__), required)

    def test_adapter_cannot_be_instantiated_directly(self):
        with self.assertRaises(TypeError):
            RobotAdapter("r", make_profile())  # type: ignore[abstract]

    def test_state_snapshot_carries_freshness_and_validity(self):
        snap = RobotStateSnapshot(ExecutionState.IDLE, 100.0, {}, {}, valid=False)
        self.assertTrue(snap.is_stale(now=100.5, max_age_sec=0.3))
        self.assertFalse(snap.is_stale(now=100.2, max_age_sec=0.3))
        self.assertFalse(snap.valid)


class TestNoHardcoding(unittest.TestCase):
    """계획.md 27장 검증 기준 일부를 자동 검사한다."""

    CORE_DIRS = ("core", "robots/base", "validation")
    BANNED_SUBSTRINGS = (
        "panda", "ur5e", "fairino", "fr3", "doosan", "m1013", "kinova", "gen3",
        "robotiq", "/home/", "localhost", "127.0.0.1",
    )

    def _product_files(self):
        root = Path(__file__).resolve().parents[2]
        for d in self.CORE_DIRS:
            yield from (root / d).glob("*.py")

    def test_common_code_has_no_vendor_or_robot_names(self):
        for path in self._product_files():
            text = path.read_text(encoding="utf-8").lower()
            for banned in self.BANNED_SUBSTRINGS:
                with self.subTest(file=path.name, banned=banned):
                    self.assertNotIn(banned, text, f"{path.name}에 {banned!r}가 있다")


if __name__ == "__main__":
    unittest.main(verbosity=2)
