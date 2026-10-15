"""Keeping a session on one model (``routing.model_selection.session``).

``continue_session`` is handed the candidates able to serve, best first, and the
session's stored state; it decides whether the session keeps its model. The
store remembers that state per session.
"""

from __future__ import annotations

import pytest

from routing.model_selection import (
    Candidate,
    SessionAction,
    SessionState,
    SessionStore,
    TurnKind,
    continue_session,
)

FIRST = Candidate("first")
SECOND = Candidate("second")
THIRD = Candidate("third")
VERSION = "v1"


def _state(candidate: Candidate, *, version: str = VERSION, turns: int = 3, switches: int = 1):
    return SessionState(candidate, version, turns=turns, switches=switches)


def test_a_session_without_state_takes_the_first_choice():
    order, action, state = continue_session([FIRST, SECOND], None, TurnKind.NEW_TURN, VERSION)

    assert order == (FIRST, SECOND)
    assert action is SessionAction.NEW
    assert state == SessionState(FIRST, VERSION, turns=1, switches=0)


def test_a_tool_loop_keeps_its_model_though_a_better_one_is_back():
    """The session fell back to SECOND; FIRST recovered mid-loop. The loop stays put."""
    order, action, state = continue_session(
        [FIRST, SECOND, THIRD], _state(SECOND), TurnKind.TOOL_CONTINUATION, VERSION
    )

    assert order == (SECOND, FIRST, THIRD)
    assert action is SessionAction.KEEP
    assert state == _state(SECOND)


def test_a_tool_loop_moves_when_its_model_can_no_longer_serve():
    """Circuit open, access lost, or the conversation outgrew the window: it is not ranked."""
    order, action, state = continue_session(
        [SECOND, THIRD], _state(FIRST), TurnKind.TOOL_CONTINUATION, VERSION
    )

    assert order == (SECOND, THIRD)
    assert action is SessionAction.FORCED_SWITCH
    assert state == SessionState(SECOND, VERSION, turns=3, switches=2)


def test_a_new_turn_returns_to_the_first_choice_once_it_is_back():
    order, action, state = continue_session(
        [FIRST, SECOND], _state(SECOND), TurnKind.NEW_TURN, VERSION
    )

    assert order == (FIRST, SECOND)
    assert action is SessionAction.TURN_SWITCH
    assert state == SessionState(FIRST, VERSION, turns=4, switches=2)


def test_a_new_turn_on_the_first_choice_keeps_it():
    order, action, state = continue_session(
        [FIRST, SECOND], _state(FIRST), TurnKind.NEW_TURN, VERSION
    )

    assert order == (FIRST, SECOND)
    assert action is SessionAction.KEEP
    assert state == _state(FIRST, turns=4)


def test_a_new_turn_whose_model_cannot_serve_is_a_forced_switch():
    order, action, state = continue_session([SECOND], _state(FIRST), TurnKind.NEW_TURN, VERSION)

    assert order == (SECOND,)
    assert action is SessionAction.FORCED_SWITCH
    assert state == SessionState(SECOND, VERSION, turns=4, switches=2)


def test_a_side_call_uses_the_sessions_model_and_changes_nothing():
    """A safety check in a session on SECOND is not answered by anything else first."""
    order, action, state = continue_session(
        [FIRST, SECOND], _state(SECOND), TurnKind.SIDE_CALL, VERSION
    )

    assert order == (SECOND, FIRST)
    assert action is SessionAction.SIDE_CALL
    assert state is None


@pytest.mark.parametrize("stored", [None, _state(THIRD)], ids=["no-session", "model-gone"])
def test_a_side_call_otherwise_takes_the_first_choice(stored):
    order, action, state = continue_session([FIRST, SECOND], stored, TurnKind.SIDE_CALL, VERSION)

    assert order == (FIRST, SECOND)
    assert action is SessionAction.SIDE_CALL
    assert state is None


def test_state_from_another_version_is_not_inherited():
    """Another tier, or this tier's configuration changed: the session starts over."""
    order, action, state = continue_session(
        [FIRST, SECOND], _state(SECOND, version="other"), TurnKind.TOOL_CONTINUATION, VERSION
    )

    assert order == (FIRST, SECOND)
    assert action is SessionAction.NEW
    assert state == SessionState(FIRST, VERSION, turns=0, switches=0)


@pytest.mark.parametrize(
    ("stored", "turn", "action"),
    [
        (None, TurnKind.NEW_TURN, SessionAction.NEW),
        (_state(FIRST), TurnKind.TOOL_CONTINUATION, SessionAction.FORCED_SWITCH),
        (_state(FIRST), TurnKind.SIDE_CALL, SessionAction.SIDE_CALL),
    ],
)
def test_with_nothing_able_to_serve_the_stored_state_is_left_alone(stored, turn, action):
    order, got_action, state = continue_session([], stored, turn, VERSION)

    assert order == ()
    assert got_action is action
    assert state is None


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _store(clock: _Clock, *, idle: float = 60.0, capacity: int = 10) -> SessionStore:
    return SessionStore(idle_timeout_seconds=idle, max_sessions=capacity, time_source=clock)


def test_the_store_returns_what_was_stored_under_the_same_key_only():
    clock = _Clock()
    store = _store(clock)
    store.put(("caller-a", "session-1"), _state(FIRST))

    assert store.get(("caller-a", "session-1")) == _state(FIRST)
    assert store.get(("caller-b", "session-1")) is None


def test_a_session_idle_past_the_timeout_is_forgotten():
    clock = _Clock()
    store = _store(clock, idle=60.0)
    store.put("s", _state(FIRST))

    clock.now += 60.0
    assert store.get("s") == _state(FIRST)
    clock.now += 0.1
    assert store.get("s") is None
    assert len(store) == 0


def test_storing_restarts_the_idle_timeout():
    clock = _Clock()
    store = _store(clock, idle=60.0)
    store.put("s", _state(FIRST))
    clock.now += 50.0
    store.put("s", _state(SECOND))
    clock.now += 50.0

    assert store.get("s") == _state(SECOND)


def test_storing_drops_expired_sessions_and_then_the_least_recent():
    clock = _Clock()
    store = _store(clock, idle=60.0, capacity=2)
    store.put("old", _state(FIRST))
    clock.now += 61.0
    store.put("a", _state(FIRST))

    assert len(store) == 1

    store.put("b", _state(FIRST))
    store.put("a", _state(SECOND))
    store.put("c", _state(FIRST))

    assert store.get("b") is None
    assert store.get("a") == _state(SECOND)
    assert store.get("c") == _state(FIRST)


@pytest.mark.parametrize(("idle", "capacity"), [(0, 10), (60, 0)])
def test_the_store_refuses_a_non_positive_bound(idle, capacity):
    with pytest.raises(ValueError):
        SessionStore(idle_timeout_seconds=idle, max_sessions=capacity)
