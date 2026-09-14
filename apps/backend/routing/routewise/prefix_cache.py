"""Session-scoped cache-aware routing primitives.

Pure, router-agnostic building blocks for RouteWise session-scoped prefix-cache
estimation:

- :class:`CacheScope` -- the isolation key ``(user, project, session, provider,
  endpoint, model/profile, key_slot, cache-affecting params)``.
- :class:`Block` / :func:`build_blocks` -- turn a canonical prompt into a
  metadata-only block-hash sequence (HMAC of fixed-size token chunks).
- :class:`CacheSignal` -- the per-candidate lookup result: how many prefix
  tokens match the scope's most recent request. Following the paper cost layer,
  a matched prefix is treated as cached (no hit-probability model).
- :class:`SessionProviderPrefixMemory` -- a bounded in-memory store of the most
  recent successful request's block sequence per scope, with TTL + LRU
  eviction. ``lookup`` returns a :class:`CacheSignal`; ``observe`` records a
  selected-success outcome for future prefix matches.
- :class:`CacheAwareCostEstimator` / :func:`price_delta_per_token` -- turn a
  :class:`CacheSignal` into the paper's cached-token cost discount.

This module never touches routing, billing, or provider transport directly. It
stores metadata only: HMAC block digests and token counts -- never raw prompt
text, token ids, tool-schema text, or credentials. Callers may use the returned
estimates under a guarded rollout flag as an input to RouteWise effective cost.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from serving.utils.logging import get_logger
from serving.utils.tokens import tokenize_text

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from typing import Any

logger = get_logger(__name__)

# Defaults for the bounded session prefix store.
DEFAULT_BLOCK_SIZE_TOKENS: int = 128
DEFAULT_TTL_SEC: float = 2 * 3600.0
DEFAULT_MAX_ENTRIES: int = 50_000
DEFAULT_HISTORY_DEPTH: int = 1
DEFAULT_MIN_MATCH_TOKENS: int = 1024

# Per-process secret so block digests are not reversible across deployments and
# never need a configured key. Callers may override for deterministic tests.
_PROCESS_SECRET: bytes = secrets.token_bytes(32)


# ---------------------------------------------------------------------------
# Cache-locality evidence
# ---------------------------------------------------------------------------


class CacheLocalityEvidenceState:
    """Evidence state for a single scope (session/provider/endpoint).

    Models three distinct facts:

    - prefix opportunity: HMAC block matching already answered by
      ``SessionProviderPrefixMemory``.
    - potential warming: a successful request may have populated the cache.
    - verified reuse: provider explicitly reported ``cached_tokens > 0``.

    States:
    - UNKNOWN: no prior request; no discount.
    - POSSIBLY_WARMED: successful dispatch, no authoritative reuse signal;
      no strong discount.
    - VERIFIED_REUSABLE: observed ``cached_tokens > 0``; evidence-backed
      discount permitted.
    - MATERIALIZATION_CANDIDATE: a successful request reported
      ``cached_tokens = 0``. The request may have populated the cache after
      the provider measured it, but it has not yet demonstrated reuse.
    - NEGATIVE: an unsuccessful request reported ``cached_tokens = 0`` after
      prior warming; the dispatched prediction was not observed as reusable.
      This never creates a cost discount and is not a claim about future
      cache residency.
    """

    UNKNOWN = "UNKNOWN"
    POSSIBLY_WARMED = "POSSIBLY_WARMED"
    MATERIALIZATION_CANDIDATE = "MATERIALIZATION_CANDIDATE"
    VERIFIED_REUSABLE = "VERIFIED_REUSABLE"
    NEGATIVE = "NEGATIVE"


@dataclass(frozen=True, slots=True)
class _CacheLocalityEvidence:
    """One observation of cache reuse for a scope."""

    state: str
    """Evidence state: UNKNOWN, POSSIBLY_WARMED, VERIFIED_REUSABLE, NEGATIVE."""

    last_cached_tokens: int
    """Most recent observed cached_tokens (0 if none)."""

    confidence: float
    """0..1. Scales the permitted discount."""

    observed_at: float
    """Timestamp of the last observation (seconds)."""

    generation: int
    """Number of observations for this scope."""


class _CacheLocalityEstimator:
    """Prefix-/endpoint-scoped evidence gate for HybridInference's PrefixCacheCoordinator.

    Unlike RouteWise #24's generic locality estimator (which learns at
    provider+affinity granularity), this estimator is keyed by the full
    ``CacheScope`` (session + endpoint + provider + model + key-slot +
    cache-affecting params). It does NOT perform prefix matching itself —
    ``SessionProviderPrefixMemory`` determines prefix opportunity via HMAC
    block matching; this estimator determines whether observed reuse evidence
    justifies applying that opportunity as a cost discount.

    It exists because HybridInference has finer endpoint and prefix
    information than RouteWise's generic abstraction can represent directly.

    Semantics (conceptually aligned with RouteWise #24):

    - ``cached_tokens > 0``: positive evidence, confidence = 1.0.
    - ``cached_tokens == 0``: negative evidence, confidence *= 0.3
      (with time decay applied first). Repeated misses degrade confidence but
      do not immediately delete evidence (transient eviction possible).
      A subsequent hit restores confidence.
    - ``cached_tokens is None``: no evidence. No positive or negative evidence
      manufactured from a missing observation.
    """

    def __init__(
        self,
        *,
        ttl_sec: float = DEFAULT_TTL_SEC,
        min_confidence: float = 0.01,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        miss_confidence_factor: float = 0.3,
        time_source: Callable[[], float] = time.time,
    ) -> None:
        if ttl_sec <= 0:
            raise ValueError(f"ttl_sec must be positive, got {ttl_sec}")
        if max_entries <= 0:
            raise ValueError(f"max_entries must be positive, got {max_entries}")
        self._ttl_sec = float(ttl_sec)
        self._half_life_sec = ttl_sec / 2.0
        self._min_confidence = float(min_confidence)
        self._max_entries = int(max_entries)
        self._miss_confidence_factor = float(miss_confidence_factor)
        self._time = time_source
        self._evidence: OrderedDict[CacheScope, _CacheLocalityEvidence] = OrderedDict()
        self._lock = threading.Lock()

    def record(
        self,
        scope: CacheScope,
        cached_tokens: int | None,
        *,
        materialization_candidate: bool = False,
    ) -> None:
        """Record a cache-locality observation for one scope.

        ``cached_tokens`` is the authoritative observed cache-reuse count from
        the provider:

        - ``cached_tokens > 0``: positive evidence, refresh state to
          VERIFIED_REUSABLE with confidence = 1.0.
        - ``cached_tokens == 0`` with ``materialization_candidate=True``:
          retain only a weak materialization candidate. A miss is measured
          before this request can warm the cache, so it must not become
          durable anti-locality.
        - ``cached_tokens == 0`` otherwise: negative evidence. Apply time
          decay then the miss penalty. State becomes NEGATIVE (or
          POSSIBLY_WARMED -> NEGATIVE).
        - ``cached_tokens is None``: no authoritative observation. Do not
          manufacture positive or negative evidence. Successful dispatch may
          still establish POTENTIAL warming (handled by ``record_dispatch``).
        """
        now = self._time()
        with self._lock:
            existing = self._evidence.get(scope)
            generation = existing.generation + 1 if existing is not None else 1

            if cached_tokens is None:
                # No authoritative observation: do NOT update evidence.
                return

            try:
                cached_tokens = int(cached_tokens)
            except (TypeError, ValueError):
                # Malformed provider telemetry is missing evidence, not a
                # measured cache miss.
                return
            if cached_tokens < 0:
                # Never turn impossible telemetry into an authoritative zero.
                return

            if cached_tokens > 0:
                # Positive observation: refresh evidence.
                self._evidence[scope] = _CacheLocalityEvidence(
                    state=CacheLocalityEvidenceState.VERIFIED_REUSABLE,
                    last_cached_tokens=cached_tokens,
                    confidence=1.0,
                    observed_at=now,
                    generation=generation,
                )
            else:
                if materialization_candidate:
                    self._evidence[scope] = _CacheLocalityEvidence(
                        state=CacheLocalityEvidenceState.MATERIALIZATION_CANDIDATE,
                        last_cached_tokens=0,
                        confidence=0.0,
                        observed_at=now,
                        generation=generation,
                    )
                    self._evidence.move_to_end(scope)
                    self._enforce_capacity()
                    return
                # Negative observation (miss): degrade confidence.
                if existing is not None:
                    age = now - existing.observed_at
                    decayed = existing.confidence * (0.5 ** (age / self._half_life_sec))
                    new_confidence = decayed * self._miss_confidence_factor
                    self._evidence[scope] = _CacheLocalityEvidence(
                        state=(
                            CacheLocalityEvidenceState.NEGATIVE
                            if new_confidence >= self._min_confidence
                            else CacheLocalityEvidenceState.POSSIBLY_WARMED
                        ),
                        last_cached_tokens=existing.last_cached_tokens,
                        confidence=new_confidence,
                        observed_at=now,
                        generation=generation,
                    )
                # If no existing evidence, a miss creates no evidence.
            # Refresh LRU recency on write (only if scope exists).
            if scope in self._evidence:
                self._evidence.move_to_end(scope)
            self._enforce_capacity()

    def record_dispatch(self, scope: CacheScope) -> None:
        """Record a successful dispatch (potential warming).

        This does NOT create positive evidence. It only advances UNKNOWN ->
        POSSIBLY_WARMED so the cost estimator can represent "a request reached
        this destination and may have populated its cache".

        If the prefix memory has been replaced with a different prompt
        (block generation changed) and there is no authoritative evidence to
        support it, the stale evidence is invalidated. This prevents a
        VERIFIED_REUSABLE observation for an old prompt from incorrectly
        transferring to an unrelated new prompt.
        """
        now = self._time()
        with self._lock:
            existing = self._evidence.get(scope)
            if existing is None:
                # First successful dispatch: potential warming, low confidence.
                self._evidence[scope] = _CacheLocalityEvidence(
                    state=CacheLocalityEvidenceState.POSSIBLY_WARMED,
                    last_cached_tokens=0,
                    confidence=0.0,
                    observed_at=now,
                    generation=1,
                )
            else:
                # Already has evidence: do not degrade. A successful dispatch
                # does not reduce existing confidence.
                # Refresh LRU recency on access.
                self._evidence.move_to_end(scope)
            self._enforce_capacity()

    def estimate(self, scope: CacheScope, current_input_tokens: int) -> tuple[int, str, float]:
        """Return (estimated_cached_tokens, evidence_state, confidence).

        Returns (0, UNKNOWN, 0.0) if no valid evidence, expired, or below
        confidence threshold. Uses lazy expiration.

        The estimated cached tokens is bounded by both the matched prefix
        length (caller responsibility) and the learned evidence:

            estimated_cached_tokens = min(matched_prefix_tokens,
                                          last_cached_tokens * decayed_confidence)

        For POSSIBLY_WARMED or MATERIALIZATION_CANDIDATE (no verified reuse
        yet): 0 (no strong discount).
        For NEGATIVE: 0 (suppressed unless confidence recovers).
        For UNKNOWN: 0.
        """
        now = self._time()
        with self._lock:
            ev = self._evidence.get(scope)
            if ev is None:
                return 0, CacheLocalityEvidenceState.UNKNOWN, 0.0
            age = now - ev.observed_at
            if age > self._ttl_sec:
                del self._evidence[scope]
                return 0, CacheLocalityEvidenceState.UNKNOWN, 0.0
            decayed = ev.confidence * (0.5 ** (age / self._half_life_sec))
            # POSSIBLY_WARMED and MATERIALIZATION_CANDIDATE intentionally have
            # zero confidence until a provider reports a hit. Keep those weak
            # states alive for the TTL so a successful miss is not silently
            # discarded on the next routing lookup.
            if (
                ev.state
                not in (
                    CacheLocalityEvidenceState.POSSIBLY_WARMED,
                    CacheLocalityEvidenceState.MATERIALIZATION_CANDIDATE,
                )
                and decayed < self._min_confidence
            ):
                del self._evidence[scope]
                return 0, CacheLocalityEvidenceState.UNKNOWN, 0.0

            if ev.state == CacheLocalityEvidenceState.VERIFIED_REUSABLE:
                estimated = int(ev.last_cached_tokens * decayed)
                estimated_tokens = min(estimated, ev.last_cached_tokens, current_input_tokens)
            else:
                # POSSIBLY_WARMED / MATERIALIZATION_CANDIDATE / NEGATIVE /
                # UNKNOWN: no strong discount.
                estimated_tokens = 0
            # Every valid evidence observation is active state. Refresh recency
            # for misses too; otherwise a hot scope with repeated NEGATIVE
            # observations can be evicted before colder scopes.
            self._evidence.move_to_end(scope)
            return estimated_tokens, ev.state, decayed

    def invalidate(self, scope: CacheScope) -> None:
        with self._lock:
            if scope in self._evidence:
                del self._evidence[scope]

    def _enforce_capacity(self) -> None:
        """Evict oldest entries if over capacity. Must be called under lock.

        Uses OrderedDict for O(1) LRU eviction, matching the strategy in
        SessionProviderPrefixMemory.
        """
        while len(self._evidence) > self._max_entries:
            self._evidence.popitem(last=False)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CacheScope:
    """Cache-isolation key for one session/provider prefix history.

    Two requests may share a cached prefix only when every field matches, so the
    scope deliberately separates user, project, session, provider, endpoint,
    model profile, key slot, and any cache-affecting request params. Cross-scope
    reuse is never assumed.
    """

    user_hash: str
    project_hash: str
    session_hash: str
    provider_id: str
    endpoint_id: str
    model_profile: str
    key_slot_id: str
    cache_affecting_params_hash: str = ""


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Block:
    """One metadata-only prefix block: an opaque digest plus its token count."""

    digest: str
    token_count: int


def _hmac_digest(secret: bytes, payload: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def canonicalize_prompt(
    messages: Sequence[Mapping[str, Any]],
    *,
    tools: Any = None,
    response_format: Any = None,
) -> str:
    """Return a deterministic string for the cache-affecting parts of a request.

    Field order and whitespace are normalized so that semantically identical
    requests serialize identically. Only prompt-shaping inputs are included.
    """
    payload = {
        "messages": list(messages),
        "tools": tools,
        "response_format": response_format,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def build_blocks(
    messages: Sequence[Mapping[str, Any]],
    *,
    tools: Any = None,
    response_format: Any = None,
    block_size: int = DEFAULT_BLOCK_SIZE_TOKENS,
    secret: bytes | None = None,
    tokenize: Callable[[str], Sequence[int]] = tokenize_text,
) -> tuple[Block, ...]:
    """Canonicalize a request and return its fixed-size token-block sequence.

    The canonical prompt is tokenized and split into ``block_size`` chunks; each
    chunk is HMAC-hashed so the stored sequence is opaque. The trailing partial
    chunk is kept so short prompts still produce a block.
    """
    if block_size <= 0:
        raise ValueError(f"block_size must be positive, got {block_size}")
    key = secret if secret is not None else _PROCESS_SECRET
    text = canonicalize_prompt(messages, tools=tools, response_format=response_format)
    token_ids = list(tokenize(text))
    blocks: list[Block] = []
    for start in range(0, len(token_ids), block_size):
        chunk = token_ids[start : start + block_size]
        if not chunk:
            continue
        payload = b",".join(str(int(tok)).encode("ascii") for tok in chunk)
        blocks.append(Block(digest=_hmac_digest(key, payload), token_count=len(chunk)))
    return tuple(blocks)


def longest_common_prefix_tokens(
    current: Sequence[Block],
    previous: Sequence[Block],
) -> int:
    """Return the summed token count of the longest shared leading block run."""
    matched = 0
    for left, right in zip(current, previous, strict=False):
        if left.digest != right.digest:
            break
        matched += left.token_count
    return matched


# ---------------------------------------------------------------------------
# Cache signal
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CacheSignal:
    """Per-candidate prefix-cache lookup result.

    ``matched_prefix_tokens`` is a deterministic exact prefix match against the
    scope's most recent request. Following the paper's cost layer, a matched
    prefix that clears the minimum-cacheable threshold is treated as cached -- no
    hit-probability model. A cold lookup returns :meth:`empty`.
    """

    matched_prefix_tokens: int
    meets_threshold: bool
    has_history: bool
    last_seen_at: float | None = None

    @classmethod
    def empty(cls) -> CacheSignal:
        """Return the cold signal used when a scope has no usable history."""
        return cls(matched_prefix_tokens=0, meets_threshold=False, has_history=False)

    @property
    def expected_cached_tokens(self) -> float:
        """Return the estimated cached input tokens (the matched prefix length)."""
        if not self.has_history or not self.meets_threshold:
            return 0.0
        return float(max(self.matched_prefix_tokens, 0))


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Entry:
    """Most-recent successful request for one scope."""

    blocks: tuple[Block, ...]
    last_seen_at: float


class SessionProviderPrefixMemory:
    """Bounded, thread-safe store of the latest prefix per session/provider scope.

    ``lookup`` compares the current request's blocks against the scope's stored
    blocks and returns a :class:`CacheSignal`. ``observe`` records a
    selected-success outcome by storing the new blocks. State is metadata-only
    and bounded by TTL and an LRU cap so it cannot grow without limit.
    """

    def __init__(
        self,
        *,
        ttl_sec: float = DEFAULT_TTL_SEC,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        min_match_tokens: int = DEFAULT_MIN_MATCH_TOKENS,
        time_source: Callable[[], float] = time.time,
    ) -> None:
        if max_entries <= 0:
            raise ValueError(f"max_entries must be positive, got {max_entries}")
        self._ttl_sec = float(ttl_sec)
        self._max_entries = int(max_entries)
        self._min_match_tokens = int(min_match_tokens)
        self._time = time_source
        self._entries: OrderedDict[CacheScope, _Entry] = OrderedDict()
        self._lock = threading.Lock()

    def lookup(
        self,
        scope: CacheScope,
        current_blocks: Sequence[Block],
        *,
        now: float | None = None,
    ) -> CacheSignal:
        """Return the cache signal for ``current_blocks`` under ``scope``."""
        ts = self._time() if now is None else now
        with self._lock:
            entry = self._entries.get(scope)
            if entry is None or self._is_expired(entry, ts):
                if entry is not None:
                    del self._entries[scope]
                return CacheSignal.empty()
            self._entries.move_to_end(scope)
            matched = longest_common_prefix_tokens(current_blocks, entry.blocks)
            return CacheSignal(
                matched_prefix_tokens=matched,
                meets_threshold=matched >= self._min_match_tokens,
                has_history=True,
                last_seen_at=entry.last_seen_at,
            )

    def observe(
        self,
        scope: CacheScope,
        blocks: Sequence[Block],
        *,
        now: float | None = None,
    ) -> None:
        """Store a selected-success request for future prefix matches."""
        ts = self._time() if now is None else now
        new_blocks = tuple(blocks)
        with self._lock:
            prior = self._entries.get(scope)
            if prior is not None and self._is_expired(prior, ts):
                del self._entries[scope]

            self._entries[scope] = _Entry(
                blocks=new_blocks,
                last_seen_at=ts,
            )
            self._entries.move_to_end(scope)
            self._evict_locked()

    def current_blocks(self, scope: CacheScope) -> tuple[Block, ...] | None:
        """Return an immutable snapshot of the remembered blocks for ``scope``."""
        with self._lock:
            entry = self._entries.get(scope)
            return None if entry is None else entry.blocks

    def __len__(self) -> int:
        """Return the number of scopes currently held."""
        with self._lock:
            return len(self._entries)

    def _is_expired(self, entry: _Entry, now: float) -> bool:
        return self._ttl_sec > 0 and (now - entry.last_seen_at) > self._ttl_sec

    def _evict_locked(self) -> None:
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def price_delta_per_token(
    prompt_price_per_m: float,
    cached_price_per_m: float | None,
) -> float:
    """Return per-token savings of a cache hit, or ``0`` when no discount applies.

    A missing, zero, or non-cheaper cached price means the provider offers no
    cache discount, so the cached price is treated as the full input price
    (``p_cache = p_in``) and the delta is ``0``. Treating a configured ``0`` as
    "free cached reads" would massively over-discount, so it is deliberately
    read as "not offered".
    """
    prompt = float(prompt_price_per_m or 0.0)
    if cached_price_per_m is None:
        return 0.0
    cached = float(cached_price_per_m)
    if cached <= 0.0 or cached >= prompt:
        return 0.0
    return (prompt - cached) / 1_000_000.0


@dataclass(frozen=True, slots=True)
class CacheAdjustment:
    """Outcome of applying a cache signal to one candidate's cold cost."""

    adjusted_cost: float
    cold_cost: float
    cache_discount: float
    expected_cached_tokens: float
    applied: bool


class CacheAwareCostEstimator:
    """Apply the paper cost layer's cached-token discount to one candidate."""

    def adjust(
        self,
        cold_cost: float,
        signal: CacheSignal,
        price_delta: float,
        *,
        enabled: bool = True,
    ) -> CacheAdjustment:
        """Return the cache-adjusted cost for one candidate provider.

        The matched prefix is treated as cached, so the discount is
        ``expected_cached_tokens * (p_in - p_cache)`` -- exactly the paper's
        on-demand effective cost ``p_in*(n - cached) + p_cache*cached`` -- floored
        so the cost cannot go negative. It is skipped (cost unchanged) when
        disabled, when there is no usable match, or when the provider offers no
        per-token cache discount.
        """
        cold = max(float(cold_cost), 0.0)
        expected = signal.expected_cached_tokens
        if not enabled or expected <= 0.0 or price_delta <= 0.0:
            return CacheAdjustment(
                adjusted_cost=cold,
                cold_cost=cold,
                cache_discount=0.0,
                expected_cached_tokens=expected,
                applied=False,
            )
        discount = min(expected * price_delta, cold)
        return CacheAdjustment(
            adjusted_cost=cold - discount,
            cold_cost=cold,
            cache_discount=discount,
            expected_cached_tokens=expected,
            applied=discount > 0.0,
        )


# ---------------------------------------------------------------------------
# Prefix-cache coordinator
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrefixCacheCostRecord:
    """Per-candidate prefix-cache cost estimate.

    Combines two distinct signals:

    - **Prefix opportunity** (``matched_prefix_tokens``): how many leading
      tokens match the scope's most recent request, determined by HMAC block
      matching in ``SessionProviderPrefixMemory``. A match means reuse is
      *possible* — it does NOT imply verified cache residency.
    - **Evidence-bounded estimate** (``expected_cached_tokens``): the actual
      token count eligible for cost adjustment, bounded by both the matched
      prefix length AND observed-reuse evidence from ``_CacheLocalityEstimator``.
      Zero unless the scope has VERIFIED_REUSABLE evidence.

    ``cache_discount`` is ``expected_cached_tokens * (p_in - p_cache)``, floored
    so cost never goes negative. ``evidence_state`` and ``evidence_confidence``
    describe the observed-reuse support behind the estimate.
    """

    matched_prefix_tokens: int
    expected_cached_tokens: float
    cache_discount: float
    would_apply: bool
    has_history: bool
    meets_threshold: bool
    evidence_state: str = CacheLocalityEvidenceState.UNKNOWN
    """The evidence state backing this estimate (UNKNOWN/POSSIBLY_WARMED/VERIFIED_REUSABLE/NEGATIVE)."""
    evidence_confidence: float = 0.0
    """Confidence in the evidence (0..1)."""


class PrefixCacheCoordinator:
    """Router-facing coordinator for session-scoped prefix-cache estimates.

    Owns the bounded memory and the cost estimator and centralizes the request
    mapping decisions (which fields form a scope, which are hashed). Sensitive
    scope fields (user, project, session, params) are HMAC hashed so no raw
    identifier is stored.

    The coordinator combines two models:

    - **Prefix opportunity** (``SessionProviderPrefixMemory``): "these requests
      share reusable prefix content" (deterministic HMAC block matching).
    - **Locality evidence** (``_CacheLocalityEstimator``): "has this destination
      actually demonstrated cache reuse?" (positive/negative/unknown semantics).
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        memory: SessionProviderPrefixMemory | None = None,
        estimator: CacheAwareCostEstimator | None = None,
        block_size: int = DEFAULT_BLOCK_SIZE_TOKENS,
        secret: bytes | None = None,
        tokenize: Callable[[str], Sequence[int]] = tokenize_text,
        evidence: _CacheLocalityEstimator | None = None,
        max_generation_entries: int | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self._memory = memory if memory is not None else SessionProviderPrefixMemory()
        self._estimator = estimator if estimator is not None else CacheAwareCostEstimator()
        self._block_size = int(block_size)
        self._secret = secret if secret is not None else _PROCESS_SECRET
        self._tokenize = tokenize
        self._evidence = evidence if evidence is not None else _CacheLocalityEstimator()
        self._generation_lock = threading.Lock()
        self._next_generation = 0
        if max_generation_entries is None:
            max_generation_entries = min(
                getattr(self._memory, "_max_entries", DEFAULT_MAX_ENTRIES),
                getattr(self._evidence, "_max_entries", DEFAULT_MAX_ENTRIES),
            )
        if max_generation_entries <= 0:
            raise ValueError(
                f"max_generation_entries must be positive, got {max_generation_entries}"
            )
        self._max_generation_entries = int(max_generation_entries)
        # This is a bounded stale-completion guard. It is kept separate from
        # prefix memory because pending attempts can reserve a generation
        # before a successful response is eligible to update that memory.
        self._scope_generations: OrderedDict[CacheScope, int] = OrderedDict()

    @property
    def memory(self) -> SessionProviderPrefixMemory:
        """Expose the backing prefix memory for metrics and tests."""
        return self._memory

    @property
    def evidence(self) -> _CacheLocalityEstimator:
        """Expose the backing evidence estimator for metrics and tests."""
        return self._evidence

    def build_blocks(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Any = None,
        response_format: Any = None,
    ) -> tuple[Block, ...]:
        """Return the block sequence for one request under this coordinator."""
        return build_blocks(
            messages,
            tools=tools,
            response_format=response_format,
            block_size=self._block_size,
            secret=self._secret,
            tokenize=self._tokenize,
        )

    def scope_for(
        self,
        *,
        session: str,
        provider_id: str,
        endpoint_id: str,
        model_profile: str,
        user: str = "",
        project: str = "",
        key_slot: str = "",
        cache_params: str = "",
    ) -> CacheScope:
        """Build a :class:`CacheScope`, hashing the sensitive identifiers."""
        return CacheScope(
            user_hash=self._hash(user),
            project_hash=self._hash(project),
            session_hash=self._hash(session),
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            model_profile=model_profile,
            key_slot_id=key_slot,
            cache_affecting_params_hash=self._hash(cache_params),
        )

    def evaluate(
        self,
        scope: CacheScope,
        blocks: Sequence[Block],
        *,
        cold_cost: float,
        price_delta: float,
        now: float | None = None,
    ) -> PrefixCacheCostRecord:
        """Look up one candidate and return its prefix-cache cost record.

        The expected cached tokens is bounded by BOTH the matched prefix length
        (opportunity) AND the learned locality evidence:

            expected_cached_tokens = min(matched_prefix_tokens,
                                          learned_evidence_estimate)

        Where ``learned_evidence_estimate`` is non-zero only for VERIFIED_REUSABLE
        state. POSSIBLY_WARMED, NEGATIVE, and UNKNOWN yield 0 (no strong discount).
        """
        signal = self._memory.lookup(scope, blocks, now=now)
        matched = signal.matched_prefix_tokens
        meets = signal.meets_threshold and signal.has_history

        # Locality evidence bounds the estimate.
        learned_tokens, evidence_state, evidence_confidence = (
            0,
            CacheLocalityEvidenceState.UNKNOWN,
            0.0,
        )
        if meets:
            learned_tokens, evidence_state, evidence_confidence = self._evidence.estimate(
                scope, matched
            )
            learned_tokens = min(learned_tokens, matched)

        effective_expected = float(learned_tokens) if learned_tokens > 0 else 0.0

        # Recompute the adjustment using the evidence-bounded expected tokens.
        # We bypass the estimator's internal signal and compute directly so the
        # cost layer still drives the final discount.
        cold = max(float(cold_cost), 0.0)
        if effective_expected <= 0.0 or price_delta <= 0.0 or not self.enabled:
            return PrefixCacheCostRecord(
                matched_prefix_tokens=matched,
                expected_cached_tokens=0.0,
                cache_discount=0.0,
                would_apply=False,
                has_history=signal.has_history,
                meets_threshold=signal.meets_threshold,
                evidence_state=evidence_state,
                evidence_confidence=evidence_confidence,
            )
        discount = min(effective_expected * price_delta, cold)
        return PrefixCacheCostRecord(
            matched_prefix_tokens=matched,
            expected_cached_tokens=effective_expected,
            cache_discount=discount,
            would_apply=discount > 0.0,
            has_history=signal.has_history,
            meets_threshold=signal.meets_threshold,
            evidence_state=evidence_state,
            evidence_confidence=evidence_confidence,
        )

    def remember(
        self,
        scope: CacheScope,
        blocks: Sequence[Block],
        *,
        now: float | None = None,
        generation: int | None = None,
    ) -> bool:
        """Store the selected provider's blocks so the next turn can match them.

        If the stored blocks are being replaced with a different prompt and
        there is existing evidence, that evidence is invalidated. VERIFIED_REUSABLE
        evidence belongs to the prefix it was observed for, not to every future
        prompt under the same session/endpoint scope.

        Also invalidates evidence when the scope is not present in prefix
        memory (e.g., it was evicted at capacity). A scope returning after
        eviction must re-establish locality from observations, not from stale
        evidence left behind by a previous prompt.
        """
        with self._generation_lock:
            generation = self._claim_generation(scope, generation)
            if generation is None:
                return False
            current = self._scope_generations.get(scope)
            if current != generation:
                return False

            # Invalidate stale evidence when prefix content changes or scope
            # was evicted. Keep the check and write together with generation
            # ordering so an older completion cannot overwrite a newer one.
            prior_blocks = self._memory.current_blocks(scope)
            new_blocks = tuple(blocks)
            if prior_blocks is not None:
                if prior_blocks != new_blocks:
                    self._evidence.invalidate(scope)
            else:
                # Scope not in memory (evicted or brand-new): clear any stale
                # evidence before reintroducing it.
                self._evidence.invalidate(scope)
            self._memory.observe(scope, new_blocks, now=now)
            return True

    def reserve_generations(self, scopes: Iterable[CacheScope]) -> dict[CacheScope, int]:
        """Reserve one ordered prefix generation for a pending request."""
        unique_scopes = tuple(dict.fromkeys(scopes))
        if not unique_scopes:
            return {}
        with self._generation_lock:
            self._next_generation += 1
            generation = self._next_generation
            # Publish the guard at reservation time. A completion may be
            # delayed while this bounded registry evicts the scope; retaining
            # the guard here lets the completion be rejected instead of being
            # mistaken for a new generation when it eventually arrives.
            for scope in unique_scopes:
                self._track_generation(scope, generation)
            return dict.fromkeys(unique_scopes, generation)

    def _claim_generation(self, scope: CacheScope, generation: int | None) -> int | None:
        if generation is None:
            self._next_generation += 1
            generation = self._next_generation
            self._track_generation(scope, generation)
            return generation

        # Supplied generations must have an active reservation/guard. In
        # particular, do not recreate a guard that was LRU-evicted while the
        # request was still pending; that would let an old completion install
        # stale memory and evidence as though it were current.
        if self._scope_generations.get(scope) != generation:
            return None
        self._scope_generations.move_to_end(scope)
        return generation

    def _track_generation(self, scope: CacheScope, generation: int) -> None:
        """Track a generation and evict the oldest stale-completion guard."""
        self._scope_generations[scope] = generation
        self._scope_generations.move_to_end(scope)
        while len(self._scope_generations) > self._max_generation_entries:
            evicted_scope, _ = self._scope_generations.popitem(last=False)
            # Evidence without a generation guard cannot safely be associated
            # with a pending request after the guard itself has been evicted.
            self._evidence.invalidate(evicted_scope)

    def _on_memory_evict(self, scope: CacheScope) -> None:
        """Callback when prefix memory evicts a scope. Keeps evidence in sync."""
        self._evidence.invalidate(scope)

    def record_evidence(
        self,
        scope: CacheScope,
        cached_tokens: int | None,
        *,
        generation: int | None = None,
        blocks: Sequence[Block] | None = None,
        materialization_candidate: bool = False,
    ) -> None:
        """Record authoritative observed cache usage for a scope.

        - ``cached_tokens > 0``: VERIFIED_REUSABLE (positive evidence).
        - ``cached_tokens == 0`` after a successful dispatch:
          MATERIALIZATION_CANDIDATE (the request may have warmed the cache
          after the provider measured its miss).
        - ``cached_tokens == 0`` after an unsuccessful dispatch: NEGATIVE
          (degrades confidence without warming prefix memory).
        - ``cached_tokens is None``: no evidence; nothing recorded.
        """
        with self._generation_lock:
            if generation is not None:
                current = self._scope_generations.get(scope)
                if current is not None and generation < current:
                    return
                if current is None:
                    # The bounded registry no longer knows this attempt. Do
                    # not let a delayed completion recreate an old entry just
                    # because its blocks happen to match current memory.
                    return
                if blocks is not None and self._memory.current_blocks(scope) != tuple(blocks):
                    # A failed/empty completion does not replace prefix
                    # memory. Its evidence is valid only if the remembered
                    # prefix is still the one dispatched by this attempt.
                    return
                if current != generation:
                    # A terminal miss does not call ``remember`` because it
                    # did not prove that the prefix was materialized. It may
                    # still update evidence when the remembered prefix is
                    # unchanged since dispatch. A different current prefix
                    # means this completion is stale and must be ignored.
                    if blocks is None or self._memory.current_blocks(scope) != tuple(blocks):
                        return
                    self._track_generation(scope, generation)
            self._evidence.record(
                scope,
                cached_tokens,
                materialization_candidate=materialization_candidate,
            )

    def record_dispatch(self, scope: CacheScope, *, generation: int | None = None) -> None:
        """Record a successful dispatch (potential warming, no reuse signal).

        This does NOT create positive evidence. It only advances UNKNOWN ->
        POSSIBLY_WARMED so the model can represent "a request reached this
        destination and may have populated its cache".
        """
        with self._generation_lock:
            if generation is not None:
                if self._scope_generations.get(scope) != generation:
                    return
                self._scope_generations.move_to_end(scope)
            self._evidence.record_dispatch(scope)

    def _hash(self, value: str) -> str:
        if not value:
            return ""
        return _hmac_digest(self._secret, value.encode("utf-8"))


__all__ = [
    "DEFAULT_BLOCK_SIZE_TOKENS",
    "DEFAULT_MIN_MATCH_TOKENS",
    "DEFAULT_TTL_SEC",
    "Block",
    "CacheAdjustment",
    "CacheAwareCostEstimator",
    "CacheLocalityEvidenceState",
    "CacheScope",
    "CacheSignal",
    "PrefixCacheCoordinator",
    "PrefixCacheCostRecord",
    "SessionProviderPrefixMemory",
    "_CacheLocalityEstimator",
    "_CacheLocalityEvidence",
    "build_blocks",
    "canonicalize_prompt",
    "longest_common_prefix_tokens",
    "price_delta_per_token",
]
