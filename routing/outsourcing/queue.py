"""Waiting queue abstraction for outsourcing."""

from abc import ABC, abstractmethod

from .request import OutsourcingRequestInfo


class WaitingQueueInterface(ABC):
    """Abstract interface for the waiting request queue.

    Implementations wrap the serving engine's actual queue.
    """

    @abstractmethod
    def add_request(self, request_info: OutsourcingRequestInfo) -> None:
        """Add a request to the waiting queue."""
        pass

    @abstractmethod
    def get_all_waiting(self) -> list[OutsourcingRequestInfo]:
        """Get snapshot of all waiting requests in queue order (FCFS).

        Returns a copy/snapshot to avoid iterator invalidation.
        """
        pass

    @abstractmethod
    def remove_requests(self, request_ids: set[str]) -> list[OutsourcingRequestInfo]:
        """Remove specified requests from the waiting queue.

        Returns the removed requests for logging/handoff.
        """
        pass

    @abstractmethod
    def get_length(self) -> int:
        """Current number of waiting requests."""
        pass

    @abstractmethod
    def peek(self) -> OutsourcingRequestInfo | None:
        """Look at the head of the queue without removing."""
        pass
