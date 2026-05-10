"""Typed routing context that flows through the completions request lifecycle.

Replaces the prior untyped ``routing_info: dict[str, Any]`` that carried
magic keys (``endpoint_id``, ``base_url``, ``provider``, ``pricing``,
``routewise``, ``upstream_cost_usd``) between layers of the chat-completions
handler.

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


@dataclass(frozen=True, slots=True)
class RouteWiseDecision:
    """RouteWise telemetry forwarded to ``record_routing_observation``.

    Opaque to the handler; only ``CompletionsLogger`` reads its fields.
    Mirrors the keys today's adapters write into ``_routing["routewise"]``.
    """

    selected_tier: str | None = None
    quota_committed: float = 0.0
    sc_committed: bool = False
    hedged: bool = False
    backup_won: bool = False
    lp_status: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
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
    routewise: dict[str, Any] | None = None
    upstream_cost_usd: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


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
            "routewise": {"selected_tier": "A", ...},
            "upstream_cost_usd": 0.012,
            ...
        }

    Unknown keys are stashed under :attr:`RoutingInfo.extra` to preserve
    the existing behavior of merging the dict into request metadata.
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
    field_names = {
        "provider",
        "base_url",
        "endpoint_id",
        "routewise",
        "upstream_cost_usd",
    }
    for key, value in adapter_routing.items():
        if key in field_names:
            known[key] = value
        elif key == "failed_attempts" and isinstance(value, list):
            existing = extra.get(key)
            extra[key] = [*(existing if isinstance(existing, list) else []), *value]
        else:
            extra[key] = value

    import dataclasses as _dc

    replacements: dict[str, Any] = {}
    for fname in field_names:
        if fname in known:
            replacements[fname] = known[fname]
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
