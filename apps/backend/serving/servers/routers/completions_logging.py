"""``CompletionsLogger`` — DB-log scheduling and routing-observation forwarding.

Replaces the module-level helpers that previously lived in
``apps/backend/serving/servers/routers/completions.py``:

- ``_schedule_db_log_task`` — fire-and-forget async write to the log store.
- ``_record_routing_observation`` — emit a ``RoutingObservation`` for online
  learning routers (RouteWise).
- ``_build_db_params`` — fill in the default ``max_tokens`` from the adapter
  config when the client didn't specify one.

The DB log payload (kwargs to ``LogStore.log_request``) is preserved
byte-for-byte — admin/recent-requests UI, billing reports, and the Slack
alert rules from PR #372 all read these rows and must keep working.
"""

from __future__ import annotations

import asyncio
from typing import Any

from routing.routers import RoutingObservation
from serving.observability.tracked_tasks import tracked_task
from serving.servers.routers.routing_info import RoutingInfo
from serving.utils.logging import get_logger

logger = get_logger(__name__)


class CompletionsLogger:
    """Encapsulate fire-and-forget side effects from the chat-completions handler.

    Owns DB log writes and RouteWise observation forwarding. The instance keeps
    a private set of background tasks so the asyncio garbage collector cannot
    cancel them mid-flight.
    """

    def __init__(self, *, log_store: Any, model_router_registry: Any | None = None) -> None:
        """Create a logger.

        Args:
            log_store: ``LogStore`` instance (Postgres / D1 / dual-write).
                ``None``-tolerant: ``schedule_log`` no-ops when this is
                ``None``, so callers do not need to pre-gate.
            model_router_registry: Accepted for backward-compat with
                bootstrap; currently unused. ``build_db_params`` resolves
                adapter configs via the ``get_adapter_config_for_provider``
                closure passed in by the handler scope rather than via this
                registry.
        """
        self._log_store = log_store
        # NOTE: ``model_router_registry`` is intentionally not stored — the
        # field was unused and ``build_db_params`` takes the lookup as an
        # argument. Constructor still accepts the kwarg for API stability.
        del model_router_registry
        self._background_tasks: set[asyncio.Task[Any]] = set()

    # -- DB log scheduling ---------------------------------------------------

    def schedule_log(self, request_id: str, log_data: dict[str, Any]) -> None:
        """Schedule a fire-and-forget write of ``log_data`` to the log store.

        ``log_data`` is forwarded verbatim as kwargs to
        ``log_store.log_request(**log_data)``. The handler is responsible
        for assembling the dict; this method only owns the asyncio
        scheduling so the row write doesn't block the HTTP response.

        Failures are recorded by ``tracked_task`` via the
        ``tracked_task_completed`` log event consumed by the alert engine.
        """
        if self._log_store is None:
            return

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running event loop (e.g., synchronous context in tests).
            logger.debug(f"No event loop; dropping log payload for {request_id}")
            return

        async def _log_to_db_background() -> None:
            try:
                await self._log_store.log_request(**log_data)
                logger.debug(f"Background DB logging completed for request {request_id}")
            except Exception as exc:
                logger.error(
                    f"Background DB logging failed for request {request_id}: {exc}",
                    exc_info=True,
                )
                raise  # let tracked_task record the failure

        task = tracked_task(_log_to_db_background(), name="request_log")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # -- Routing observation forwarding -------------------------------------

    def record_routing_observation(
        self,
        active_router: Any,
        model_id: str,
        routing: RoutingInfo | dict[str, Any] | None,
        *,
        ttft_ms: float | None,
        total_latency_ms: float,
        prompt_tokens: int,
        completion_tokens: int,
        success: bool,
    ) -> None:
        """Emit a ``RoutingObservation`` for online-learning routers.

        Accepts either a ``RoutingInfo`` (preferred) or the legacy untyped
        dict (e.g., ``getattr(exc, "_routing", None)``) so the handler's
        exception path — which today receives a raw dict from the adapter —
        keeps working without further plumbing changes.
        """
        provider, endpoint_id, strategy_metadata = _extract_observation_keys(routing)
        obs = RoutingObservation(
            model_id=model_id,
            endpoint_id=endpoint_id or provider or "unknown",
            ttft_ms=ttft_ms,
            total_latency_ms=total_latency_ms,
            token_count=prompt_tokens + completion_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            success=success,
            strategy_metadata=strategy_metadata,
        )
        active_router.record_observation(obs)

    # -- DB params builder --------------------------------------------------

    def build_db_params(
        self,
        params: dict[str, Any],
        provider: str,
        base_url: str | None,
        get_adapter_config_for_provider: Any,
    ) -> dict[str, Any]:
        """Reconstruct request params for the DB log row.

        Fills the default ``max_tokens`` from the matching adapter config
        when the client didn't supply one. ``get_adapter_config_for_provider``
        is the existing closure from the handler scope (it knows the chosen
        ``model``) — passed in so this stays a pure helper.

        The function returns ``None`` for unregistered providers (e.g., the
        synthetic ``"router"`` placeholder); ``getattr(None, "x", default)``
        raises ``AttributeError`` rather than returning the default, so we
        guard explicitly before reading the attribute.
        """
        _params = dict(params)
        if _params.get("max_tokens") is None:
            config = get_adapter_config_for_provider(provider, base_url)
            if config is not None:
                _params["max_tokens"] = getattr(config, "max_output_length", None)
        return _params


def _extract_observation_keys(
    routing: RoutingInfo | dict[str, Any] | None,
) -> tuple[str | None, str | None, dict[str, Any]]:
    """Return ``(provider, endpoint_id, strategy_metadata)`` from either type.

    Mirrors the legacy lookup precedence the handler used:
    ``endpoint_id`` > ``base_url`` > ``provider`` for the observation key.
    """
    if routing is None:
        return None, None, {}
    if isinstance(routing, RoutingInfo):
        endpoint = routing.endpoint_id or routing.base_url
        return routing.provider, endpoint, dict(routing.strategy_metadata or {})
    # Legacy dict shape (used by exception._routing).
    endpoint = routing.get("endpoint_id") or routing.get("base_url")
    strategy_metadata: dict[str, Any] = {}
    raw_strategy_metadata = routing.get("strategy_metadata")
    if isinstance(raw_strategy_metadata, dict):
        strategy_metadata.update(raw_strategy_metadata)
    routewise = routing.get("routewise")
    if isinstance(routewise, dict):
        existing_routewise = strategy_metadata.get("routewise")
        merged_routewise = dict(existing_routewise) if isinstance(existing_routewise, dict) else {}
        merged_routewise.update(routewise)
        strategy_metadata["routewise"] = merged_routewise
    return (
        routing.get("provider"),
        endpoint,
        strategy_metadata,
    )
