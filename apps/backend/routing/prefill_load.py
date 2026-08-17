"""In-flight prefill accounting so routing can steer around busy endpoints.

Weighted-random selection balances *request counts*, which is the wrong unit
for a prefill-bound deployment. One 700k-token cache-miss prompt occupies a
replica for minutes while a 500-token prompt costs milliseconds, yet both count
as "one request". The result is head-of-line blocking: small interactive
requests queue behind a mega-prefill on the replica that happens to be holding
it, while sibling replicas sit idle.

What is tracked is *un-cached* prefill. Charging full prompt size would be
actively harmful: the fleet runs above 90% prefix-cache hit, so a warm 500k
continuation would report its endpoint as blocked when it is really about to
prefill a few thousand delta tokens, and every diversion that followed would
land on a cold endpoint and manufacture a real cache miss. The cached prefix is
estimated from the caller's last *completed* prompt on that endpoint -- agentic prompts
grow monotonically, so the growth is the un-cached part -- which costs one dict
lookup rather than the tokenization a true prefix match would need.

This module tracks, per endpoint, how many un-cached prompt tokens are currently
in prefill, and exposes that as a selection signal:

- :func:`estimate_prefill_tokens` -- a deliberately cheap prompt-size estimate.
- :class:`PrefillLoadTracker` -- in-flight token accounting per endpoint, plus
  the "elephant" (very large prefill) counters used for isolation.
- :meth:`PrefillLoadTracker.select_index` -- power-of-two-choices selection that
  keeps configured weights meaningful while avoiding the hot endpoint.

The tracker measures *prefill*, not whole requests: a lease is released once the
first token arrives, because at that point the prompt is resident and the
endpoint is decoding rather than prefilling. Holding the lease for the whole
stream would let a long, cheap decode masquerade as prefill pressure.

Accounting only: this module never blocks, rejects, or queues a request. The
worst it does is prefer a different endpoint that is already admissible.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from serving.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = get_logger(__name__)


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int from the environment, falling back on bad input.

    Args:
        name: Environment variable name.
        default: Value to use when unset, unparseable, or negative.

    Returns:
        The configured integer, or ``default``.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using %d", name, raw, default)
        return default
    if value < 0:
        logger.warning("negative %s=%d; using %d", name, value, default)
        return default
    return value


# Default-on with an env kill switch, matching ROUTING_AFFINITY_ENABLED.
PREFILL_AWARE_ENABLED: bool = os.environ.get("ROUTING_PREFILL_AWARE_ENABLED", "1") != "0"

# A prompt at or above this many estimated tokens is an "elephant": large enough
# that its prefill alone can stall an endpoint for other callers. 200k is drawn
# from the production distribution, where p95 prompt size sits far below it and
# the requests that caused multi-minute TTFT sat well above.
ELEPHANT_TOKENS: int = _env_int("ROUTING_PREFILL_ELEPHANT_TOKENS", 200_000)

# How many elephants one endpoint may prefill concurrently. Two stacked
# mega-prefills are what turns a slow request into a timed-out one, so the
# default keeps them serialized across replicas rather than piled onto one.
ELEPHANT_LIMIT: int = _env_int("ROUTING_PREFILL_ELEPHANT_LIMIT", 1)

# Backlog at which the load tie-break starts overriding a weighted draw. Below
# this an endpoint is merely busy, not blocked, and route weights (which encode
# cost and provider preference, not just capacity) should keep deciding. Sized
# well above an ordinary prompt so normal traffic never trips it.
INTERVENE_TOKENS: int = _env_int("ROUTING_PREFILL_INTERVENE_TOKENS", 50_000)

# Session affinity is a cache-locality optimization, not a correctness
# guarantee. Above this in-flight backlog the pin costs more (queueing behind a
# mega-prefill) than the prefix-cache hit it buys, so selection is re-run.
AFFINITY_BACKLOG_CEILING: int = _env_int("ROUTING_PREFILL_AFFINITY_CEILING", 150_000)

# Scheduling priority stamped on requests to endpoints that run sglang priority
# scheduling. sglang schedules the *higher* integer first and preempts a running
# request only once the gap reaches its --priority-scheduling-preemption-threshold
# (default 10), so the spacing between these three values *is* the policy:
#
#   interactive - elephant = 20  >= 10  -> an arriving small prompt can retract a
#                                          mega-prefill that already holds the GPU
#   interactive - large     =  5  <  10  -> ordinary prompts only queue ahead of a
#                                          large one, never preempt it
#   interactive - interactive = 0        -> peers never preempt each other, so
#                                          normal traffic sees no retraction churn
#
# Retraction is not free even though the radix cache keeps the prefix, which is
# why only the elephant tier is exposed to it: it is the one tier whose prefill
# is long enough that waiting it out is worse than restarting it.
PRIORITY_INTERACTIVE: int = _env_int("ROUTING_PRIORITY_INTERACTIVE", 20)
PRIORITY_LARGE: int = _env_int("ROUTING_PRIORITY_LARGE", 15)
PRIORITY_ELEPHANT: int = _env_int("ROUTING_PRIORITY_ELEPHANT", 0)

# Bounds on the per-(caller, endpoint) prompt-size memory used to discount a
# warm continuation. One small int per live conversation; TTL matches the
# radix cache's useful lifetime closely enough that a stale hint just means we
# briefly over-discount one request.
_PREFIX_HINT_TTL_SEC: float = 2 * 3600.0
_PREFIX_HINT_MAX_ENTRIES: int = 50_000

# Chars per token for the cheap estimator. Deliberately coarse -- see
# estimate_prefill_tokens for why precision is not worth the CPU here.
_CHARS_PER_TOKEN: int = 4

# Flat costs for non-text blocks, mirroring serving.utils.tokens so a base64
# image is never char-counted as a colossal text prompt.
_IMAGE_TOKENS: int = 85
_AUDIO_TOKENS: int = 200

# How much of each message identifies the conversation it belongs to, and how
# many messages deep to look for the opening turn. A per-message budget rather
# than one budget over the whole head: a coding agent's system prompt runs well
# past any single-slice bound, and a shared system prompt that swallowed the
# budget would fingerprint every one of that agent's conversations identically
# -- which is the collision this exists to prevent.
_FINGERPRINT_CHARS_PER_MESSAGE: int = 2048
_FINGERPRINT_MAX_MESSAGES: int = 4

# Width of the anchor window -- the slice of the remembered prompt's tail that a
# later prompt must reproduce, at the same offset, to be treated as extending it
# rather than forking from it. Wide enough that matching it by accident is not a
# concern, narrow enough to hash on every request.
_ANCHOR_CHARS: int = 2048

# Sentinel for "take all the text" when slicing a message for the anchor walk.
_ANCHOR_TEXT_UNBOUNDED: int = 1 << 62


def _content_chars(content: Any) -> int:
    """Return an approximate character count for one message's content.

    Args:
        content: A message ``content`` field: a string, a list of multimodal
            blocks, or None.

    Returns:
        Character count for text, with flat per-modality substitutes (scaled to
        chars) for image/audio blocks.
    """
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, str):
                total += len(block)
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("image", "image_url"):
                total += _IMAGE_TOKENS * _CHARS_PER_TOKEN
            elif btype in ("audio", "input_audio"):
                total += _AUDIO_TOKENS * _CHARS_PER_TOKEN
            else:
                text = block.get("text")
                if isinstance(text, str):
                    total += len(text)
        return total
    return len(str(content))


def estimate_prefill_tokens(
    messages: Sequence[dict[str, Any]] | None,
    *,
    tools: Any = None,
    response_format: Any = None,
) -> int:
    """Estimate prompt size in tokens, cheaply enough for the routing hot path.

    Uses a character heuristic rather than ``serving.utils.tokens``: real
    tokenization of a 700k-token prompt means pushing megabytes of text through
    tiktoken on every request, and this value only has to be good enough to rank
    endpoints and recognize an elephant. Being off by 20% changes nothing about
    which endpoint wins; spending 100ms of CPU to route would.

    Everything the adapter forwards counts, not just ``content``. Tool
    definitions and a structured-output schema are serialized into the same
    upstream body, and inside each message ``_clean_message`` preserves every
    non-None field -- so an assistant turn carrying ``tool_calls`` with large
    ``arguments``, or ``reasoning_content``, is prompt-bearing even when its
    ``content`` is null. An agentic history of tool calls, or a large MCP
    catalog, therefore imposes far more prefill than its visible turn suggests,
    and sizing on message content alone would rank it as though it had not.

    Args:
        messages: OpenAI-style messages, or None.
        tools: Tool definitions from the request, if any.
        response_format: Structured-output spec from the request, if any.

    Returns:
        Estimated prompt tokens (never negative).
    """
    chars = 0
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        chars += _content_chars(message.get("content")) + _message_extra_chars(message)
    chars += _serialized_chars(tools) + _serialized_chars(response_format)
    return chars // _CHARS_PER_TOKEN


def _prompt_text(messages: Sequence[dict[str, Any]] | None) -> list[str]:
    """Return the prompt's text pieces, in the order the upstream sees them.

    Binary blocks are skipped rather than substituted: their flat token cost is
    a sizing convenience and has no stable textual form, so including it would
    make the anchor below disagree with itself between two identical prompts.
    """
    pieces: list[str] = []
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        text = _content_text(message.get("content"), _ANCHOR_TEXT_UNBOUNDED)
        if text:
            pieces.append(text)
        extra = {k: v for k, v in message.items() if k not in ("role", "content") and v is not None}
        if extra:
            with contextlib.suppress(TypeError, ValueError):
                pieces.append(json.dumps(extra, sort_keys=True))
    return pieces


def prompt_anchor(messages: Sequence[dict[str, Any]] | None) -> tuple[int, str] | None:
    """Return (text length, digest of the final ``_ANCHOR_CHARS``) for a prompt.

    Stored with the prefix hint and re-checked on the next turn. A continuation
    reproduces the remembered prompt exactly and then appends, so the same
    window at the same offset still hashes the same; a fork or retry that shares
    only the opening -- same system prompt, same first user turn, diverging
    later -- does not, and is charged in full instead of subtracting a prefix
    the endpoint never cached past the fork point.

    This is what makes the discount evidence-based rather than assumed. The
    conversation fingerprint says "the same conversation started here"; the
    anchor says "and this prompt really contains the one we measured".

    Returns:
        The pair, or None for a prompt with no anchorable text.
    """
    pieces = _prompt_text(messages)
    if not pieces:
        return None
    joined = "".join(pieces)
    if not joined:
        return None
    window = joined[-_ANCHOR_CHARS:]
    digest = hashlib.blake2b(window.encode("utf-8", "ignore"), digest_size=8).hexdigest()
    return len(joined), digest


def _anchor_holds(messages: Sequence[dict[str, Any]] | None, anchor: tuple[int, str]) -> bool:
    """Return True when this prompt reproduces ``anchor`` at the same offset."""
    length, digest = anchor
    if length <= 0:
        return False
    joined = "".join(_prompt_text(messages))
    if len(joined) < length:
        # Shorter than what was measured: cannot be an extension of it.
        return False
    window = joined[max(length - _ANCHOR_CHARS, 0) : length]
    return hashlib.blake2b(window.encode("utf-8", "ignore"), digest_size=8).hexdigest() == digest


def _message_extra_chars(message: dict[str, Any]) -> int:
    """Return the char cost of a message's non-content fields.

    ``role`` is a word and ``content`` is counted separately by the caller;
    everything else the adapter preserves -- ``tool_calls`` and their serialized
    ``arguments``, ``reasoning_content``, ``name``, ``tool_call_id`` -- is prompt
    text the upstream prefills like any other.
    """
    extra = {k: v for k, v in message.items() if k not in ("role", "content") and v is not None}
    return _serialized_chars(extra)


def _serialized_chars(value: Any) -> int:
    """Return the char cost of a non-message prompt field (tools, schemas).

    These reach the upstream as JSON, so their serialized length is what the
    model prefills. Sized with ``json.dumps`` rather than walked structurally
    because the shape is arbitrary and the objects are small next to the prompts
    this module exists to measure; anything unserializable is skipped rather
    than guessed at.
    """
    if not value:
        return 0
    try:
        return len(json.dumps(value))
    except (TypeError, ValueError):
        return 0


def _content_text(content: Any, limit: int) -> str:
    """Return up to ``limit`` chars of a message's text, ignoring binary blocks.

    Only text identifies a conversation here: image and audio blocks are skipped
    rather than summarized, because a base64 payload would dominate the slice
    and two different prompts carrying the same attachment would collide.
    """
    if limit <= 0:
        return ""
    if isinstance(content, str):
        return content[:limit]
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    remaining = limit
    for block in content:
        if remaining <= 0:
            break
        text = ""
        if isinstance(block, str):
            text = block
        elif isinstance(block, dict) and block.get("type") not in (
            "image",
            "image_url",
            "audio",
            "input_audio",
        ):
            value = block.get("text")
            text = value if isinstance(value, str) else ""
        if not text:
            continue
        parts.append(text[:remaining])
        remaining -= len(parts[-1])
    return "".join(parts)


def conversation_fingerprint(
    messages: Sequence[dict[str, Any]] | None,
    *,
    tools: Any = None,
    response_format: Any = None,
) -> str | None:
    """Identify *which* conversation a prompt belongs to, cheaply.

    The warm-continuation discount keys on the caller, and a caller is not a
    conversation: one API key, grant, or NAT'd IP sends many. Without an
    identity, a caller who follows a 500k-token conversation with an unrelated
    500k-token one has the second discounted to nothing -- harmless as a routing
    hint, but as a *priority* it hands a genuinely cold mega-prefill the
    interactive tier, letting it jump the queue and retract a real elephant.

    What identifies a conversation is its *opening*: the leading system messages
    plus the first user turn. Both are fixed for the life of an append-only
    conversation -- later turns are appended after them -- so a continuation
    hashes identically from turn two onwards, while a different conversation
    from the same caller differs in its first user message and does not.

    Each message contributes at most ``_FINGERPRINT_CHARS_PER_MESSAGE``, and at
    most ``_FINGERPRINT_MAX_MESSAGES`` are read. The per-message budget is the
    load-bearing part: one budget spread over the head would be swallowed whole
    by a coding agent's system prompt, which runs to tens of KB, and every
    conversation that agent ever sends would then fingerprint the same -- the
    exact collision this exists to prevent. Roles are hashed alongside the text
    so a message boundary cannot be forged by concatenation.

    Two conversations that share both a system prompt *and* a first user turn
    still collide, and that is the intended limit: they share a genuine prefix
    of that length, which really is resident in the endpoint's radix cache.

    Tool definitions and the output schema are part of the identity too, because
    they are part of the *prefix*: the chat template renders them ahead of the
    conversation, so swapping one tool catalog for another of the same size
    invalidates the cache from that point while leaving both the messages and
    the token total unchanged. Hashed whole rather than sliced -- a change
    anywhere in them breaks the prefix, so it must break the digest.

    Args:
        messages: OpenAI-style messages, or None.
        tools: Tool definitions from the request, if any.
        response_format: Structured-output spec from the request, if any.

    Returns:
        A short hex digest, or None when there is nothing to fingerprint (a
        pure-image first turn with no tools), which callers must read as "cannot
        vouch for this" rather than as a match.
    """
    head: list[str] = []
    for label, value in (("tools", tools), ("schema", response_format)):
        # Unserializable: contributes nothing rather than a fake identity.
        with contextlib.suppress(TypeError, ValueError):
            if value:
                head.append(f"{label}:{json.dumps(value, sort_keys=True)}")
    seen_user = False
    for message in (messages or ())[:_FINGERPRINT_MAX_MESSAGES]:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        text = _content_text(message.get("content"), _FINGERPRINT_CHARS_PER_MESSAGE)
        if text:
            head.append(f"{role}:{text}")
        # The first user turn is the discriminator; nothing after it adds
        # identity, and reading further would make the digest move as the
        # conversation grows.
        if role == "user":
            seen_user = True
        if seen_user:
            break
    if not head:
        return None
    return hashlib.blake2b("\x00".join(head).encode("utf-8", "ignore"), digest_size=8).hexdigest()


def priority_for_prefill(tokens: int) -> int:
    """Map an estimated prompt size to an upstream scheduling priority.

    The gateway is the only authority on this value -- a client-supplied
    ``priority`` never reaches an upstream, because the adapters forward a
    whitelist of sampling params and this is not one of them. Which is the
    point: priority is a claim about the *cost* a request imposes on the shared
    replica, and no caller is disinterested about its own.

    The tiers reuse the thresholds selection already runs on, so one prompt is
    never an elephant for routing and an ordinary request for scheduling:

    - at or above :data:`ELEPHANT_TOKENS` -- a prefill long enough to stall the
      replica for everyone else; scheduled last and preemptible.
    - at or above :data:`INTERVENE_TOKENS` -- large enough to matter, not large
      enough to be worth retracting once it has started.
    - below both -- interactive traffic, which is what this exists to protect.

    Size is the only input on purpose. Deriving it from the caller (paying tier,
    API key) would make it a fairness lever rather than a scheduling one, and the
    request that suffers most from a mega-prefill is usually another caller's.

    Args:
        tokens: Estimated un-cached prompt tokens, from
            :func:`estimate_prefill_tokens`.

    Returns:
        The priority integer to stamp on the upstream request body.
    """
    if tokens >= ELEPHANT_TOKENS:
        return PRIORITY_ELEPHANT
    if tokens >= INTERVENE_TOKENS:
        return PRIORITY_LARGE
    return PRIORITY_INTERACTIVE


@dataclass(frozen=True)
class _PrefixHint:
    """One caller's last prompt on one endpoint, for warm-continuation discounting.

    Attributes:
        tokens: That prompt's estimated total size.
        expires_at: Clock value past which the radix cache is assumed cold.
        fingerprint: Which conversation it was, or None when it could not be
            fingerprinted. Consulted only by callers that pass one, so routing
            keeps the caller-scoped discount it was built with while scheduling
            priority -- where a wrong discount preempts real work rather than
            merely skewing a load estimate -- requires the match.
        anchor: That prompt's length and tail digest, so a later turn can be
            shown to *contain* it rather than merely to share its opening.
    """

    tokens: int
    expires_at: float
    fingerprint: str | None = None
    anchor: tuple[int, str] | None = None


@dataclass
class PrefillLease:
    """A claim on one endpoint's prefill budget, released once prefill ends.

    Attributes:
        endpoint_id: Endpoint the tokens were charged to.
        tokens: Estimated *un-cached* prefill tokens charged.
        elephant: Whether this lease counted against the elephant limit.
        affinity_key: Caller this load belongs to, so a caller's own in-flight
            work can be excluded when testing its affinity ceiling.
        released: Set once the lease has been returned; makes release idempotent
            so the streaming path can release on first token and again in a
            ``finally`` without double-crediting.
        prompt_tokens: This request's estimated *total* prompt size, written to
            the prefix hint only once prefill is confirmed complete.
        fingerprint: Which conversation the prompt belongs to, stored with that
            hint so a later turn can prove it is the same one.
        anchor: This prompt's length and tail digest, stored with that hint so a
            later turn can prove it contains this one.
    """

    endpoint_id: str
    tokens: int
    elephant: bool
    affinity_key: str | None = None
    released: bool = False
    prompt_tokens: int = 0
    fingerprint: str | None = None
    anchor: tuple[int, str] | None = None


class PrefillLoadTracker:
    """Per-endpoint in-flight prefill accounting and load-aware selection.

    Thread safety: all mutable state is guarded by a single lock. Critical
    sections do no I/O and hold no awaits, matching the router-owned lock
    pattern used elsewhere in ``routing/``.

    What is counted is *un-cached* prefill, not prompt size. Nearly all large
    prompts here are warm continuations -- the production fleet runs above 90%
    prefix-cache hit -- so charging full prompt length would report an endpoint
    as blocked when it is about to prefill a few thousand delta tokens, and the
    diversions that followed would each land on a cold endpoint and manufacture
    the very cache misses this exists to prevent.

    Args:
        elephant_tokens: Un-cached prefill at which a request counts as an
            elephant.
        elephant_limit: Concurrent elephants permitted per endpoint.
        clock: Monotonic time source, injectable for tests.
    """

    def __init__(
        self,
        *,
        elephant_tokens: int = ELEPHANT_TOKENS,
        elephant_limit: int = ELEPHANT_LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._backlog: dict[str, int] = {}
        self._elephants: dict[str, int] = {}
        # Load attributed to one caller on one endpoint, so a caller's own work
        # never counts against its own affinity ceiling.
        self._by_caller: dict[tuple[str, str], int] = {}
        # (affinity_key, endpoint_id) -> (last prompt tokens, expiry).
        self._prefix_hints: OrderedDict[tuple[str, str], _PrefixHint] = OrderedDict()
        self._elephant_tokens = max(int(elephant_tokens), 1)
        self._elephant_limit = max(int(elephant_limit), 1)
        self._clock = clock

    def uncached_estimate(
        self,
        endpoint_id: str,
        tokens: int,
        affinity_key: str | None = None,
        *,
        fingerprint: str | None = None,
        messages: Sequence[dict[str, Any]] | None = None,
    ) -> int:
        """Estimate what this endpoint must actually prefill for this prompt.

        Agentic prompts grow monotonically: turn N+1 is turn N plus a tool
        result. So for a caller that already sent a prompt to this endpoint,
        the un-cached remainder is roughly the growth since then, and the rest
        is resident in the endpoint's radix cache. A caller with no history on
        the endpoint gets charged in full, which is correct -- that is exactly
        the cold prefill that blocks a replica, and it makes moving a warm
        session look as expensive as it really is.

        Deliberately a remembered integer rather than block hashing: this runs
        on the routing path, and the tokenization a real prefix match needs is
        the cost this whole module is written to avoid.

        Two optional checks tighten this for callers that cannot afford a wrong
        answer. ``fingerprint`` requires the remembered turn to belong to the
        same conversation; ``messages`` additionally requires this prompt to
        *contain* the remembered one, verified against its stored anchor. A fork
        or retry sharing only the opening passes the first and fails the second,
        which is the difference between subtracting a cached prefix and
        subtracting one that diverged.

        Args:
            endpoint_id: Candidate endpoint.
            tokens: Estimated total prompt tokens.
            affinity_key: Caller identity, or None when unknown.
            fingerprint: Conversation identity to require a match on.
            messages: This request's messages, to verify the stored anchor.

        Returns:
            Estimated un-cached prefill tokens, never negative.
        """
        total = max(int(tokens), 0)
        if affinity_key is None:
            return total
        with self._lock:
            hint = self._prefix_hints.get((affinity_key, endpoint_id))
            if hint is None or hint.expires_at <= self._clock():
                return total
            if fingerprint is not None and fingerprint != hint.fingerprint:
                return total
            if messages is not None and not (
                hint.anchor is not None and _anchor_holds(messages, hint.anchor)
            ):
                # Shares the opening but does not contain the measured prompt:
                # a fork or a retry, whose divergent suffix is a cold prefill.
                return total
            return max(total - hint.tokens, 0)

    @property
    def elephant_tokens(self) -> int:
        """Prompt size at or above which a request is an elephant."""
        return self._elephant_tokens

    @property
    def elephant_limit(self) -> int:
        """Concurrent elephants permitted per endpoint."""
        return self._elephant_limit

    def is_elephant(self, tokens: int) -> bool:
        """Return True when a prompt of ``tokens`` counts as an elephant."""
        return tokens >= self._elephant_tokens

    def backlog(self, endpoint_id: str) -> int:
        """Return in-flight prefill tokens currently charged to an endpoint."""
        with self._lock:
            return self._backlog.get(endpoint_id, 0)

    def elephants(self, endpoint_id: str) -> int:
        """Return the number of elephants currently prefilling on an endpoint."""
        with self._lock:
            return self._elephants.get(endpoint_id, 0)

    def acquire(
        self,
        endpoint_id: str,
        tokens: int,
        *,
        affinity_key: str | None = None,
        fingerprint: str | None = None,
        anchor: tuple[int, str] | None = None,
    ) -> PrefillLease:
        """Charge a request's estimated un-cached prefill to an endpoint.

        Never blocks or refuses: admission is decided during selection, and a
        request that reached dispatch must always be allowed to proceed.

        The prompt is *not* remembered here. A hint written at dispatch would
        claim a prefix that no upstream has built yet: two overlapping turns of
        one conversation would have the second discounted against the first
        while the first is still prefilling, and a dispatch that fails would
        leave behind a hint for a prefix that was never cached. It is written by
        :meth:`release` instead, once prefill is confirmed complete.

        Args:
            endpoint_id: Endpoint about to receive the request.
            tokens: Estimated *total* prompt tokens; the cached prefix is
                discounted here.
            affinity_key: Caller identity, when known.
            fingerprint: Conversation identity, carried on the lease and stored
                with the hint when prefill completes.

        Returns:
            The lease to hand back to :meth:`release`.
        """
        total = max(int(tokens), 0)
        charged = self.uncached_estimate(endpoint_id, total, affinity_key)
        elephant = self.is_elephant(charged)
        with self._lock:
            self._backlog[endpoint_id] = self._backlog.get(endpoint_id, 0) + charged
            if elephant:
                self._elephants[endpoint_id] = self._elephants.get(endpoint_id, 0) + 1
            if affinity_key is not None:
                caller = (endpoint_id, affinity_key)
                self._by_caller[caller] = self._by_caller.get(caller, 0) + charged
        return PrefillLease(
            endpoint_id=endpoint_id,
            tokens=charged,
            elephant=elephant,
            affinity_key=affinity_key,
            prompt_tokens=total,
            fingerprint=fingerprint,
            anchor=anchor,
        )

    def _remember_prompt_locked(
        self,
        affinity_key: str,
        endpoint_id: str,
        tokens: int,
        now: float,
        fingerprint: str | None = None,
        anchor: tuple[int, str] | None = None,
    ) -> None:
        """Record a prompt size for warm-continuation discounting. Caller holds the lock."""
        key = (affinity_key, endpoint_id)
        self._prefix_hints[key] = _PrefixHint(
            tokens=tokens,
            expires_at=now + _PREFIX_HINT_TTL_SEC,
            fingerprint=fingerprint,
            anchor=anchor,
        )
        self._prefix_hints.move_to_end(key)
        while len(self._prefix_hints) > _PREFIX_HINT_MAX_ENTRIES:
            self._prefix_hints.popitem(last=False)

    def release(self, lease: PrefillLease | None, *, prefill_confirmed: bool = False) -> None:
        """Return a lease's tokens to an endpoint's budget.

        Idempotent and None-tolerant so callers can release at the natural point
        (first token) and again from a ``finally`` without special-casing.

        ``prefill_confirmed`` is what writes the warm-prefix hint, and it means
        exactly one thing: this endpoint has now finished prefilling this prompt,
        so the prefix really is resident. Releases that unwind an error or a
        client disconnect leave it False and write nothing -- a prefix nobody
        built must not discount the next request, least of all into a priority
        tier that preempts the request still building it.

        Args:
            lease: The lease from :meth:`acquire`, or None.
            prefill_confirmed: True only where the upstream has demonstrably
                finished prefill (a returned response, or the first content
                token of a stream).
        """
        if lease is None or lease.released:
            return
        with self._lock:
            if lease.released:
                return
            lease.released = True
            if prefill_confirmed and lease.affinity_key is not None:
                self._remember_prompt_locked(
                    lease.affinity_key,
                    lease.endpoint_id,
                    lease.prompt_tokens,
                    self._clock(),
                    lease.fingerprint,
                    lease.anchor,
                )
            remaining = self._backlog.get(lease.endpoint_id, 0) - lease.tokens
            if remaining > 0:
                self._backlog[lease.endpoint_id] = remaining
            else:
                self._backlog.pop(lease.endpoint_id, None)
            if lease.elephant:
                left = self._elephants.get(lease.endpoint_id, 0) - 1
                if left > 0:
                    self._elephants[lease.endpoint_id] = left
                else:
                    self._elephants.pop(lease.endpoint_id, None)
            if lease.affinity_key is not None:
                caller = (lease.endpoint_id, lease.affinity_key)
                owed = self._by_caller.get(caller, 0) - lease.tokens
                if owed > 0:
                    self._by_caller[caller] = owed
                else:
                    self._by_caller.pop(caller, None)

    def should_keep_affinity(
        self,
        endpoint_id: str,
        *,
        affinity_key: str | None = None,
        ceiling: int = AFFINITY_BACKLOG_CEILING,
    ) -> bool:
        """Return True when a pinned endpoint is idle enough to keep using.

        The caller's own in-flight work is excluded from the comparison. The
        ceiling exists to keep a caller out of *someone else's* queue; breaking
        a pin because of load the caller itself put there just relocates that
        work to a cold endpoint and pays a full prefill for nothing. Without
        this, a client issuing parallel turns on one conversation would evict
        itself from its own warm endpoint.

        Args:
            endpoint_id: The endpoint the caller is currently pinned to.
            affinity_key: Caller identity, whose own load is discounted.
            ceiling: Foreign backlog above which the pin is dropped for this
                request.

        Returns:
            False when the pin should be ignored and selection re-run. The pin
            itself is left in place -- one busy moment should not cost a caller
            its cache locality for the next five minutes.
        """
        if not PREFILL_AWARE_ENABLED:
            return True
        with self._lock:
            total = self._backlog.get(endpoint_id, 0)
            own = (
                self._by_caller.get((endpoint_id, affinity_key), 0)
                if affinity_key is not None
                else 0
            )
        return (total - own) <= ceiling

    def select_index(
        self,
        keys: Sequence[str],
        weights: Sequence[float],
        tokens: int,
        rand: Callable[[], float],
        affinity_key: str | None = None,
        avoid: str | None = None,
    ) -> int:
        """Pick a candidate by weight, then break toward the lighter endpoint.

        Power-of-two-choices: draw two independent weighted samples and keep
        whichever currently holds less prefill. This preserves the intent of
        configured weights (a route weighted 10x is still drawn ~10x as often)
        while making it very unlikely to land on the one endpoint that is busy
        prefilling something huge -- the single-sample weighted draw has no way
        to avoid that.

        Elephants additionally skip endpoints already at ``elephant_limit``, so
        two mega-prefills serialize across replicas instead of stacking on one.
        If every candidate is at the limit the restriction is dropped: routing
        degrades to "least loaded", never to "refuse to route".

        The load tie-break only engages once the heavier draw is carrying at
        least ``INTERVENE_TOKENS``. Route weights encode more than capacity --
        for remote providers they encode cost and contractual preference -- so
        below that floor the configured weight is left to decide and this stays
        a no-op. It intervenes for the pathology it was built for (an endpoint
        genuinely buried in prefill) and not for ordinary jitter.

        Elephant status is judged per candidate, not once for the request: the
        same prompt can be a few thousand delta tokens on the endpoint holding
        its prefix and a full cold prefill everywhere else. Judging it once
        would bar a warm continuation from the one endpoint that could serve it
        cheaply.

        Args:
            keys: Candidate endpoint ids, parallel to ``weights``.
            weights: Positive selection weights, parallel to ``keys``.
            tokens: Estimated *total* prompt tokens for this request.
            rand: Zero-argument callable returning a float in [0, 1).
            affinity_key: Caller identity, used to discount a warm prefix.
            avoid: Endpoint already judged too backlogged for this caller.
                Dropped from the candidates when anything else remains --
                a weighted draw would otherwise re-pick it a fair fraction of
                the time, undoing the decision that was just made.

        Returns:
            Index into ``keys`` of the selected candidate.
        """
        count = len(keys)
        if count == 0:
            raise ValueError("select_index requires at least one candidate")
        if count == 1:
            return 0

        eligible = list(range(count))
        if avoid is not None:
            remaining = [i for i in eligible if keys[i] != avoid]
            if remaining:
                eligible = remaining
        if PREFILL_AWARE_ENABLED:
            # Computed before taking the lock; uncached_estimate locks too.
            elephant_here = [
                self.is_elephant(self.uncached_estimate(keys[i], tokens, affinity_key))
                for i in eligible
            ]
            if any(elephant_here):
                with self._lock:
                    unsaturated = [
                        i
                        for i in eligible
                        if not elephant_here[i]
                        or self._elephants.get(keys[i], 0) < self._elephant_limit
                    ]
                if unsaturated:
                    eligible = unsaturated

        first = _weighted_pick(eligible, weights, rand)
        if not PREFILL_AWARE_ENABLED:
            return first
        second = _weighted_pick(eligible, weights, rand)
        if first == second:
            return first
        with self._lock:
            first_load = self._backlog.get(keys[first], 0)
            second_load = self._backlog.get(keys[second], 0)
        if max(first_load, second_load) < INTERVENE_TOKENS:
            return first
        return first if first_load <= second_load else second

    def snapshot(self) -> dict[str, dict[str, int]]:
        """Return a copy of current per-endpoint load, for logging and admin views."""
        with self._lock:
            return {
                endpoint_id: {
                    "prefill_tokens": tokens,
                    "elephants": self._elephants.get(endpoint_id, 0),
                }
                for endpoint_id, tokens in self._backlog.items()
            }


def _weighted_pick(
    eligible: Sequence[int],
    weights: Sequence[float],
    rand: Callable[[], float],
) -> int:
    """Draw one index from ``eligible`` in proportion to its weight.

    Args:
        eligible: Candidate indices into ``weights``.
        weights: Selection weights.
        rand: Zero-argument callable returning a float in [0, 1).

    Returns:
        The chosen index. Falls back to the last eligible index when weights sum
        to zero or floating-point drift leaves the draw past the final bucket.
    """
    total = sum(max(weights[i], 0.0) for i in eligible)
    if total <= 0:
        return eligible[-1]
    threshold = rand() * total
    cumulative = 0.0
    for i in eligible:
        cumulative += max(weights[i], 0.0)
        if threshold <= cumulative:
            return i
    return eligible[-1]
