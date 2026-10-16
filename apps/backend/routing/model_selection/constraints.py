"""Hard limits: which of a tier's candidates can serve a request at all.

The serving layer has already left out of the catalog every model the caller may
not use and every model with no endpoint admitted (``model_access_denial()``,
``eligible_adapters()``). What is checked here is whether a model the caller can
reach is able to answer *this* request.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from routing.model_selection.types import DropReason

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from routing.model_selection.types import Candidate, CandidateInfo, RequestFeatures

__all__ = ["PROMPT_ESTIMATE_HEADROOM", "drop_reason", "split_by_constraints"]

#: How far the prompt estimate is trusted to fall short. It counts four UTF-8
#: bytes a token, which runs low on text that tokenizes densely, so the context
#: check sizes the prompt this much larger. A request that only just fits is sent
#: to a candidate with a wider window: that costs less than an upstream refusal,
#: which no other model would get the chance to answer.
PROMPT_ESTIMATE_HEADROOM: float = 1.25


def drop_reason(
    candidate: Candidate,
    info: CandidateInfo | None,
    features: RequestFeatures,
) -> DropReason | None:
    """Return why ``candidate`` cannot serve the request, or ``None`` if it can.

    Args:
        candidate: The candidate to check.
        info: Its model's catalog entry, or ``None`` when the catalog has none.
        features: The request.
    """
    if info is None:
        return DropReason.UNAVAILABLE
    if candidate.reasoning_effort is not None and (
        candidate.reasoning_effort not in info.reasoning_efforts
    ):
        return DropReason.EFFORT
    if features.uses_tools and not info.supports_tools:
        return DropReason.TOOLS
    if not features.input_modalities <= info.input_modalities:
        return DropReason.MODALITY
    # Adapters clamp max_tokens to the model's output limit, and the upstream
    # counts the clamped value against the window. Without max_tokens nothing is
    # reserved: the upstream then sizes the output to what is left.
    reserved_output = min(features.max_tokens, info.max_output_length) if features.max_tokens else 0
    prompt = math.ceil(features.prompt_tokens * PROMPT_ESTIMATE_HEADROOM)
    if prompt + reserved_output > info.context_length:
        return DropReason.CONTEXT
    return None


def split_by_constraints(
    candidates: Sequence[Candidate],
    catalog: Mapping[str, CandidateInfo],
    features: RequestFeatures,
) -> tuple[list[Candidate], list[tuple[Candidate, DropReason]]]:
    """Return the candidates able to serve, and the rest with their reasons.

    Both lists keep the order ``candidates`` came in.
    """
    able: list[Candidate] = []
    dropped: list[tuple[Candidate, DropReason]] = []
    for candidate in candidates:
        reason = drop_reason(candidate, catalog.get(candidate.model_id), features)
        if reason is None:
            able.append(candidate)
        else:
            dropped.append((candidate, reason))
    return able, dropped
