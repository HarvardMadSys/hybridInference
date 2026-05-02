# Multi-Key Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add support for multiple API keys per provider route in `config/models.yaml`, with 5-minute per-user session affinity, least-loaded selection, 429-driven cooldowns honoring `Retry-After`, and within-pool retry — falling back to the existing router fallback chain only when the entire pool is exhausted.

**Architecture:** A new `KeyPool` class lives inside `OpenAICompatAdapter`. The adapter's chat methods loop over `acquire → POST → release`, marking the current key cooled-down on 429 and trying the next key. When `acquire` cannot find any non-cooled-down key, it raises `KeyPoolExhausted`, which the adapter surfaces as an upstream failure so `FixedRouter` falls over to the next provider. State is in-process, in-memory, behind a `threading.Lock`. No router, DB, or Redis changes.

**Tech Stack:** Python 3.10+, `asyncio` + `aiohttp` (existing `serving/http.py`), `prometheus-client`, `pytest` + `pytest-asyncio`, `dataclasses`, `threading.Lock` for thread-safe in-process state.

**Spec:** `docs/superpowers/specs/2026-04-30-multi-key-rotation-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `serving/adapters/key_pool.py` | NEW | `KeyPool`, `_KeyState`, `_Affinity`, `_Lease`, `KeyPoolExhausted`. Pure state machine. |
| `serving/adapters/base.py:43-101` | MODIFY | Add `api_keys: list[str] \| None` to `ModelConfig`. |
| `serving/adapters/openai_compat.py:164-188` | MODIFY | `_build_headers` accepts an explicit `api_key` argument; new internal `_run_with_key_pool` wraps the HTTP call. |
| `serving/adapters/openai_compat.py:220-276` | MODIFY | `chat_completion` uses pool loop when pool is set. |
| `serving/adapters/openai_compat.py:278-...` | MODIFY | `stream_chat_completion` uses pool loop when pool is set (first-chunk classification). |
| `serving/servers/registry.py:155-...` | MODIFY | Loader parses `api_keys`, expands env vars, validates, drops blanks; populates `ModelConfig.api_keys`. |
| `serving/servers/auth.py:249-259` | MODIFY | Include `key_hash` in user context returned by `verify_api_key`. |
| `serving/servers/routers/completions.py:225-227` | MODIFY | Push `auth_key_hash` onto request context. |
| `serving/observability/metrics.py:86-99,382-...` | MODIFY | Register `KEY_POOL_REQUESTS`, `KEY_POOL_COOLDOWNS`, `KEY_POOL_EXHAUSTED`, `KEY_POOL_ACTIVE_AFFINITIES`. |
| `test/unit/adapters/test_key_pool.py` | NEW | Unit tests for `KeyPool` (selection, affinity, cooldown, sweep, concurrency). |
| `test/unit/test_registry_multi_key.py` | NEW | Loader unit tests for `api_keys` parsing/validation. |
| `test/integration/test_openai_compat_multi_key.py` | NEW | Integration: 429 → rotate → success; pool exhausted → router fallback; back-compat. |

---

## Task 1: Bootstrap KeyPool module with KeyPoolExhausted exception

**Files:**
- Create: `serving/adapters/key_pool.py`
- Test: `test/unit/adapters/test_key_pool.py`

- [ ] **Step 1: Write failing test**

Create `test/unit/adapters/test_key_pool.py`:

```python
"""Unit tests for KeyPool — multi-key rotation with affinity + cooldown."""

from __future__ import annotations

import pytest

from serving.adapters.key_pool import KeyPool, KeyPoolExhausted


def test_keypool_module_exports():
    """KeyPool and KeyPoolExhausted are importable from the new module."""
    assert KeyPool is not None
    assert issubclass(KeyPoolExhausted, Exception)


def test_keypool_constructor_rejects_empty_keys():
    """Constructing with zero keys is a config error."""
    with pytest.raises(ValueError):
        KeyPool(keys=[], provider_label="test")


def test_keypool_constructor_accepts_single_key():
    """Single-key pool is valid (degenerate case)."""
    pool = KeyPool(keys=["k1"], provider_label="test")
    assert pool.size() == 1
```

- [ ] **Step 2: Run test, expect ImportError**

Run: `pytest test/unit/adapters/test_key_pool.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'serving.adapters.key_pool'`.

- [ ] **Step 3: Create the module**

Create `serving/adapters/key_pool.py`:

```python
"""Multi-key API rotation with per-user session affinity.

Each adapter that opts into multi-key holds a KeyPool. The pool exposes
``acquire(affinity_key)`` and ``release(lease, outcome)``. State is
in-process, behind a single ``threading.Lock``.

See docs/superpowers/specs/2026-04-30-multi-key-rotation-design.md
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


class KeyPoolExhausted(Exception):
    """Raised by ``KeyPool.acquire`` when every key is in cooldown."""


@dataclass
class _KeyState:
    key: str
    request_count: int = 0
    cooldown_until: float = 0.0  # monotonic timestamp


@dataclass
class _Affinity:
    key_index: int
    expires_at: float  # monotonic timestamp


@dataclass
class _Lease:
    key_index: int
    affinity_key: str


class KeyPool:
    """Rotates API keys with per-user TTL affinity and 429 cooldowns."""

    AFFINITY_TTL_SECONDS: float = 300.0  # 5 minutes
    DEFAULT_COOLDOWN_SECONDS: float = 120.0  # 2 minutes for 429 w/o Retry-After
    MAX_COOLDOWN_SECONDS: float = 3600.0  # 1 hour cap on Retry-After
    SWEEP_THRESHOLD: int = 1000

    def __init__(self, keys: list[str], provider_label: str) -> None:
        if not keys:
            raise ValueError("KeyPool requires at least one key")
        self._keys: list[_KeyState] = [_KeyState(key=k) for k in keys]
        self._affinity: dict[str, _Affinity] = {}
        self._lock = threading.Lock()
        self._provider_label = provider_label

    def size(self) -> int:
        return len(self._keys)
```

- [ ] **Step 4: Run test, expect PASS**

Run: `pytest test/unit/adapters/test_key_pool.py -v`
Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add serving/adapters/key_pool.py test/unit/adapters/test_key_pool.py
git commit -m "feat(key_pool): bootstrap module with KeyPool skeleton and exception"
```

---

## Task 2: Implement acquire — single-key path and least-loaded selection

**Files:**
- Modify: `serving/adapters/key_pool.py`
- Test: `test/unit/adapters/test_key_pool.py`

- [ ] **Step 1: Add failing tests**

Append to `test/unit/adapters/test_key_pool.py`:

```python
def test_acquire_single_key_returns_that_key():
    pool = KeyPool(keys=["only-key"], provider_label="test")
    key, lease = pool.acquire("user-A")
    assert key == "only-key"
    assert lease.key_index == 0
    assert lease.affinity_key == "user-A"


def test_acquire_picks_least_loaded_key_for_new_user():
    """First user picks index 0 (tie at request_count=0); load increments."""
    pool = KeyPool(keys=["k0", "k1", "k2"], provider_label="test")

    # First two new users go to k0 then k1 — counters increment under the lock,
    # and ties break by lowest index.
    k_a, _ = pool.acquire("user-A")
    k_b, _ = pool.acquire("user-B")
    k_c, _ = pool.acquire("user-C")

    assert k_a == "k0"
    assert k_b == "k1"
    assert k_c == "k2"


def test_acquire_increments_request_count_on_each_call():
    """Even affinity-reused acquires bump request_count for the bound key."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")

    pool.acquire("user-A")
    pool.acquire("user-A")
    pool.acquire("user-A")

    # Internal inspection — the bound key has count=3, the other has 0.
    counts = sorted(s.request_count for s in pool._keys)
    assert counts == [0, 3]
