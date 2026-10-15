"""Choosing the model for one request to a tier."""

from __future__ import annotations

from typing import TYPE_CHECKING

from routing.model_selection.constraints import split_by_constraints
from routing.model_selection.session import continue_session
from routing.model_selection.types import Decision

if TYPE_CHECKING:
    from collections.abc import Mapping

    from routing.model_selection.config import AutoProfile
    from routing.model_selection.types import CandidateInfo, RequestFeatures, SessionState

__all__ = ["select"]


def select(
    profile: AutoProfile,
    features: RequestFeatures,
    catalog: Mapping[str, CandidateInfo],
    state: SessionState | None,
) -> Decision:
    """Choose the candidates to serve a request, best first.

    Leaves out the candidates that cannot serve it, ranks the rest by the tier's
    scorer and objective, and applies the session rules
    (:mod:`routing.model_selection.session`).

    Selection has no side effects. It reserves nothing and contacts no endpoint,
    so no endpoint gains health evidence from it; the stored session state is
    the caller's to update, from ``Decision.next_state``.

    Args:
        profile: The tier the request asked for.
        features: The request.
        catalog: The models the caller may use and that have an endpoint
            admitted, by the names candidates use. A candidate whose model is
            missing is unavailable.
        state: The session's stored state, if the request belongs to one.
    """
    able, dropped = split_by_constraints(profile.candidates, catalog, features)
    estimates = profile.scorer.score(features, able)
    ranked = profile.objective.rank(able, estimates)
    order, action, next_state = continue_session(ranked, state, features.turn, profile.version)
    return Decision(
        profile=profile.name,
        ranked=order,
        dropped=tuple(dropped),
        session_action=action,
        next_state=next_state,
        profile_version=profile.version,
        scorer_version=profile.scorer.version,
    )
