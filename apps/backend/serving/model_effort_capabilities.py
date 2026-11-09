"""Which reasoning-effort values a listed model's routes will actually deliver.

The model listings (``servers/routers/models.py``) publish this so a client can
offer the levels a request would carry to the model, and nothing else. It is
read from the effective effort contract and from which adapter class serves
the route. An exact upstream preset can supply that contract. Nothing is
inferred from a model's name.

**Per adapter.** Only :class:`OpenAICompatAdapter` (and its subclasses)
forwards ``reasoning_effort`` to the provider, and only when the parameter is
in the route's ``supported_params`` or the route declares a
``reasoning_effort_map`` (``OpenAICompatAdapter._apply_supported_passthrough_params``,
``serving.adapters.reasoning_effort``). Every surface
reaches it the same way: ``/v1/chat/completions`` passes the field through,
``/v1/responses`` maps ``reasoning.effort`` onto it, and ``/v1/messages`` maps
``output_config.effort`` onto it only for a value in the declared domain
(``anthropic_messages._apply_reasoning_effort``). So an adapter is

- **known, with a domain** -- a route with a ``reasoning_effort_map``: its
  public levels (the map's keys, never the upstream values it sends); or a
  route that forwards the parameter and declares a well-formed
  ``reasoning_efforts`` list: exactly those values;
- **known, with none** -- an adapter that forwards the parameter but whose
  route explicitly leaves it out of ``supported_params``: the field is dropped
  on every surface, so no value is delivered;
- **unknown** -- an adapter class that does not forward the parameter itself
  (native Anthropic, Vertex Claude, Gemini), so the configuration does not say
  what reaches the provider; or a route that takes the parameter with no
  domain declared (the documented "not offered", where the chat surface would
  still pass a value through), an undeclared route, or a malformed domain.

**Per model.** A model is listed once, by its canonical id, with the routes of
the entry the listing shows; any of them may serve a request. A value is
published only if every route delivers it: one unknown route makes the model
unknown, and routes that share no value publish an empty domain.

Unknown publishes nothing, which is also what a gateway predating this field
answers, so a client cannot mistake missing knowledge for "no effort".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serving.adapters.reasoning_effort import EFFORT_PARAM, effort_contract

if TYPE_CHECKING:
    from collections.abc import Iterable

#: The levels the Anthropic Models API describes in ``capabilities.effort``.
ANTHROPIC_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
#: A declared value longer than this is not a level; the domain is malformed.
_MAX_LEVEL_CHARS = 32


def _well_formed(levels: tuple[str, ...]) -> list[str] | None:
    """``levels`` as a published domain, or ``None`` when any value is malformed."""
    domain: list[str] = []
    for value in levels:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > _MAX_LEVEL_CHARS
            or value in domain
        ):
            return None
        domain.append(value)
    return domain


def adapter_effort_domain(adapter: Any) -> list[str] | None:
    """The values ``adapter``'s route delivers: a list (maybe empty), or ``None`` for unknown."""
    if not getattr(adapter, "forwards_reasoning_effort", False):
        return None
    contract = effort_contract(getattr(adapter, "config", None))
    if not contract.forwards:
        return [] if getattr(adapter.config, "reasoning_effort_declared", True) else None
    if contract.levels is None:
        return None
    return _well_formed(contract.levels)


def model_effort_domain(adapters: Iterable[Any]) -> list[str] | None:
    """The values every one of a model's routes delivers, in the first route's order.

    ``None`` when there is no route, or any route is unknown.
    """
    domains = [adapter_effort_domain(adapter) for adapter in adapters]
    if not domains or any(domain is None for domain in domains):
        return None
    first, *rest = (domain for domain in domains if domain is not None)
    return [value for value in first if all(value in other for other in rest)]


def openai_reasoning(domain: list[str] | None) -> dict[str, Any] | None:
    """The ``reasoning`` object of an OpenAI/OpenRouter-shaped model entry.

    ``{"supported_efforts": [...]}`` -- the field OpenRouter's model listing
    uses -- in the order the deployment declared them; an empty list says no
    value is delivered. ``None`` (unknown) is published as ``null``.
    """
    if domain is None:
        return None
    return {"supported_efforts": list(domain)}


def anthropic_capabilities(domain: list[str] | None) -> dict[str, Any] | None:
    """The ``capabilities`` object of an Anthropic-shaped model entry, effort only.

    The Anthropic Models API's ``capabilities.effort``: ``supported`` and one
    ``{"supported": bool}`` per level it names. A declared value outside those
    levels has no place in this shape and is not listed in it. ``None``
    (unknown) leaves ``capabilities`` out. Only ``effort`` is populated: the
    registry describes no other capability in this vocabulary.
    """
    if domain is None:
        return None
    effort: dict[str, Any] = {
        level: {"supported": level in domain} for level in ANTHROPIC_EFFORT_LEVELS
    }
    effort["supported"] = any(level in domain for level in ANTHROPIC_EFFORT_LEVELS)
    return {"effort": effort}


__all__ = [
    "ANTHROPIC_EFFORT_LEVELS",
    "EFFORT_PARAM",
    "adapter_effort_domain",
    "anthropic_capabilities",
    "model_effort_domain",
    "openai_reasoning",
]
