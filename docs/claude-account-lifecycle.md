# Claude Account Lifecycle Management

Design document for improving the token/account management layer of the Claude subscription adapter.

**Status:** Draft
**Author:** murphy
**Date:** 2026-03-17
**Related code:** `serving/adapters/claude_token.py`, `codex_token.py`, `claude_pool.py`, `claude_sub.py`

---

## Table of Contents

1. [Current State Analysis](#1-current-state-analysis)
2. [Account Data Model Improvements](#2-account-data-model-improvements)
3. [Account State Machine](#3-account-state-machine)
4. [Error Classification Refinement](#4-error-classification-refinement)
5. [Acquire → Refresh → Verify Flow](#5-acquire--refresh--verify-flow)
6. [Import / Update Deduplication Rules](#6-import--update-deduplication-rules)
7. [Credential Storage Security](#7-credential-storage-security)
8. [Admin Observability Endpoint](#8-admin-observability-endpoint)
9. [Phased Implementation Plan](#9-phased-implementation-plan)
10. [Reference Implementations](#10-reference-implementations)

---

## 1. Current State Analysis

### What exists

The Claude subscription path currently has three layers:

| Layer | File | Responsibility |
|---|---|---|
| Credential provider | `claude_token.py` → `ClaudeCredentialProvider` | Load, refresh, persist OAuth tokens |
| Account pool | `codex_token.py` → `AccountPool` | Health-aware round-robin rotation |
| Shared singleton | `claude_pool.py` → `get_shared_pool()` | Process-wide lazy-init with double-checked locking |

Two request surfaces consume the shared pool:

- **`claude_sub.py` → `ClaudeSubscriptionAdapter`** — Chat Completions → Anthropic Messages API translation, with paid-API fallback.
- **`anthropic_proxy.py`** — Identity proxy (Anthropic Messages API in, same out), raw byte forwarding for streaming.

Import/deduplication is handled by `scripts/import_claude_auth.py`, which reads Claude Code CLI credentials from `~/.claude/.credentials.json` and merges into `var/data/claude_accounts.json`.

### What's broken or missing

| Gap | Impact | Current behaviour |
|---|---|---|
| **Health state is in-memory only** | Process restart loses all cooldown/failure history. An account revoked at Anthropic's side re-enters the pool as "healthy" on every restart. | `AccountHealth` dataclass lives only in `AccountPool._health` dict. Never persisted. |
| **No account identity verification** | Cannot verify that a refresh token still belongs to the expected org/email. A token swap or org reassignment goes undetected. | `_refresh_token()` updates `organization_id` and `email` from the token response, but never checks them against expected values. |
| **No state machine** | Account states (active, rate-limited, revoked, disabled) are ad-hoc. There's no `revoked` state — a 401 from a revoked refresh token causes repeated retries then crash. | `AccountHealth.healthy` is a bool with `cooldown_until` for timed recovery. A permanently invalid account auto-recovers after cooldown, wasting attempts. |
| **Token refresh failures cause hard errors** | A single `RuntimeError` from `_refresh_token()` propagates up uncaught by `AccountPool`, causing a 500 to the user. | `get_valid_token()` raises `RuntimeError` on non-200 refresh response. `anthropic_proxy.py` has `_acquire_with_retry()` to handle this, but `claude_sub.py` does not. |
| **No `invalid_grant` detection** | When Anthropic revokes a refresh token, the OAuth endpoint returns `{"error": "invalid_grant"}`. This is not parsed — it's treated the same as a transient 400. | `_refresh_token()` checks `resp.status != 200` but does not inspect the response body. |
| **No admin observability** | No way to check pool health, per-account status, token expiry, or failure counts without reading logs. | No HTTP endpoint exposes pool state. |
| **Credential file has no integrity check** | `_persist()` writes atomically (tmp + rename), but there's no versioning, backup, or checksum. A corrupted write is silent. | Atomic write is good, but no recovery mechanism if the file content itself is invalid JSON (e.g., truncated). |
| **Import dedup is fragile** | Merge rule requires both `organization_id` AND `email` to match. CLI credentials often lack `organization_id` (set to `""`), so every import appends a duplicate. | `import_claude_auth.py` lines 247-253: identity match requires both fields present and equal. |

---

## 2. Account Data Model Improvements

### Current `ClaudeAccountCredential`

```python
@dataclass
class ClaudeAccountCredential:
    id: str                  # "acct_01"
    label: str               # "murphy-max"
    access_token: str
    refresh_token: str
    expires_at: int          # Unix epoch in milliseconds
    organization_id: str     # From token response
    email: str = ""
    plan: str = "pro"        # pro / max / team / enterprise
    enabled: bool = True
```

### Proposed additions

```python
@dataclass
class ClaudeAccountCredential:
    # --- existing fields (unchanged) ---
    id: str
    label: str
    access_token: str
    refresh_token: str
    expires_at: int
    organization_id: str
    email: str = ""
    plan: str = "pro"

    # --- new identity fields ---
    account_uuid: str = ""          # Anthropic account UUID (from token response `account.uuid`)
    expected_org_id: str = ""       # Pinned org ID for drift detection

    # --- new lifecycle fields ---
    state: str = "active"           # See §3 state machine
    state_changed_at: int = 0       # Unix ms when state last changed
    revoke_reason: str = ""         # Why the account was revoked (e.g., "invalid_grant")
    last_verified_at: int = 0       # Unix ms of last successful identity verification
    consecutive_failures: int = 0   # Persisted failure count (survives restart)

    # --- new metadata ---
    added_at: int = 0               # Unix ms when account was first imported
    last_refreshed_at: int = 0      # Unix ms of last successful token refresh
    refresh_count: int = 0          # Total successful refreshes (lifetime)
```

**`enabled` field removed — `state` is the single source of truth.**

The existing `enabled: bool` field is replaced by the `state` field. `state: "disabled"` is the equivalent of `enabled: False`. Keeping both would create ambiguity (which one wins?). On v1 → v2 migration, `enabled: false` entries are converted to `state: "disabled"`. The `enabled` key is no longer read or written.

### Storage format change

The JSON file gains a `version` field for forward compatibility:

```json
{
  "version": 2,
  "accounts": [
    {
      "id": "acct_01",
      "state": "active",
      "state_changed_at": 1710700000000,
      "account_uuid": "usr_...",
      "expected_org_id": "org_...",
      "consecutive_failures": 0,
      ...
    }
  ]
}
```

**Migration:** On load, if `version` is missing (v1), treat all accounts as `state: "active"`, fill new fields with defaults, and persist back as v2.

---

## 3. Account State Machine

```
                 ┌─────────────────────────────────────────────┐
                 │                                             │
                 ▼                                             │
  ┌──────────┐  success   ┌──────────┐  cooldown_expired  ┌───┴──────┐
  │  active   │◄─────────│ cooldown  │◄───────────────────│ cooldown │
  │           │           │           │                    │ (auto)   │
  └─────┬────┘           └──────────┘                    └──────────┘
        │                     ▲
        │ 429 / consecutive   │
        │ failures >= N       │
        ▼                     │
  ┌──────────┐                │
  │ cooldown  │───────────────┘
  │           │  timer expires → re-enter active
  └─────┬────┘
        │ still failing after
        │ re-entry (threshold)
        ▼
  ┌──────────┐
  │ revoked   │   ← invalid_grant / repeated 401 post-refresh
  │           │   → TERMINAL (requires admin action)
  └─────┬────┘
        │ admin explicit disable
        ▼
  ┌──────────┐
  │ disabled  │   ← admin API or state="disabled" in storage
  │           │   → never acquired, never refreshed
  └──────────┘
```

### State definitions

| State | `acquire()` eligible | Token refresh | Transitions out |
|---|---|---|---|
| **active** | Yes | Normal proactive refresh | → `cooldown` on failure threshold or 429 |
| **cooldown** | No | Suspended | → `active` after cooldown timer; → `revoked` if re-entry still fails |
| **revoked** | No | Suspended | → `active` only via admin action (re-import with valid tokens) |
| **disabled** | No | Suspended | → `active` only via admin action (admin API or re-import) |

### Key rules

1. **`active → cooldown`**: Triggered by `consecutive_failures >= threshold` (default 3) or immediate 429 (rate limit).
2. **`cooldown → active`**: Auto-recovery after `cooldown_seconds` (default 60s for failures, 120s for 429).
3. **`cooldown → revoked`**: If the account enters cooldown 3 times within a sliding window (e.g., 10 minutes) without any intervening success, it's escalated to `revoked`. This prevents the "revolving door" of a permanently-bad account cycling through cooldown.
4. **`* → revoked`**: Immediate on `invalid_grant` error from the OAuth token endpoint. This indicates Anthropic has revoked the refresh token — retrying is pointless.
5. **`revoked` / `disabled`**: Terminal states. Only admin action can transition back to `active`. See §6 for re-import behaviour.
6. **State is persisted**: On every state transition, write the new state to disk via the credential provider. This ensures restart consistency.

### Ownership: who holds lifecycle state, who persists

**Design decision:** Lifecycle state (`state`, `consecutive_failures`, `state_changed_at`, `revoke_reason`) lives on `ClaudeAccountCredential` and is persisted by `ClaudeCredentialProvider._persist()`. The shared `AccountPool` remains a stateless, provider-agnostic rotation engine — it only holds ephemeral runtime health (`AccountHealth`) as it does today.

The rationale:

- `AccountPool` in `codex_token.py` is shared between Codex and Claude adapters. Pushing Claude-specific lifecycle semantics (revoked, invalid_grant) into it would break Codex or require provider-type branching.
- `ClaudeCredentialProvider` already owns the JSON file and the `_persist()` method. It is the natural owner of durable state.
- `AccountPool._health` continues to serve as the **fast-path** health check for `acquire()`. The provider syncs durable state into the pool's health on load and on state transitions.

**Concretely, the boundary is:**

| Concern | Owner | Persisted? |
|---|---|---|
| `state` (active/cooldown/revoked/disabled) | `ClaudeAccountCredential` → `ClaudeCredentialProvider._persist()` | Yes (JSON file) |
| `consecutive_failures`, `state_changed_at`, `revoke_reason` | `ClaudeAccountCredential` → `ClaudeCredentialProvider._persist()` | Yes (JSON file) |
| Ephemeral cooldown timer (`cooldown_until`) | `AccountPool._health` (in-memory `AccountHealth`) | No (runtime only) |
| Round-robin index, last_success/last_failure timestamps | `AccountPool._health` (in-memory `AccountHealth`) | No (runtime only) |

**State sync protocol:**

1. **On load:** `ClaudeCredentialProvider.load_accounts()` reads persisted `state` for each account. It filters out `revoked`/`disabled` accounts — they are never passed to `AccountPool`. Accounts persisted as `cooldown` are **promoted to `active`** on load (see "Cooldown restart semantics" below) and enter the pool normally.
2. **On state transition at runtime:** The call site (e.g., `_acquire_with_retry` in `claude_sub.py`, or `report_failure` callback) calls `provider.transition_state(account, new_state, reason)` which: (a) updates `account.state`, `state_changed_at`, `revoke_reason` on the credential object; (b) persists to disk; (c) if transitioning to `revoked`/`disabled`, calls `pool.deactivate(account.id)` to permanently exclude the account from future `acquire()` calls.
3. **`AccountPool` gets one minimal addition:** a `deactivate(account_id)` method (see below). No persistence, no provider reference, no Claude-specific logic.

**Runtime removal of revoked/disabled accounts from the pool:**

The doc says "AccountPool is not modified" in terms of persistence or Claude-specific semantics. But we do need a mechanism to stop `acquire()` from selecting a permanently-dead account at runtime. The chosen approach is **option C from the review: a minimal `deactivate()` method on `AccountPool`**.

```python
# Addition to AccountPool (codex_token.py)
def deactivate(self, account_id: str) -> None:
    """Permanently exclude an account from acquire() rotation.

    This is a generic pool operation (not Claude-specific).
    The account is removed from _accounts and _health.
    """
    self._accounts = [a for a in self._accounts if a.id != account_id]
    self._health.pop(account_id, None)
    if self._index >= len(self._accounts):
        self._index = 0
```

This is provider-agnostic — any pool consumer (Codex or Claude) could call it. The Claude-specific part (deciding *when* to call it) stays in `ClaudeCredentialProvider.transition_state()`.

**Runtime re-addition of recovered accounts to the pool:**

`deactivate()` has a symmetric counterpart: `activate(account)`. This is needed when a terminal-state account is restored to `active` via admin API or re-import while the process is still running.

```python
# Addition to AccountPool (codex_token.py)
def activate(self, account) -> None:
    """Add an account back into acquire() rotation.

    No-op if the account is already in the pool.
    This is a generic pool operation (not Claude-specific).
    """
    if any(a.id == account.id for a in self._accounts):
        return
    self._accounts.append(account)
```

**When is `activate()` called?**

- **Admin API** (`POST /admin/claude/accounts/{id}/state` with `{"state": "active"}`): the endpoint handler calls `provider.transition_state(account, "active")` then `pool.activate(account)`.
- **Re-import:** The import script writes to disk only; it does not interact with the running process. The restored account enters the pool on next process restart. This is acceptable because re-import is an offline admin operation. If hot-reload is desired, the admin API is the right tool.

This means there are two paths back to `active`:

| Recovery path | Pool effect | Requires restart? |
|---|---|---|
| Admin API (`POST .../state`) | `pool.activate()` — immediate | No |
| Re-import (`import_claude_auth.py`) | Disk only | Yes (or follow up with admin API) |

**Cooldown restart semantics:**

Only `revoked` and `disabled` are durable terminal states. `cooldown` is **not preserved across restarts**:

- `cooldown_until` is NOT persisted. It is purely a runtime timer in `AccountPool._health`.
- On restart, if an account was persisted as `state: "cooldown"`, `load_accounts()` promotes it to `state: "active"` (and persists that change). The rationale: cooldown is a short-lived backoff (60-120s). If the process restarted, any meaningful cooldown has almost certainly expired. Trying to reconstruct a precise `cooldown_until` from `state_changed_at` would add complexity for negligible benefit — especially since the cooldown duration is variable (60s for failures, 120s for 429, dynamic for `retry-after`).
- If the underlying problem persists (e.g., account is rate-limited), the first request after restart will re-trigger `cooldown` via the normal failure path. This is the same behaviour as a fresh start.
- `revoked` and `disabled` ARE preserved exactly: these accounts are filtered out on load and never enter the pool.

Summary of restart behaviour:

| Persisted state | On-load behaviour |
|---|---|
| `active` | Enter pool as-is |
| `cooldown` | Promote to `active`, enter pool (cooldown timer lost, re-triggered on failure) |
| `revoked` | Filtered out, never enters pool |
| `disabled` | Filtered out, never enters pool |

**Persisted failure counter reset on success:**

When a request succeeds, the caller must reset the persisted `consecutive_failures` on the credential, not just the pool's in-memory health. This is done via a new `ClaudeCredentialProvider.report_success()` method:

```python
# New method on ClaudeCredentialProvider
async def report_success(self, account: ClaudeAccountCredential) -> None:
    """Reset persisted failure counter on successful request."""
    if account.consecutive_failures == 0:
        return  # nothing to persist
    account.consecutive_failures = 0
    await self._persist()
```

This is intentionally lightweight: it only persists if the counter was non-zero, avoiding unnecessary disk writes on the happy path.

```python
# New method on ClaudeCredentialProvider
async def transition_state(
    self,
    account: ClaudeAccountCredential,
    new_state: str,
    reason: str = "",
    *,
    pool: AccountPool | None = None,
) -> None:
    """Durably transition an account's lifecycle state.

    Args:
        pool: If provided, also updates the pool's runtime state:
              - revoked/disabled → pool.deactivate()
              - active (from terminal) → pool.activate()
    """
    old_state = account.state
    account.state = new_state
    account.state_changed_at = int(time.time() * 1000)
    if new_state == "revoked":
        account.revoke_reason = reason
    if new_state == "active":
        account.consecutive_failures = 0
        account.revoke_reason = ""
    await self._persist()
    logger.warning(
        f"Account {account.id} state: {old_state} → {new_state}"
        + (f" ({reason})" if reason else "")
    )
    # Sync pool runtime state
    if pool is not None:
        if new_state in ("revoked", "disabled"):
            pool.deactivate(account.id)
        elif new_state == "active" and old_state in ("revoked", "disabled"):
            pool.activate(account)
```

---

## 4. Error Classification Refinement

### Current classification

```python
@staticmethod
def _classify_error(status_code: int) -> str:
    if status_code in (401, 403, 429):
        return "account"
    if status_code >= 500:
        return "upstream"
    return "client"
```

This is too coarse. A 401 from an expired-but-refreshable token is very different from a 401 from a revoked account.

### Proposed classification

| Category | Trigger | Health impact | Action |
|---|---|---|---|
| **token_expired** | 401 from Messages API + token locally near/past expiry | None | Force-refresh, retry once. Already implemented. |
| **token_rejected** | 401 from Messages API + token not locally expired + refresh succeeds | None (transient) | Retry with new token. Already implemented. |
| **account_revoked** | 401 post-refresh retry still fails, OR `invalid_grant` on refresh | → `revoked` immediately | Stop retrying this account. Log alert. |
| **rate_limited** | 429 from Messages API | → `cooldown` (120s) | Back off. Do not retry on this account. |
| **quota_exhausted** | 429 with `retry-after` > 300s or body contains "daily limit" | → `cooldown` (dynamic, from `retry-after`) | Longer cooldown. |
| **permission_denied** | 403 from Messages API | Increment failures | May indicate plan downgrade or feature gate. |
| **upstream_error** | 5xx from Messages API | No health impact | Retry on same account (upstream transient). |
| **client_error** | 400, 404, 422 from Messages API | No health impact | Return error to caller. Account is fine. |
| **refresh_failed_transient** | Non-200 from OAuth endpoint, not `invalid_grant` | Increment failures | May be Anthropic outage. Retry later. |
| **refresh_failed_permanent** | `invalid_grant` from OAuth endpoint | → `revoked` | Refresh token is dead. |

### Implementation: parse OAuth error response

```python
async def _refresh_token(self, account: ClaudeAccountCredential) -> None:
    ...
    if resp.status != 200:
        body = await resp.text()
        try:
            error_data = json.loads(body)
            error_code = error_data.get("error", "")
        except json.JSONDecodeError:
            error_code = ""

        if error_code == "invalid_grant":
            # Permanent: refresh token revoked
            raise RefreshTokenRevokedError(account.id, body)
        else:
            # Transient: OAuth endpoint issue
            raise RefreshTokenTransientError(account.id, resp.status, body)
```

New exception hierarchy:

```python
class TokenRefreshError(Exception):
    """Base class for token refresh failures."""

class RefreshTokenRevokedError(TokenRefreshError):
    """Refresh token permanently invalidated (invalid_grant)."""

class RefreshTokenTransientError(TokenRefreshError):
    """Transient refresh failure (network, server error, etc.)."""
```

---

## 5. Acquire → Refresh → Verify Flow

### Current flow (simplified)

```
caller → AccountPool.acquire() → round-robin pick healthy account
       → ClaudeCredentialProvider.get_valid_token(account)
           → if not expired: return cached access_token
           → if expired: _refresh_token(account)
               → POST /v1/oauth/token  {grant_type: refresh_token, ...}
               → update access_token, refresh_token, expires_at in-memory
               → _persist() to disk
               → return new access_token
       → use access_token in request
       → on 401: force_refresh=True, retry once
       → report_success() or report_failure()
```

### Problems

1. **No identity verification after refresh.** A refresh could return tokens for a different org (e.g., if the user re-authenticated with a different account). The adapter would silently use the wrong org's quota.
2. **`claude_sub.py` does not catch `RuntimeError` from refresh.** Unlike `anthropic_proxy.py`'s `_acquire_with_retry()`, the adapter's `_subscription_chat()` lets refresh failures propagate as 500s.
3. **No proactive background refresh.** Tokens are only refreshed on-demand. If all accounts expire simultaneously (e.g., after a long idle period), the first N requests all hit the refresh path serially under locks.

### Proposed flow

```
                    ┌──────────────────────────────────────────────┐
                    │         Background Refresh Loop              │
                    │  (every 60s, check all active accounts)      │
                    │  → if expires_in < 2 * refresh_margin:       │
                    │      acquire lock, refresh, verify, persist  │
                    └──────────────────────────────────────────────┘

caller → AccountPool.acquire()
           → round-robin among pool members
             (terminal states already excluded via deactivate();
              cooldown enforced by pool's in-memory health check)
           → raise NoHealthyAccountError if none eligible

       → ClaudeCredentialProvider.get_valid_token(account)
           → if not expired and not force_refresh:
               return cached access_token
           → acquire per-account lock (single-flight)
           → try: _refresh_and_verify(account)
           → except RefreshTokenRevokedError:
               → account.state = "revoked"
               → persist, log alert
               → raise  (caller will try next account)
           → except RefreshTokenTransientError:
               → increment consecutive_failures
               → if threshold reached: account.state = "cooldown"
               → raise  (caller will try next account)
           → return new access_token

       → POST to Anthropic Messages API
       → on success:
           → pool.report_success(account.id)          [in-memory health]
           → provider.report_success(account)          [persisted state — see below]
       → on 401: force_refresh + retry once
           → if retry also 401: → provider.transition_state(revoked)
       → on 429: provider.transition_state(cooldown) + pool.report_failure()
       → on 5xx: no health impact, normal retry
```

### `_refresh_and_verify()`

```python
async def _refresh_and_verify(self, account: ClaudeAccountCredential) -> None:
    """Refresh token and verify identity consistency.

    Note: _refresh_token() is refactored to NOT call _persist() itself.
    This method performs the single persist at the end, after updating
    all metadata fields, to avoid double-persist per refresh.
    """
    await self._refresh_token(account)  # updates tokens in-memory, raises on failure

    # Identity verification: check org_id hasn't drifted
    if account.expected_org_id and account.organization_id != account.expected_org_id:
        logger.error(
            f"Identity drift detected for {account.id}: "
            f"expected org {account.expected_org_id}, got {account.organization_id}"
        )
        # Don't block — log alert for admin investigation
        # Could optionally escalate to revoked if strict mode is enabled

    account.last_verified_at = int(time.time() * 1000)
    account.last_refreshed_at = int(time.time() * 1000)
    account.refresh_count += 1

    # Single persist covers token update + metadata update
    await self._persist()
```

**Persist responsibility change:** Currently `_refresh_token()` calls `_persist()` at the end. With the introduction of `_refresh_and_verify()`, the persist is moved to the wrapper so that token fields AND metadata fields are written in one atomic operation. `_refresh_token()` is changed to only update in-memory fields. Callers that still use `_refresh_token()` directly (e.g., the import probe) must call `_persist()` themselves.

### Background proactive refresh

**Lifecycle ownership:** The background refresh loop is explicitly managed by `claude_pool.py`, NOT implicitly started from `ClaudeCredentialProvider.__init__()` or `get_shared_pool()`.

The design:

1. **`get_shared_pool()`** returns `(provider, pool)` as today — no side effects, no background tasks.
2. **A new `start_background_refresh()` function** in `claude_pool.py` creates the task and stores the handle in a module-level `_refresh_task: asyncio.Task | None`. It is called exactly once from the application startup sequence (e.g., the FastAPI `lifespan` context manager), not from the singleton initializer.
3. **A corresponding `stop_background_refresh()` function** cancels the task and awaits it. Called from the lifespan shutdown path.
4. **`_reset_for_testing()`** also cancels the task, preventing test pollution.
5. **Guard against double-start:** `start_background_refresh()` is a no-op if `_refresh_task` is already running.

```python
# claude_pool.py additions

_refresh_task: asyncio.Task | None = None

async def start_background_refresh(*, interval: int = 60) -> None:
    """Start the background token refresh loop. Call once from app startup."""
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return  # already running
    provider, _ = get_shared_pool()
    _refresh_task = asyncio.create_task(
        provider.background_refresh_loop(interval=interval),
        name="claude-bg-refresh",
    )
    logger.info(f"[claude_pool] Background refresh started (interval={interval}s)")

async def stop_background_refresh() -> None:
    """Stop the background refresh loop. Call from app shutdown."""
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        _refresh_task.cancel()
        try:
            await _refresh_task
        except asyncio.CancelledError:
            pass
    _refresh_task = None
    logger.info("[claude_pool] Background refresh stopped")

def _reset_for_testing() -> None:
    """Reset the singleton — only for unit tests."""
    global _provider, _pool, _refresh_task
    with _lock:
        if _refresh_task is not None and not _refresh_task.done():
            _refresh_task.cancel()
        _refresh_task = None
        _provider = None
        _pool = None
```

The loop itself lives on `ClaudeCredentialProvider` as an async method, but it does NOT self-start:

```python
# claude_token.py
async def background_refresh_loop(self, *, interval: int = 60) -> None:
    """Proactively refresh tokens approaching expiry.

    Must be driven by an external caller (claude_pool.start_background_refresh).
    Runs until cancelled.
    """
    while True:
        await asyncio.sleep(interval)
        for account in list(self._accounts.values()):
            if account.state != "active":
                continue
            remaining_ms = account.expires_at - int(time.time() * 1000)
            if remaining_ms < self._refresh_margin * 2 * 1000:
                try:
                    await self.get_valid_token(account)
                except Exception:
                    logger.warning(f"Background refresh failed for {account.id}")
```

**Deployment assumption: single-worker process.**

This design assumes a single-worker deployment (one uvicorn process), which is the current production configuration. Under this assumption, there is exactly one `ClaudeCredentialProvider` instance, one `_accounts` dict, and no concurrent `_persist()` calls from different processes.

**Multi-worker is NOT safe with the current persist model.** If multiple uvicorn workers each hold their own in-memory `_accounts` snapshot, `_persist()` does a full-file rewrite from that snapshot. Worker B's persist would overwrite worker A's freshly-written tokens/state with B's stale snapshot — a real lost update, not just a benign last-writer-wins. The atomic rename only prevents torn writes, not logical conflicts.

If multi-worker is needed in the future, the options are (in order of preference):

| Option | Complexity | Description |
|---|---|---|
| **Single shared worker for token ops** | Low | Run one background worker that owns all refresh/persist. Other workers delegate via IPC or shared memory. |
| **File lock on persist** | Medium | `fcntl.flock()` + read-merge-write: before persisting, read the current file, merge only the fields this process changed, then write. Prevents lost updates but adds I/O on every persist. |
| **Per-account files** | Medium | Store each account as `var/data/claude_accounts/{id}.json`. Eliminates cross-account write conflicts entirely. Requires migration. |

For Phase 1-3, single-worker is the explicit assumption. Phase 4 can optionally address multi-worker if the deployment model changes.

---

## 6. Import / Update Deduplication Rules

### Current merge rules (`import_claude_auth.py` lines 236-268)

1. Explicit `--account-id` with existing ID match → update in-place
2. Both `organization_id` AND `email` present and match → update in-place
3. Otherwise → append new account

**Problem:** CLI credentials typically have `organization_id: ""` (not available in the credential file). So rule 2 never matches, and every re-import creates a duplicate.

### Proposed merge rules (ordered by priority)

| Priority | Match condition | Action |
|---|---|---|
| 1 | `--account-id` explicitly provided and matches existing `id` | Update in-place |
| 2 | `account_uuid` present in both and matches | Update in-place |
| 3 | `refresh_token` matches existing (same credential) | Update in-place |
| 4 | `organization_id` non-empty and matches, AND (`email` matches or either is empty) | Update in-place |
| 5 | No match | Append new account |

**Key change:** Rule 3 (refresh token match) catches the common case of re-importing the same CLI session without re-authenticating. Since a refresh token is unique per OAuth session, this is a reliable identity signal. Rule 4 relaxes the current strict `org_id + email` requirement — if the org matches and the email is unknown, that's still the same account.

**Limitation:** These rules cannot catch every duplicate. If a user runs `claude login` again (creating a new OAuth session), the new session has a fresh refresh token and no `account_uuid` / `organization_id` until a token refresh fills them in. In this case, the import will append a new entry. The duplicate will be resolved on the next refresh (when `account_uuid` is populated), or the user can pass `--account-id` explicitly. This is a known gap — see Phase 3 milestone wording below.

### Implementation sketch

```python
def _find_existing_account(existing: list[dict], creds: dict, explicit_id: str | None) -> dict | None:
    for acct in existing:
        # Rule 1: explicit ID
        if explicit_id and acct.get("id") == explicit_id:
            return acct
        # Rule 2: account_uuid
        if creds.get("account_uuid") and acct.get("account_uuid") == creds["account_uuid"]:
            return acct
        # Rule 3: refresh_token
        if acct.get("refresh_token") == creds["refresh_token"]:
            return acct
        # Rule 4: org_id (relaxed)
        if (
            creds.get("organization_id")
            and acct.get("organization_id") == creds["organization_id"]
            and (not creds.get("email") or not acct.get("email") or acct["email"] == creds["email"])
        ):
            return acct
    return None
```

### Additional import improvements

- **Set `added_at`** on first import, never overwrite.
- **Set `expected_org_id`** from the first successful refresh response's `organization_id`. This pins the identity.
- **Preserve metadata on update:** `refresh_count`, `added_at`, and `last_verified_at` are preserved from the existing entry on re-import.
- **Warn on plan downgrade:** If existing `plan` is `max` and new import is `pro`, warn the user (may indicate wrong source credentials).

### Post-import identity probe (recommended)

After a successful import (new or update), the import script should optionally call a read-only Anthropic control-plane endpoint to populate `account_uuid`, `organization_id`, and `email`. This eliminates the "identity gap" where a freshly-imported account has empty identity fields until its first runtime refresh.

**Prefer read-only endpoints over token refresh.** A refresh rotates the `refresh_token`, which means the CLI's copy (in `~/.claude/.credentials.json`) becomes stale — a subsequent re-import would carry a different `refresh_token`, breaking dedup Rule 3. A read-only profile call avoids this side effect.

Probe strategy (in order of preference):

1. **Read-only control-plane endpoint** (provisional — e.g., `GET /api/auth/profile` or `GET /api/oauth/claude_cli/roles`) — if a suitable endpoint exists, use the imported `access_token` as bearer auth. No state change, no token rotation. **These endpoints are not yet validated against live traffic.** Implementation must probe and verify availability before relying on them; if neither responds usefully, fall through to option 2.
2. **Token refresh** — fallback if the access token is expired or no read-only profile endpoint is available. The rotated refresh token is written to disk, and the user is warned that their CLI credentials are now out of sync.
3. **No probe (default if unvalidated)** — if neither read-only endpoints nor refresh have been proven reliable in the target environment, the import script defaults to `--no-probe` behaviour. Identity fields are left empty and will be populated on first runtime refresh.

```
$ python scripts/import_claude_auth.py
Read credentials from ~/.claude/.credentials.json
  ...
Added new account: acct_01 (claude-max-acct_01)
Probing identity via /api/auth/profile...
  account_uuid: usr_abc123
  organization_id: org_def456
  email: murphy@example.com
Written to var/data/claude_accounts.json
```

The `--no-probe` flag skips this step for offline/CI use. If the probe fails (e.g., network error, unknown endpoint), print a warning but still write the account — identity will be filled on the first runtime refresh.

**Dedup impact:** The probe improves dedup for *future* imports of the same account. It does NOT help the current import (dedup matching happens before the probe). And if the probe falls back to a refresh, the rotated `refresh_token` actually weakens Rule 3 for subsequent imports from the same CLI session. This is why read-only profile is strongly preferred.

### Re-import and revoked accounts

**Rule:** Re-importing fresh credentials into a `revoked` account automatically resets it to `active`, with `consecutive_failures = 0` and a cleared `revoke_reason`. The importer prints a clear message: `"Account acct_01 was revoked (reason: invalid_grant), resetting to active with new credentials."`

**Rationale:** The whole point of `revoked` being terminal at *runtime* is to prevent the system from endlessly retrying a dead refresh token. But if a human explicitly imports new, valid credentials via the CLI script, that's the "admin action" the state machine requires — it just happens through the import tool rather than the admin API. Keeping the account `revoked` after re-import with valid tokens would force the user to run a second admin API call for no reason.

**What is NOT an automatic reset:** The admin observability endpoint (`POST /admin/claude/accounts/{id}/state`) can also transition `revoked → active`, but only when requested explicitly. The background refresh loop and the runtime request path never auto-reset a `revoked` account.

This resolves the apparent contradiction in the state machine section: `revoked` is terminal *at runtime* (no automatic recovery), but the import script and admin API are both valid "admin action" paths to re-activate.

---

## 7. Credential Storage Security

### Current state

- **Storage:** `var/data/claude_accounts.json`, plain JSON, `chmod 0600`.
- **Gitignored:** Yes, via `var/**` in `.gitignore`.
- **Tokens in memory:** `ClaudeAccountCredential` holds tokens as plain strings, accessible to any code in the process.

### Assessment

The current approach is reasonable for a single-server deployment. Full encryption at rest would add complexity disproportionate to the threat model (the server process needs to read the tokens anyway). However, several low-cost improvements are worthwhile:

### Proposed improvements

| Improvement | Effort | Impact |
|---|---|---|
| **File permission enforcement on load** | Low | `load_accounts()` should verify the file is `0600` and owned by the current user. Warn (or refuse to load) if permissions are too open. |
| **Backup before persist** | Low | Before atomic write, copy current file to `claude_accounts.json.bak`. Allows manual recovery. |
| **JSON schema validation on load** | Low | Validate required fields and types before constructing `ClaudeAccountCredential`. Currently, a missing field causes an unhandled `KeyError`. |
| **Redact tokens in logs** | Low | Audit all `logger.*` calls to ensure access/refresh tokens are never logged in full. Currently `import_claude_auth.py` logs last 20 chars of access token — acceptable, but refresh tokens should never be logged. |
| **Optional encryption at rest** | Medium | Support an environment variable `CLAUDE_ACCOUNTS_KEY` for AES-256-GCM encryption of the JSON file. Decrypt on load, encrypt on persist. Optional — plain JSON remains the default. |
| **Separate refresh tokens** | Medium | Store refresh tokens in a separate file with stricter permissions, so the main accounts file can be shared (e.g., for read-only observability) without exposing long-lived secrets. |

### Priority

For Phase 1: file permission enforcement, backup-before-persist, JSON validation, log redaction.
Encryption at rest and token separation are Phase 2 — nice-to-have for multi-operator deployments.

---

## 8. Admin Observability Endpoint

### Motivation

Currently, diagnosing account issues requires reading server logs. There's no way to quickly check which accounts are healthy, when tokens expire, or how many failures each account has accumulated.

### Proposed endpoint

```
GET /admin/claude/accounts
Authorization: Bearer <ADMIN_TOKEN>
```

### Response schema

```json
{
  "pool_size": 3,
  "active_count": 2,
  "cooldown_count": 1,
  "revoked_count": 0,
  "disabled_count": 0,
  "accounts": [
    {
      "id": "acct_01",
      "label": "murphy-max",
      "state": "active",
      "plan": "max",
      "email": "m***@example.com",
      "organization_id": "org_...",
      "token_expires_at": "2026-03-17T15:30:00Z",
      "token_expires_in_seconds": 1800,
      "consecutive_failures": 0,
      "last_success": "2026-03-17T15:00:12Z",
      "last_failure": null,
      "last_refreshed_at": "2026-03-17T14:55:00Z",
      "refresh_count": 42,
      "state_changed_at": "2026-03-17T10:00:00Z"
    },
    {
      "id": "acct_02",
      "label": "team-pro",
      "state": "cooldown",
      "plan": "pro",
      "cooldown_until": "2026-03-17T15:02:00Z",
      "cooldown_remaining_seconds": 45,
      "consecutive_failures": 3,
      "last_failure": "2026-03-17T15:00:55Z",
      "revoke_reason": ""
    }
  ]
}
```

### Security

- **Admin-only:** Use the existing `verify_admin_token()` dependency from `serving/servers/auth.py`, which checks `Authorization: Bearer {ADMIN_TOKEN}` (env var `ADMIN_TOKEN`). Do NOT introduce a new `settings.admin_api_key` — reuse what's already there.
- **Redacted tokens:** Never include `access_token` or `refresh_token` in the response. Email is partially masked.
- **No mutations:** This is read-only. State changes (e.g., re-enable a revoked account) go through a separate `POST /admin/claude/accounts/{id}/state` endpoint.

### Admin state mutation endpoint

```
POST /admin/claude/accounts/{account_id}/state
Authorization: Bearer <ADMIN_TOKEN>
Content-Type: application/json

{"state": "active"}
```

Allowed transitions: `revoked → active` (with valid refresh token), `disabled → active`, `active → disabled`.

---

## 9. Phased Implementation Plan

### Implementation Scope

This section clarifies what has been implemented (MVP) versus what is deferred.

**MVP (implemented)** — the highest-value items for single-user stability:

| Item | Files | Status |
|---|---|---|
| `TokenRefreshError` hierarchy + `invalid_grant` detection | `claude_token.py` | Done |
| `state` field replacing `enabled`, v1→v2 migration, `transition_state()` | `claude_token.py` | Done |
| Backup-before-persist, JSON validation on load | `claude_token.py` | Done |
| `deactivate()` / `activate()` on `AccountPool` | `codex_token.py` | Done |
| `_acquire_with_retry()` in `claude_sub.py` | `claude_sub.py` | Done |
| Import dedup fix (refresh_token match, relaxed org_id, revoked reset) | `import_claude_auth.py` | Done |
| Read-only inspect CLI tool | `scripts/inspect_claude_accounts.py` | Done |

**Next iteration (deferred):**

| Item | Section | Reason deferred |
|---|---|---|
| Admin HTTP endpoints (`GET/POST /admin/claude/accounts`) | §8 | Overkill for single user; inspect script covers observability |
| Background refresh loop + lifecycle in `claude_pool.py` | §5 | Low traffic doesn't cause thundering herd |
| Identity verification (`account_uuid`, `expected_org_id`, drift detection) | §5 | Single user, single org — drift is unlikely |
| Post-import identity probe | §6 | Read-only profile endpoints not yet validated |
| Prometheus gauges for account state | §8 | No alerting need with 1-3 accounts |
| Multi-worker persist safety | §5 | Single-worker deployment assumption holds |
| Encryption at rest | §7 | Disproportionate to threat model |
| Cooldown escalation (revolving-door → revoked) | §3 | Simple cooldown + revoked covers current needs |

---

### Phase 1: Resilience (critical fixes)

**Goal:** Stop hard-crashing on token refresh failures. Detect revoked accounts.

| Task | Files | Effort |
|---|---|---|
| Add `TokenRefreshError` hierarchy | `claude_token.py` | S |
| Parse `invalid_grant` in `_refresh_token()` | `claude_token.py` | S |
| Add `_acquire_with_retry()` to `claude_sub.py` (match `anthropic_proxy.py`) | `claude_sub.py` | S |
| Replace `enabled` with `state` field on `ClaudeAccountCredential` | `claude_token.py` | S |
| Persist `state` and `consecutive_failures` to disk | `claude_token.py` (`_persist()`) | M |
| Implement `transition_state()` and `report_success()` on provider | `claude_token.py` (`ClaudeCredentialProvider`) | M |
| Add `deactivate(account_id)` to `AccountPool` | `codex_token.py` (generic, not Claude-specific) | S |
| Filter `revoked`/`disabled` on load; promote `cooldown` → `active` | `claude_token.py` (`load_accounts()`) | S |
| JSON schema validation on `load_accounts()` | `claude_token.py` | S |
| Backup-before-persist in `_persist()` | `claude_token.py` | S |
| Storage format migration v1 → v2 (`enabled` → `state`, add `version`) | `claude_token.py` (`load_accounts()`) | S |

**Milestone:** Account that returns `invalid_grant` is marked `revoked` and never retried. Restart preserves state.

### Phase 2: Observability

**Goal:** Admin can inspect pool health without SSH/logs.

| Task | Files | Effort |
|---|---|---|
| Add `GET /admin/claude/accounts` endpoint | New: `serving/servers/routers/admin_claude.py` | M |
| Add `POST /admin/claude/accounts/{id}/state` | Same file | M |
| Wire endpoints with existing `verify_admin_token()` dep | `admin_claude.py` (uses `auth.py`) | S |
| Emit structured log on every state transition | `claude_token.py` (via `transition_state()`) | S |
| Add Prometheus gauge: `claude_pool_account_state{id, state}` | `serving/observability/metrics.py` | S |

**Milestone:** `curl /admin/claude/accounts` returns full pool status. Grafana dashboard shows account states.

### Phase 3: Proactive Refresh & Identity

**Goal:** Prevent thundering herd on token expiry. Detect identity drift.

| Task | Files | Effort |
|---|---|---|
| Background refresh loop | `claude_token.py` | M |
| Identity verification on refresh (org_id drift detection) | `claude_token.py` | S |
| Add `account_uuid`, `expected_org_id`, `last_verified_at` fields | `claude_token.py` | S |
| Improved dedup rules in import script | `scripts/import_claude_auth.py` | M |
| Post-import identity probe (`--no-probe` to skip) | `scripts/import_claude_auth.py` | S |
| File permission check on load | `claude_token.py` | S |

**Milestone:** Tokens are proactively refreshed before expiry (no thundering herd). Import deduplication improved: same-session re-imports deduplicated via `refresh_token` match (Rule 3); post-import identity probe populates `account_uuid` for future cross-session dedup (Rule 2). Explicit `--account-id` remains the only guaranteed dedup path for all cases.

### Phase 4: Hardening (optional)

**Goal:** Production-grade for multi-operator deployment.

**Deployment assumption:** Phases 1-3 assume single-worker process. Phase 4 optionally addresses multi-worker if the deployment model changes.

| Task | Files | Effort |
|---|---|---|
| Optional encryption at rest (`CLAUDE_ACCOUNTS_KEY`) | `claude_token.py` | M |
| Separate refresh token storage | `claude_token.py`, import script | M |
| Cooldown escalation (revolving-door → revoked) | `claude_token.py` (`transition_state()`) | M |
| Multi-worker persist safety (file lock + merge-on-write, or per-account files) | `claude_token.py` | L |
| Log audit: ensure no full tokens in logs | All adapter files | S |

---

## 10. Reference Implementations

### CCProxy-API

[CCProxy-API](https://github.com/nicepkg/CCProxy-API) is a community Claude subscription proxy. Relevant patterns:

- **Account state tracking:** Maintains `status` field per account (`active`, `limited`, `error`) with timestamps. State is persisted to a JSON config file.
- **Token refresh error handling:** Distinguishes between `invalid_grant` (permanent, marks account as `error`) and other refresh failures (transient, retries with backoff).
- **Cooldown with escalation:** Accounts that hit rate limits enter a cooldown period. Repeated cooldowns within a time window escalate to a longer "penalty box" duration.
- **Admin dashboard:** Web UI showing per-account status, token expiry, request counts, and error rates.

**Applicable to us:** The cooldown escalation pattern and `invalid_grant` handling are directly relevant. Their admin dashboard is heavier than we need — a JSON API endpoint suffices.

### CLIProxyAPI

[CLIProxyAPI](https://github.com/nicepkg/CLIProxyAPI) is a simpler proxy focused on forwarding Claude Code CLI traffic. Relevant patterns:

- **OAuth constants:** Our `_CLIENT_ID`, `_OAUTH_TOKEN_URL`, `_ANTHROPIC_VERSION`, `_ANTHROPIC_BETA`, and `_REQUIRED_SYSTEM_PREFIX` values are sourced from this project. These are public OAuth client parameters used by the Claude Code CLI.
- **System prompt requirement:** OAuth subscription access requires the system prompt to begin with the Claude Code identity string. Both `claude_sub.py` and `anthropic_proxy.py` enforce this via `_ensure_system_prefix()`.
- **Beta header evolution:** The `Anthropic-Beta` header value changes as Anthropic adds/removes beta features. CLIProxyAPI tracks these changes. We should consider making this configurable rather than hardcoded.

**Applicable to us:** We've already adopted their OAuth constants. The main takeaway is to track upstream changes to beta headers and version strings, potentially via a config setting rather than code constants.

### Key differences from reference implementations

| Aspect | CCProxy-API | CLIProxyAPI | Our system |
|---|---|---|---|
| Multi-account pool | Yes | No (single account) | Yes |
| Health-aware rotation | Basic | N/A | Yes (round-robin + cooldown) |
| Fallback to paid API | No | No | Yes |
| Identity proxy surface | No | Yes | Yes (`anthropic_proxy.py`) |
| Chat Completions translation | Yes | No | Yes (`claude_sub.py`) |
| Credential persistence | Config file | Session only | JSON file with atomic write |

Our system combines features from both, plus the OpenAI-compatible Chat Completions translation layer and dual-surface architecture (Chat Completions + native Anthropic). The lifecycle improvements proposed in this document would bring our account management to parity with CCProxy-API's state tracking while maintaining our unique architecture.

---

## Appendix: File Reference

| File | Role |
|---|---|
| `serving/adapters/claude_token.py` | `ClaudeAccountCredential`, `ClaudeCredentialProvider` — OAuth lifecycle |
| `serving/adapters/codex_token.py` | `AccountPool`, `AccountHealth` — health-aware rotation |
| `serving/adapters/claude_pool.py` | Process-wide shared singleton (provider + pool) |
| `serving/adapters/claude_sub.py` | Chat Completions → Anthropic Messages API adapter |
| `serving/servers/routers/anthropic_proxy.py` | Native Anthropic Messages API reverse proxy |
| `scripts/import_claude_auth.py` | Import/dedup CLI credentials |
| `var/data/claude_accounts.json` | Credential storage (gitignored) |
| `serving/config/settings.py` | `claude_sub_*` configuration settings |
