"""OpenRouter adapter: thin OpenAICompatAdapter subclass with OR-specific request shape."""

from __future__ import annotations

from typing import Any

from .openai_compat import OpenAICompatAdapter

# Attribution headers required for OpenRouter leaderboard / free-tier limits.
# Hardcoded — single deployment, no per-route override needed.
_HTTP_REFERER = "https://freeinference.org"
_X_TITLE = "FreeInference"


class OpenRouterAdapter(OpenAICompatAdapter):
    """OpenAI-compatible adapter for OpenRouter.

    Adds OpenRouter-specific request augmentation on top of the generic
    OpenAICompatAdapter:
    - Attribution headers (HTTP-Referer, X-Title).
    - ``usage: {include: true}`` on every request so OpenRouter returns the
      per-request ``cost`` field.
    - ``stream_options: {include_usage: true}`` on streaming requests so the
      final SSE chunk carries the usage block.
    - ``provider: {order: [<slug>], allow_fallbacks: false}`` when the route
      uses the bracket form ``kind: openrouter[<slug>]`` (config field
      ``openrouter_pinned_provider``).

    Threads ``upstream_cost_usd`` from the response usage block into the
    internal ``_routing`` metadata block:
    - non-stream: this class overrides ``_parse_completion_response`` to
      attach ``_routing`` (the parent does not attach one for non-stream
      responses).
    - stream: handled by the parent's extended ``_build_final_chunk``, which
      reads ``upstream_cost_usd`` off the ``UsageInfo`` we pass through.
    """

    def _build_headers(self, api_key_override: str | None = None) -> dict[str, str]:
        headers = super()._build_headers(api_key_override=api_key_override)
        headers["HTTP-Referer"] = _HTTP_REFERER
        headers["X-Title"] = _X_TITLE
        return headers

    def _augment_payload(self, payload: dict[str, Any], *, stream: bool) -> dict[str, Any]:
        payload["usage"] = {"include": True}
        pin = getattr(self.config, "openrouter_pinned_provider", None)
        if pin:
            payload["provider"] = {"order": [pin], "allow_fallbacks": False}
        if stream:
            existing = dict(payload.get("stream_options") or {})
            existing["include_usage"] = True
            payload["stream_options"] = existing
        return payload

    def _parse_completion_response(self, response: dict[str, Any]) -> dict[str, Any]:
        formatted = super()._parse_completion_response(response)
        usage_info = self._parse_usage(response.get("usage", {}))
        routing: dict[str, Any] = {
            "provider": self.config.provider,
            "base_url": self.config.base_url,
            "endpoint_id": getattr(self.config, "endpoint_id", None) or self.config.provider,
        }
        if usage_info.upstream_cost_usd is not None:
            routing["upstream_cost_usd"] = usage_info.upstream_cost_usd
        formatted["_routing"] = routing
        return formatted
