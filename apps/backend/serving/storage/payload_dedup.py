"""Drop request-body content that ``api_logs`` already stores in dedicated columns.

``api_logs.request_payload`` keeps the inbound request body so admin tooling can
see exactly what a client sent. Two of its keys are also written to their own
columns on the *same row* — ``messages`` to ``prompt`` and ``tools`` to ``tools``
— so keeping them inside the JSONB body stored the largest part of every request
twice. That duplication is why ``request_payload`` grew to roughly half of a
517 GB table.

The strip belongs to the storage layer, immediately before the INSERT, and not to
the routers that build the body: while a request is in flight, callers read
``request_payload["tools"]`` (to populate the ``tools`` column) and
``request_payload["messages"]``/``["tools"]`` (to estimate input tokens when a
client disconnects mid-stream). Removing the keys any earlier would NULL the
tools column and under-bill that traffic.

Dedup is *verified*, never assumed
----------------------------------
A key is only dropped when this row demonstrably stores the same value
elsewhere. Assuming duplication loses data in two real cases:

* The ``prompt`` column on the OpenAI-compat surface is a pydantic
  re-serialization (``ChatMessage`` is declared ``extra="ignore"``), so
  per-message extras a client sent — ``cache_control``, ``prefix``/``partial``,
  vendor fields — exist *only* in ``request_payload``. Those survive here, under
  ``messages_extra``: a list positionally aligned with ``messages`` holding just
  the fields the ``prompt`` column did not keep. The key is absent when there is
  nothing extra, which is the common case.
* Early-error rows (model-not-found, role-gated, unsupported-modality) log
  ``early_params``, which carries no ``tools``. The ``tools`` column is NULL on
  those rows, so ``request_payload`` is the only copy and ``tools`` is kept.

Everything else in the body — ``model``, ``system``, ``stream``, sampling params,
``tool_choice``, ``response_format``, vendor extras — is unique to
``request_payload`` and is always preserved. ``system`` in particular is the only
stored copy of the Anthropic-surface system prompt (the ``prompt`` column holds
messages only) and feeds ``metadata.agent``.

Reading a row back: ``messages`` present means the full messages are inline (rows
written before this change); ``messages`` absent means read the ``prompt``
column, and apply ``messages_extra`` by index if present.
"""

from __future__ import annotations

import json
from typing import Any

# Request-body keys whose content has a dedicated api_logs column:
#   "messages" -> prompt
#   "tools"    -> tools
DUPLICATED_PAYLOAD_KEYS: tuple[str, ...] = ("messages", "tools")

# Where the per-message leftovers go when the prompt column did not keep them.
MESSAGE_RESIDUAL_KEY = "messages_extra"

_SENTINEL = object()


def _message_residuals(sent: Any, stored: Any) -> tuple[bool, list[dict[str, Any]] | None]:
    """Compare the body's messages against the ones the ``prompt`` column stored.

    Returns ``(strippable, residuals)``. ``strippable`` is False when the two
    cannot be aligned confidently, in which case the caller must keep the
    original ``messages`` untouched. ``residuals`` is None when every message was
    stored losslessly, or a positionally aligned list of the leftover fields.
    """
    if not isinstance(sent, list) or not isinstance(stored, list) or len(sent) != len(stored):
        return False, None

    residuals: list[dict[str, Any]] = []
    for sent_msg, stored_msg in zip(sent, stored, strict=True):
        if not isinstance(sent_msg, dict) or not isinstance(stored_msg, dict):
            return False, None
        residuals.append(
            {
                key: value
                for key, value in sent_msg.items()
                if stored_msg.get(key, _SENTINEL) != value
            }
        )

    if not any(residuals):
        return True, None
    return True, residuals


def _strip_dict(
    payload: dict[str, Any],
    stored_messages: Any,
    stored_tools: Any,
) -> dict[str, Any]:
    """Return *payload* minus whatever this row provably stores elsewhere."""
    result = dict(payload)

    if "messages" in result:
        strippable, residuals = _message_residuals(result["messages"], stored_messages)
        if strippable:
            del result["messages"]
            if residuals is not None:
                result[MESSAGE_RESIDUAL_KEY] = residuals

    # Only drop tools when the tools column actually holds the same value. On
    # early-error rows params carries no tools, so this is the only copy.
    if "tools" in result and stored_tools is not None and result["tools"] == stored_tools:
        del result["tools"]

    return result


def strip_duplicated_payload_keys(
    payload: Any,
    *,
    stored_messages: Any = None,
    stored_tools: Any = None,
) -> Any:
    """Return *payload* without the content already stored in its own columns.

    ``stored_messages`` and ``stored_tools`` are the values this same row writes
    to the ``prompt`` and ``tools`` columns; they must already be null-byte
    sanitized so the comparison matches what is actually persisted. Passing
    neither strips nothing, which keeps the function safe by default.

    Handles every shape ``request_payload`` arrives in, preserving the type it
    was given so the caller's serialization behaviour is unchanged:

    * ``dict`` — a new dict; the input is never mutated, because callers may
      still be reading it (e.g. for ``metadata.agent``).
    * ``str`` — a JSON object is re-encoded; a string that is not a JSON object
      (or is malformed JSON) is returned unchanged.
    * anything else, including ``None`` — returned unchanged.
    """
    if isinstance(payload, dict):
        if not any(key in payload for key in DUPLICATED_PAYLOAD_KEYS):
            return payload
        return _strip_dict(payload, stored_messages, stored_tools)

    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except ValueError:
            return payload
        if not isinstance(decoded, dict):
            return payload
        if not any(key in decoded for key in DUPLICATED_PAYLOAD_KEYS):
            return payload
        stripped = _strip_dict(decoded, stored_messages, stored_tools)
        if stripped == decoded:
            return payload
        return json.dumps(stripped)

    return payload
