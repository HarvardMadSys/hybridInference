"""What this gateway tells the admission process beside a self-hosted engine.

A route may declare that a Nimbus router in admission-only mode answers at its
address, in the engine's place (``ModelConfig.admission``). That process decides
whether the engine can serve a request within its first-token budget, and to
decide it needs two things only this gateway knows: how soon the model's cloud
routes would answer instead, and how long the request has already been here. It
also records the caller's session and this gateway's id for the request, so the
two sides' records line up.

The router that sends to such a route pushes the request's terms around the
dispatch (:func:`admission_context`), and the adapter turns them into headers as
it sends (:func:`admission_headers`). The time the request has been held is read
when the headers are built, from the moment ``RequestIdMiddleware`` saw the
request, so it counts everything this gateway did first. It is a duration, not a
time, so the two machines' clocks need not agree.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from serving.utils import context as req_ctx

__all__ = [
    "ELAPSED_HEADER",
    "REMOTE_TTFT_HEADER",
    "REQUEST_ID_HEADER",
    "SESSION_HEADER",
    "AdmissionTerms",
    "admission_context",
    "admission_headers",
]

#: The cloud's first-token time for this request, in milliseconds; ``inf`` when no
#: cloud route can take it, which leaves the request no cutoff.
REMOTE_TTFT_HEADER = "X-Nimbus-Remote-TTFT-Ms"
#: How long this gateway held the request before sending it, in milliseconds.
ELAPSED_HEADER = "X-Nimbus-Elapsed-Ms"
#: This gateway's id for the request, which the process keeps in its record.
REQUEST_ID_HEADER = "X-Nimbus-Request-Id"
#: The caller's session, which the process reads as the request's own.
SESSION_HEADER = "X-Session-Id"

# Longer values are left out rather than cut: a cut id matches nothing.
_MAX_HEADER_TEXT = 128


@dataclass(frozen=True, slots=True)
class AdmissionTerms:
    """One request's terms for the admission process that may take it.

    Attributes:
        remote_ttft_ms: The cloud's first-token time for the request, in
            milliseconds, or ``math.inf`` when no cloud route can take it.
        session_id: The caller's session, or None.
        request_id: This gateway's id for the request, or None.
    """

    remote_ttft_ms: float
    session_id: str | None = None
    request_id: str | None = None


def admission_context(terms: AdmissionTerms) -> dict[str, Any]:
    """Return the request-context entry that carries ``terms`` to the adapter.

    Push it around the dispatch with ``req_ctx.push(**...)``, as the queue terms
    are (``upstream_limiter.dispatch_queue_context``).
    """
    return {req_ctx.UPSTREAM_ADMISSION: terms}


def _milliseconds(value: float) -> str:
    return "inf" if math.isinf(value) else f"{value:.3f}"


def _header_text(value: object) -> str | None:
    """Return ``value`` as a header may carry it, or None.

    Printable ASCII of a bounded length: an id is the caller's own text, and a
    header that failed to encode would fail the request it only describes.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_HEADER_TEXT or not (text.isascii() and text.isprintable()):
        return None
    return text


def admission_headers(*, now: float | None = None) -> dict[str, str]:
    """Return the headers that tell an admission process about the dispatch being sent.

    The held time whenever this gateway knows when the request arrived, and the
    terms of the router that sends the dispatch. A dispatch no router described
    -- a pinned request the shared router runs, a model on another strategy --
    has nobody to take it to the cloud on a decline, so the process is told
    there is no cloud time: left to its own profile's provider line it would
    decline requests at a cutoff nothing waits behind.

    Args:
        now: The ``time.monotonic()`` time the dispatch is sent; read here when
            not given.
    """
    context = req_ctx.get()
    headers: dict[str, str] = {}
    received = context.get(req_ctx.REQUEST_RECEIVED)
    if isinstance(received, (int, float)) and not isinstance(received, bool):
        sent = time.monotonic() if now is None else now
        headers[ELAPSED_HEADER] = _milliseconds(max(0.0, (sent - received) * 1000.0))
    terms = context.get(req_ctx.UPSTREAM_ADMISSION)
    if not isinstance(terms, AdmissionTerms):
        headers[REMOTE_TTFT_HEADER] = _milliseconds(math.inf)
        return headers
    if not math.isnan(terms.remote_ttft_ms) and terms.remote_ttft_ms >= 0:
        headers[REMOTE_TTFT_HEADER] = _milliseconds(terms.remote_ttft_ms)
    for name, value in (
        (SESSION_HEADER, terms.session_id),
        (REQUEST_ID_HEADER, terms.request_id),
    ):
        text = _header_text(value)
        if text is not None:
            headers[name] = text
    return headers
