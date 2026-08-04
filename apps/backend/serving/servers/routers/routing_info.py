"""Typed routing context that flows through the completions request lifecycle.

Replaces the prior untyped ``routing_info: dict[str, Any]`` that carried
magic keys (``endpoint_id``, ``base_url``, ``provider``, ``pricing``,
``strategy_metadata``, ``upstream_cost_usd``) between layers of the chat-completions
handler.

Legacy adapter metadata may still arrive under ``routewise``; it is translated
into ``strategy_metadata["routewise"]`` for compatibility.

PR A introduces the type and the centralized exception-status-code helper.
PR B will introduce ``PricingLookup`` / ``CostTracker`` and start using the
typed ``Pricing`` dataclass at runtime; for now ``RoutingInfo.pricing`` is
typed as ``dict[str, Any] | None`` to preserve the production wire format
that ``serving.storage.utils.calculate_cost`` and downstream log consumers
depend on byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from serving.utils import context as req_ctx

_ROUTEWISE_UNSET = object()


def merge_strategy_metadata(
    base: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Shallow-merge strategy metadata, preserving nested routewise keys."""
    merged = dict(base) if isinstance(base, dict) else {}
    existing_routewise = merged.get("routewise")
    incoming_routewise = incoming.get("routewise")
    merged.update(incoming)
    if isinstance(incoming_routewise, dict):
        merged["routewise"] = {
            **(existing_routewise if isinstance(existing_routewise, dict) else {}),
            **incoming_routewise,
        }
    return merged


@dataclass(frozen=True, slots=True)
class Pricing:
    """Per-provider, per-model upstream pricing.

    Field values mirror the float-parsed values of the adapter pricing dict
    keys (``prompt``/``completion``/``input_cache_reads``/
    ``input_cache_writes``). Units match what ``serving.storage.utils.
    calculate_cost`` consumes — i.e., USD per million tokens — so that
    ``CostTracker._compute_cost`` produces byte-for-byte equivalent results
    against the legacy dict-based path.
    """

    prompt_price: float
    completion_price: float
    cache_read_price: float = 0.0
    cache_write_price: float = 0.0


@dataclass(frozen=True, slots=True, init=False)
class RoutingInfo:
    """Per-request routing state carried through the handler pipeline.

    ``pricing`` is the typed in-process view used by
    :class:`~serving.servers.routers.completions_cost.CostTracker`. The
    adapter's raw pricing dict (string-valued, with keys ``prompt`` /
    ``completion`` / ``input_cache_reads`` / ``input_cache_writes``)
    flows through unchanged into ``extra["pricing"]`` so downstream
    components — notably ``LogStore.log_request`` which consumes the dict
    directly — keep working byte-for-byte.
    """

    request_id: str
    model: str
    provider: str | None = None
    endpoint_id: str | None = None
    base_url: str | None = None
    pricing: Pricing | None = None
    strategy_metadata: dict[str, Any] | None = None
    upstream_cost_usd: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        request_id: str,
        model: str,
        provider: str | None = None,
        endpoint_id: str | None = None,
        base_url: str | None = None,
        pricing: Pricing | None = None,
        strategy_metadata: dict[str, Any] | None = None,
        routewise: dict[str, Any] | None | object = _ROUTEWISE_UNSET,
        upstream_cost_usd: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if isinstance(strategy_metadata, dict):
            strategy_metadata = dict(strategy_metadata)
        if routewise is None:
            if isinstance(strategy_metadata, dict):
                strategy_metadata.pop("routewise", None)
                if not strategy_metadata:
                    strategy_metadata = None
        elif isinstance(routewise, dict):
            strategy_metadata = merge_strategy_metadata(
                strategy_metadata,
                {"routewise": routewise},
            )

        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "endpoint_id", endpoint_id)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "pricing", pricing)
        object.__setattr__(self, "strategy_metadata", strategy_metadata)
        object.__setattr__(self, "upstream_cost_usd", upstream_cost_usd)
        object.__setattr__(self, "extra", extra if extra is not None else {})

    @property
    def routewise(self) -> dict[str, Any] | None:
        """Legacy compatibility view of RouteWise strategy metadata."""
        if not isinstance(self.strategy_metadata, dict):
            return None
        routewise = self.strategy_metadata.get("routewise")
        return routewise if isinstance(routewise, dict) else None


def build_initial_routing_info(
    request: Any,
    *,
    request_id: str,
    pin_provider: str | None,
) -> RoutingInfo:
    """Construct the pre-routing ``RoutingInfo`` from the chat request.

    The handler later enriches the returned instance via
    :func:`merge_adapter_routing` (which itself uses :func:`dataclasses.replace`)
    once the adapter response has surfaced its ``_routing`` metadata.
    """
    return RoutingInfo(
        request_id=request_id,
        model=getattr(request, "model", ""),
        provider=pin_provider,
    )


