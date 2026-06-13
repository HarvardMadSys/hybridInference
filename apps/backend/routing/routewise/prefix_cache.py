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
    from collections.abc import Callable, Mapping, Sequence
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

    It captures the cache discount estimate for a candidate. Callers may apply
    it only when guarded cost adjustment is enabled.
    """

    matched_prefix_tokens: int
    expected_cached_tokens: float
    cache_discount: float
    would_apply: bool
    has_history: bool
    meets_threshold: bool


class PrefixCacheCoordinator:
    """Router-facing coordinator for session-scoped prefix-cache estimates.

    Owns the bounded memory and the cost estimator and centralizes the request
    mapping decisions (which fields form a scope, which are hashed). Sensitive
    scope fields (user, project, session, params) are HMAC hashed so no raw
    identifier is stored.
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
    ) -> None:
        self.enabled = bool(enabled)
        self._memory = memory if memory is not None else SessionProviderPrefixMemory()
        self._estimator = estimator if estimator is not None else CacheAwareCostEstimator()
        self._block_size = int(block_size)
        self._secret = secret if secret is not None else _PROCESS_SECRET
        self._tokenize = tokenize

    @property
    def memory(self) -> SessionProviderPrefixMemory:
        """Expose the backing prefix memory for metrics and tests."""
        return self._memory

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
        """Look up one candidate and return its prefix-cache cost record."""
        signal = self._memory.lookup(scope, blocks, now=now)
        adjustment = self._estimator.adjust(cold_cost, signal, price_delta)
        return PrefixCacheCostRecord(
            matched_prefix_tokens=signal.matched_prefix_tokens,
            expected_cached_tokens=signal.expected_cached_tokens,
            cache_discount=adjustment.cache_discount,
            would_apply=adjustment.applied,
            has_history=signal.has_history,
            meets_threshold=signal.meets_threshold,
        )

    def remember(
        self,
        scope: CacheScope,
        blocks: Sequence[Block],
        *,
        now: float | None = None,
    ) -> None:
        """Store the selected provider's blocks so the next turn can match them."""
        self._memory.observe(
            scope,
            blocks,
            now=now,
        )

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
    "CacheScope",
    "CacheSignal",
    "PrefixCacheCoordinator",
    "PrefixCacheCostRecord",
    "SessionProviderPrefixMemory",
    "build_blocks",
    "canonicalize_prompt",
    "longest_common_prefix_tokens",
    "price_delta_per_token",
]
