"""Completion outcome classification.

Why this exists
---------------
A transport-level success is not evidence that an agent made progress. A
provider can answer HTTP 200 with a perfectly well-formed terminal event whose
entire output is a warmup notice::

    "The model is starting up - this takes about 120 seconds. Please wait..."

That response contains *text*, so any "did we get content?" test scores it as a
success. When such a success is allowed to update prefix-cache locality state,
a transient provider condition can permanently damage the router's model of
where the warm prefix lives.

This module separates two questions that were previously conflated:

* **transport success** -- did the HTTP request complete? (``bool``)
* **semantic outcome**  -- did this response constitute real work?

``CompletionOutcome`` is the second. It is deliberately provider-neutral and
carries no routing policy; consumers decide what each outcome means for them.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

__all__ = ["CompletionOutcome", "classify_completion_outcome"]


class CompletionOutcome(StrEnum):
    """What a completed request actually represents."""

    #: Real assistant output was produced.
    PROGRESS = "progress"
    #: The objective was demonstrably finished.
    COMPLETE = "complete"
    #: Provider is not ready (warming up, cold replica, queued). Transport
    #: succeeded but no inference work happened.
    TRANSIENT_NO_PROGRESS = "transient_no_progress"
    #: Success, but no assistant-visible output.
    EMPTY = "empty"
    #: Same non-progressing response as a recent prior attempt.
    REPEATED_NOOP = "repeated_noop"
    #: Structured provider/transport failure.
    PROVIDER_ERROR = "provider_error"
    #: The request was abandoned or cancelled by the client.
    ABORTED = "aborted"
    #: Could not be determined. Must never be treated as positive evidence.
    UNKNOWN = "unknown"

    @property
    def admits_real_work(self) -> bool:
        """Whether this outcome may be used as evidence that real work happened.

        Only ``PROGRESS`` and ``COMPLETE`` qualify. Everything else -- including
        ``UNKNOWN`` -- must leave positive evidence untouched, because an
        indeterminate observation is not grounds for discarding what we already
        know.
        """
        return self in (CompletionOutcome.PROGRESS, CompletionOutcome.COMPLETE)

    @classmethod
    def from_response(
        cls,
        *,
        content: str | None,
        usage: dict[str, Any] | None,
        http_status: int | None,
        terminal: bool,
    ) -> CompletionOutcome:
        """Classify a single response.

        ``usage`` is the provider's own reported usage mapping, when available.
        ``http_status`` of None means no HTTP response was obtained.
        """
        return classify_completion_outcome(
            content=content,
            usage=usage,
            http_status=http_status,
            terminal=terminal,
        )


#: Provider warmup / placeholder notices recognized by the shared classifier.
_DEFAULT_TRANSIENT_MARKERS: tuple[str, ...] = (
    r"the model is starting up",
    r"model is starting up",
    r"the model is warming up",
    r"model is warming up",
    r"warming up the model",
    r"model is loading,? please wait",
    r"loading the model,? please wait",
    r"please wait while the model (loads|starts)",
    r"the model is not ready",
    r"model is initializing",
)

# Match a marker only at the beginning, and allow only a small set of known
# provider-notice tails. This avoids classifying a genuine answer as transient
# just because it quotes a warmup phrase in the middle or continues the phrase
# with free-form explanation.
_NOTICE_SUFFIX = (
    # Keep the punctuation fallback outside the repeated group. Repeating an
    # optional group let caller-controlled punctuation trigger exponential
    # backtracking in the event loop.
    r"[\s.!\u2026,;:-]*"  # punctuation / spacing
    r"(?:"
    r"(?:"
    r"this (?:takes|will take) about \d{1,4} (?:seconds?|minutes?)"
    r"|please wait(?: while (?:the )?model (?:loads|starts|initializes))?"
    r"|hold on"
    r"|try again(?: (?:soon|later|shortly))?"
    r"|please try again(?: (?:soon|later|shortly))?"
    r"|shortly"
    r"|yet"
    r"|now"
    r")[\s.!\u2026,;:-]*"
    r")*$"
)
_TRANSIENT_MARKERS: list[re.Pattern[str]] = [
    re.compile(pattern + _NOTICE_SUFFIX, re.IGNORECASE) for pattern in _DEFAULT_TRANSIENT_MARKERS
]


def _looks_transient_text(text: str) -> bool:
    return any(marker.match(text) for marker in _TRANSIENT_MARKERS)


def _output_token_count(usage: dict[str, Any] | None) -> int | None:
    if not isinstance(usage, dict):
        return None
    for key in ("output_tokens", "completion_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def classify_completion_outcome(
    *,
    content: str | None,
    usage: dict[str, Any] | None = None,
    http_status: int | None = None,
    terminal: bool = True,
) -> CompletionOutcome:
    """Decide what a response represents.

    Order matters: a structured provider signal outranks text, and text
    outranks the mere presence of output tokens. A response that says "the model
    is starting up" is transient *even though* it has text and reports output
    tokens -- that combination is precisely the incident signature.
    """
    if http_status is not None and http_status >= 400:
        return CompletionOutcome.PROVIDER_ERROR
    if http_status is None:
        return CompletionOutcome.UNKNOWN
    if not terminal:
        return CompletionOutcome.ABORTED

    text = (content or "").strip()

    # 1. Structured provider signal first: transient text beats everything else.
    if text and _looks_transient_text(text):
        return CompletionOutcome.TRANSIENT_NO_PROGRESS

    # 2. Genuine content.
    if text:
        return CompletionOutcome.PROGRESS

    # 3. No content. If the provider reported output tokens, something was
    #    generated that we simply could not see (e.g. reasoning-only); do not
    #    call that empty.
    out_tokens = _output_token_count(usage)
    if out_tokens is not None and out_tokens > 0:
        return CompletionOutcome.PROGRESS

    return CompletionOutcome.EMPTY
