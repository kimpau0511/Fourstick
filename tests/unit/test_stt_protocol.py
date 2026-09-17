"""STT WebSocket 계약 테스트 (md/개발플랜.md 1-09)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from stt.protocol import (
    ACCEPTED_CLIENT_MESSAGES,
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    ClientMessage,
    InvalidSttTransition,
    ServerMessage,
    SttSessionState,
    accepts,
    can_transition,
    transition,
)


class TestSessionStates(unittest.TestCase):
    def test_every_state_has_transition_and_message_entries(self):
        self.assertEqual(set(ALLOWED_TRANSITIONS), set(SttSessionState))
        self.assertEqual(set(ACCEPTED_CLIENT_MESSAGES), set(SttSessionState))

    def test_speech_must_be_observed_before_speaking(self):
        self.assertFalse(can_transition(SttSessionState.IDLE, SttSessionState.SPEAKING))
        self.assertTrue(can_transition(SttSessionState.LISTENING, SttSessionState.SPEAKING))

    def test_flush_without_speech_can_go_straight_to_finalizing(self):
        self.assertTrue(can_transition(SttSessionState.LISTENING, SttSessionState.FINALIZING))

    def test_finalizing_returns_to_listening_for_the_next_utterance(self):
        self.assertTrue(can_transition(SttSessionState.FINALIZING, SttSessionState.LISTENING))

    def test_closed_is_terminal(self):
        self.assertEqual(ALLOWED_TRANSITIONS[SttSessionState.CLOSED], frozenset())
        self.assertIn(SttSessionState.CLOSED, TERMINAL_STATES)
        with self.assertRaises(InvalidSttTransition):
            transition(SttSessionState.CLOSED, SttSessionState.LISTENING)

    def test_every_state_can_be_closed(self):
        for state in SttSessionState:
            if state is SttSessionState.CLOSED:
                continue
            with self.subTest(state=state):
                self.assertTrue(can_transition(state, SttSessionState.CLOSED))


class TestMessageAcceptance(unittest.TestCase):
    def test_abort_is_accepted_in_every_live_state(self):
        """정지를 세션 상태 때문에 막지 않는다."""
        for state in SttSessionState:
            if state is SttSessionState.CLOSED:
                continue
            with self.subTest(state=state):
                self.assertTrue(accepts(state, ClientMessage.ABORT))

    def test_audio_is_refused_before_start(self):
        self.assertFalse(accepts(SttSessionState.IDLE, ClientMessage.AUDIO))

    def test_audio_is_refused_while_finalizing(self):
        self.assertFalse(accepts(SttSessionState.FINALIZING, ClientMessage.AUDIO))

    def test_start_is_only_accepted_once(self):
        self.assertTrue(accepts(SttSessionState.IDLE, ClientMessage.START))
        for state in (SttSessionState.LISTENING, SttSessionState.SPEAKING):
            with self.subTest(state=state):
                self.assertFalse(accepts(state, ClientMessage.START))

    def test_closed_session_accepts_nothing(self):
        for msg in ClientMessage:
            with self.subTest(msg=msg):
                self.assertFalse(accepts(SttSessionState.CLOSED, msg))


class TestMessageKinds(unittest.TestCase):
    def test_partial_and_final_are_distinct(self):
        self.assertNotEqual(ServerMessage.PARTIAL, ServerMessage.FINAL)

    def test_failure_is_a_message_not_just_a_socket_close(self):
        """소켓만 끊으면 클라이언트가 이유를 알 수 없다."""
        self.assertIn(ServerMessage.ERROR, set(ServerMessage))

    def test_clarify_exists_for_ambiguous_input(self):
        self.assertIn(ServerMessage.CLARIFY, set(ServerMessage))

    def test_message_values_are_unique(self):
        for enum in (ClientMessage, ServerMessage, SttSessionState):
            with self.subTest(enum=enum.__name__):
                values = [m.value for m in enum]
                self.assertEqual(len(values), len(set(values)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
