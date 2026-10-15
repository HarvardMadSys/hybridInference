"""Keeping a session on one model, and the store that remembers which.

Switching models mid-session is not free: the new model starts with a cold
prompt cache, and in an agent's tool loop it inherits a plan it did not make. So
a session stays on its model, and moves only at a safe boundary:

- **In a tool loop** it keeps its model while that model can serve. When it
  cannot -- its circuit opened, the caller lost access, the conversation outgrew
  its window -- the session moves to the best candidate that can, there and then.
- **At a new user turn** the request is ranked afresh, and the session moves if
  a better-ranked candidate can serve. With a ranking that never changes, that
  is the turn a session that fell back returns to its first choice.
- **A side call** uses the session's model if it can serve and the first choice
  otherwise, and changes nothing.
- **A session with no state**, or with state from another tier or another
  version of this one, is ranked as new. The store forgets a session that has
  been idle for its timeout.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from routing.model_selection.types import SessionAction, SessionState, TurnKind

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable, Sequence

    from routing.model_selection.types import Candidate

__all__ = ["SessionStore", "continue_session"]


def continue_session(
    ranked: Sequence[Candidate],
    state: SessionState | None,
    turn: TurnKind,
    profile_version: str,
) -> tuple[tuple[Candidate, ...], SessionAction, SessionState | None]:
    """Apply the session rules to a ranking.

    Args:
        ranked: The candidates able to serve, best first.
        state: The session's stored state, if any.
        turn: Where the request falls in its session.
        profile_version: The version of the tier the request asked for.

    Returns:
        The candidates in the order to use them, what happened to the session's
        model, and the state to store (``None`` to leave the stored state alone).
    """
    usable = state if state is not None and state.profile_version == profile_version else None
    current = usable.candidate if usable is not None else None
    order = tuple(ranked)

    if turn is TurnKind.SIDE_CALL:
        if current in order:
            order = (current, *(candidate for candidate in order if candidate != current))
        return order, SessionAction.SIDE_CALL, None
    if not order:
        return order, (SessionAction.NEW if usable is None else SessionAction.FORCED_SWITCH), None

    new_turn = 1 if turn is TurnKind.NEW_TURN else 0
    if usable is None:
        return order, SessionAction.NEW, SessionState(order[0], profile_version, turns=new_turn)

    turns = usable.turns + new_turn
    if current not in order:
        action = SessionAction.FORCED_SWITCH
    elif turn is TurnKind.TOOL_CONTINUATION or order[0] == current:
        order = (current, *(candidate for candidate in order if candidate != current))
        return (
            order,
            SessionAction.KEEP,
            SessionState(current, profile_version, turns=turns, switches=usable.switches),
        )
    else:
        action = SessionAction.TURN_SWITCH
    return (
        order,
        action,
        SessionState(order[0], profile_version, turns=turns, switches=usable.switches + 1),
    )


class SessionStore:
    """Bounded, thread-safe, in-process map from session key to session state.

    The gateway runs as one process, so this is where the state lives; a restart
    forgets every session, and its next request is ranked as new. Moving to
    several replicas means moving this to shared storage.

    Keys are whatever the serving layer builds, and must include the caller: a
    session id is declared by the client, so a key without the caller would let
    one caller rewrite another's session.

    An entry expires ``idle_timeout_seconds`` after it was last stored. Past
    ``max_sessions`` the least recently stored entry is dropped.
    """

    def __init__(
        self,
        *,
        idle_timeout_seconds: float,
        max_sessions: int,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create an empty store.

        Raises:
            ValueError: If the timeout or the capacity is not positive.
        """
        if idle_timeout_seconds <= 0:
            raise ValueError(f"idle_timeout_seconds must be positive, got {idle_timeout_seconds}")
        if max_sessions <= 0:
            raise ValueError(f"max_sessions must be positive, got {max_sessions}")
        self._idle_timeout = float(idle_timeout_seconds)
        self._max_sessions = int(max_sessions)
        self._time = time_source
        # Ordered by when each entry was last stored, oldest first.
        self._entries: OrderedDict[Hashable, tuple[SessionState, float]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> SessionState | None:
        """Return the session's state, or ``None`` if it has none or it expired."""
        now = self._time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            state, stored_at = entry
            if now - stored_at > self._idle_timeout:
                del self._entries[key]
                return None
            return state

    def put(self, key: Hashable, state: SessionState) -> None:
        """Store the session's state and restart its idle timeout."""
        now = self._time()
        with self._lock:
            self._entries[key] = (state, now)
            self._entries.move_to_end(key)
            while self._entries:
                _, (_, oldest_at) = next(iter(self._entries.items()))
                if now - oldest_at <= self._idle_timeout:
                    break
                self._entries.popitem(last=False)
            while len(self._entries) > self._max_sessions:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        """Return the number of sessions held, expired ones not yet dropped included."""
        with self._lock:
            return len(self._entries)