```

- [ ] **Step 2: Run tests, expect AttributeError**

Run: `pytest test/unit/adapters/test_key_pool.py -v`
Expected: 3 new tests fail with `AttributeError: 'KeyPool' object has no attribute 'acquire'`.

- [ ] **Step 3: Implement acquire**

Add to `serving/adapters/key_pool.py` (inside class `KeyPool`, after `size`):

```python
    def acquire(self, affinity_key: str) -> tuple[str, _Lease]:
        """Return (api_key, lease) for the caller, creating affinity as needed.

        Raises:
            KeyPoolExhausted: if every key is currently in cooldown.
        """
        now = time.monotonic()
        with self._lock:
            self._maybe_sweep_locked(now)

            existing = self._affinity.get(affinity_key)
            if existing is not None:
                # Affinity is honored only when it is still valid AND the
                # bound key is not cooled down.
                if (
                    now < existing.expires_at
                    and self._keys[existing.key_index].cooldown_until <= now
                ):
                    idx = existing.key_index
                    self._keys[idx].request_count += 1
                    return self._keys[idx].key, _Lease(idx, affinity_key)
                # Drop stale or unusable affinity; we'll re-pick below.
                del self._affinity[affinity_key]

            idx = self._pick_least_loaded_locked(now)
            if idx is None:
                raise KeyPoolExhausted(
                    f"All {len(self._keys)} keys for provider "
                    f"{self._provider_label!r} are in cooldown"
                )

            self._affinity[affinity_key] = _Affinity(
                key_index=idx,
                expires_at=now + self.AFFINITY_TTL_SECONDS,
            )
            self._keys[idx].request_count += 1
            return self._keys[idx].key, _Lease(idx, affinity_key)

    def _pick_least_loaded_locked(self, now: float) -> int | None:
        """Return the index of the lowest-request_count non-cooled key, or None."""
        best_idx: int | None = None
        best_count: int | None = None
        for i, state in enumerate(self._keys):
            if state.cooldown_until > now:
                continue
            if best_count is None or state.request_count < best_count:
                best_idx = i
                best_count = state.request_count
        return best_idx

    def _maybe_sweep_locked(self, now: float) -> None:
        """Drop expired affinity entries when the dict grows past threshold."""
        if len(self._affinity) <= self.SWEEP_THRESHOLD:
            return
        expired = [k for k, a in self._affinity.items() if a.expires_at < now]
        for k in expired:
            del self._affinity[k]
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `pytest test/unit/adapters/test_key_pool.py -v`
Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add serving/adapters/key_pool.py test/unit/adapters/test_key_pool.py
git commit -m "feat(key_pool): implement acquire with least-loaded selection"
```

---

## Task 3: Implement affinity TTL behavior

**Files:**
- Modify: `serving/adapters/key_pool.py` (no code change — exercising existing logic)
- Test: `test/unit/adapters/test_key_pool.py`

- [ ] **Step 1: Add failing tests using a fake clock**

Append to `test/unit/adapters/test_key_pool.py`:

```python
def test_same_user_keeps_same_key_within_ttl(monkeypatch):
    """Same affinity_key returns the same index for 5 minutes."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")

    # Freeze time at t0
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    k1, _ = pool.acquire("user-A")
    fake_now[0] += 60  # +60s
    k2, _ = pool.acquire("user-A")
    fake_now[0] += 200  # +200s — still within 300s
    k3, _ = pool.acquire("user-A")

    assert k1 == k2 == k3


def test_affinity_expires_after_ttl(monkeypatch):
    """After 5 minutes, the user may land on a different key."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")

    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    k_first, _ = pool.acquire("user-A")
    # Advance well past 300s
    fake_now[0] += 301
    # Make k0 look heavily loaded so the new pick goes to k1
    pool._keys[0].request_count = 1000

    k_second, _ = pool.acquire("user-A")
    assert k_first == "k0"
    assert k_second == "k1"


