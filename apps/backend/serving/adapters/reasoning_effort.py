"""A route's reasoning-effort contract: which public levels it takes, and what it sends.

Two vocabularies meet here. The **public** level is what a client names --
``reasoning_effort`` on Chat Completions, ``reasoning.effort`` on Responses,
``output_config.effort`` on Messages -- and what the model listings publish.
The **upstream** value is what the route's server receives as
``reasoning_effort``. They are the same string unless the route says otherwise.

A route says otherwise with ``reasoning_effort_map`` (public level -> upstream
value). It is a complete, strict declaration for that one route:

- its keys are the only public levels the route takes, in the order they are
  published; any other value is refused with a 400 before anything is sent
  (:class:`ReasoningEffortRefused`), never dropped or rounded;
- a level is translated exactly once, by the adapter that is about to send the
  request (``OpenAICompatAdapter._apply_supported_passthrough_params``), from
  the public value the request still carries. Retries, fallback and hedging
  hand each attempt the same public value, so nothing is translated twice, and
  another route translates with its own map or not at all;
- it forwards the parameter on its own: ``supported_params`` and
  ``reasoning_efforts`` are not consulted on a mapped route.

A route without a map keeps the behavior it always had (the *legacy*
contract): the parameter travels unchanged when ``supported_params`` names it,
``reasoning_efforts`` is its declared public domain when there is one, and an
undeclared domain stays undeclared -- unknown is never read as "none".

No level, tier or mapping is built in. Only adapters that put
``reasoning_effort`` on the wire (``forwards_reasoning_effort``) may carry a
map; the registry and the admin API refuse one anywhere else.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp

#: The request parameter this contract governs.
EFFORT_PARAM = "reasoning_effort"
#: The configuration field, on a route and in an admin capability override.
EFFORT_MAP_FIELD = "reasoning_effort_map"
#: A level is a short token: no spaces, no punctuation beyond ``_ . -``.
_LEVEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
#: More entries than any provider documents; a longer map is a mistake.
MAX_MAP_ENTRIES = 16


class ReasoningEffortRefused(aiohttp.ClientResponseError):
    """A public level a mapped route does not take. A 400, raised before any upstream I/O.

    An ``aiohttp`` ``ClientResponseError`` (as ``RemoteMediaRefused`` is) so
    every caller handles it as the upstream 400 it stands in for: the breaker
    exempts it as a client error, the chat surfaces answer 400 with its message,
    and ``/v1/messages`` an ``invalid_request_error``.
    """

    def __init__(self, requested: Any, levels: tuple[str, ...]) -> None:
        offered = ", ".join(levels)
        super().__init__(
            request_info=None,  # type: ignore[arg-type]
            history=(),
            status=400,
            message=(
                f"reasoning effort {requested!r} is not supported by this model; "
                f"supported: {offered}"
            ),
        )
        self.requested = requested
        self.levels = levels

    def __str__(self) -> str:
        # ``ClientResponseError.__str__`` reads the request's URL, and there is none.
        return f"400, message={self.message!r}"


def _entries(raw: Any) -> list[tuple[Any, Any]]:
    """``raw``'s (public, upstream) entries, in order: a mapping, or a list of pairs.

    The list form is how a map is stored: a JSONB object does not keep key
    order, and the order is the order the levels are published in.
    """
    if isinstance(raw, Mapping):
        return list(raw.items())
    if isinstance(raw, list) and all(
        isinstance(pair, (list, tuple)) and len(pair) == 2 for pair in raw
    ):
        return [(pair[0], pair[1]) for pair in raw]
    raise ValueError(f"{EFFORT_MAP_FIELD} must be a mapping of public level to upstream value")


def validate_effort_map(raw: Any) -> dict[str, str]:
    """Return ``raw`` as a public -> upstream map, or raise ``ValueError`` saying why.

    A non-empty mapping of level tokens to level tokens (or the stored list of
    ``[public, upstream]`` pairs), each public level once. Order is kept: it is
    the order the levels are published in.
    """
    entries = _entries(raw)
    if not entries:
        raise ValueError(f"{EFFORT_MAP_FIELD} must name at least one level")
    if len(entries) > MAX_MAP_ENTRIES:
        raise ValueError(f"{EFFORT_MAP_FIELD} may name at most {MAX_MAP_ENTRIES} levels")
    cleaned: dict[str, str] = {}
    for public, upstream in entries:
        for role, value in (("public level", public), ("upstream value", upstream)):
            if not isinstance(value, str) or not _LEVEL_RE.fullmatch(value):
                raise ValueError(
                    f"{EFFORT_MAP_FIELD} {role} {value!r} must be a short token "
                    "(letters, digits, '_', '.', '-'; at most 32 characters)"
                )
        if public in cleaned:
            raise ValueError(f"{EFFORT_MAP_FIELD} names public level {public!r} twice")
        cleaned[public] = upstream
    return cleaned


def stored_effort_map(effort_map: Mapping[str, str] | None) -> list[list[str]] | None:
    """``effort_map`` as it is stored: ``[[public, upstream], ...]`` in order, ``None`` for none."""
    return [[public, upstream] for public, upstream in effort_map.items()] if effort_map else None


def read_effort_map(raw: Any) -> dict[str, str]:
    """A map as stored or declared (object or pairs), as a dict; ``{}`` for none or malformed."""
    if raw is None:
        return {}
    try:
        return validate_effort_map(raw)
    except ValueError:
        return {}


@dataclass(frozen=True)
class EffortContract:
    """What one route does with a request's ``reasoning_effort``.

    ``forwards``: the parameter reaches the upstream at all. ``levels``: the
    public levels the route declares, or ``None`` when it declares none (an
    unknown domain, distinct from ``()``). ``upstream``: the translation of a
    mapped route, ``None`` on a legacy one (values travel unchanged).
    """

    forwards: bool
    levels: tuple[str, ...] | None
    upstream: Mapping[str, str] | None = None

    @property
    def mapped(self) -> bool:
        """Whether the route declares the strict public -> upstream contract."""
        return self.upstream is not None


def _legacy_levels(config: Any) -> tuple[str, ...] | None:
    declared = getattr(config, "reasoning_efforts", None)
    if not isinstance(declared, (list, tuple)) or not declared:
        return None
    # As declared: a malformed value stays visible to the readers that check.
    return tuple(declared)


def effort_contract(config: Any) -> EffortContract:
    """The contract ``config``'s route serves ``reasoning_effort`` under.

    Reads the configuration only; whether the adapter class forwards the
    parameter at all is the caller's question (``forwards_reasoning_effort``),
    and a mapped route always has one that does.
    """
    mapping = getattr(config, EFFORT_MAP_FIELD, None)
    if isinstance(mapping, Mapping) and mapping:
        return EffortContract(forwards=True, levels=tuple(mapping), upstream=dict(mapping))
    supported = getattr(config, "supported_params", None) or ()
    if EFFORT_PARAM not in supported:
        return EffortContract(forwards=False, levels=())
    return EffortContract(forwards=True, levels=_legacy_levels(config))


def declared_public_levels(config: Any) -> tuple[str, ...]:
    """The public levels the route declares it delivers; empty when none or undeclared."""
    contract = effort_contract(config)
    return (contract.levels or ()) if contract.forwards else ()


def translate_effort(contract: EffortContract, requested: Any) -> Any:
    """The value ``contract``'s route sends as ``reasoning_effort`` for ``requested``.

    A legacy route sends it unchanged. A mapped route sends its translation, or
    refuses a level it does not declare (:class:`ReasoningEffortRefused`).
    """
    if contract.upstream is None:
        return requested
    if not isinstance(requested, str) or requested not in contract.upstream:
        raise ReasoningEffortRefused(requested, contract.levels or ())
    return contract.upstream[requested]


def upstream_effort(config: Any, requested: Any) -> Any:
    """:func:`translate_effort` under ``config``'s contract as it is now."""
    return translate_effort(effort_contract(config), requested)


__all__ = [
    "EFFORT_MAP_FIELD",
    "EFFORT_PARAM",
    "MAX_MAP_ENTRIES",
    "EffortContract",
    "ReasoningEffortRefused",
    "declared_public_levels",
    "effort_contract",
    "read_effort_map",
    "stored_effort_map",
    "translate_effort",
    "upstream_effort",
    "validate_effort_map",
]
