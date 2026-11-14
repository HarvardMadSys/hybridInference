r"""Shared extraction helpers for completion response text and usage.

Every request surface -- FixedRouter, RouteWise, hedging and ``/v1/messages`` --
needs the same three things from a response: the generated text, the provider's
usage, and a verdict on whether a *stream* has produced real work yet. They live
here so the surfaces cannot drift, as ``routing/AGENTS.md`` asks.

The stream verdict is the delicate one. A provider answers a warmup notice one
token at a time, so the first delta is frequently a fragment of a notice ("The",
"\n\n", "Model") that a single classification would read as progress and use to
close a breaker the warmup failures had just opened. :class:`StreamProgressTracker`
re-evaluates the *accumulated* prefix as each chunk arrives and stays undecided
until that prefix can no longer become a warmup notice -- while still releasing a
half-open probe as soon as genuine output proves the endpoint is serving.
"""

from __future__ import annotations

import json
from typing import Any

from routing.completion_outcome import CompletionOutcome, classify_stream_prefix

__all__ = [
    "StreamProgressTracker",
    "accumulate_stream_prefix",
    "chunk_text",
    "response_text",
    "response_usage",
]

#: How much generated text a stream-prefix decision may look at. A warmup
#: notice is short and a genuine answer diverges from every marker within a few
#: characters, so a response that is still ambiguous never reaches this bound;
#: it only caps how much memory a pathological stream can pin.
_STREAM_PREFIX_MAX_CHARS = 512


def response_text(response: Any) -> str | None:
    """Extract generated content from a non-streaming response body.

    A tool call is represented by a non-empty sentinel so the classifier
    treats a structured call as real work even when it has no prose or usage.
    """
    if not isinstance(response, dict):
        return None
    choices = response.get("choices") or [{}]
    message = (choices[0] or {}).get("message") or {}
    content = (
        message.get("content")
        or message.get("reasoning_content")
        or message.get("reasoning")
        or message.get("thinking")
    )
    if isinstance(content, str) and content:
        return content
    if message.get("tool_calls") or message.get("function_call"):
        return "[tool_call]"
    return None


def response_usage(response: Any) -> dict[str, Any] | None:
    """Extract usage from a response body, or None when absent."""
    if not isinstance(response, dict):
        return None
    usage = response.get("usage")
    return usage if isinstance(usage, dict) else None


def chunk_text(chunk: Any) -> str | None:
    """Extract generated text from one stream chunk for outcome classification.

    Mirrors ``routing.streaming.has_non_empty_content``: text, reasoning and
    thinking deltas return their text; a structured call returns the same
    ``[tool_call]`` sentinel :func:`response_text` uses.

    Nothing is cached. A process-wide cache of raw response chunks -- the shape
    an earlier revision used to avoid re-parsing -- would hold other requests'
    generated output in memory long after they were served, for no benefit a
    bounded incremental prefix does not already give.
    """
    if not isinstance(chunk, (str, bytes)):
        return None
    text = chunk.decode("utf-8", errors="surrogatepass") if isinstance(chunk, bytes) else chunk
    prefix = "data: "
    if not text.startswith(prefix):
        return None
    payload = text[len(prefix) :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    choices = obj.get("choices") or []
    if not choices:
        return None
    delta = (choices[0] or {}).get("delta") or {}
    content = (
        delta.get("content")
        or delta.get("reasoning_content")
        or delta.get("reasoning")
        or delta.get("thinking")
    )
    if isinstance(content, str) and content:
        return content
    if delta.get("tool_calls") or delta.get("function_call"):
        return "[tool_call]"
    return None


class StreamProgressTracker:
    """Decide, chunk by chunk, whether a streamed response proves real work.

    ``observe`` folds one chunk's generated text into a bounded running prefix
    and returns ``PROGRESS`` the moment that prefix can no longer be a warmup
    notice. While the prefix is still ambiguous it returns ``None``, and
    :meth:`finish` gives the terminal verdict when the stream ends.

    The prefix is built incrementally, so each chunk costs one extraction and
    one classification over at most ``max_chars`` characters: neither the CPU
    nor the memory cost grows with the number of chunks. Leading whitespace is
    not accumulated at all -- a runaway whitespace stream (JSON-mode grammars
    emit them) costs one extraction per chunk and never hides the real content
    that follows it.
    """

    __slots__ = ("_cap", "_outcome", "_saw_content", "_text")

    def __init__(self, max_chars: int = _STREAM_PREFIX_MAX_CHARS) -> None:
        self._cap = max(1, max_chars)
        self._text = ""
        self._saw_content = False
        self._outcome: CompletionOutcome | None = None

    @property
    def decided(self) -> bool:
        """Return whether the verdict is already known."""
        return self._outcome is not None

    @property
    def saw_content(self) -> bool:
        """Return whether any non-whitespace generated text has arrived."""
        return self._saw_content

    def observe(self, text: str | None) -> CompletionOutcome | None:
        """Fold one chunk's generated text in; return PROGRESS once proven.

        Returns ``None`` while the accumulated prefix could still be a warmup
        notice -- the caller must keep the attempt undecided and its half-open
        probe claimed. Once decided, later chunks are ignored.
        """
        if self._outcome is not None:
            return self._outcome
        if not text:
            return None
        if not self._saw_content:
            if not text.strip():
                # Leading whitespace is not evidence either way, and keeping it
                # would let a whitespace flood fill the prefix until the real
                # content no longer fits.
                return None
            self._saw_content = True
        room = self._cap - len(self._text)
        if room > 0:
            self._text += text[:room]
        outcome = classify_stream_prefix(self._text)
        if outcome is not None and outcome.admits_real_work:
            self._outcome = outcome
        return self._outcome

    def finish(self) -> CompletionOutcome | None:
        """Return the terminal verdict for the accumulated prefix.

        ``None`` means nothing classifiable ever arrived (an empty or
        whitespace-only stream), which is not success evidence.
        """
        if self._outcome is not None:
            return self._outcome
        if not self._saw_content:
            return None
        self._outcome = classify_stream_prefix(self._text, terminal=True)
        return self._outcome


def accumulate_stream_prefix(
    chunks: list[Any],
    max_chars: int = _STREAM_PREFIX_MAX_CHARS,
) -> str | None:
    """Accumulate generated text from the first content-bearing stream chunks.

    A warmup notice can be split across multiple SSE chunks. Classifying the
    first content-bearing chunk alone would read "The" as PROGRESS before the
    remaining text reveals the response is a warmup notice. This returns the
    concatenated text from the chunks seen so far, bounded to avoid unbounded
    accumulation, so the classifier can decide from the response prefix.

    Callers that see one chunk at a time should use
    :class:`StreamProgressTracker` instead: this function re-reads the whole
    list, which is quadratic in the number of chunks.
    """
    parts: list[str] = []
    total = 0
    for chunk in chunks:
        text = chunk_text(chunk)
        if text:
            room = max_chars - total
            parts.append(text[:room])
            total += min(len(text), room)
            if total >= max_chars:
                break
    if not parts:
        return None
    return "".join(parts)
