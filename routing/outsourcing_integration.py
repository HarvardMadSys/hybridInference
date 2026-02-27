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

from routing.outsourcing.decision import OutsourcingDecision
from routing.tree_cache import TreeCache, create_tree_cache
from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from routing.outsourcing.adapters import SGLangWaitingQueueAdapter
    from routing.outsourcing.decision import OutsourcingEngine
    from routing.outsourcing.request import OutsourcingRequestInfo
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
        # Track full payloads so we can re-dispatch requests if needed
        self._request_payloads: dict[str, dict[str, Any]] = {}
        # Cache the latest SGLang metrics to avoid duplicate fetches where possible
        self._last_sglang_metrics: dict[str, Any] = {}

        if hasattr(self.waiting_queue, "set_request_update_hook"):
            self.waiting_queue.set_request_update_hook(self._refresh_request_snapshot)

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

    def _refresh_request_snapshot(self, request: OutsourcingRequestInfo) -> None:
        """Refresh cached-token estimates before the engine reads the queue."""
        prompt_text = self._request_prompts.get(request.request_id)
        if not prompt_text:
            prompt_text = request.metadata.get("prompt_text") if request.metadata else None
        if not prompt_text:
            return

        cached_tokens = self.tree_cache.estimate_cached_tokens(
            prompt_text, update_access_time=False
        )
        # Clamp cached tokens to avoid overstating remaining work
        request.num_cached_tokens = max(0, min(cached_tokens, request.num_prompt_tokens))

    def _assess_sglang_capacity(self) -> tuple[bool, float | None]:
        """Check SGLang queue depth and decide if we should force local dispatch.

        Short-circuits to force-local only when BOTH conditions hold:
        1. SGLang internal queue is idle (request_queue < 1)
        2. Shadow queue has at most 1 request (the current one just added)

        If the shadow queue has accumulated requests (i.e. other requests are
        still in-flight locally), we must let the outsourcing engine evaluate
        even when SGLang's internal queue appears empty.
        """
        metrics = self.waiting_queue.get_metrics(safe=True)
        if metrics:
            self._last_sglang_metrics = metrics

        queue_raw = (metrics or {}).get("request_queue")
        try:
            queue_depth = float(queue_raw)
        except (TypeError, ValueError):
            queue_depth = None

        sglang_idle = queue_depth is not None and queue_depth < 1
        shadow_queue_small = self.waiting_queue.get_length() <= 1

        if sglang_idle and shadow_queue_small:
            return True, queue_depth
        return False, queue_depth

    def _make_forced_local_decision(self, queue_depth: float | None) -> OutsourcingDecision:
        """Construct a placeholder decision when we skip outsourcing due to idle SGLang."""
        depth_str = "unknown" if queue_depth is None else f"{queue_depth:.2f}"
        return OutsourcingDecision(
            should_outsource=False,
            requests_to_outsource=[],
            requests_to_keep=[],
            reason=f"SGLang idle (request_queue={depth_str}), dispatching shadow request locally",
            metrics={
                "forced_local": True,
                "sglang_request_queue": queue_depth,
            },
        )

    def decide(
        self,
        messages: list[dict[str, Any]] | list[Any],
        request_id: str | None = None,
        prefill_slo_seconds: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Pure decision: determine adapter and routing metadata without executing.

        This extracts the decision logic from chat_completion() so that
        NimbusRouter (inheriting BaseRouter) can call it from _select_adapter()
        and attach the metadata later.

        Args:
            messages: Chat messages in OpenAI format
            request_id: Optional request ID for tracking
            prefill_slo_seconds: Optional TTFT SLO
            params: Request parameters (temperature, max_tokens, etc.)

        Returns:
            Dict with 'adapter', 'routing_decision', 'request_id', 'reason',
            'cached_tokens', 'model_id', 'queue_length', and 'decision' object.
        """
        if params is None:
            params = {}

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

        # Generate request ID if not provided
        if request_id is None:
            request_id = f"req-{int(time.time() * 1000)}-{self.stats['total_requests']}"

        # Store prompt text for potential TreeCache update later
        self._request_prompts[request_id] = prompt_text
        self._request_payloads[request_id] = {
            "messages": messages_dict,
            "params": dict(params),
            "prefill_slo_seconds": prefill_slo_seconds,
        }

        # Add request to waiting queue for outsourcing consideration
        from routing.outsourcing import OutsourcingRequestInfo

        self.waiting_queue.add_request(
            OutsourcingRequestInfo(
                request_id=request_id,
                arrival_time=time.time(),
                num_prompt_tokens=num_prompt_tokens,
                num_output_tokens=num_output_tokens,
                num_cached_tokens=cached_tokens,
                prefill_slo_seconds=prefill_slo_seconds,
                metadata={"prompt_text": prompt_text},
            )
        )

        # Make outsourcing decision.
        # _assess_sglang_capacity() does a single get_metrics() call; we pass the
        # cached result to should_outsource() so the engine skips its own HTTP fetch.
        current_time = time.time()
        force_local, queue_depth = self._assess_sglang_capacity()
        if force_local:
            decision = self._make_forced_local_decision(queue_depth)
            logger.info(
                "[Outsourcing] SGLang idle (queue_depth=%s); forcing local dispatch for %s",
                f"{queue_depth:.2f}" if queue_depth is not None else "unknown",
                request_id,
            )
        else:
            decision = self.outsourcing_engine.should_outsource(
                current_time,
                prefetched_metrics=self._last_sglang_metrics,
            )

        # Apply outsourcing decision
        if decision.should_outsource:
            self.stats["outsourcing_decisions"] += 1
            outsourced_requests = self.outsourcing_engine.apply_outsourcing(decision)

            other_outsourced = len([r for r in outsourced_requests if r.request_id != request_id])
            if other_outsourced > 0:
                self.stats["other_requests_outsourced"] += other_outsourced
                logger.info(
                    f"[Outsourcing] {other_outsourced} other request(s) also marked for outsourcing"
                )

            # NOTE: kept requests are NOT removed from shadow queue here.
            # They stay in the queue until execution completes (removed in
            # NimbusRouter._execute_adapter / _execute_stream_adapter finally).
            # This lets the FLOP algorithm see all in-flight local requests.
            for kept_id in decision.requests_to_keep:
                if kept_id in self._request_prompts:
                    self.tree_cache.insert(self._request_prompts[kept_id])
                    del self._request_prompts[kept_id]
                self._request_payloads.pop(kept_id, None)

            for req in outsourced_requests:
                self._request_prompts.pop(req.request_id, None)
                self._request_payloads.pop(req.request_id, None)

            outsourced_ids = {req.request_id for req in outsourced_requests}
            for req_id in decision.requests_to_outsource:
                if req_id not in outsourced_ids:
                    self._request_prompts.pop(req_id, None)
                    self._request_payloads.pop(req_id, None)

        # Determine routing for THIS request
        if decision.should_outsource and request_id in decision.requests_to_outsource:
            target_adapter = self.remote_adapter
            routing_decision = "outsourced"
            self.stats["outsourced_requests"] += 1
            logger.info(
                f"[Outsourcing] Request {request_id} outsourced to remote API. "
                f"Reason: {decision.reason}. Metrics: {decision.metrics}"
            )
        else:
            target_adapter = self.local_adapter
            routing_decision = "local"
            self.stats["local_requests"] += 1

            if not decision.should_outsource:
                # NOTE: do NOT remove from shadow queue here — the request is
                # about to execute locally. It will be removed when execution
                # completes (NimbusRouter._execute_adapter finally block).
                self.tree_cache.insert(prompt_text)
                self._request_prompts.pop(request_id, None)
                self._request_payloads.pop(request_id, None)

            logger.info(
                f"[Outsourcing] Request {request_id} kept local. "
                f"Queue length: {self.waiting_queue.get_length()}, "
                f"cached_tokens: {cached_tokens}"
            )

        return {
            "adapter": target_adapter,
            "routing_decision": routing_decision,
            "request_id": request_id,
            "reason": decision.reason,
            "cached_tokens": cached_tokens,
            "model_id": self.model_id,
            "queue_length": self.waiting_queue.get_length(),
            "decision": decision,
            # Observability fields from violation detection
            "est_ttft_seconds": decision.metrics.get("head_est_ttft") if decision.metrics else None,
            "sglang_pending_count": decision.metrics.get("sglang_pending_count", 0)
            if decision.metrics
            else 0,
            "observed_ttft": decision.metrics.get("observed_ttft") if decision.metrics else None,
            "trigger": decision.metrics.get("trigger", "none") if decision.metrics else "none",
        }

    async def chat_completion(
        self,
        messages: list[ChatMessage] | list[dict[str, Any]],
        request_id: str | None = None,
        prefill_slo_seconds: float | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Route a chat completion request with outsourcing logic.

        This is the standalone entry point. When used via NimbusRouter (BaseRouter),
        the decide() + adapter execution path is used instead.

        Args:
            messages: Chat messages in OpenAI format
            request_id: Optional request ID for tracking
            prefill_slo_seconds: Optional SLO requirement for time-to-first-token
            **params: Additional parameters (temperature, max_tokens, etc.)

        Returns:
            Chat completion response with _routing metadata
        """
        result = self.decide(messages, request_id, prefill_slo_seconds, params)
        target_adapter = result["adapter"]

        # Convert messages to dict format for adapter
        messages_dict = [
            msg.model_dump() if hasattr(msg, "model_dump") else msg for msg in messages
        ]

        # Execute the request through the selected adapter
        response = await target_adapter.chat_completion(messages_dict, **params)

        # Add routing metadata
        response["_routing"] = {
            "provider": target_adapter.config.provider,
            "base_url": target_adapter.config.base_url,
            "outsourcing": {
                "decision": result["routing_decision"],
                "request_id": result["request_id"],
                "reason": result["reason"],
                "queue_length": result["queue_length"],
                "model_id": result["model_id"],
                "cached_tokens": result["cached_tokens"],
            },
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

        This is the standalone entry point. When used via NimbusRouter (BaseRouter),
        the decide() + adapter execution path is used instead.

        Args:
            messages: Chat messages in OpenAI format
            request_id: Optional request ID for tracking
            prefill_slo_seconds: Optional SLO requirement for time-to-first-token
            **params: Additional parameters

        Yields:
            SSE chunks from the adapter
        """
        result = self.decide(messages, request_id, prefill_slo_seconds, params)
        target_adapter = result["adapter"]

        # Convert messages to dict format for adapter
        messages_dict = [
            msg.model_dump() if hasattr(msg, "model_dump") else msg for msg in messages
        ]

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
