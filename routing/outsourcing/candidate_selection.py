"""Candidate selection for outsourcing decisions.

Ported from vidur's outsourcing module.
"""

from typing import Set

from routing.outsourcing.request import OutsourcingRequestInfo


class CandidateSelector:
    """Select candidate requests for outsourcing consideration."""

    def __init__(self):
        """Initialize the candidate selector."""
        pass

    def collect_candidates(
        self,
        waiting_requests: list[OutsourcingRequestInfo],
        outsourced_req_ids: Set[str],
    ) -> list[OutsourcingRequestInfo]:
        """
        Collect requests eligible for outsourcing.

        Includes:
        - Waiting requests not yet outsourced

        Excludes:
        - Already outsourced requests

        Args:
            waiting_requests: List of waiting requests
            outsourced_req_ids: Set of already outsourced request IDs

        Returns:
            List of candidate requests for outsourcing
        """
        candidates = []

        # Get waiting requests that haven't been outsourced yet
        for r in waiting_requests:
            if r.request_id not in outsourced_req_ids:
                candidates.append(r)

        return candidates
