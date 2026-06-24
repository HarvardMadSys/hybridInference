"""Ordered-fallback embedding adapter.

Embedding models do not go through the weighted ``RouteExecutor`` that chat
models use — the ``/v1/embeddings`` endpoint dispatches to a single adapter
object looked up by model id. ``FallbackEmbeddingAdapter`` lets an embedding
model declare more than one route (e.g. a primary local sglang endpoint plus a
``staging`` canary) by wrapping the route adapters in order and trying them in
turn: the first backend that succeeds serves the request, and a failure falls
through to the next.

It mimics the embedding-relevant surface of a real adapter so existing consumers
keep working unchanged: ``config`` exposes the *primary* route's ``ModelConfig``
(used for the model catalog, pricing, and provider metadata), and
``serving_config`` exposes the ``ModelConfig`` of whichever backend actually
served the current request — the primary, or a fallback like the staging canary
when the primary is down — so the endpoint can attribute the log row and cost
increment to the real provider.

A single adapter object is reused across all concurrent requests for a model, so
the served route is tracked in a per-request ``ContextVar`` rather than on the
instance: an instance attribute would let overlapping requests clobber each
other's attribution, whereas a ``ContextVar`` is isolated per asyncio task (i.e.
per request).
"""

from __future__ import annotations

import contextvars
from typing import Any

from serving.utils.logging import get_logger

logger = get_logger(__name__)

# Per-request record of which backend served the current embedding call. Set
# inside ``embeddings()`` on success and read back through the ``serving_config``
# property. Defaults to ``None`` (no fallback adapter ran in this context, or
# every backend failed), in which case ``serving_config`` falls back to the
# primary route's config.
_serving_config: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "embedding_serving_config", default=None
)


class FallbackEmbeddingAdapter:
    """Try an ordered list of embedding adapters, falling back on failure.

    Args:
        adapters: Route adapters in priority order (primary first). Must be
            non-empty; the embeddings loader only wraps when there are 2+.
    """

    def __init__(self, adapters: list[Any]) -> None:
        if not adapters:
            raise ValueError("FallbackEmbeddingAdapter requires at least one adapter")
        self._adapters = list(adapters)
        # Catalog/metadata reflect the primary route.
        self.config = self._adapters[0].config

    @property
    def serving_config(self) -> Any:
        """``ModelConfig`` of the backend that served the current request.

        Reads the per-request ``ContextVar`` so concurrent requests through this
        shared adapter never see each other's attribution. Falls back to the
        primary route's config when no served backend was recorded for this
        context (e.g. read outside a request, or every backend failed).
        """
        return _serving_config.get() or self.config

    async def embeddings(self, input_data: str | list[str], **params: Any) -> dict[str, Any]:
        """Return embeddings from the first backend that succeeds.

        Tries each adapter in order; on failure logs and falls through to the
        next. If every backend fails, re-raises the last exception so the
        endpoint's existing error handling (status mapping, logging) is
        preserved. ``BaseException`` (e.g. cancellation) is intentionally not
        caught.
        """
        # Reset up front so a context that somehow outlives a prior call (or an
        # all-fail path) reports the primary rather than a stale served config.
        _serving_config.set(None)
        last_exc: Exception | None = None
        total = len(self._adapters)
        for index, adapter in enumerate(self._adapters):
            try:
                response = await adapter.embeddings(input_data, **params)
            except Exception as exc:
                # Any failure (HTTP error, connection error, malformed call)
                # falls through to the next backend; BaseException such as
                # cancellation is intentionally not caught.
                last_exc = exc
                endpoint = getattr(getattr(adapter, "config", None), "endpoint_id", "unknown")
                if index + 1 < total:
                    logger.warning(
                        "Embedding backend %s failed (%s); falling back to next route",
                        endpoint,
                        exc,
                    )
                else:
                    logger.error(
                        "Embedding backend %s failed (%s); no fallback routes left",
                        endpoint,
                        exc,
                    )
                continue
            _serving_config.set(getattr(adapter, "config", self.config))
            return response
        # The loop always runs at least once (non-empty adapters), so a failure
        # path here guarantees last_exc is set.
        assert last_exc is not None
        raise last_exc
