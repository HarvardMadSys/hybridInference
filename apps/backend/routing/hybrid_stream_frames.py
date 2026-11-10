"""SSE frame inspection for the hybrid streaming path.

The hybrid layer forwards backend chunks one at a time and must not reinterpret
them: a fallback is allowed only until the client can see something, and after
that the stream belongs to one backend. Two questions decide that, and both are
about the frame's bytes rather than about any backend's state:

``is_client_visible``
    Does this frame carry anything the client will actually see? Backends emit a
    synthetic routing frame first (``{"choices": [], "_routing": {...}}``) which
    the serving layer drops, so it does not commit an attempt.
``tag_backend_chunk``
    Which backend served this frame? Recorded inside an existing ``_routing``
    object; every other frame is returned untouched, byte for byte.

Both helpers are pure: they read a chunk and, at most, add one key to a routing
payload. Neither decides whether to fall back -- that stays with
:mod:`routing.hybrid` -- and neither changes framing or ordering.

Neither parses a frame that cannot carry ``_routing`` (:func:`carries_routing`):
nearly every frame of a stream is a content delta, and each used to be parsed
two or three times on its way through this layer.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["carries_routing", "is_client_visible", "tag_backend_chunk"]

# How a routing key reads in a serialized frame. The gateway writes every frame
# that carries one with ``json.dumps``, which writes an ASCII key verbatim.
_ROUTING_KEY = '"_routing"'


def carries_routing(chunk: Any) -> bool:
    """Return whether ``chunk`` can carry a ``_routing`` object, without parsing it.

    True for any non-string chunk, which is read as it is, and for a string
    whose text contains the key. A string without it is skipped unparsed: every
    frame the gateway writes spells the key out, so a frame that does not cannot
    be one of them. Only an upstream that spelled the key with JSON escapes
    could tell the difference, and its frame then reads as visible and is
    forwarded untagged -- the direction that forgoes a fallback, never the one
    that splices two answers together.
    """
    return not isinstance(chunk, str) or _ROUTING_KEY in chunk


def is_client_visible(chunk: Any) -> bool:
    """Return whether ``chunk`` carries anything the client will actually see.

    Mirrors the serving layer's own contract (``openai_chat_serializer.
    sanitize_chunk``): a frame is dropped before it reaches the client exactly
    when it carries ``_routing`` metadata, no ``choices`` and no ``usage``. That
    is the shape of the synthetic routing frame every backend emits first, and of
    the attempt-history frame the hybrid layer injects.

    Nothing else is treated as invisible, so anything unrecognised -- a
    keep-alive, a malformed frame, a byte payload -- counts as committed. Being
    wrong in that direction only forgoes a fallback; being wrong the other way
    would splice two answers into one stream.
    """
    if not isinstance(chunk, str) or not chunk.startswith("data: "):
        return True
    if not carries_routing(chunk):
        return True
    raw = chunk[6:].strip()
    if raw == "[DONE]":
        return True
    try:
        payload = json.loads(raw)
    except ValueError:
        return True
    if not isinstance(payload, dict):
        return True
    if "_routing" not in payload:
        return True
    return bool(payload.get("choices")) or payload.get("usage") is not None


def tag_backend_chunk(chunk: Any, backend_name: str) -> Any:
    """Record the serving backend in an SSE chunk's routing payload.

    Only ``data:`` frames that already carry a ``_routing`` object are touched;
    everything else -- keep-alives, content deltas, the terminal ``[DONE]`` --
    is forwarded byte-for-byte.
    """
    if not isinstance(chunk, str) or not chunk.startswith("data: "):
        return chunk
    if not carries_routing(chunk):
        return chunk
    raw = chunk[6:].strip()
    if raw == "[DONE]":
        return chunk
    try:
        payload = json.loads(raw)
    except ValueError:
        return chunk
    if not isinstance(payload, dict):
        return chunk
    routing = payload.get("_routing")
    if not isinstance(routing, dict):
        return chunk
    routing.setdefault("backend", backend_name)
    return f"data: {json.dumps(payload)}\n\n"