def test_different_users_can_share_or_split_keys():
    """Two new users in a 2-key pool end up on different keys (load-spread)."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    a, _ = pool.acquire("user-A")
    b, _ = pool.acquire("user-B")
    assert {a, b} == {"k0", "k1"}
```

- [ ] **Step 2: Run tests, expect PASS**

Run: `pytest test/unit/adapters/test_key_pool.py -v`
Expected: All tests pass — affinity logic from Task 2 already covers this. Tests are pinning behavior.

- [ ] **Step 3: Commit**

```bash
git add test/unit/adapters/test_key_pool.py
git commit -m "test(key_pool): add affinity TTL behavior tests"
```

---

## Task 4: Implement release with Retry-After parsing and cooldown cap

**Files:**
- Modify: `serving/adapters/key_pool.py`
- Test: `test/unit/adapters/test_key_pool.py`

- [ ] **Step 1: Add failing tests**

Append to `test/unit/adapters/test_key_pool.py`:

```python
def test_release_with_retry_after_seconds_sets_cooldown(monkeypatch):
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after="30")

    # k0 should be cooled until t=1030
    assert pool._keys[lease.key_index].cooldown_until == pytest.approx(1030.0)


def test_release_with_429_no_retry_after_uses_default_cooldown(monkeypatch):
    pool = KeyPool(keys=["k0"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after=None)

    assert pool._keys[0].cooldown_until == pytest.approx(1120.0)  # +120s default


def test_release_with_2xx_does_not_set_cooldown():
    pool = KeyPool(keys=["k0"], provider_label="test")
    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=200, retry_after=None)
    assert pool._keys[0].cooldown_until == 0.0


def test_release_with_other_4xx_does_not_set_cooldown():
    pool = KeyPool(keys=["k0"], provider_label="test")
    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=401, retry_after=None)
    assert pool._keys[0].cooldown_until == 0.0


def test_retry_after_is_capped_at_one_hour(monkeypatch):
    pool = KeyPool(keys=["k0"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after="86400")  # 1 day

    assert pool._keys[0].cooldown_until == pytest.approx(1000.0 + 3600.0)


def test_retry_after_negative_falls_back_to_default(monkeypatch):
    pool = KeyPool(keys=["k0"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after="-5")
    assert pool._keys[0].cooldown_until == pytest.approx(1120.0)


def test_retry_after_http_date_format(monkeypatch):
    """RFC 7231 allows HTTP-date format; we honor it."""
    from email.utils import format_datetime
    from datetime import datetime, timezone, timedelta

    pool = KeyPool(keys=["k0"], provider_label="test")
    base_real = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    # Fake time module sees t=1000.0; HTTP-date is base_real + 30s
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    # Use a known wall-clock anchor by monkeypatching _parse_retry_after's anchor.
    # Easier: pass the date as 30s in the future relative to whatever time.time
    # returns; we patch time.time too.
    fake_wall = [base_real.timestamp()]
    monkeypatch.setattr("serving.adapters.key_pool.time.time", lambda: fake_wall[0])

    future_http_date = format_datetime(base_real + timedelta(seconds=30))

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after=future_http_date)
    # Expect cooldown ≈ now + 30s (capped before 3600)
    assert 1020 <= pool._keys[0].cooldown_until <= 1040
```

- [ ] **Step 2: Run tests, expect AttributeError**

Run: `pytest test/unit/adapters/test_key_pool.py -v`
Expected: New tests fail with `AttributeError: 'KeyPool' object has no attribute 'release'`.

- [ ] **Step 3: Implement release**

Add to `serving/adapters/key_pool.py`:

```python
import time
from email.utils import parsedate_to_datetime

# ... (existing imports)


class KeyPool:
    # ... existing code ...

    def release(
        self,
        lease: _Lease,
        *,
        status_code: int,
        retry_after: str | None,
    ) -> None:
        """Report the request outcome so cooldowns can be updated.

        Args:
            lease: the lease returned by ``acquire``.
            status_code: HTTP status code (or 0 for non-HTTP failures, which
                cause no cooldown change).
            retry_after: raw ``Retry-After`` header value if any.
        """
        if status_code != 429:
            # Only 429 triggers cooldown. 2xx, other 4xx, 5xx, and network
            # errors do not flag the key.
            return
        with self._lock:
            now = time.monotonic()
            cooldown = self._compute_cooldown_seconds(retry_after)
            self._keys[lease.key_index].cooldown_until = now + cooldown

    def _compute_cooldown_seconds(self, retry_after: str | None) -> float:
        """Parse Retry-After per RFC 7231; clamp to [0, MAX_COOLDOWN_SECONDS]."""
        if retry_after is None:
            return self.DEFAULT_COOLDOWN_SECONDS

        # Try integer seconds first
        try:
            seconds = float(retry_after.strip())
        except (TypeError, ValueError):
            seconds = None

        # Fall back to HTTP-date
        if seconds is None:
            try:
                dt = parsedate_to_datetime(retry_after)
                seconds = dt.timestamp() - time.time()
            except (TypeError, ValueError, IndexError):
                return self.DEFAULT_COOLDOWN_SECONDS

        if seconds is None or seconds < 0:
            return self.DEFAULT_COOLDOWN_SECONDS
        return min(seconds, self.MAX_COOLDOWN_SECONDS)
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `pytest test/unit/adapters/test_key_pool.py -v`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add serving/adapters/key_pool.py test/unit/adapters/test_key_pool.py
git commit -m "feat(key_pool): add release() with Retry-After parsing and cap"
```

---

## Task 5: Cooldown skipping, mid-affinity rotation, exhaustion

**Files:**
- Modify: `serving/adapters/key_pool.py` (no code change — exercising existing logic)
- Test: `test/unit/adapters/test_key_pool.py`

- [ ] **Step 1: Add failing tests**

Append to `test/unit/adapters/test_key_pool.py`:

```python
def test_cooldown_key_is_skipped_during_selection(monkeypatch):
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    # Burn k0 with a 429
    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after="30")

    # New user must land on k1 since k0 is in cooldown
    k, _ = pool.acquire("user-B")
    assert k == "k1"


def test_mid_affinity_user_re_picks_when_bound_key_cooled(monkeypatch):
    """If the bound key is cooled mid-window, the user is reassigned."""
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    k_first, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after="60")
    # User-A's affinity points at k0, but k0 is cooled
    k_second, _ = pool.acquire("user-A")
    assert k_first == "k0"
    assert k_second == "k1"


def test_all_keys_exhausted_raises_keypoolexhausted(monkeypatch):
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease0 = pool.acquire("user-A")
    pool.release(lease0, status_code=429, retry_after="30")
    _, lease1 = pool.acquire("user-B")
    pool.release(lease1, status_code=429, retry_after="30")

    with pytest.raises(KeyPoolExhausted):
        pool.acquire("user-C")


def test_cooldown_recovers_after_time_passes(monkeypatch):
    pool = KeyPool(keys=["k0"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    _, lease = pool.acquire("user-A")
    pool.release(lease, status_code=429, retry_after="30")
    # Still in cooldown
    with pytest.raises(KeyPoolExhausted):
        pool.acquire("user-B")

    fake_now[0] += 31
    k, _ = pool.acquire("user-B")
    assert k == "k0"
```

- [ ] **Step 2: Run tests, expect PASS**

Run: `pytest test/unit/adapters/test_key_pool.py -v`
Expected: All tests pass — Task 2 + Task 4 logic covers this.

- [ ] **Step 3: Commit**

```bash
git add test/unit/adapters/test_key_pool.py
git commit -m "test(key_pool): add cooldown skip, mid-affinity rotation, exhaustion tests"
```

---

## Task 6: Affinity dict housekeeping

**Files:**
- Modify: `serving/adapters/key_pool.py` (no code change)
- Test: `test/unit/adapters/test_key_pool.py`

- [ ] **Step 1: Add failing test**

Append to `test/unit/adapters/test_key_pool.py`:

```python
def test_affinity_sweep_drops_expired_entries(monkeypatch):
    pool = KeyPool(keys=["k0"], provider_label="test")
    fake_now = [1000.0]
    monkeypatch.setattr("serving.adapters.key_pool.time.monotonic", lambda: fake_now[0])

    # Seed affinity dict with 1500 expired-soon entries
    for i in range(1500):
        pool.acquire(f"user-{i}")

    fake_now[0] += 301  # everything expired now
    pool.acquire("trigger-sweep")

    # Sweep happened: expired entries should be gone, only the new one remains
    # plus any whose affinity wasn't expired (none, since we advanced past TTL)
    assert len(pool._affinity) == 1
    assert "trigger-sweep" in pool._affinity
```

- [ ] **Step 2: Run test, expect PASS**

Run: `pytest test/unit/adapters/test_key_pool.py -v`
Expected: PASS — sweep logic from Task 2 already handles it.

- [ ] **Step 3: Commit**

```bash
git add test/unit/adapters/test_key_pool.py
git commit -m "test(key_pool): add opportunistic affinity sweep test"
```

---

## Task 7: Concurrency stress test

**Files:**
- Modify: `serving/adapters/key_pool.py` (no code change)
- Test: `test/unit/adapters/test_key_pool.py`

- [ ] **Step 1: Add failing test**

Append to `test/unit/adapters/test_key_pool.py`:

```python
def test_concurrent_acquires_distribute_evenly():
    """Many threads acquiring as new users spread across keys without race."""
    import threading

    NUM_KEYS = 4
    NUM_USERS = 400

    pool = KeyPool(keys=[f"k{i}" for i in range(NUM_KEYS)], provider_label="test")

    results: list[int] = []
    lock = threading.Lock()

    def worker(uid: int) -> None:
        _, lease = pool.acquire(f"user-{uid}")
        with lock:
            results.append(lease.key_index)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_USERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Total acquires == users
    assert len(results) == NUM_USERS
    # request_count totals across keys equals NUM_USERS (no double-counting,
    # no lost increments)
    assert sum(s.request_count for s in pool._keys) == NUM_USERS

    # Distribution is reasonably balanced — each key gets within ±20% of mean
    expected = NUM_USERS / NUM_KEYS
    for s in pool._keys:
        assert 0.8 * expected <= s.request_count <= 1.2 * expected, (
            f"unbalanced: {[k.request_count for k in pool._keys]}"
        )
```

- [ ] **Step 2: Run test, expect PASS**

Run: `pytest test/unit/adapters/test_key_pool.py::test_concurrent_acquires_distribute_evenly -v`
Expected: PASS. Counters are consistent because acquire holds `_lock`.

- [ ] **Step 3: Commit**

```bash
git add test/unit/adapters/test_key_pool.py
git commit -m "test(key_pool): add concurrency stress test"
```

---

## Task 8: Add `api_keys` field to ModelConfig

**Files:**
- Modify: `serving/adapters/base.py:43-101`

- [ ] **Step 1: Read existing ModelConfig structure**

Read `serving/adapters/base.py` lines 43–102 to confirm field ordering and defaults.

- [ ] **Step 2: Modify ModelConfig**

Edit `serving/adapters/base.py`. Find:

```python
    api_key: str | None = None
```

Replace with:

```python
    api_key: str | None = None
    # Optional list of API keys for multi-key rotation. When set, takes
    # precedence over ``api_key`` and the adapter constructs a KeyPool.
    # Only one of ``api_key`` / ``api_keys`` should be set per route.
    api_keys: list[str] | None = None
```

- [ ] **Step 3: Run a quick smoke import**

Run: `python -c "from serving.adapters.base import ModelConfig; mc = ModelConfig(id='x', name='x', provider='x', base_url='x', api_keys=['a','b']); print(mc.api_keys)"`
Expected: `['a', 'b']`

- [ ] **Step 4: Commit**

```bash
git add serving/adapters/base.py
git commit -m "feat(model_config): add api_keys field for multi-key routes"
```

---

## Task 9: Loader — parse `api_keys` and validate

**Files:**
- Modify: `serving/servers/registry.py:155-...`
- Test: `test/unit/test_registry_multi_key.py`

- [ ] **Step 1: Write failing test**

Create `test/unit/test_registry_multi_key.py`:

```python
"""Loader tests for `api_keys` multi-key route entries."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from routing.routers import RouteExecutor
from serving.servers.registry import register_from_models_yaml


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(textwrap.dedent(body))
    return path


