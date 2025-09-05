from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from serving.adapters.base import BaseAdapter


@dataclass
class RouteConfig:
    """Weighted adapter list for a model."""

    adapters: list[tuple[BaseAdapter, float]]


class RouteExecutor:
    """Weighted routing executor with automatic fallback.

    Responsibilities:
    - Maintain a mapping from model_id -> weighted adapter list
    - Select an adapter by weight for each request
    - On failure, try remaining adapters in order
    """

    def __init__(self) -> None:
        self.routes: dict[str, RouteConfig] = {}

    def register_route(
        self, model_id: str, adapters_with_weights: list[tuple[BaseAdapter, float]]
    ) -> None:
        """Register a weighted route for a model.

        Args:
            model_id: Model identifier.
            adapters_with_weights: List of (adapter, weight) tuples.
                Weights will be normalized to sum to 1.0.
        """
        total_weight = sum(weight for _, weight in adapters_with_weights)
        if total_weight <= 0:
            return
        normalized = [(adapter, weight / total_weight) for adapter, weight in adapters_with_weights]
        self.routes[model_id] = RouteConfig(adapters=normalized)

    def _select_adapter(self, model_id: str) -> BaseAdapter | None:
        """Select an adapter using weighted random selection.

        Args:
            model_id: Model identifier.

        Returns:
            Selected adapter or None if no route configured.
        """
        route = self.routes.get(model_id)
        if not route or not route.adapters:
            return None
        rand = random.random()
        cumulative = 0.0
        for adapter, weight in route.adapters:
            cumulative += weight
            if rand <= cumulative:
                return adapter
        return route.adapters[-1][0]

    async def chat_completion(
        self, model_id: str, messages: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """Execute chat completion with automatic fallback.

        Args:
            model_id: Model identifier.
            messages: Chat messages in OpenAI format.
            **params: Additional parameters for the adapter.

        Returns:
            Chat completion response with routing metadata.

        Raises:
            ValueError: If no route configured for model.
        """
        primary = self._select_adapter(model_id)
        if not primary:
            raise ValueError(f"No route configured for model {model_id}")
        try:
            resp = await primary.chat_completion(messages, **params)
            resp["_routing"] = {
                "provider": primary.config.provider,
                "base_url": primary.config.base_url,
            }
            return resp
        except Exception as primary_error:
            route = self.routes[model_id]
            for adapter, _ in route.adapters:
                if adapter == primary:
                    continue
                try:
                    resp = await adapter.chat_completion(messages, **params)
                    resp["_routing"] = {
                        "provider": adapter.config.provider,
                        "base_url": adapter.config.base_url,
                        "fallback": True,
                    }
                    return resp
                except Exception:
                    continue
            raise primary_error

    async def stream_chat_completion(
        self, model_id: str, messages: list[dict[str, Any]], **params: Any
    ) -> AsyncIterator[Any]:
        """Stream chat completion with automatic fallback.

        Args:
            model_id: Model identifier.
            messages: Chat messages in OpenAI format.
            **params: Additional parameters for the adapter.

        Yields:
            SSE chunks from the adapter.

        Raises:
            ValueError: If no route configured for model.
        """
        primary = self._select_adapter(model_id)
        if not primary:
            raise ValueError(f"No route configured for model {model_id}")
        try:
            async for chunk in primary.stream_chat_completion(messages, **params):
                yield chunk
            return
        except Exception as primary_error:
            route = self.routes[model_id]
            for adapter, _ in route.adapters:
                if adapter == primary:
                    continue
                try:
                    async for chunk in adapter.stream_chat_completion(messages, **params):
                        yield chunk
                    return
                except Exception:
                    continue
            raise primary_error
