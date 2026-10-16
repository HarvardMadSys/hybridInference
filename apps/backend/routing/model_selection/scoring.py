"""Estimating candidates and ranking them by a tier's objective.

The two are kept apart, as RouteLLM keeps them. A :class:`Scorer` estimates
each candidate and chooses nothing; an :class:`Objective` turns the estimates
into an order. Every tier can share one scorer and differ only in its objective.

This release has one of each. :class:`StaticScorer` scores a candidate by its
place in the tier's list and :class:`MaxQuality` ranks by score, so a tier ranks
its candidates in the order its configuration lists them. Scores that vary by
request, and the objectives that trade quality against cost or latency, need
inputs the gateway does not have yet; they plug in behind these two interfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from routing.model_selection.types import Candidate, RequestFeatures

__all__ = ["Estimate", "MaxQuality", "Objective", "Scorer", "StaticScorer"]


@dataclass(frozen=True, slots=True)
class Estimate:
    """A scorer's estimate of one candidate on one request.

    Attributes:
        quality: Expected quality relative to the tier's other candidates, in
            ``(0, 1]``; higher is better.
    """

    quality: float


class Scorer(Protocol):
    """Estimates candidates; chooses nothing."""

    version: str

    def score(
        self, features: RequestFeatures, candidates: Sequence[Candidate]
    ) -> Mapping[Candidate, Estimate]:
        """Return an estimate for each of ``candidates``."""
        ...


class Objective(Protocol):
    """Orders candidates by their estimates. The part a tier chooses."""

    name: str

    def rank(
        self, candidates: Sequence[Candidate], estimates: Mapping[Candidate, Estimate]
    ) -> list[Candidate]:
        """Return ``candidates``, best first."""
        ...


class StaticScorer:
    """Scores a candidate by its place in the tier's list, first highest.

    A candidate's score depends only on the full list, never on which of its
    entries can serve the request, so it reads the same in every decision.
    """

    version = "static-1"

    def __init__(self, candidates: Sequence[Candidate]):
        """Score ``candidates`` from ``1.0`` for the first down toward ``0``."""
        count = len(candidates)
        self._quality = {
            candidate: (count - index) / count for index, candidate in enumerate(candidates)
        }

    def score(
        self, features: RequestFeatures, candidates: Sequence[Candidate]
    ) -> Mapping[Candidate, Estimate]:
        """Return each candidate's fixed estimate; ``features`` does not move it."""
        return {candidate: Estimate(self._quality[candidate]) for candidate in candidates}


class MaxQuality:
    """Ranks by estimated quality, keeping the given order between equals."""

    name = "max_quality"

    def rank(
        self, candidates: Sequence[Candidate], estimates: Mapping[Candidate, Estimate]
    ) -> list[Candidate]:
        """Return ``candidates`` from highest estimated quality to lowest."""
        return sorted(candidates, key=lambda candidate: -estimates[candidate].quality)
