"""Integration layer between outsourcing engine and routing system.

This module connects the outsourcing decision engine with the serving layer,
allowing intelligent request routing between local SGLang and external APIs.

Key Features:
- TreeCache integration for prefix cache hit estimation
- SLO-aware outsourcing decisions
- Automatic routing between local and remote adapters
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from routing.tree_cache import TreeCache, create_tree_cache
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from routing.outsourcing.adapters import SGLangWaitingQueueAdapter
    from routing.outsourcing.decision import OutsourcingEngine
    from serving.adapters.base import BaseAdapter
    from serving.schemas import ChatMessage

logger = get_logger(__name__)

# Default max output tokens when not specified
DEFAULT_MAX_OUTPUT_TOKENS = 512


def extract_prompt_text(messages: list[dict[str, Any]]) -> str:
    """Extract prompt text from OpenAI-format messages.

    Concatenates all message contents into a single string for prefix matching.
    This is used by TreeCache to estimate KV cache hits.

    Args:
        messages: Chat messages in OpenAI format with 'role' and 'content' fields

    Returns:
        Concatenated prompt text
    """
    parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        # Include role for more accurate prefix matching
        # (different roles = different prompts even with same content)
        if role:
            parts.append(f"<|{role}|>")
        if content:
            parts.append(str(content))
    return "".join(parts)


def _get_max_output_tokens(params: dict[str, Any]) -> int:
    """Safely extract max_tokens from params, handling None values.

    Args:
        params: Request parameters dict

    Returns:
        Integer value for max output tokens
    """
    max_tokens = params.get("max_tokens")
    if max_tokens is None:
        return DEFAULT_MAX_OUTPUT_TOKENS
    return int(max_tokens)


class OutsourcingRouter:
    """Routing layer that integrates outsourcing decisions with prefix cache awareness.

    This router manages a single model's hybrid routing between local SGLang
    and external APIs based on SLO requirements and queue state.

    The decision is made based on:
    - Current queue state in SGLang
    - SLO requirements
    - FLOP calculations (adjusted for prefix cache hits)
    - Cost optimization

    TreeCache Integration:
        Each OutsourcingRouter maintains its own TreeCache instance that
        approximates the local SGLang's RadixCache. When a request arrives:
        1. Query TreeCache to estimate cached tokens (prefix cache hit)
        2. Adjust remaining_prompt_tokens for more accurate FLOP estimation
        3. If request stays local, update TreeCache with the prompt

    Note on Queue Management:
        The waiting_queue maintained here is a "shadow queue" that tracks requests
        for outsourcing decisions. It does NOT represent the actual SGLang queue.
        Requests are kept in the shadow queue until a routing decision is made,
        allowing the OutsourcingEngine to see the accumulated backlog and make
        informed decisions about which requests to outsource.
    """

    def __init__(
        self,
        local_adapter: BaseAdapter,
        remote_adapter: BaseAdapter,
        outsourcing_engine: OutsourcingEngine,
        waiting_queue: SGLangWaitingQueueAdapter,
        model_id: str,
        tree_cache: TreeCache | None = None,
        tree_cache_max_size_mb: float = 100.0,
        chars_per_token: float = 4.0,
    ):
        """Initialize the outsourcing router.

        Args:
            local_adapter: Adapter for local SGLang deployment
            remote_adapter: Adapter for remote API (e.g., Zhipu, OpenAI)
            outsourcing_engine: The outsourcing decision engine
            waiting_queue: The SGLang waiting queue adapter
            model_id: Model identifier (e.g., "glm-4.6")
            tree_cache: Optional pre-configured TreeCache instance.
                If None, a new TreeCache will be created.
            tree_cache_max_size_mb: Maximum cache size in MB (used if tree_cache is None)
            chars_per_token: Average characters per token for estimation
        """
        self.local_adapter = local_adapter
        self.remote_adapter = remote_adapter
        self.outsourcing_engine = outsourcing_engine
        self.waiting_queue = waiting_queue
        self.model_id = model_id

        # Initialize TreeCache for prefix cache hit estimation
        if tree_cache is not None:
            self.tree_cache = tree_cache
        else:
            self.tree_cache = create_tree_cache(
                max_size_mb=tree_cache_max_size_mb,
                chars_per_token=chars_per_token,
            )

        # Track prompt texts for requests (needed for TreeCache updates)
        self._request_prompts: dict[str, str] = {}

        # Statistics for monitoring
        self.stats = {
            "total_requests": 0,
            "local_requests": 0,
            "outsourced_requests": 0,
            "outsourcing_decisions": 0,
            "total_cached_tokens": 0,  # Sum of all prefix cache hits
            "cache_hit_requests": 0,  # Requests with any prefix cache hit
            "other_requests_outsourced": 0,  # Requests outsourced due to other requests' decisions
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

        # Extract prompt text for TreeCache
        prompt_text = extract_prompt_text(messages_dict)

        # Estimate prefix cache hit using TreeCache
        cached_tokens = self.tree_cache.estimate_cached_tokens(prompt_text, update_access_time=True)
        if cached_tokens > 0:
            self.stats["cache_hit_requests"] += 1
            self.stats["total_cached_tokens"] += cached_tokens

        # Estimate token counts for the request
        from serving.utils.tokens import estimate_prompt_tokens

        num_prompt_tokens = estimate_prompt_tokens(messages_dict)
        num_output_tokens = _get_max_output_tokens(params)

        # Generate request ID if not provided
        if request_id is None:
            request_id = f"req-{int(time.time() * 1000)}-{self.stats['total_requests']}"

        # Store prompt text for potential TreeCache update later
        self._request_prompts[request_id] = prompt_text

        # Add request to waiting queue for outsourcing consideration
        from routing.outsourcing import OutsourcingRequestInfo

        self.waiting_queue.add_request(
            OutsourcingRequestInfo(
                request_id=request_id,
                arrival_time=time.time(),
                num_prompt_tokens=num_prompt_tokens,
                num_output_tokens=num_output_tokens,
                num_cached_tokens=cached_tokens,  # Pass cached tokens for FLOP adjustment
                prefill_slo_seconds=prefill_slo_seconds,
            )
        )

        # Make outsourcing decision
        current_time = time.time()
        decision = self.outsourcing_engine.should_outsource(current_time)

        # Always apply outsourcing decision to handle ALL requests marked for outsourcing
        # This ensures requests other than the current one are also properly handled
        if decision.should_outsource:
            self.stats["outsourcing_decisions"] += 1

            # Apply the decision - this removes outsourced requests from the queue
            outsourced_requests = self.outsourcing_engine.apply_outsourcing(decision)

            # Count how many OTHER requests were outsourced (not the current one)
            other_outsourced = len([r for r in outsourced_requests if r.request_id != request_id])
            if other_outsourced > 0:
                self.stats["other_requests_outsourced"] += other_outsourced
                logger.info(
                    f"[Outsourcing] {other_outsourced} other request(s) also marked for outsourcing"
                )

            # CRITICAL: Remove kept requests from the waiting queue to prevent queue leak
            # The outsourced requests are already removed by apply_outsourcing()
            # But kept requests must also be removed - they will be processed locally
            if decision.requests_to_keep:
                self.waiting_queue.remove_requests(set(decision.requests_to_keep))

            # Update TreeCache for requests that will stay local
            for kept_id in decision.requests_to_keep:
                if kept_id in self._request_prompts:
                    self.tree_cache.insert(self._request_prompts[kept_id])
                    del self._request_prompts[kept_id]

            # Clean up prompt texts for outsourced requests (they won't update local cache)
            for req in outsourced_requests:
                self._request_prompts.pop(req.request_id, None)

        # Determine routing for THIS request
        if decision.should_outsource and request_id in decision.requests_to_outsource:
            # This request should be outsourced
            target_adapter = self.remote_adapter
            routing_decision = "outsourced"
            self.stats["outsourced_requests"] += 1

            logger.info(
                f"[Outsourcing] Request {request_id} outsourced to remote API. "
                f"Reason: {decision.reason}. "
                f"Queue metrics: {decision.metrics}"
            )
        else:
            # Keep local
            target_adapter = self.local_adapter
            routing_decision = "local"
            self.stats["local_requests"] += 1

            # If no outsourcing decision was made, we still need to:
            # 1. Remove this request from the queue (it will be processed locally)
            # 2. Update TreeCache
            if not decision.should_outsource:
                self.waiting_queue.remove_requests({request_id})
                self.tree_cache.insert(prompt_text)
                self._request_prompts.pop(request_id, None)

            logger.info(
                f"[Outsourcing] Request {request_id} kept local. "
                f"Queue length: {self.waiting_queue.get_length()}, "
                f"cached_tokens: {cached_tokens}"
            )

        # Execute the request through the selected adapter
        response = await target_adapter.chat_completion(messages_dict, **params)

        # Add routing metadata
        response["_outsourcing"] = {
            "decision": routing_decision,
            "request_id": request_id,
            "reason": decision.reason if decision.should_outsource else "no_slo_violations",
            "queue_length": self.waiting_queue.get_length(),
            "model_id": self.model_id,
            "cached_tokens": cached_tokens,
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

        # Extract prompt text for TreeCache
        prompt_text = extract_prompt_text(messages_dict)

        # Estimate prefix cache hit using TreeCache
        cached_tokens = self.tree_cache.estimate_cached_tokens(prompt_text, update_access_time=True)
        if cached_tokens > 0:
            self.stats["cache_hit_requests"] += 1
            self.stats["total_cached_tokens"] += cached_tokens

        # Estimate token counts
        from serving.utils.tokens import estimate_prompt_tokens

        num_prompt_tokens = estimate_prompt_tokens(messages_dict)
        num_output_tokens = _get_max_output_tokens(params)

        # Generate request ID
        if request_id is None:
            request_id = f"req-{int(time.time() * 1000)}-{self.stats['total_requests']}"

        # Store prompt text for potential TreeCache update later
        self._request_prompts[request_id] = prompt_text

        # Add to queue
        from routing.outsourcing import OutsourcingRequestInfo

        self.waiting_queue.add_request(
            OutsourcingRequestInfo(
                request_id=request_id,
                arrival_time=time.time(),
                num_prompt_tokens=num_prompt_tokens,
                num_output_tokens=num_output_tokens,
                num_cached_tokens=cached_tokens,  # Pass cached tokens
                prefill_slo_seconds=prefill_slo_seconds,
            )
        )

        # Make outsourcing decision
        current_time = time.time()
        decision = self.outsourcing_engine.should_outsource(current_time)

        # Always apply outsourcing decision to handle ALL requests marked for outsourcing
        if decision.should_outsource:
            self.stats["outsourcing_decisions"] += 1

            # Apply the decision
            outsourced_requests = self.outsourcing_engine.apply_outsourcing(decision)

            # Count other outsourced requests
            other_outsourced = len([r for r in outsourced_requests if r.request_id != request_id])
            if other_outsourced > 0:
                self.stats["other_requests_outsourced"] += other_outsourced

            # CRITICAL: Remove kept requests from the waiting queue to prevent queue leak
            if decision.requests_to_keep:
                self.waiting_queue.remove_requests(set(decision.requests_to_keep))

            # Update TreeCache for kept requests
            for kept_id in decision.requests_to_keep:
                if kept_id in self._request_prompts:
                    self.tree_cache.insert(self._request_prompts[kept_id])
                    del self._request_prompts[kept_id]

            # Clean up outsourced request prompts
            for req in outsourced_requests:
                self._request_prompts.pop(req.request_id, None)

        # Determine routing for THIS request
        if decision.should_outsource and request_id in decision.requests_to_outsource:
            target_adapter = self.remote_adapter
            self.stats["outsourced_requests"] += 1
            logger.info(f"[Outsourcing Stream] Request {request_id} outsourced to remote API")
        else:
            target_adapter = self.local_adapter
            self.stats["local_requests"] += 1

            # Handle case where no outsourcing decision was made
            if not decision.should_outsource:
                self.waiting_queue.remove_requests({request_id})
                self.tree_cache.insert(prompt_text)
                self._request_prompts.pop(request_id, None)

            logger.info(
                f"[Outsourcing Stream] Request {request_id} kept local, "
                f"cached_tokens: {cached_tokens}"
            )

        # Stream from the selected adapter
        async for chunk in target_adapter.stream_chat_completion(messages_dict, **params):
            yield chunk

    def get_stats(self) -> dict[str, Any]:
        """Get current routing statistics.

        Returns:
            Dictionary with routing metrics including TreeCache stats
        """
        queue_metrics = self.waiting_queue.get_metrics(safe=True)
        tree_cache_stats = self.tree_cache.get_stats()

        return {
            **self.stats,
            "queue_length": self.waiting_queue.get_length(),
            "sglang_metrics": queue_metrics,
            "outsourcing_rate": (
                self.stats["outsourced_requests"] / self.stats["total_requests"]
                if self.stats["total_requests"] > 0
                else 0.0
            ),
            "cache_hit_rate": (
                self.stats["cache_hit_requests"] / self.stats["total_requests"]
                if self.stats["total_requests"] > 0
                else 0.0
            ),
            "avg_cached_tokens": (
                self.stats["total_cached_tokens"] / self.stats["total_requests"]
                if self.stats["total_requests"] > 0
                else 0.0
            ),
            "tree_cache": tree_cache_stats,
            "pending_prompts": len(self._request_prompts),
        }

    def reset_stats(self) -> None:
        """Reset routing statistics."""
        self.stats = {
            "total_requests": 0,
            "local_requests": 0,
            "outsourced_requests": 0,
            "outsourcing_decisions": 0,
            "total_cached_tokens": 0,
            "cache_hit_requests": 0,
            "other_requests_outsourced": 0,
        }

    def clear_tree_cache(self) -> None:
        """Clear the TreeCache.

        This can be useful when the local SGLang's KV cache is cleared
        (e.g., after restart) and we need to reset our approximation.
        """
        self.tree_cache.clear()
        self._request_prompts.clear()
        logger.info(f"[{self.model_id}] TreeCache cleared")
