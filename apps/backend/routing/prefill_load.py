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
estimated from the caller's last prompt size on that endpoint -- agentic prompts
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


def estimate_prefill_tokens(messages: Sequence[dict[str, Any]] | None) -> int:
    """Estimate prompt size in tokens, cheaply enough for the routing hot path.

    Uses a character heuristic rather than ``serving.utils.tokens``: real
    tokenization of a 700k-token prompt means pushing megabytes of text through
    tiktoken on every request, and this value only has to be good enough to rank
    endpoints and recognize an elephant. Being off by 20% changes nothing about
    which endpoint wins; spending 100ms of CPU to route would.

    Args:
        messages: OpenAI-style messages, or None.

    Returns:
        Estimated prompt tokens (never negative).
    """
    if not messages:
        return 0
    chars = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        chars += _content_chars(message.get("content"))
    return chars // _CHARS_PER_TOKEN


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
    """

    endpoint_id: str
    tokens: int
    elephant: bool
    affinity_key: str | None = None
    released: bool = False


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
        self._prefix_hints: OrderedDict[tuple[str, str], tuple[int, float]] = OrderedDict()
        self._elephant_tokens = max(int(elephant_tokens), 1)
        self._elephant_limit = max(int(elephant_limit), 1)
        self._clock = clock

    def uncached_estimate(
        self,
        endpoint_id: str,
        tokens: int,
        affinity_key: str | None = None,
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

        Args:
            endpoint_id: Candidate endpoint.
            tokens: Estimated total prompt tokens.
            affinity_key: Caller identity, or None when unknown.

        Returns:
            Estimated un-cached prefill tokens, never negative.
        """
        total = max(int(tokens), 0)
        if affinity_key is None:
            return total
        with self._lock:
            hint = self._prefix_hints.get((affinity_key, endpoint_id))
            if hint is None or hint[1] <= self._clock():
                return total
            return max(total - hint[0], 0)

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
    ) -> PrefillLease:
        """Charge a request's estimated un-cached prefill to an endpoint.

        Never blocks or refuses: admission is decided during selection, and a
        request that reached dispatch must always be allowed to proceed.

        Also records this prompt's size against ``(affinity_key, endpoint_id)``
        so the caller's next turn on this endpoint is recognized as warm.

        Args:
            endpoint_id: Endpoint about to receive the request.
            tokens: Estimated *total* prompt tokens; the cached prefix is
                discounted here.
            affinity_key: Caller identity, when known.

        Returns:
            The lease to hand back to :meth:`release`.
        """
        total = max(int(tokens), 0)
        charged = self.uncached_estimate(endpoint_id, total, affinity_key)
        elephant = self.is_elephant(charged)
        now = self._clock()
        with self._lock:
            self._backlog[endpoint_id] = self._backlog.get(endpoint_id, 0) + charged
            if elephant:
                self._elephants[endpoint_id] = self._elephants.get(endpoint_id, 0) + 1
            if affinity_key is not None:
                caller = (endpoint_id, affinity_key)
                self._by_caller[caller] = self._by_caller.get(caller, 0) + charged
                self._remember_prompt_locked(affinity_key, endpoint_id, total, now)
        return PrefillLease(
            endpoint_id=endpoint_id,
            tokens=charged,
            elephant=elephant,
            affinity_key=affinity_key,
        )

    def _remember_prompt_locked(
        self,
        affinity_key: str,
        endpoint_id: str,
        tokens: int,
        now: float,
    ) -> None:
        """Record a prompt size for warm-continuation discounting. Caller holds the lock."""
        key = (affinity_key, endpoint_id)
        self._prefix_hints[key] = (tokens, now + _PREFIX_HINT_TTL_SEC)
        self._prefix_hints.move_to_end(key)
        while len(self._prefix_hints) > _PREFIX_HINT_MAX_ENTRIES:
            self._prefix_hints.popitem(last=False)

    def release(self, lease: PrefillLease | None) -> None:
        """Return a lease's tokens to an endpoint's budget.

        Idempotent and None-tolerant so callers can release at the natural point
        (first token) and again from a ``finally`` without special-casing.

        Args:
            lease: The lease from :meth:`acquire`, or None.
        """
        if lease is None or lease.released:
            return
        with self._lock:
            if lease.released:
                return
            lease.released = True
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

        Returns:
            Index into ``keys`` of the selected candidate.
        """
        count = len(keys)
        if count == 0:
            raise ValueError("select_index requires at least one candidate")
        if count == 1:
            return 0

        eligible = list(range(count))
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