def merge_adapter_routing(
    base: RoutingInfo,
    adapter_routing: dict[str, Any] | None,
) -> RoutingInfo:
    """Return a new ``RoutingInfo`` enriched from an adapter ``_routing`` dict.

    The adapter dict is the wire format produced by routers/adapters (see
    ``apps/backend/routing/routers.py`` and ``adapters/*``) and looks like::

        {
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "endpoint_id": "openai-prod",
            "pricing": {"prompt": "0.5", "completion": "1.5"},
            "strategy_metadata": {"custom_strategy": {...}},
            "upstream_cost_usd": 0.012,
            ...
        }

    Unknown keys are stashed under :attr:`RoutingInfo.extra` to preserve
    the existing behavior of merging the dict into request metadata.
    Legacy ``routewise`` dicts are stored as ``strategy_metadata["routewise"]``.
    """
    if not adapter_routing:
        return base

    # ``pricing`` from adapters is a string-valued dict (e.g., ``{"prompt":
    # "0.5", "completion": "1.5"}``). ``RoutingInfo.pricing`` is now a typed
    # ``Pricing | None``, so we deliberately route the raw pricing dict into
    # ``extra["pricing"]``; ``PricingLookup.for_routing`` reads it from there
    # and does the typed conversion at the lookup boundary. This keeps the
    # log payload (which still wants the raw dict) byte-for-byte stable.
    known: dict[str, Any] = {}
    extra: dict[str, Any] = dict(base.extra)
    strategy_metadata = (
        dict(base.strategy_metadata) if isinstance(base.strategy_metadata, dict) else None
    )
    routewise: dict[str, Any] | None = None
    field_names = {
        "provider",
        "base_url",
        "endpoint_id",
        "upstream_cost_usd",
    }
    for key, value in adapter_routing.items():
        if key in field_names:
            known[key] = value
        elif key == "strategy_metadata" and isinstance(value, dict):
            strategy_metadata = merge_strategy_metadata(strategy_metadata, value)
        elif key == "routewise" and isinstance(value, dict):
            routewise = value
        elif key == "failed_attempts" and isinstance(value, list):
            existing = extra.get(key)
            extra[key] = [*(existing if isinstance(existing, list) else []), *value]
        else:
            extra[key] = value

    if routewise is not None:
        strategy_metadata = merge_strategy_metadata(
            strategy_metadata,
            {"routewise": routewise},
        )

    import dataclasses as _dc

    replacements: dict[str, Any] = {}
    for fname in field_names:
        if fname in known:
            replacements[fname] = known[fname]
    if strategy_metadata != base.strategy_metadata:
        replacements["strategy_metadata"] = strategy_metadata
    if extra != base.extra:
        replacements["extra"] = extra
    return _dc.replace(base, **replacements) if replacements else base


def _status_code_from_exception(exc: BaseException) -> int:
    """Extract an HTTP status code from common upstream exception shapes.

    Centralizes the 6-attribute fallback chain previously duplicated in
    ``stream_generator`` and the non-streaming exception handler.
    Falls back through:

    1. ``exc.status_code`` (OpenAI / Anthropic SDKs)
    2. ``exc.response.status_code`` (httpx, requests)
    3. ``exc.response.status`` (older httpx-style responses)
    4. ``exc.status`` (aiohttp)
    5. ``exc.code`` (some custom exceptions)

    Returns ``500`` when none of the above yield an integer.
    """
    direct = getattr(exc, "status_code", None)
    if isinstance(direct, int):
        return direct

    response = getattr(exc, "response", None)
    if response is not None:
        resp_status_code = getattr(response, "status_code", None)
        if isinstance(resp_status_code, int):
            return resp_status_code
        resp_status = getattr(response, "status", None)
        if isinstance(resp_status, int):
            return resp_status

    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status

    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code

    return 500


def _provider_for_error(exc_routing: Any) -> str:
    """Resolve the provider label to record on an error log row.

    Prefer the real upstream provider preserved on ``exc._routing`` by the
    routing layer (``routers.py`` attaches ``{"provider": ...}`` when an
    adapter call fails). The request-context ``provider`` is set only inside
    the ``req_ctx.push`` scope wrapping the adapter call, and that scope has
    already been reset by the time an exception reaches the error handler — so
    reading it here yields no provider and a genuine upstream failure would be
    misattributed to the ``"router"`` sentinel, then silently dropped by the
    provider-performance aggregations (which exclude ``provider IN
    ('', 'router')``). Fall back to the context only defensively, then to
    ``"router"`` for genuine pre-routing failures where no upstream was ever
    selected (``exc._routing`` absent, or explicitly set to ``"router"``).

    Mirrors the success path, which already prefers ``routing.provider`` over
    the context (see ``completions.py``).
    """
    if isinstance(exc_routing, dict):
        provider = exc_routing.get("provider")
        if provider:
            return provider
    ctx = req_ctx.get()
    return ctx.get("provider", "router") if ctx else "router"


def _publish_error_provider(exc_routing: Any) -> str:
    """Resolve the error-path provider label and publish it into the context.

    Returns whatever ``_provider_for_error`` resolved, and additionally writes it
    back into ``req_ctx`` when a real upstream was selected. The DB error log
    reads the label from a dict it is handed, but ``RequestLogMiddleware`` and the
    in-process alert rules can only read the request context — and the
    ``req_ctx.push`` scope wrapping the adapter call is already unwound by the
    time an exception reaches the error handler. Without this the request-log
    record carries no provider at all, so an upstream failure is
    indistinguishable from one the gateway raised itself: that is how a relayed
    upstream 401 was filed as a routine auth challenge and logged below the
    default threshold for an hour, and why the failed-request-rate rule could not
    tell the two apart either.

    The ``"router"`` sentinel is deliberately not published: it means no upstream
    was ever selected (a pre-routing failure), so labelling the record with it
    would misattribute the failure *and* defeat the distinction both consumers
    draw — they treat "has a provider" as "an upstream refused us".
    """
    provider = _provider_for_error(exc_routing)
    if provider and provider != "router":
        req_ctx.update({"provider": provider})
    return provider