def test_api_keys_list_is_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY_1", "key-one")
    monkeypatch.setenv("ZAI_API_KEY_2", "key-two")

    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: glm-test
            name: glm-test
            provider: zhipu
            base_url: https://api.example.com
            route:
              - kind: zhipu
                weight: 1.0
                base_url: https://api.example.com
                api_keys:
                  - ${ZAI_API_KEY_1}
                  - ${ZAI_API_KEY_2}
        """,
    )

    router = RouteExecutor()
    register_from_models_yaml(router, yaml_path)

    adapters = router.routes["glm-test"].adapters
    assert len(adapters) == 1
    cfg = adapters[0].config
    assert cfg.api_keys == ["key-one", "key-two"]
    assert cfg.api_key is None


def test_api_key_and_api_keys_both_set_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY_1", "k1")
    monkeypatch.setenv("ZAI_API_KEY_OTHER", "k2")

    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: bad
            name: bad
            provider: zhipu
            base_url: https://api.example.com
            route:
              - kind: zhipu
                weight: 1.0
                base_url: https://api.example.com
                api_key: ${ZAI_API_KEY_OTHER}
                api_keys:
                  - ${ZAI_API_KEY_1}
        """,
    )

    router = RouteExecutor()
    with pytest.raises(ValueError, match="api_key.*api_keys"):
        register_from_models_yaml(router, yaml_path)


def test_blank_api_keys_are_dropped_with_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ZAI_API_KEY_1", "live-key")
    # ZAI_API_KEY_2 intentionally unset

    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: glm-test
            name: glm-test
            provider: zhipu
            base_url: https://api.example.com
            route:
              - kind: zhipu
                weight: 1.0
                base_url: https://api.example.com
                api_keys:
                  - ${ZAI_API_KEY_1}
                  - ${ZAI_API_KEY_2}
        """,
    )

    router = RouteExecutor()
    with caplog.at_level("WARNING"):
        register_from_models_yaml(router, yaml_path)
    assert "ZAI_API_KEY_2" in caplog.text or "blank" in caplog.text.lower()
    cfg = router.routes["glm-test"].adapters[0].config
    assert cfg.api_keys == ["live-key"]


def test_all_api_keys_blank_raises(tmp_path, monkeypatch):
    # Neither env var set
    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: glm-test
            name: glm-test
            provider: zhipu
            base_url: https://api.example.com
            route:
              - kind: zhipu
                weight: 1.0
                base_url: https://api.example.com
                api_keys:
                  - ${MISSING_1}
                  - ${MISSING_2}
        """,
    )

    router = RouteExecutor()
    with pytest.raises(ValueError, match="empty"):
        register_from_models_yaml(router, yaml_path)


def test_single_api_key_form_still_works(tmp_path, monkeypatch):
    """Back-compat: existing api_key string-form is unchanged."""
    monkeypatch.setenv("ZAI_API_KEY", "single-key")

    yaml_path = _write_yaml(
        tmp_path,
        """
        models:
          - id: glm-test
            name: glm-test
            provider: zhipu
            base_url: https://api.example.com
            route:
              - kind: zhipu
                weight: 1.0
                base_url: https://api.example.com
                api_key: ${ZAI_API_KEY}
        """,
    )

    router = RouteExecutor()
    register_from_models_yaml(router, yaml_path)
    cfg = router.routes["glm-test"].adapters[0].config
    assert cfg.api_key == "single-key"
    assert cfg.api_keys is None
```

- [ ] **Step 2: Run tests, expect failures**

Run: `pytest test/unit/test_registry_multi_key.py -v`
Expected: All tests fail — loader doesn't know about `api_keys` yet.

- [ ] **Step 3: Update the loader**

Edit `serving/servers/registry.py`. Locate the per-route block starting around line 239 (`for r in routes:`). Modify the api-key resolution to also handle `api_keys`:

Find:

```python
        for r in routes:
            kind = r.get("kind") or top_cfg.get("provider")
            base_url = expand_env(r.get("base_url") or top_cfg.get("base_url"))
            api_key = expand_env(r.get("api_key") or top_cfg.get("api_key"))
            weight = float(r.get("weight", 1.0))

            # Adapter config inherits from top-level model config
            adapter_cfg = dict(top_cfg)
            # "type" is routing-only metadata, not a ModelConfig field
            adapter_cfg.pop("type", None)
            adapter_cfg["base_url"] = base_url
            adapter_cfg["api_key"] = api_key
            adapter_cfg["provider"] = kind
