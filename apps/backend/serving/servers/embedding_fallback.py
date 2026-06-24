"""Ordered-fallback embedding adapter.

Embedding models do not go through the weighted ``RouteExecutor`` that chat
models use — the ``/v1/embeddings`` endpoint dispatches to a single adapter
object looked up by model id. ``FallbackEmbeddingAdapter`` lets an embedding
model declare more than one route (e.g. a primary local sglang endpoint plus a
``staging`` canary) by wrapping the route adapters in order and trying them in
turn: the first backend that succeeds serves the request, and a failure falls
through to the next.

It mimics the embedding-relevant surface of a real adapter so existing consumers
keep working unchanged:

- ``config`` exposes the *primary* route's ``ModelConfig`` — used for the model
  catalog, pricing, and provider metadata.
- ``serving_config`` records the ``ModelConfig`` of whichever backend actually
  served the most recent successful request, so the endpoint can attribute the
  log row and cost increment to the real provider (the primary, or the canary
  when the primary is down).
"""

from __future__ import annotations

from typing import Any

from serving.utils.logging import get_logger

logger = get_logger(__name__)


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
        # Catalog/metadata reflect the primary route. ``serving_config`` tracks
        # the backend that actually served the latest request; it starts at the
        # primary so a read before the first call is still meaningful.
        self.config = self._adapters[0].config
        self.serving_config = self._adapters[0].config

    async def embeddings(self, input_data: str | list[str], **params: Any) -> dict[str, Any]:
        """Return embeddings from the first backend that succeeds.

        Tries each adapter in order; on failure logs and falls through to the
        next. If every backend fails, re-raises the last exception so the
        endpoint's existing error handling (status mapping, logging) is
        preserved. ``BaseException`` (e.g. cancellation) is intentionally not
        caught.
        """
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
            self.serving_config = getattr(adapter, "config", self.config)
            return response
        # The loop always runs at least once (non-empty adapters), so a failure
        # path here guarantees last_exc is set.
        assert last_exc is not None
        raise last_exc
