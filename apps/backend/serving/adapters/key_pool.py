"""Multi-key API rotation with sequential (use-one-until-it-errors) selection.

Each adapter that opts into multi-key holds a KeyPool. The pool exposes
``acquire(affinity_key)`` and ``release(lease, outcome)``. State is
in-process, behind a single ``threading.Lock``.

Selection is **sequential**: the pool always hands out the earliest usable
key and only advances to a later key once an earlier one is muted. A key is
muted for ``MUTE_SECONDS`` (5 minutes) on upstream failures that are
key-specific or transient — rate limit/quota (429), auth/permission
(401/402/403), request-timeout / too-early (408/425), any 5xx, and non-HTTP
failures such as timeouts and connection errors. Request-scoped client errors
(other 4xx like 400/404/422) do *not* mute: they fail identically on every
key, so muting would take the whole pool down. The net effect is "use one key
until it runs out of quota or errors, then move to the next".

Key-specific failures (429/401/402/403) normally mute even the *last* usable
key, since retrying that key immediately cannot succeed. But for a pool with
only one key configured, that means every rate-limit blip costs a flat
5-minute total blackout with nothing to rotate to. To keep that blip-tolerant
without giving up the safety net for a genuinely dead/exhausted key, the sole
remaining key instead gets a couple of free passes
(``SOLE_KEY_BACKOFF_THRESHOLD``) before it starts muting, then backs off
exponentially from ``SOLE_KEY_BACKOFF_BASE_SECONDS`` up to the same
``MUTE_SECONDS`` ceiling as a sustained failure streak continues.

**Tier reservation.** Each key carries a ``min_role`` (default ``"free"`` — no
reservation). A caller whose role does not meet a key's ``min_role`` never sees
that key: it is filtered out of ``acquire``, out ``size``, and out of the
sole-key bookkeeping in ``release``. Among the keys a caller *can* use,
reserved keys are preferred over shared ones (highest ``min_role`` first, then
configuration order), so an entitled caller drains the capacity set aside for
it before touching the pool every tier shares. ``role=None`` means an
unrestricted internal caller (health probes, warmups) and sees every key.

See docs/agents/specs/archive/2026-04-30-multi-key-rotation-design.md for the
original (least-loaded) design this supersedes.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from serving.config.settings import ROLE_RANK, has_role

# 4xx statuses that are key-specific (not request-scoped) and so should mute the
# key and trigger rotation: auth/permission, payment, request-timeout/too-early,
# and rate limit/quota. All other 4xx are treated as request-scoped.
_MUTABLE_4XX = frozenset({401, 402, 403, 408, 425, 429})

# The subset of mutable statuses where the *key itself* is the problem — quota
# exhausted (429) or auth/payment rejected (401/402/403). These mute the key
# even when it is the only usable one, since retrying the same key cannot
# succeed. Every other mutable status (408/425, 5xx, network sentinel 0) is
# transient / provider-side: muting the sole remaining key on a blip would take
# the route offline for no reason, so the last usable key is kept for those.
_KEY_SPECIFIC_STATUSES = frozenset({401, 402, 403, 429})


def should_mute_status(status_code: int) -> bool:
    """Whether an upstream outcome should mute the key and trigger rotation.

    Mutes on failures that are key-specific or transient: rate limit/quota
    (429), auth/permission (401/402/403), request-timeout / too-early
    (408/425), any 5xx, and non-HTTP failures (``status_code == 0`` for
    timeouts / connection errors). A 2xx response and request-scoped client
    errors (other 4xx such as 400/404/422) do not mute — a bad request fails
    identically on every key, so muting it would disable the whole pool.
    """
    if 200 <= status_code < 300:
        return False
    if status_code == 0:
        return True
    if status_code in _MUTABLE_4XX:
        return True
    return 500 <= status_code <= 599


def is_key_specific_status(status_code: int) -> bool:
    """Whether the status means the key itself is unusable (quota/auth/payment).

    Key-specific failures mute the key even when it is the only one left.
    Transient / provider-side failures (408/425, 5xx, network) instead keep the
    last usable key alive so a single blip cannot take a sole-key route offline.
    """
    return status_code in _KEY_SPECIFIC_STATUSES


DEFAULT_MIN_ROLE = "free"


def normalize_min_role(min_role: str | None) -> str:
    """Return a validated per-key ``min_role``, defaulting to no reservation.

    Unknown or empty values fall back to ``"free"`` (unreserved) rather than
    raising: a key that cannot be interpreted must keep serving traffic, and the
    admin API validates the value before it is ever persisted.
    """
    if isinstance(min_role, str) and min_role in ROLE_RANK:
        return min_role
    return DEFAULT_MIN_ROLE


class KeyPoolExhausted(Exception):
    """Raised by ``KeyPool.acquire`` when no key is usable by the caller.

    Either every key is in cooldown, or the ones still usable are all reserved
    for a higher tier than the caller holds.
    """


@dataclass
class _KeyState:
    key: str
    request_count: int = 0
    cooldown_until: float = 0.0  # monotonic timestamp
    removed: bool = False
    # Lowest role allowed to use this key. ``"free"`` (the default) means the
    # key is shared by every tier; anything higher reserves it.
    min_role: str = DEFAULT_MIN_ROLE
    # Consecutive mute-worthy failures since the last 2xx, used to back off
    # the sole-remaining-key mute duration (see release()). Reset on success;
    # request-scoped 4xx (never mute-worthy either way) leaves it untouched
    # since it says nothing about this key's own health.
    consecutive_failures: int = 0


@dataclass
class _Affinity:
    key_index: int
    expires_at: float  # monotonic timestamp


def _role_may_use(role: str | None, state: _KeyState) -> bool:
    """Whether a caller holding *role* is entitled to this key.

    ``role=None`` is an unrestricted internal caller (health probe, warmup) and
    may use any key. Everyone else must meet the key's ``min_role``.
    """
    if state.min_role == DEFAULT_MIN_ROLE:
        return True
    if role is None:
        return True
    return has_role(role, state.min_role)


@dataclass
class Lease:
    """Round-trip token returned by ``KeyPool.acquire`` and consumed by ``release``.

    ``role`` is the caller's role at acquire time (None for an unrestricted
    internal caller). ``release`` replays it so the sole-remaining-key backoff is
    judged against the keys *this* caller could have rotated to — a key reserved
    for a higher tier is not a fallback for a free-tier request.
    """

    key_index: int
    affinity_key: str
    role: str | None = None


class KeyPool:
    """Hands out API keys sequentially, muting a key on a key-specific error."""

    AFFINITY_TTL_SECONDS: float = 300.0  # 5 minutes
    MUTE_SECONDS: float = 300.0  # 5 minutes — key-specific/transient errors mute the key
    SWEEP_THRESHOLD: int = 1000
    # Sole-remaining-key backoff for key-specific failures (429/401/402/403):
    # tolerate this many consecutive failures with no mute at all, then start
    # muting at BASE_SECONDS and double on each further consecutive failure,
    # capped at MUTE_SECONDS. Multi-key mutes are unaffected — they mute
    # immediately since another key can still serve traffic.
    SOLE_KEY_BACKOFF_THRESHOLD: int = 2
    SOLE_KEY_BACKOFF_BASE_SECONDS: float = 15.0

    def __init__(
        self,
        keys: list[str],
        provider_label: str,
        min_roles: dict[str, str] | None = None,
    ) -> None:
        if not keys:
            raise ValueError("KeyPool requires at least one key")
        roles = min_roles or {}
        self._keys: list[_KeyState] = [
            _KeyState(key=k, min_role=normalize_min_role(roles.get(k))) for k in keys
        ]
        self._affinity: dict[str, _Affinity] = {}
        self._lock = threading.Lock()
        self._provider_label = provider_label

    def size(self, role: str | None = None) -> int:
        """Return how many active (non-removed) keys *role* is allowed to use.

        ``role=None`` counts every active key (unrestricted internal caller).
        Muted keys still count — this bounds the caller's rotation attempts.
        """
        with self._lock:
            return sum(1 for s in self._keys if not s.removed and _role_may_use(role, s))

    def snapshot_keys(self) -> list[str]:
        """Return a snapshot of every active key currently in the pool."""
        with self._lock:
            return [s.key for s in self._keys if not s.removed]

    def snapshot_min_roles(self) -> dict[str, str]:
        """Return ``{key: min_role}`` for every active key in the pool."""
        with self._lock:
            return {s.key: s.min_role for s in self._keys if not s.removed}

    def add_key(self, key: str, *, min_role: str | None = None) -> int:
        """Add a key to the pool, returning its slot index.

        Idempotent: if ``key`` is already present (active or removed), the
        existing slot is reactivated and returned. Otherwise a new slot is
        appended. ``min_role`` reserves the key for that tier and above; passing
        None on a re-add leaves an existing slot's reservation untouched.
        """
        with self._lock:
            for idx, state in enumerate(self._keys):
                if state.key == key:
                    state.removed = False
                    if min_role is not None:
                        state.min_role = normalize_min_role(min_role)
                    return idx
            self._keys.append(_KeyState(key=key, min_role=normalize_min_role(min_role)))
            return len(self._keys) - 1

    def set_key_min_role(self, key: str, min_role: str | None) -> bool:
        """Re-tier an active key in place. Returns True when a key was updated.

        Affinity entries bound to the key are dropped so a caller who no longer
        qualifies is re-picked on its next request instead of riding a stale
        binding to a key it may not be entitled to.
        """
        normalized = normalize_min_role(min_role)
        with self._lock:
            for idx, state in enumerate(self._keys):
                if state.key == key and not state.removed:
                    if state.min_role == normalized:
                        return True
                    state.min_role = normalized
                    stale = [k for k, a in self._affinity.items() if a.key_index == idx]
                    for k in stale:
                        del self._affinity[k]
                    return True
            return False

    def remove_key(self, key: str) -> bool:
        """Mark a key as removed and drop affinity entries pointing at it.

        Returns True if the key was present and removed, False otherwise.
        Slots are tombstoned (not popped) so existing key indices remain
        stable for in-flight leases.
        """
        with self._lock:
            for idx, state in enumerate(self._keys):
                if state.key == key and not state.removed:
                    state.removed = True
                    stale = [k for k, a in self._affinity.items() if a.key_index == idx]
                    for k in stale:
                        del self._affinity[k]
                    return True
            return False

    def affinity_count(self) -> int:
        """Return the number of active per-user affinity entries."""
        return len(self._affinity)

    def acquire(self, affinity_key: str, *, role: str | None = None) -> tuple[str, Lease]:
        """Return (api_key, lease) for the caller, creating affinity as needed.

        Keys reserved above *role* are invisible to this call — they are neither
        selected nor honored through an existing affinity binding. ``role=None``
        is an unrestricted internal caller.

        Raises:
            KeyPoolExhausted: if every key the caller may use is in cooldown
                (or the caller may use none at all).
        """
        now = time.monotonic()
        with self._lock:
            self._maybe_sweep_locked(now)

            existing = self._affinity.get(affinity_key)
            if existing is not None:
                bound = self._keys[existing.key_index]
                # Affinity is honored only when it is still valid AND the bound
                # key is not cooled down, removed, or reserved above the caller
                # (a role can change, or the key can be re-tiered, under a live
                # binding).
                if (
                    now < existing.expires_at
                    and not bound.removed
                    and bound.cooldown_until <= now
                    and _role_may_use(role, bound)
                ):
                    idx = existing.key_index
                    self._keys[idx].request_count += 1
                    return self._keys[idx].key, Lease(idx, affinity_key, role)
                # Drop stale or unusable affinity; we'll re-pick below.
                del self._affinity[affinity_key]

            idx = self._pick_first_available_locked(now, role)
            if idx is None:
                raise KeyPoolExhausted(
                    f"No usable API key for provider {self._provider_label!r} "
                    f"(role={role or 'unrestricted'}, {len(self._keys)} configured): "
                    "every key the caller may use is muted or reserved for a higher tier"
                )

            self._affinity[affinity_key] = _Affinity(
                key_index=idx,
                expires_at=now + self.AFFINITY_TTL_SECONDS,
            )
            self._keys[idx].request_count += 1
            return self._keys[idx].key, Lease(idx, affinity_key, role)

    def _pick_first_available_locked(self, now: float, role: str | None = None) -> int | None:
        """Return the index of the best key *role* may use, or None.

        Sequential selection: traffic concentrates on the earliest key that is
        neither removed nor muted, and only advances to a later key once the
        earlier ones are muted. ``request_count`` is no longer a selection
        signal — it is retained purely for telemetry.

        Reservation reorders that scan rather than replacing it: keys reserved
        for the highest tier the caller still qualifies for come first, then
        configuration order within a tier. An entitled caller therefore spends
        the capacity set aside for it before falling back to the shared keys the
        lower tiers depend on.
        """
        candidates = [
            (-ROLE_RANK.get(state.min_role, 0), i)
            for i, state in enumerate(self._keys)
            if not state.removed and state.cooldown_until <= now and _role_may_use(role, state)
        ]
        if not candidates:
            return None
        return min(candidates)[1]

    def _maybe_sweep_locked(self, now: float) -> None:
        """Drop expired affinity entries when the dict grows past threshold."""
        if len(self._affinity) <= self.SWEEP_THRESHOLD:
            return
        expired = [k for k, a in self._affinity.items() if a.expires_at < now]
        for k in expired:
            del self._affinity[k]

    def release(self, lease: Lease, *, status_code: int | None) -> bool:
        """Report the request outcome; return True iff the key was muted.

        ``status_code=None`` is a NEUTRAL release: the outcome is unknown
        (e.g. a mid-open I/O error that may have fired after the upstream
        already returned 2xx). The key is released completely unchanged —
        neither muted nor credited with a success. Releasing such failures
        as 200 would reset ``consecutive_failures`` and defeat the sole-key
        backoff during a sustained outage that mixes 429s with resets.

        Mutes the leased key when ``should_mute_status(status_code)`` is true
        — i.e. for key-specific or transient failures (429, 401/402/403,
        408/425, 5xx, and non-HTTP failures signalled by ``status_code ==
        0``). A 2xx response and request-scoped client errors (other 4xx)
        leave the key usable.

        Guard: a transient / provider-side failure (408/425/5xx/network)
        never mutes the *last* usable key, so a single upstream blip cannot
        take a sole-key route offline (subsequent requests would otherwise
        hit KeyPoolExhausted without even attempting the provider).

        Key-specific failures (quota/auth/payment: 429/401/402/403) mute
        immediately at the full ``MUTE_SECONDS`` when another key can take
        over. When this is the *last* usable key, though, there is nowhere to
        rotate to — muting immediately at the full 5 minutes would turn every
        rate-limit blip into a flat 5-minute blackout. Instead the sole key
        gets ``SOLE_KEY_BACKOFF_THRESHOLD`` consecutive failures for free,
        then backs off from ``SOLE_KEY_BACKOFF_BASE_SECONDS``, doubling per
        additional consecutive failure, capped at ``MUTE_SECONDS`` — tolerant
        of a momentary blip, still self-protecting against a sustained outage
        or genuinely exhausted key.

        Args:
            lease: the lease returned by ``acquire``.
            status_code: HTTP status code, 0 for non-HTTP failures
                (timeouts / network errors), or None for a neutral release.

        Returns:
            True if the key was muted (caller should rotate to another key),
            False otherwise (caller should propagate the error).
        """
        if status_code is None:
            return False
        if not should_mute_status(status_code):
            if 200 <= status_code < 300:
                with self._lock:
                    self._keys[lease.key_index].consecutive_failures = 0
            return False
        with self._lock:
            now = time.monotonic()
            state = self._keys[lease.key_index]
            state.consecutive_failures += 1
            is_sole_key = not self._has_other_usable_key_locked(lease.key_index, now, lease.role)

            if not is_key_specific_status(status_code):
                if is_sole_key:
                    # Transient error on the only usable key — keep it in service.
                    return False
                state.cooldown_until = now + self.MUTE_SECONDS
                return True

            if is_sole_key:
                if state.consecutive_failures <= self.SOLE_KEY_BACKOFF_THRESHOLD:
                    # A couple of free passes — there's no key to rotate to
                    # anyway, so a single blip shouldn't cost 5 minutes.
                    return False
                backoff_step = min(
                    state.consecutive_failures - self.SOLE_KEY_BACKOFF_THRESHOLD - 1,
                    8,  # 2**8 * BASE already exceeds MUTE_SECONDS; caps the exponent
                )
                duration = min(
                    self.SOLE_KEY_BACKOFF_BASE_SECONDS * (2**backoff_step),
                    self.MUTE_SECONDS,
                )
                state.cooldown_until = now + duration
                return True

            state.cooldown_until = now + self.MUTE_SECONDS
            return True

    def _has_other_usable_key_locked(
        self, exclude_idx: int, now: float, role: str | None = None
    ) -> bool:
        """Whether another key *role* may use is active and not muted.

        Judged from the leaseholder's perspective: a key reserved above *role*
        is not somewhere this caller can rotate to, so it must not cancel the
        sole-key protection that keeps the caller's last key in service.
        """
        for i, state in enumerate(self._keys):
            if i == exclude_idx or state.removed:
                continue
            if state.cooldown_until > now:
                continue
            if not _role_may_use(role, state):
                continue
            return True
        return False