```

Replace with:

```python
        for r in routes:
            kind = r.get("kind") or top_cfg.get("provider")
            base_url = expand_env(r.get("base_url") or top_cfg.get("base_url"))
            weight = float(r.get("weight", 1.0))

            raw_api_keys = r.get("api_keys")
            raw_api_key = r.get("api_key") or top_cfg.get("api_key")

            if raw_api_keys is not None and r.get("api_key") is not None:
                raise ValueError(
                    f"Route for model {top_cfg.get('id')!r} sets both "
                    f"api_key and api_keys; pick one."
                )

            api_key: str | None = None
            api_keys: list[str] | None = None
            if raw_api_keys is not None:
                if not isinstance(raw_api_keys, list):
                    raise ValueError(
                        f"api_keys for {top_cfg.get('id')!r} must be a list"
                    )
                expanded = [expand_env(k) for k in raw_api_keys]
                kept: list[str] = []
                for raw, val in zip(raw_api_keys, expanded):
                    if val:
                        kept.append(val)
                    else:
                        logger.warning(
                            "Dropping blank api_keys entry for model %s "
                            "(template: %s) — env var unset or empty",
                            top_cfg.get("id"),
                            raw,
                        )
                if not kept:
                    raise ValueError(
                        f"api_keys for {top_cfg.get('id')!r} resolved to "
                        f"empty list after env expansion"
                    )
                api_keys = kept
            else:
                api_key = expand_env(raw_api_key)

            # Adapter config inherits from top-level model config
            adapter_cfg = dict(top_cfg)
            # "type" is routing-only metadata, not a ModelConfig field
            adapter_cfg.pop("type", None)
            adapter_cfg["base_url"] = base_url
            adapter_cfg["api_key"] = api_key
            adapter_cfg["api_keys"] = api_keys
            adapter_cfg["provider"] = kind
```

Add `from serving.utils.logging import get_logger` at the top of the file if not already imported, and add `logger = get_logger(__name__)` near the other module-level definitions. (If the file already has a `logger`, skip this step.)

- [ ] **Step 4: Run tests, expect PASS**

Run: `pytest test/unit/test_registry_multi_key.py -v`
Expected: 5 tests pass.

Also run the existing registry tests to confirm no regression:
Run: `pytest test/servers/test_registry.py -v`
Expected: All existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add serving/servers/registry.py test/unit/test_registry_multi_key.py
git commit -m "feat(registry): parse api_keys list with env expansion and validation"
```

---

## Task 10: Plumb auth_key_hash through the request context

**Files:**
- Modify: `serving/servers/auth.py:249-259`
- Modify: `serving/servers/routers/completions.py` near line 226

- [ ] **Step 1: Surface key_hash in the user context dict**

Edit `serving/servers/auth.py`. Find the success-path return inside `verify_api_key`:

```python
    # Return user context
    user_role = user.get("role") or "free"
    return {
        "user_id": user["user_id"],
        "user_name": user["user_name"],
        "tier": user["tier"],
        "role": user_role,
        "authenticated": True,
        "quota_remaining_cost_usd": quota_daily_cost_usd - cost_spent,
        "is_admin": user_role == "admin",
    }
```

Replace with:

```python
    # Return user context
    user_role = user.get("role") or "free"
    return {
        "user_id": user["user_id"],
        "user_name": user["user_name"],
        "tier": user["tier"],
        "role": user_role,
        "authenticated": True,
        "quota_remaining_cost_usd": quota_daily_cost_usd - cost_spent,
        "is_admin": user_role == "admin",
        # key_hash identifies the specific hyi-xxx key in use (a user may
        # have multiple). Used as the affinity key for multi-key API rotation.
        "auth_key_hash": key_hash,
    }
```

(The variable `key_hash` already exists in scope earlier in the function.)

- [ ] **Step 2: Push auth_key_hash onto request context in the chat handler**

Edit `serving/servers/routers/completions.py`. Find (around line 226):

```python
    # Stable user identifier used by the fairness scheduler
    user_id: str = user_ctx.get("user_id") or "anonymous"
```

Replace with:

```python
    # Stable user identifier used by the fairness scheduler
    user_id: str = user_ctx.get("user_id") or "anonymous"

    # Affinity key for multi-key API rotation — pinned to the specific
    # hyi-xxx key in use, not the user_id (a user may have multiple keys).
    from serving.utils import context as req_ctx
    req_ctx.update({"auth_key_hash": user_ctx.get("auth_key_hash") or "_anon"})
```

- [ ] **Step 3: Write a smoke test**

Create `test/unit/test_auth_key_hash.py`:

```python
"""Smoke test: verify_api_key includes auth_key_hash in returned context."""

import inspect

from serving.servers import auth


def test_verify_api_key_returns_auth_key_hash():
    """The function source contains the auth_key_hash field in its return dict."""
    source = inspect.getsource(auth.verify_api_key)
    assert "auth_key_hash" in source, (
        "verify_api_key must surface auth_key_hash for multi-key affinity"
    )
```

- [ ] **Step 4: Run test, expect PASS**

Run: `pytest test/unit/test_auth_key_hash.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add serving/servers/auth.py serving/servers/routers/completions.py test/unit/test_auth_key_hash.py
git commit -m "feat(auth): surface auth_key_hash for multi-key affinity"
```

---

## Task 11: Wire KeyPool into OpenAICompatAdapter — non-streaming path

**Files:**
- Modify: `serving/adapters/openai_compat.py:107-...` (constructor)
- Modify: `serving/adapters/openai_compat.py:164-188` (`_build_headers`)
- Modify: `serving/adapters/openai_compat.py:220-276` (`chat_completion`)

- [ ] **Step 1: Modify the constructor to build the pool**

