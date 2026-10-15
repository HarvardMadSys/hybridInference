"""Which of a tier's candidates can serve a request (``routing.model_selection.constraints``)."""

from __future__ import annotations

import pytest

from routing.model_selection import (
    PROMPT_ESTIMATE_HEADROOM,
    Candidate,
    CandidateInfo,
    DropReason,
    RequestFeatures,
    TurnKind,
    drop_reason,
)
from routing.model_selection.constraints import split_by_constraints

WINDOW = 10_000
INFO = CandidateInfo(
    context_length=WINDOW,
    max_output_length=2_000,
    input_modalities=frozenset({"text", "image"}),
    supports_tools=True,
    reasoning_efforts=frozenset({"low", "high"}),
)


def _features(**overrides) -> RequestFeatures:
    return RequestFeatures(**{"prompt_tokens": 100, "turn": TurnKind.NEW_TURN, **overrides})


def test_a_model_that_can_serve_is_kept():
    assert drop_reason(Candidate("m", "high"), INFO, _features(uses_tools=True)) is None


@pytest.mark.parametrize(
    ("candidate", "info", "features", "reason"),
    [
        pytest.param(
            Candidate("m"), None, _features(), DropReason.UNAVAILABLE, id="not-in-catalog"
        ),
        pytest.param(Candidate("m", "max"), INFO, _features(), DropReason.EFFORT, id="effort"),
        pytest.param(
            Candidate("m", "low"),
            CandidateInfo(WINDOW, 2_000),
            _features(),
            DropReason.EFFORT,
            id="effort-on-a-model-without-the-knob",
        ),
        pytest.param(
            Candidate("m"),
            CandidateInfo(WINDOW, 2_000, supports_tools=False),
            _features(uses_tools=True),
            DropReason.TOOLS,
            id="tools",
        ),
        pytest.param(
            Candidate("m"),
            INFO,
            _features(input_modalities=frozenset({"text", "audio"})),
            DropReason.MODALITY,
            id="modality",
        ),
        pytest.param(
            Candidate("m"), INFO, _features(prompt_tokens=WINDOW), DropReason.CONTEXT, id="context"
        ),
    ],
)
def test_each_reason_is_reported(candidate, info, features, reason):
    assert drop_reason(candidate, info, features) is reason


def test_a_candidate_without_an_effort_needs_no_effort_support():
    assert drop_reason(Candidate("m"), CandidateInfo(WINDOW, 2_000), _features()) is None


def test_the_prompt_estimate_is_given_headroom():
    """An estimate that only just fits is treated as the underestimate it may be."""
    fits = int(WINDOW / PROMPT_ESTIMATE_HEADROOM)

    assert drop_reason(Candidate("m"), INFO, _features(prompt_tokens=fits)) is None
    assert (
        drop_reason(Candidate("m"), INFO, _features(prompt_tokens=fits + 1)) is DropReason.CONTEXT
    )


def test_max_tokens_reserves_room_up_to_the_models_output_limit():
    """Adapters clamp max_tokens to the output limit, so a larger one rules nothing out."""
    prompt = int((WINDOW - 2_000) / PROMPT_ESTIMATE_HEADROOM)

    assert (
        drop_reason(Candidate("m"), INFO, _features(prompt_tokens=prompt, max_tokens=64_000))
        is None
    )
    assert (
        drop_reason(Candidate("m"), INFO, _features(prompt_tokens=prompt + 1, max_tokens=64_000))
        is DropReason.CONTEXT
    )
    assert drop_reason(Candidate("m"), INFO, _features(prompt_tokens=prompt + 1)) is None


def test_split_keeps_order_and_reasons():
    catalog = {"small": CandidateInfo(4_000, 1_000), "large": CandidateInfo(100_000, 8_000)}
    candidates = [Candidate("small"), Candidate("gone"), Candidate("large")]

    able, dropped = split_by_constraints(candidates, catalog, _features(prompt_tokens=5_000))

    assert able == [Candidate("large")]
    assert dropped == [
        (Candidate("small"), DropReason.CONTEXT),
        (Candidate("gone"), DropReason.UNAVAILABLE),
    ]
