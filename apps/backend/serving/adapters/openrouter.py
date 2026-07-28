"""OpenRouter adapter: thin OpenAICompatAdapter subclass with OR-specific request shape."""

from __future__ import annotations

from typing import Any

from serving.config.site_identity import get_site_identity

from .openai_compat import OpenAICompatAdapter

# Attribution headers required for OpenRouter leaderboard / free-tier limits;
# sourced from the site identity so a distribution attributes as itself.


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
    - ``provider: {sort: <policy>}`` for bare OpenRouter routes with
      ``openrouter_sort`` set to ``price``, ``throughput``, or ``latency``.

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
        site = get_site_identity()
        # Omit rather than send empty: a deployment that has declared no public
        # URL has nothing to attribute, and an empty header is not a value.
        if site.public_base_url:
            headers["HTTP-Referer"] = site.public_base_url
        if site.name:
            headers["X-Title"] = site.name
        return headers

    def _augment_payload(self, payload: dict[str, Any], *, stream: bool) -> dict[str, Any]:
        payload["usage"] = {"include": True}
        pin = getattr(self.config, "openrouter_pinned_provider", None)
        if pin:
            payload["provider"] = {"order": [pin], "allow_fallbacks": False}
        elif sort := getattr(self.config, "openrouter_sort", None):
            payload["provider"] = {"sort": sort}
        if stream:
            existing = dict(payload.get("stream_options") or {})
            existing["include_usage"] = True
            payload["stream_options"] = existing
        return payload

    def _parse_completion_response(self, response: dict[str, Any]) -> dict[str, Any]:
        from .profiles import extract_tool_calls_for_profile

        choice = response["choices"][0]
        message = choice["message"]
        tool_calls = extract_tool_calls_for_profile(self._usage_profile, message)
        usage_info = self._parse_usage(response.get("usage", {}))

        formatted = self.format_response(
            content=message.get("content", ""),
            model=self.config.id,
            usage=usage_info,
            tool_calls=tool_calls,
            reasoning_content=message.get("reasoning_content"),
            finish_reason=choice.get("finish_reason", "stop"),
        )

        routing: dict[str, Any] = {
            "provider": self.config.provider,
            "base_url": self.config.base_url,
            "endpoint_id": getattr(self.config, "endpoint_id", None) or self.config.provider,
        }
        if usage_info.upstream_cost_usd is not None:
            routing["upstream_cost_usd"] = usage_info.upstream_cost_usd
        formatted["_routing"] = routing
        return formatted