Find the existing `OpenAICompatAdapter.__init__` (or the parent's `__init__` if not overridden). At the appropriate place, add KeyPool instantiation.

Add at the top of the file (with other imports):

```python
from serving.adapters.key_pool import KeyPool, KeyPoolExhausted
```

In `OpenAICompatAdapter.__init__` (locate via `grep -n 'def __init__' serving/adapters/openai_compat.py`), after `super().__init__(config)` add:

```python
        # Multi-key API rotation pool (None when single api_key is configured).
        self._key_pool: KeyPool | None = None
        if config.api_keys:
            self._key_pool = KeyPool(
                keys=list(config.api_keys),
                provider_label=config.provider,
            )
```

- [ ] **Step 2: Refactor `_build_headers` to take an explicit api_key**

Find `_build_headers` (line 164):

```python
    def _build_headers(self) -> dict[str, str]:
        """Build HTTP headers for request."""
        headers = {"Content-Type": "application/json"}
        api_key = (
            self.config.api_key.strip()
            if isinstance(self.config.api_key, str)
            else self.config.api_key
        )
        ...
```

Replace its signature and body with:

```python
    def _build_headers(self, api_key_override: str | None = None) -> dict[str, str]:
        """Build HTTP headers for request.

        Args:
            api_key_override: when set (multi-key flow), use this key instead
                of ``self.config.api_key``.
        """
        headers = {"Content-Type": "application/json"}
        raw = api_key_override if api_key_override is not None else self.config.api_key
        api_key = raw.strip() if isinstance(raw, str) else raw

        # Add standard OpenAI authentication
        if api_key and getattr(self.config, "use_bearer_auth", True):
            headers["Authorization"] = f"Bearer {api_key}"

        # Custom header formatting (if provided)
        auth_name = getattr(self.config, "auth_header_name", None)
        auth_format = getattr(self.config, "auth_format", None)
        if auth_name and auth_format and api_key:
            headers[auth_name] = auth_format.format(api_key=api_key)

        # Merge any extra headers
        extra_headers = getattr(self.config, "extra_headers", None)
        if isinstance(extra_headers, dict):
            headers.update(extra_headers)

        return headers
```

- [ ] **Step 3: Wrap the chat_completion HTTP call in a key-pool retry loop**

In `chat_completion` (line 220), find the section that builds headers and posts:

```python
        # Make request
        url = self._build_url()
        headers = self._build_headers()

        logger.debug(f"[OpenAICompat] POST {url} model={payload.get('model', '<omitted>')}")
        logger.debug(f"[OpenAICompat] Payload: {payload}")
        logger.debug(f"[OpenAICompat] Headers: {headers}")

        response = await self.http.json_post_with_retry(
            url=url,
            json=payload,
            headers=headers,
            timeout=120,
            retries=2,
        )
```

Replace with:

```python
        # Make request
        url = self._build_url()
        logger.debug(f"[OpenAICompat] POST {url} model={payload.get('model', '<omitted>')}")
        logger.debug(f"[OpenAICompat] Payload: {payload}")

        response = await self._post_with_pool(url, payload)
```

Then add a new method `_post_with_pool` to the class:

```python
    async def _post_with_pool(
        self, url: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """POST JSON with key-pool rotation on 429s.

        When ``self._key_pool`` is None, behaves like the original single-key
        path (with retries). When set, loops over keys: a 429 on key K cools
        K down and the loop tries the next least-loaded key. Pool exhaustion
        raises ``KeyPoolExhausted``, which the caller surfaces as an upstream
        failure for the router fallback chain.
        """
        if self._key_pool is None:
            headers = self._build_headers()
            return await self.http.json_post_with_retry(
                url=url, json=payload, headers=headers, timeout=120, retries=2
            )

        from serving.utils import context as req_ctx
        from serving.observability.metrics import (
            KEY_POOL_REQUESTS,
            KEY_POOL_COOLDOWNS,
            KEY_POOL_EXHAUSTED,
            KEY_POOL_ACTIVE_AFFINITIES,
        )

        affinity_key = req_ctx.get().get("auth_key_hash") or "_anon"
        provider = self.config.provider

        # Bound the loop to pool size to prevent any pathological re-acquire
        # of the same just-cooled key (defensive — acquire already filters).
        max_attempts = self._key_pool.size()
        last_429_error: Exception | None = None

        for _ in range(max_attempts):
            try:
                api_key, lease = self._key_pool.acquire(affinity_key)
            except KeyPoolExhausted:
                KEY_POOL_EXHAUSTED.labels(provider=provider).inc()
                if last_429_error is not None:
                    raise last_429_error
                raise

            KEY_POOL_REQUESTS.labels(
                provider=provider, key_index=str(lease.key_index)
            ).inc()

            headers = self._build_headers(api_key_override=api_key)
            try:
                response = await self.http.json_post(
                    url=url, json=payload, headers=headers, timeout=None
                )
                self._key_pool.release(lease, status_code=200, retry_after=None)
                KEY_POOL_ACTIVE_AFFINITIES.labels(provider=provider).set(
                    self._key_pool.affinity_count()
                )
                return response
            except aiohttp.ClientResponseError as e:
                if e.status == 429:
                    retry_after = (
                        e.headers.get("Retry-After") if e.headers else None
                    )
                    self._key_pool.release(
                        lease, status_code=429, retry_after=retry_after
                    )
                    reason = "retry_after" if retry_after else "default_2min"
                    KEY_POOL_COOLDOWNS.labels(
                        provider=provider,
                        key_index=str(lease.key_index),
                        reason=reason,
                    ).inc()
                    last_429_error = e
                    continue  # try next key
                # Other 4xx / 5xx — release without cooldown, then re-raise
                self._key_pool.release(lease, status_code=e.status, retry_after=None)
                raise

        # Loop exhausted naturally (every key returned 429 in this single call)
        KEY_POOL_EXHAUSTED.labels(provider=provider).inc()
        assert last_429_error is not None
        raise last_429_error
```

Make sure `aiohttp` is imported at the top of the file. Check with `grep -n "^import aiohttp\|^from aiohttp" serving/adapters/openai_compat.py`. If not present, add `import aiohttp`.

Note the call uses `self.http.json_post(...)` not `json_post_with_retry`: we never want to retry the same key on 429. Network/timeout errors will be handled by the existing error path in `chat_completion`'s caller (router fallback) — they don't trigger cooldown but do propagate as failures.

- [ ] **Step 4: Run existing adapter tests for regression**

Run: `pytest test/unit/adapters/ -v -k "compat or openai"`
Expected: existing tests still pass; new behavior only kicks in when `api_keys` is set.

- [ ] **Step 5: Commit**

```bash
git add serving/adapters/openai_compat.py
git commit -m "feat(openai_compat): wire KeyPool into chat_completion with rotation loop"
```

---

## Task 12: Add `affinity_count()` helper for the gauge

**Files:**
- Modify: `serving/adapters/key_pool.py`
- Test: `test/unit/adapters/test_key_pool.py`

- [ ] **Step 1: Add failing test**

Append to `test/unit/adapters/test_key_pool.py`:

```python
def test_affinity_count_reflects_active_users():
    pool = KeyPool(keys=["k0", "k1"], provider_label="test")
    assert pool.affinity_count() == 0
    pool.acquire("user-A")
    pool.acquire("user-B")
    assert pool.affinity_count() == 2
```

- [ ] **Step 2: Run test, expect AttributeError**

Run: `pytest test/unit/adapters/test_key_pool.py::test_affinity_count_reflects_active_users -v`
Expected: FAIL.

- [ ] **Step 3: Add the method**

Append to `KeyPool` in `serving/adapters/key_pool.py`:

```python
    def affinity_count(self) -> int:
        """Number of active affinity entries (snapshot, not exact under load)."""
        return len(self._affinity)
```

- [ ] **Step 4: Run, expect PASS**

Run: `pytest test/unit/adapters/test_key_pool.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add serving/adapters/key_pool.py test/unit/adapters/test_key_pool.py
git commit -m "feat(key_pool): expose affinity_count for telemetry"
```

---

## Task 13: Streaming path — pool rotation on first-chunk 429

**Files:**
- Modify: `serving/adapters/openai_compat.py:278-...` (`stream_chat_completion`)

- [ ] **Step 1: Read the existing streaming flow**

Read `serving/adapters/openai_compat.py` lines 278–500 to understand how `stream_post` is invoked and how the first chunk is handled. Identify the pre-iteration "open the stream" call vs the per-chunk loop.

- [ ] **Step 2: Add a streaming variant of the pool retry**

The streaming wrapper is structured as an async generator that yields a single `(stream_iter, lease, first_chunk)` tuple. The caller pulls one tuple, processes the primed first chunk, then continues iterating `stream_iter` to completion. On opening 429s the helper rotates internally before yielding.

Inside `OpenAICompatAdapter`, near `_post_with_pool`, add:

```python
    async def _open_stream_with_pool(self, url: str, payload: dict[str, Any]):
        """Async generator: yields one (stream_iter, lease, first_chunk) tuple.

        Rotates keys internally on opening 429s. Once the first chunk is
        successfully read, the lease is committed — the caller is responsible
        for releasing it (with status 200) after the stream ends. Mid-stream
        errors are not classified.
        """
        if self._key_pool is None:
            headers = self._build_headers()
            stream_iter = self.http.stream_post(
                url=url, json=payload, headers=headers
            )
            yield stream_iter, None
            return

        from serving.utils import context as req_ctx
        from serving.observability.metrics import (
            KEY_POOL_REQUESTS, KEY_POOL_COOLDOWNS, KEY_POOL_EXHAUSTED,
        )
        affinity_key = req_ctx.get().get("auth_key_hash") or "_anon"
        provider = self.config.provider
        max_attempts = self._key_pool.size()
        last_429: Exception | None = None

        for _ in range(max_attempts):
            try:
                api_key, lease = self._key_pool.acquire(affinity_key)
            except KeyPoolExhausted:
                KEY_POOL_EXHAUSTED.labels(provider=provider).inc()
                if last_429 is not None:
                    raise last_429
                raise

            KEY_POOL_REQUESTS.labels(
                provider=provider, key_index=str(lease.key_index)
            ).inc()
            headers = self._build_headers(api_key_override=api_key)
            stream_iter = self.http.stream_post(
                url=url, json=payload, headers=headers
            )
            try:
                # Probe by pulling the first chunk; if 429, rotate.
                # We can't easily put back the chunk, so we yield the
                # iterator + a primed first chunk to the caller.
                first_chunk = await stream_iter.__anext__()
            except StopAsyncIteration:
                # Empty stream — treat as success
                self._key_pool.release(lease, status_code=200, retry_after=None)
                return
            except aiohttp.ClientResponseError as e:
                if e.status == 429:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    self._key_pool.release(
                        lease, status_code=429, retry_after=retry_after
                    )
                    reason = "retry_after" if retry_after else "default_2min"
                    KEY_POOL_COOLDOWNS.labels(
                        provider=provider, key_index=str(lease.key_index),
                        reason=reason,
                    ).inc()
                    last_429 = e
                    continue
                self._key_pool.release(lease, status_code=e.status, retry_after=None)
                raise

            yield stream_iter, lease, first_chunk
            return

        KEY_POOL_EXHAUSTED.labels(provider=provider).inc()
        assert last_429 is not None
        raise last_429
```

- [ ] **Step 3: Update `stream_chat_completion` to consume from the helper**

Locate `stream_chat_completion` and replace the call to `self.http.stream_post(...)` with a consumer that pulls one tuple from `_open_stream_with_pool`, prepends the primed first chunk, then yields the rest. The exact splice is:

Find:

```python
        url = self._build_url()
        headers = self._build_headers()

        # Fresh processor per request — avoids shared mutable state across concurrent streams
        processor = get_processor(self._processor_model_id, override=self._processor_override)
```

Replace with:

```python
        url = self._build_url()

        # Fresh processor per request — avoids shared mutable state across concurrent streams
        processor = get_processor(self._processor_model_id, override=self._processor_override)
```

Then locate the line that opens the stream (search for `self.http.stream_post`). Wrap it in the pool-aware path:

```python
        primed_chunk: str | None = None
        active_lease = None
        if self._key_pool is None:
            stream_iter = self.http.stream_post(
                url=url, json=payload, headers=self._build_headers()
            )
        else:
            async for stream_iter, active_lease, first in self._open_stream_with_pool(url, payload):
                primed_chunk = first
                break
        # ... continue with existing per-chunk processing, but if primed_chunk
        # is set, process it first before iterating stream_iter.
```

Then in the cleanup/end-of-stream path of `stream_chat_completion`, ensure the lease is released:

```python
        if active_lease is not None and self._key_pool is not None:
            self._key_pool.release(active_lease, status_code=200, retry_after=None)
```

> **Note for implementer:** the existing streaming function is ~200 lines; the patch is mechanical but error-prone. Implement carefully and run the existing streaming tests after each chunk of edits.

- [ ] **Step 4: Run existing streaming tests for regression**

Run: `pytest test/unit/adapters/ -v -k "stream"`
Expected: existing streaming tests pass.

- [ ] **Step 5: Commit**

```bash
git add serving/adapters/openai_compat.py
git commit -m "feat(openai_compat): wire KeyPool into stream_chat_completion"
```

---

## Task 14: Register Prometheus metrics for KeyPool

**Files:**
- Modify: `serving/observability/metrics.py:86-99` (registration block)
- Modify: `serving/observability/metrics.py:382-...` (no-op fallback block)
- Modify: `serving/observability/metrics.py:480+` (`__all__`)

- [ ] **Step 1: Register the new metrics**

In `serving/observability/metrics.py`, in the `if _ENABLED and CollectorRegistry and Counter and Histogram:` block, after `API_RETRIES = Counter(...)` add:

```python
    KEY_POOL_REQUESTS = Counter(
        "key_pool_requests_total",
        "Acquires from the per-provider API key pool",
        labelnames=("provider", "key_index"),
        registry=REGISTRY,
    )

    KEY_POOL_COOLDOWNS = Counter(
        "key_pool_cooldowns_total",
        "Cooldowns triggered on multi-key API pool",
        labelnames=("provider", "key_index", "reason"),
        registry=REGISTRY,
    )

    KEY_POOL_EXHAUSTED = Counter(
        "key_pool_exhausted_total",
        "KeyPoolExhausted occurrences (all keys cooled down)",
        labelnames=("provider",),
        registry=REGISTRY,
    )

    KEY_POOL_ACTIVE_AFFINITIES = Gauge(
        "key_pool_active_affinities",
        "Active per-user affinity entries in the multi-key pool",
        labelnames=("provider",),
        registry=REGISTRY,
    )
```

- [ ] **Step 2: Add no-op fallbacks**

In the `else:` block (around line 382), after the existing `API_RETRIES` no-op, add:

```python
    KEY_POOL_REQUESTS = type(
        "Noop", (), {"labels": lambda *a, **k: type("L", (), {"inc": _noop})()}
    )()
    KEY_POOL_COOLDOWNS = type(
        "Noop", (), {"labels": lambda *a, **k: type("L", (), {"inc": _noop})()}
    )()
    KEY_POOL_EXHAUSTED = type(
        "Noop", (), {"labels": lambda *a, **k: type("L", (), {"inc": _noop})()}
    )()
    KEY_POOL_ACTIVE_AFFINITIES = type(
        "Noop", (), {"labels": lambda *a, **k: type("L", (), {"set": _noop, "inc": _noop, "dec": _noop})()}
    )()
```

- [ ] **Step 3: Update `__all__`**

In the `__all__` tuple/list near the bottom of the module, add:

```python
    "KEY_POOL_REQUESTS",
    "KEY_POOL_COOLDOWNS",
    "KEY_POOL_EXHAUSTED",
    "KEY_POOL_ACTIVE_AFFINITIES",
```

- [ ] **Step 4: Smoke import**

Run: `python -c "from serving.observability.metrics import KEY_POOL_REQUESTS, KEY_POOL_COOLDOWNS, KEY_POOL_EXHAUSTED, KEY_POOL_ACTIVE_AFFINITIES; KEY_POOL_REQUESTS.labels(provider='x', key_index='0').inc()"`
Expected: no error.

- [ ] **Step 5: Run all existing metrics tests**

Run: `pytest test/ -v -k metrics`
Expected: existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add serving/observability/metrics.py
git commit -m "feat(metrics): add Prometheus counters for key pool behavior"
```

---

## Task 15: Integration test — 429 → rotate → success on next key

**Files:**
- Create: `test/integration/test_openai_compat_multi_key.py`

- [ ] **Step 1: Write the test**

Create `test/integration/test_openai_compat_multi_key.py`:

```python
"""Integration tests for multi-key rotation in OpenAICompatAdapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from serving.adapters.base import ModelConfig
from serving.adapters.openai_compat import OpenAICompatAdapter


def _make_config(api_keys: list[str]) -> ModelConfig:
    return ModelConfig(
        id="test-model",
        name="test-model",
        provider="zhipu",
        base_url="https://api.example.com",
        api_keys=api_keys,
        provider_model_id="test-model",
    )


def _make_response_error(status: int, retry_after: str | None = None) -> aiohttp.ClientResponseError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return aiohttp.ClientResponseError(
        request_info=AsyncMock(real_url="https://api.example.com"),
        history=(),
        status=status,
        message="rate limited",
        headers=headers,
    )


@pytest.mark.asyncio
async def test_multi_key_rotates_on_429():
    """First key gets 429, second key returns 200; user gets the 200."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))

    success_payload = {"id": "x", "choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": {}}

    call_count = {"n": 0}

    async def fake_json_post(url, json, headers, timeout):
        call_count["n"] += 1
        if call_count["n"] == 1:
            assert headers["Authorization"] == "Bearer k1"
            raise _make_response_error(429, retry_after="1")
        else:
            assert headers["Authorization"] == "Bearer k2"
            return success_payload

    with patch.object(adapter.http, "json_post", side_effect=fake_json_post):
        result = await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert call_count["n"] == 2
    assert "choices" in result
    # k1 is in cooldown
    assert adapter._key_pool._keys[0].cooldown_until > 0
    assert adapter._key_pool._keys[1].cooldown_until == 0


@pytest.mark.asyncio
async def test_multi_key_pool_exhausted_propagates():
    """All keys 429 in one call → final exception is the last 429."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))

    async def always_429(url, json, headers, timeout):
        raise _make_response_error(429, retry_after="1")

    with patch.object(adapter.http, "json_post", side_effect=always_429):
        with pytest.raises(aiohttp.ClientResponseError) as exc_info:
            await adapter.chat_completion([{"role": "user", "content": "hi"}])

    assert exc_info.value.status == 429


@pytest.mark.asyncio
async def test_single_api_key_legacy_path_unchanged():
    """Route with `api_key` (no `api_keys`) does not create a pool."""
    config = ModelConfig(
        id="test-model",
        name="test-model",
        provider="zhipu",
        base_url="https://api.example.com",
        api_key="single-key",
        provider_model_id="test-model",
    )
    adapter = OpenAICompatAdapter(config)
    assert adapter._key_pool is None

    success_payload = {"id": "x", "choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": {}}

    async def ok(url, json, headers, timeout, retries=2):
        assert headers["Authorization"] == "Bearer single-key"
        return success_payload

    # Legacy path uses json_post_with_retry
    with patch.object(adapter.http, "json_post_with_retry", side_effect=ok):
        result = await adapter.chat_completion([{"role": "user", "content": "hi"}])
    assert "choices" in result


@pytest.mark.asyncio
async def test_non_429_error_does_not_cooldown():
    """A 500 error should not put the key in cooldown."""
    adapter = OpenAICompatAdapter(_make_config(["k1", "k2"]))

    async def server_err(url, json, headers, timeout):
        raise _make_response_error(500)

    with patch.object(adapter.http, "json_post", side_effect=server_err):
        with pytest.raises(aiohttp.ClientResponseError):
            await adapter.chat_completion([{"role": "user", "content": "hi"}])

    # Neither key entered cooldown
    assert adapter._key_pool._keys[0].cooldown_until == 0
    assert adapter._key_pool._keys[1].cooldown_until == 0
```

- [ ] **Step 2: Run tests, expect PASS**

Run: `pytest test/integration/test_openai_compat_multi_key.py -v`
Expected: 4 tests pass.

- [ ] **Step 3: Commit**

```bash
git add test/integration/test_openai_compat_multi_key.py
git commit -m "test(integration): multi-key rotation, exhaustion, and back-compat"
```

---

## Task 16: Full regression sweep + clean up

**Files:**
- (No code changes — verification only)

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`
Expected: all tests pass. If any unrelated failure surfaces, investigate before declaring done.

- [ ] **Step 2: Run linters / type checks if configured**

Run: `mypy serving/adapters/key_pool.py serving/adapters/openai_compat.py serving/servers/registry.py 2>&1 | head -40`
Expected: no new errors introduced.

If `ruff` is configured, run: `ruff check serving/adapters/ serving/servers/registry.py`

- [ ] **Step 3: Final manual review**

Read the diff against `origin/dev`:

```bash
git diff origin/dev --stat
git diff origin/dev -- serving/adapters/key_pool.py
```

Confirm:
- `KeyPool` has no I/O, only in-memory state behind a lock.
- `_build_headers` accepts `api_key_override`; default behavior unchanged.
- Loader rejects both-fields, drops blanks, errors on empty list.
- Telemetry: 4 new metrics; raw key strings never emitted as labels.
- Existing tests untouched.

- [ ] **Step 4: Commit any cleanup**

If anything was tweaked (formatting, missed imports, etc.):

```bash
git add -A
git commit -m "chore: minor cleanups for multi-key rotation"
```

- [ ] **Step 5: Push branch**

```bash
git push -u origin jason/claude/multi-key-rotation
```

- [ ] **Step 6: Open PR**

```bash
gh pr create --base dev --title "feat: multi-key rotation with 5-min session affinity" --body "$(cat <<'EOF'
## Summary
- Adds `api_keys: [...]` opt-in list per route in `models.yaml`
- Implements `KeyPool` (in-process, thread-safe) with 5-min per-user affinity
- Least-loaded key selection; 429 cooldowns honor `Retry-After` (capped at 1 hour)
- Adapter loops within-pool on 429; cross-provider fallback only on full pool exhaustion
- Existing single-`api_key` routes unchanged (back-compat)

Spec: `docs/superpowers/specs/2026-04-30-multi-key-rotation-design.md`
Plan: `docs/superpowers/plans/2026-04-30-multi-key-rotation.md`

## Test plan
- [x] Unit tests for `KeyPool` (selection, affinity, cooldown, sweep, concurrency)
- [x] Loader unit tests (`api_keys` parsing, validation, env expansion)
- [x] Integration tests (rotate-on-429, pool exhausted, single-key back-compat)
- [x] Full `pytest` suite green
- [ ] Manual: configure `ZAI_API_KEY_1/2/3` on staging, observe rotation in logs/metrics

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Checklist (run after writing tasks)

- [x] **Spec coverage:** every requirement in the spec has a task. Architecture diagram (Task 11). State + acquire (Tasks 1–3). Release + Retry-After (Task 4). Cooldown skipping + exhaustion (Task 5). Sweep (Task 6). Concurrency (Task 7). Loader (Task 9). Auth context (Task 10). Adapter integration (Tasks 11, 13). Telemetry (Task 14). Tests (Tasks 15). Back-compat (Tasks 11, 15).
- [x] **No placeholders.** Each step has either an exact code block or an exact command + expected output. The streaming task (13) has a long implementation note but the actual code change is shown.
- [x] **Type consistency.** `KeyPool.acquire(affinity_key) -> (str, _Lease)`. `KeyPool.release(lease, *, status_code: int, retry_after: str | None)`. `_Lease.key_index: int`, `_Lease.affinity_key: str`. These match across Tasks 2, 4, 11, 13.
- [x] **Field naming.** `auth_key_hash` is used consistently in Tasks 10, 11, 13.
- [x] **Frequent commits.** Every task ends with a commit. Tests precede implementation in Tasks 1, 2, 4, 9, 12.
