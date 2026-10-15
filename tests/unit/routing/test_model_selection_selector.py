"""Choosing the model for a request to a tier (``routing.model_selection.select``).

These run a tier from configuration through to a decision, and follow one
session across requests the way the serving layer will: read the stored state,
select, store ``next_state``.
"""

from __future__ import annotations

from routing.model_selection import (
    Candidate,
    CandidateInfo,
    DropReason,
    RequestFeatures,
    SessionAction,
    SessionStore,
    TurnKind,
    parse_auto_config,
    select,
)

CONFIG = parse_auto_config(
    {
        "profiles": {
            "balance": {
                "candidates": [
                    {"model": "mid", "reasoning_effort": "high"},
                    {"model": "frontier"},
                    {"model": "small"},
                ]
            }
        }
    }
)
BALANCE = CONFIG.resolve("auto-balance")
MID = Candidate("mid", "high")
FRONTIER = Candidate("frontier")
SMALL = Candidate("small")
CATALOG = {
    "mid": CandidateInfo(
        64_000, 16_000, supports_tools=True, reasoning_efforts=frozenset({"low", "high"})
    ),
    "frontier": CandidateInfo(200_000, 32_000, frozenset({"text", "image"}), supports_tools=True),
    "small": CandidateInfo(32_000, 8_000, supports_tools=True),
}


def _features(turn: TurnKind = TurnKind.NEW_TURN, **overrides) -> RequestFeatures:
    return RequestFeatures(
        **{
            "prompt_tokens": 2_000,
            "turn": turn,
            "max_tokens": 8_000,
            "uses_tools": True,
            **overrides,
        }
    )


def _without(*model_ids: str) -> dict[str, CandidateInfo]:
    return {model_id: info for model_id, info in CATALOG.items() if model_id not in model_ids}


def test_a_tier_ranks_its_candidates_in_the_order_listed():
    decision = select(BALANCE, _features(), CATALOG, None)

    assert decision.chosen == MID
    assert decision.ranked == (MID, FRONTIER, SMALL)
    assert decision.dropped == ()
    assert decision.session_action is SessionAction.NEW
    assert decision.profile == "balance"
    assert decision.profile_version == BALANCE.version
    assert decision.scorer_version == "static-1"


def test_candidates_that_cannot_serve_are_dropped_with_their_reasons():
    """An image prompt of 40k tokens: only the frontier model takes images, and fits."""
    features = _features(prompt_tokens=40_000, input_modalities=frozenset({"text", "image"}))

    decision = select(BALANCE, features, CATALOG, None)

    assert decision.ranked == (FRONTIER,)
    assert decision.dropped == ((MID, DropReason.MODALITY), (SMALL, DropReason.MODALITY))


def test_a_first_choice_out_of_the_catalog_falls_to_the_next():
    """Circuit open or closed to the caller: the serving layer leaves it out."""
    decision = select(BALANCE, _features(), _without("mid"), None)

    assert decision.chosen == FRONTIER
    assert decision.dropped == ((MID, DropReason.UNAVAILABLE),)


def test_no_candidate_left_leaves_the_session_alone():
    decision = select(BALANCE, _features(), {}, None)

    assert decision.chosen is None
    assert decision.next_state is None
    assert [reason for _, reason in decision.dropped] == [DropReason.UNAVAILABLE] * 3


def test_the_same_request_gets_the_same_decision():
    assert select(BALANCE, _features(), CATALOG, None) == select(
        BALANCE, _features(), CATALOG, None
    )


def test_a_session_across_an_outage_and_a_growing_context():
    """One agent session, request by request, as the serving layer will drive it."""
    store = SessionStore(idle_timeout_seconds=1800, max_sessions=100)
    key = ("caller", "session")

    def request(features: RequestFeatures, catalog: dict[str, CandidateInfo]):
        decision = select(BALANCE, features, catalog, store.get(key))
        if decision.next_state is not None:
            store.put(key, decision.next_state)
        return decision.chosen, decision.session_action

    # The session opens on its first choice and works through a tool loop.
    assert request(_features(), CATALOG) == (MID, SessionAction.NEW)
    assert request(_features(TurnKind.TOOL_CONTINUATION), CATALOG) == (MID, SessionAction.KEEP)

    # The first choice's circuit opens mid-loop: the loop moves on, and stays moved
    # after the first choice recovers.
    assert request(_features(TurnKind.TOOL_CONTINUATION), _without("mid")) == (
        FRONTIER,
        SessionAction.FORCED_SWITCH,
    )
    assert request(_features(TurnKind.TOOL_CONTINUATION), CATALOG) == (FRONTIER, SessionAction.KEEP)

    # A permission check in the loop is answered by the session's model, and
    # changes nothing.
    assert request(_features(TurnKind.SIDE_CALL), CATALOG) == (FRONTIER, SessionAction.SIDE_CALL)

    # The user's next message is the boundary where the session goes back.
    assert request(_features(), CATALOG) == (MID, SessionAction.TURN_SWITCH)

    # The conversation outgrows the first choice's window, mid-loop or not.
    long = _features(TurnKind.TOOL_CONTINUATION, prompt_tokens=60_000)
    assert request(long, CATALOG) == (FRONTIER, SessionAction.FORCED_SWITCH)

    state = store.get(key)
    assert state is not None
    assert (state.candidate, state.turns, state.switches) == (FRONTIER, 2, 3)


def test_a_session_on_another_tier_starts_over_on_this_one():
    fast = parse_auto_config(
        {
            "default_profile": "fast",
            "profiles": {"fast": {"candidates": [{"model": "small"}, {"model": "frontier"}]}},
        }
    ).resolve("auto-fast")
    on_fast = select(fast, _features(), CATALOG, None).next_state

    decision = select(BALANCE, _features(TurnKind.TOOL_CONTINUATION), CATALOG, on_fast)

    assert decision.chosen == MID
    assert decision.session_action is SessionAction.NEW
