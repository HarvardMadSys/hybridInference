"""Integration layer between outsourcing engine and routing system.

This module connects the outsourcing decision engine with the serving layer,
allowing intelligent request routing between local SGLang and external APIs.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from routing.outsourcing.adapters import SGLangWaitingQueueAdapter
    from routing.outsourcing.decision import OutsourcingEngine
    from serving.adapters.base import BaseAdapter
    from serving.schemas import ChatMessage

logger = get_logger(__name__)


class OutsourcingRouter:
    """Routing layer that integrates outsourcing decisions.

    This router manages a single model's hybrid routing between local SGLang
    and external APIs based on SLO requirements and queue state.

    The decision is made based on:
    - Current queue state in SGLang
    - SLO requirements
    - FLOP calculations
    - Cost optimization
    """

    def __init__(
        self,
        local_adapter: BaseAdapter,
        remote_adapter: BaseAdapter,
        outsourcing_engine: OutsourcingEngine,
        waiting_queue: SGLangWaitingQueueAdapter,
        model_id: str,
    ):
        """Initialize the outsourcing router.

        Args:
            local_adapter: Adapter for local SGLang deployment
            remote_adapter: Adapter for remote API (e.g., Zhipu, OpenAI)
            outsourcing_engine: The outsourcing decision engine
            waiting_queue: The SGLang waiting queue adapter
            model_id: Model identifier (e.g., "glm-4.6")
        """
        self.local_adapter = local_adapter
        self.remote_adapter = remote_adapter
        self.outsourcing_engine = outsourcing_engine
        self.waiting_queue = waiting_queue
        self.model_id = model_id

        # Statistics for monitoring
        self.stats = {
            "total_requests": 0,
            "local_requests": 0,
            "outsourced_requests": 0,
            "outsourcing_decisions": 0,
        }

    async def chat_completion(
        self,
        messages: list[ChatMessage] | list[dict[str, Any]],
        request_id: str | None = None,
        prefill_slo_seconds: float | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Route a chat completion request with outsourcing logic.

        Args:
            messages: Chat messages in OpenAI format
            request_id: Optional request ID for tracking
            prefill_slo_seconds: Optional SLO requirement for time-to-first-token
            **params: Additional parameters (temperature, max_tokens, etc.)

        Returns:
            Chat completion response
        """
        self.stats["total_requests"] += 1

        # Convert messages to dict format if needed
        messages_dict = [
            msg.model_dump() if hasattr(msg, "model_dump") else msg for msg in messages
        ]

        # Estimate token counts for the request
        from serving.utils.tokens import estimate_prompt_tokens

        num_prompt_tokens = estimate_prompt_tokens(messages_dict)
        num_output_tokens = params.get("max_tokens", 512)

        # Generate request ID if not provided
        if request_id is None:
            request_id = f"req-{int(time.time() * 1000)}-{self.stats['total_requests']}"

        # Add request to waiting queue for outsourcing consideration
        from routing.outsourcing import OutsourcingRequestInfo

        self.waiting_queue.add_request(
            OutsourcingRequestInfo(
                request_id=request_id,
                arrival_time=time.time(),
                num_prompt_tokens=num_prompt_tokens,
                num_output_tokens=num_output_tokens,
                prefill_slo_seconds=prefill_slo_seconds,
            )
        )

        # Make outsourcing decision
        current_time = time.time()
        decision = self.outsourcing_engine.should_outsource(current_time)

        routing_decision = "local"
        target_adapter = self.local_adapter

        if decision.should_outsource and request_id in decision.requests_to_outsource:
            # This request should be outsourced
            target_adapter = self.remote_adapter
            routing_decision = "outsourced"
            self.stats["outsourced_requests"] += 1

            # Remove from waiting queue
            self.outsourcing_engine.apply_outsourcing(decision)

            logger.info(
                f"[Outsourcing] Request {request_id} outsourced to remote API. "
                f"Reason: {decision.reason}. "
                f"Queue metrics: {decision.metrics}"
            )
        else:
            # Keep local
            self.stats["local_requests"] += 1

            # Remove from our tracking queue (SGLang will handle it)
            self.waiting_queue.remove_requests({request_id})

            logger.info(
                f"[Outsourcing] Request {request_id} kept local. "
                f"Queue length: {self.waiting_queue.get_length()}"
            )

        if decision.should_outsource:
            self.stats["outsourcing_decisions"] += 1

        # Execute the request through the selected adapter
        response = await target_adapter.chat_completion(messages_dict, **params)

        # Add routing metadata
        response["_outsourcing"] = {
            "decision": routing_decision,
            "request_id": request_id,
            "reason": decision.reason if decision.should_outsource else "no_slo_violations",
            "queue_length": self.waiting_queue.get_length(),
            "model_id": self.model_id,
        }

        return response

    async def stream_chat_completion(
        self,
        messages: list[ChatMessage] | list[dict[str, Any]],
        request_id: str | None = None,
        prefill_slo_seconds: float | None = None,
        **params: Any,
    ):
        """Route a streaming chat completion request with outsourcing logic.

        Args:
            messages: Chat messages in OpenAI format
            request_id: Optional request ID for tracking
            prefill_slo_seconds: Optional SLO requirement for time-to-first-token
            **params: Additional parameters

        Yields:
            SSE chunks from the adapter
        """
        self.stats["total_requests"] += 1

        # Convert messages to dict format if needed
        messages_dict = [
            msg.model_dump() if hasattr(msg, "model_dump") else msg for msg in messages
        ]

        # Estimate token counts
        from serving.utils.tokens import estimate_prompt_tokens

        num_prompt_tokens = estimate_prompt_tokens(messages_dict)
        num_output_tokens = params.get("max_tokens", 512)

        # Generate request ID
        if request_id is None:
            request_id = f"req-{int(time.time() * 1000)}-{self.stats['total_requests']}"

        # Add to queue
        from routing.outsourcing import OutsourcingRequestInfo

        self.waiting_queue.add_request(
            OutsourcingRequestInfo(
                request_id=request_id,
                arrival_time=time.time(),
                num_prompt_tokens=num_prompt_tokens,
                num_output_tokens=num_output_tokens,
                prefill_slo_seconds=prefill_slo_seconds,
            )
        )

        # Make outsourcing decision
        current_time = time.time()
        decision = self.outsourcing_engine.should_outsource(current_time)

        if decision.should_outsource and request_id in decision.requests_to_outsource:
            target_adapter = self.remote_adapter
            self.stats["outsourced_requests"] += 1
            self.outsourcing_engine.apply_outsourcing(decision)
            logger.info(f"[Outsourcing Stream] Request {request_id} outsourced to remote API")
        else:
            target_adapter = self.local_adapter
            self.stats["local_requests"] += 1
            self.waiting_queue.remove_requests({request_id})
            logger.info(f"[Outsourcing Stream] Request {request_id} kept local")

        if decision.should_outsource:
            self.stats["outsourcing_decisions"] += 1

        # Stream from the selected adapter
        async for chunk in target_adapter.stream_chat_completion(messages_dict, **params):
            yield chunk

    def get_stats(self) -> dict[str, Any]:
        """Get current routing statistics.

        Returns:
            Dictionary with routing metrics
        """
        queue_metrics = self.waiting_queue.get_metrics(safe=True)

        return {
            **self.stats,
            "queue_length": self.waiting_queue.get_length(),
            "sglang_metrics": queue_metrics,
            "outsourcing_rate": (
                self.stats["outsourced_requests"] / self.stats["total_requests"]
                if self.stats["total_requests"] > 0
                else 0.0
            ),
        }

    def reset_stats(self) -> None:
        """Reset routing statistics."""
        self.stats = {
            "total_requests": 0,
            "local_requests": 0,
            "outsourced_requests": 0,
            "outsourcing_decisions": 0,
        }
