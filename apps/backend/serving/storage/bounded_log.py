"""Bounded copies of what an ``api_logs`` row stores for a refused request.

A request the gateway or its upstream refuses with a 4xx is not billed, yet its
row used to store the whole body: the prompt column, ``request_payload`` and the
tool definitions, every one serialized on the event loop (``strip_null_bytes``,
``json_safe``, ``json.dumps``) and then written to the database. One caller
naming a model that does not exist could make the gateway store, for free,
however much it sent. A refused request's row keeps a summary instead: the same
fields, holding at most :data:`REJECTED_LOG_MAX_CHARS` characters each, with
``metadata.payload_truncated`` set when anything was cut.

The summaries are built by walking the value under a character budget, so their
cost is bounded by the budget and not by the size of what was sent. Lists keep
their head and their tail, so a conversation keeps its system prompt and its
latest turn; text cut in the middle keeps both ends, so a stored error still
reads ``Model '…' not found`` (the failed-request alerter matches that text).

Successful requests, and any other status, are stored as before.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Characters of a refused request's prompt, body or tool definitions a row
#: keeps: the bound :data:`serving.observability.rejection_log.REJECTED_PROMPT_MAX_CHARS`
#: already puts on a gate rejection's prompt.
REJECTED_LOG_MAX_CHARS = 65_536

#: Longest model id a row keeps. The model a client names is free text, and an
#: unknown one is stored as sent; no real id comes anywhere near this.
LOGGED_MODEL_ID_MAX_CHARS = 256

#: Longest error text a row keeps (``format_exception_for_db`` caps upstream
#: errors at 4000 characters already).
LOGGED_ERROR_MAX_CHARS = 4096

#: Status logged for a client that disconnected mid-stream: a request that was
#: served, not refused, so its row is kept whole.
_CLIENT_DISCONNECT_STATUS = 499

#: How deep a summary follows nested containers. A decoded JSON body is at most
#: as deep as the interpreter's recursion limit; nothing a person reads needs this.
_MAX_DEPTH = 32

#: Longest dictionary key a summary keeps.
_MAX_KEY_CHARS = 256

#: How many of a body's leading fields are sorted small-first. A request body
#: has a few dozen fields at most; past this they are taken in order.
_LEADING_FIELDS = 64

#: A string this short counts as a small field (a model id, a stop sequence).
_SMALL_TEXT_CHARS = 128

_ELLIPSIS = "…"


def _is_small(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) <= _SMALL_TEXT_CHARS
    return value is None or isinstance(value, bool | int | float)


def is_client_rejection(status_code: Any) -> bool:
    """Whether *status_code* is a refusal whose row is kept as a summary.

    Every 4xx: the gateway's own refusals (unknown model, access, modality,
    limits) and an upstream's. Not 499, which records a client that went away
    from a request that was being served.
    """
    return (
        isinstance(status_code, int)
        and 400 <= status_code < 500
        and status_code != _CLIENT_DISCONNECT_STATUS
    )


def elide_middle(text: str, max_chars: int) -> str:
    """*text* shortened to *max_chars* by dropping its middle, if it is longer."""
    if len(text) <= max_chars:
        return text
    head = (max_chars - len(_ELLIPSIS)) // 2
    tail = max_chars - len(_ELLIPSIS) - head
    return text[:head] + _ELLIPSIS + (text[-tail:] if tail > 0 else "")


class _Summary:
    """One budgeted walk over a JSON-like value."""

    def __init__(self, max_chars: int) -> None:
        self.left = max_chars
        self.cut = False

    def copy(self, node: Any, depth: int) -> Any:
        if isinstance(node, str):
            return self._text(node)
        if node is None or isinstance(node, bool | int | float):
            self.left -= 4
            return node
        if depth >= _MAX_DEPTH:
            self.cut = True
            self.left -= 4
            return _ELLIPSIS
        if isinstance(node, dict):
            return self._mapping(node, depth, ())
        if isinstance(node, list | tuple):
            return self._sequence(node, depth)
        # Not a JSON value (bytes, a model object): never stringify it, which
        # costs whatever its size is.
        self.cut = True
        self.left -= 16
        return f"<{type(node).__name__}>"

    def _text(self, text: str) -> str:
        cost = len(text) + 2
        if cost <= self.left:
            self.left -= cost
            return text
        keep = max(self.left - 2, 0)
        self.left = 0
        self.cut = True
        return text[:keep] + _ELLIPSIS

    def mapping(self, node: dict[Any, Any], skip_keys: tuple[str, ...]) -> dict[str, Any]:
        """A top-level dict: its small fields first, so a large one cannot crowd them out."""
        leading = list(itertools.islice(node.items(), _LEADING_FIELDS))
        small = [item for item in leading if _is_small(item[1])]
        large = [item for item in leading if not _is_small(item[1])]
        rest = itertools.islice(node.items(), _LEADING_FIELDS, None)
        return self._fields(itertools.chain(small, large, rest), 0, skip_keys)

    def _mapping(
        self, node: dict[Any, Any], depth: int, skip_keys: tuple[str, ...]
    ) -> dict[str, Any]:
        return self._fields(iter(node.items()), depth, skip_keys)

    def _fields(
        self, items: Iterator[tuple[Any, Any]], depth: int, skip_keys: tuple[str, ...]
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        self.left -= 2
        for key, value in items:
            if key in skip_keys:
                continue
            if self.left <= 0:
                self.cut = True
                break
            name = key if isinstance(key, str) else str(key)
            if len(name) > _MAX_KEY_CHARS:
                name = name[:_MAX_KEY_CHARS] + _ELLIPSIS
                self.cut = True
            self.left -= len(name) + 4
            out[name] = self.copy(value, depth + 1)
        return out

    def _sequence(self, items: list[Any] | tuple[Any, ...], depth: int) -> list[Any]:
        self.left -= 2
        if len(items) == 1:
            return [self.copy(items[0], depth + 1)] if self.left > 0 else []
        budget = self.left
        # The head gets half the budget and the tail what is left after it, so
        # a long list keeps its first and its last entries.
        self.left = budget // 2
        head: list[Any] = []
        index = 0
        while index < len(items) and self.left > 0:
            head.append(self.copy(items[index], depth + 1))
            index += 1
        self.left += budget - budget // 2
        tail: list[Any] = []
        last = len(items) - 1
        while last >= index and self.left > 0:
            tail.append(self.copy(items[last], depth + 1))
            last -= 1
        if last >= index:
            self.cut = True
        tail.reverse()
        return head + tail


def bounded_json(
    value: Any,
    max_chars: int = REJECTED_LOG_MAX_CHARS,
    *,
    skip_keys: tuple[str, ...] = (),
) -> tuple[Any, bool]:
    """Return a copy of *value* holding about *max_chars* characters, and whether it was cut.

    The copy has the shape of the original (a dict stays a dict, a messages list
    a list of messages), so the readers of the stored row need no special case.
    *skip_keys* leaves those top-level keys of a dict out; that is not a cut.
    The work done is proportional to *max_chars*, whatever the size of *value*.
    """
    summary = _Summary(max_chars)
    if isinstance(value, dict):
        copied: Any = summary.mapping(value, skip_keys)
    else:
        copied = summary.copy(value, 0)
    return copied, summary.cut


def bound_rejected_log_payload(log_data: dict[str, Any]) -> dict[str, Any]:
    """Return the ``LogStore.log_request`` kwargs to store for *log_data*.

    *log_data* itself when its status is not a client rejection
    (:func:`is_client_rejection`); otherwise a copy whose prompt, request body,
    tool definitions, model id and error are bounded. The body leaves out
    ``messages`` when the row has a prompt, which holds them: the convention of
    :mod:`serving.storage.payload_dedup`. The caller's dicts are not modified.
    """
    if not is_client_rejection(log_data.get("status_code")):
        return log_data
    out = dict(log_data)
    cut = False
    prompt = out.get("prompt")
    if prompt is not None:
        out["prompt"], prompt_cut = bounded_json(prompt)
        cut = cut or prompt_cut
    payload = out.get("request_payload")
    if payload is not None:
        skip = ("messages",) if prompt else ()
        out["request_payload"], payload_cut = bounded_json(payload, skip_keys=skip)
        cut = cut or payload_cut
    params = out.get("params")
    if isinstance(params, dict) and params.get("tools") is not None:
        tools, tools_cut = bounded_json(params["tools"])
        out["params"] = {**params, "tools": tools}
        cut = cut or tools_cut
    for field, limit in (
        ("model_id", LOGGED_MODEL_ID_MAX_CHARS),
        ("error", LOGGED_ERROR_MAX_CHARS),
    ):
        text = out.get(field)
        if isinstance(text, str) and len(text) > limit:
            out[field] = elide_middle(text, limit)
            cut = True
    if cut:
        out["metadata"] = {**(out.get("metadata") or {}), "payload_truncated": True}
    return out
