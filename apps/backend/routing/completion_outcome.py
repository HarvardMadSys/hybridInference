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

__all__ = [
    "CompletionOutcome",
    "classify_completion_outcome",
    "classify_stream_prefix",
    "could_still_be_transient_prefix",
]


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
#: These are regex sources; ``_TRANSIENT_PREFIX_MARKERS`` below holds the same
#: notices as literal text for incremental (partial) stream matching.
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

#: The same notices as literal text, so a *partial* stream can be tested with
#: ``str.startswith`` instead of being matched against regex source (where the
#: syntax itself, ``",?"`` or ``"(loads|starts)"``, never appears in provider
#: output). Commas and alternatives are expanded one literal per form.
_TRANSIENT_PREFIX_MARKERS: tuple[str, ...] = (
    "the model is starting up",
    "model is starting up",
    "the model is warming up",
    "model is warming up",
    "warming up the model",
    "model is loading please wait",
    "model is loading, please wait",
    "loading the model please wait",
    "loading the model, please wait",
    "please wait while the model loads",
    "please wait while the model starts",
    "the model is not ready",
    "model is initializing",
)

#: Known, bounded ways a provider continues a warmup notice. Only these may
#: follow a marker; anything else is a genuine answer that happens to open with
#: a marker, and must be classified on its content.
_TRANSIENT_SUFFIX_PREFIXES: tuple[str, ...] = (
    "this takes about",
    "this will take about",
    "please wait",
    "please wait while the model loads",
    "please wait while the model starts",
    "please wait while the model initializes",
    "hold on",
    "try again",
    "try again soon",
    "try again later",
    "try again shortly",
    "please try again",
    "please try again soon",
    "please try again later",
    "please try again shortly",
    "give it another",
    "shortly",
    "yet",
    "now",
)

# Match a marker only at the beginning, and allow only a small set of known
# provider-notice tails. This avoids classifying a genuine answer as transient
# just because it quotes a warmup phrase in the middle or continues the phrase
# with free-form explanation.
# Possessive punctuation so the repeated suffix cannot backtrack into the
# punctuation run; atomic alternatives so overlapping words ("try again" /
# "please try again" / "shortly") are tried once, not re-partitioned on failure.
_NOTICE_PUNCT = r"[\s.!…,;:-]*+"
_NOTICE_ALTERNATIVES = (
    r"(?:"
    r"this (?:takes|will take) about \d{1,4} (?:seconds?|minutes?)"
    r"|please wait(?: while (?:the )?model (?:loads|starts|initializes))?"
    r"|hold on"
    r"|try again(?: (?:soon|later|shortly))?"
    r"|please try again(?: (?:soon|later|shortly))?"
    r"|give it another (?:second|\d{1,2} seconds?)"
    r"|shortly"
    r"|yet"
    r"|now"
    r")"
)
_NOTICE_SUFFIX = _NOTICE_PUNCT + r"(?:(?>" + _NOTICE_ALTERNATIVES + r")" + _NOTICE_PUNCT + r")*+$"

# A bounded provider notice is short. Anything longer is a genuine answer,
# not a notice tail, and must not be fed to the regex (it runs on the event
# loop via record_routing_observation).
_MAX_TRANSIENT_TEXT_LEN = 1000
_TRANSIENT_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(marker + _NOTICE_SUFFIX, re.IGNORECASE) for marker in _DEFAULT_TRANSIENT_MARKERS
)


def _looks_transient_text(text: str) -> bool:
    """Return whether *text* is a short response that starts with a warmup marker."""
    if len(text) > _MAX_TRANSIENT_TEXT_LEN:
        return False
    return any(marker.match(text) for marker in _TRANSIENT_MARKERS)


def _possible_notice_suffix(text: str) -> bool:
    """Return whether the text after a marker can still be a notice suffix."""
    suffix = text.strip(" \t.!…,:;-").lower()
    if not suffix:
        return True

    for phrase in _TRANSIENT_SUFFIX_PREFIXES:
        if phrase.startswith(suffix):
            return True
        if suffix.startswith(phrase):
            remainder = suffix[len(phrase) :].strip(" \t.!…,:;-")
            if not remainder:
                return True
            if any(
                next_phrase.startswith(remainder) or remainder.startswith(next_phrase)
                for next_phrase in _TRANSIENT_SUFFIX_PREFIXES
            ):
                return True

    # Duration suffixes have a bounded numeric component. Once their literal
    # introducer appears, wait for the terminal chunk to decide whether the
    # remaining text forms a valid duration or a substantive continuation.
    return suffix.startswith("this takes about") or suffix.startswith("this will take about")


def could_still_be_transient_prefix(content: str | None) -> bool:
    """Return whether *content* can still become a bounded warmup notice.

    Streaming callers must defer a success decision while this is true. The
    final response classifier remains authoritative once the stream terminates.
    Only the literal marker list is consulted, so a fragment is compared against
    provider wording and never against regex source.
    """
    raw = content or ""
    if len(raw) > _MAX_TRANSIENT_TEXT_LEN:
        return False
    normalized = raw.strip().lower()
    if not normalized:
        return True

    for marker in _TRANSIENT_PREFIX_MARKERS:
        if marker.startswith(normalized):
            return True
        if normalized.startswith(marker) and _possible_notice_suffix(normalized[len(marker) :]):
            return True
    return False


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

    text = content or ""

    # 1. Structured provider signal first: transient text beats everything else.
    if text and not text.isspace():
        # Warmup detection is bounded to short responses, so a large answer
        # never pays a full-body strip or a regex scan.
        if len(text) <= _MAX_TRANSIENT_TEXT_LEN and _looks_transient_text(text.strip()):
            return CompletionOutcome.TRANSIENT_NO_PROGRESS

        # 2. Genuine content.
        return CompletionOutcome.PROGRESS

    # 3. No content. If the provider reported output tokens, something was
    #    generated that we simply could not see (e.g. reasoning-only); do not
    #    call that empty.
    out_tokens = _output_token_count(usage)
    if out_tokens is not None and out_tokens > 0:
        return CompletionOutcome.PROGRESS

    return CompletionOutcome.EMPTY


def classify_stream_prefix(
    content: str | None,
    *,
    terminal: bool = False,
) -> CompletionOutcome | None:
    """Classify available stream text, returning ``None`` while ambiguous.

    A final answer may begin with the first few characters of a warmup marker.
    Until the prefix diverges from every known marker/suffix or the stream ends,
    that fragment is not enough evidence to close a provider breaker.
    """
    if not terminal and could_still_be_transient_prefix(content):
        return None
    return classify_completion_outcome(
        content=content,
        http_status=200,
        terminal=True,
    )
