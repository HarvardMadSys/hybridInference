# Scrub Provider-Specific Info From User-Facing Error Messages

**Status:** Approved
**Date:** 2026-05-02
**Author:** jason (via Claude Code)

## Problem

When upstream providers (Anthropic, OpenAI, OpenRouter, etc.) return errors, the
proxy currently surfaces the raw exception string to end users. This leaks
implementation details (provider names, upstream URLs, vendor-specific error
codes/messages) that we don't want to expose. Operators still need full provider
context for debugging.

Examples of leakage today:
- `serving/servers/routers/completions.py:1111` — `HTTPException(exc_status_code, str(exc))`
- `serving/servers/routers/anthropic_proxy.py:644-658` — `_forward_upstream_error`
  returns the raw upstream JSON body verbatim.
- `serving/adapters/claude.py:140-144` — wraps upstream error text in a
  `RuntimeError`, which propagates upward and ends up in `str(exc)`.

## Goal

User-facing HTTP error responses contain a generic, status-code-appropriate
message and a `request_id` the user can quote to support. The full unfiltered
error (including provider name and raw upstream body) continues to be persisted
to `api_logs.error` so operators can debug from the request_id.

## Non-Goals

- Changing log levels, log formats, or stdout/stderr scrubbing.
- Touching the `provider` column in `api_logs` (that field stays as-is — it's
  operator-only data).
- Reworking the exception hierarchy beyond what's needed for scrubbing.
- Scrubbing in non-HTTP code paths (background jobs, health probes).

## Design

### Scrubber

New helper in `serving/exceptions.py`:

```python
def scrub_error_for_user(
    exc: BaseException | None,
    request_id: str,
    status_code: int,
) -> str:
    """
    Return a user-safe error message that contains no provider-specific
    information. The full original error is expected to be persisted to
    api_logs.error separately by the caller.
    """
```

Behavior — message selection is driven by `status_code` only. The exception
content is **never** echoed back to the user (we do not attempt regex-scrubbing
of the original message — that's fragile).

| Status code     | User-visible message                                       |
|-----------------|------------------------------------------------------------|
| 401, 403        | `Authentication failed (request_id: <id>)`                 |
| 429             | `Rate limit exceeded (request_id: <id>)`                   |
| 400, 422        | `Invalid request (request_id: <id>)`                       |
| 5xx             | `Upstream service error (request_id: <id>)`                |
| anything else   | `Request failed (request_id: <id>)`                        |

If `request_id` is empty/None, omit the `(request_id: ...)` suffix.

### Allowlist for known-safe messages

Some exceptions raised inside `serving/exceptions.py` are already authored by
us and contain no provider info (e.g., quota errors, auth errors raised by our
own auth middleware). These should pass through unchanged so users still see
useful messages like "Daily quota exceeded".

Mechanism: a new marker base class `UserFacingError(Exception)`. Any exception
inheriting from `UserFacingError` is exempt from scrubbing — the scrubber
returns `str(exc)` directly (still appended with `(request_id: <id>)`).

Existing classes in `serving/exceptions.py` that should inherit from
`UserFacingError`:
- All explicit auth/business-logic errors already defined there.

The implementing agent must audit `serving/exceptions.py` and mark every
existing exception class that is intentionally user-facing. New exception
classes default to **not** user-facing (i.e., they get scrubbed) — this is the
safe default.

### Touch points

1. **`serving/servers/routers/completions.py:1111`**
   Replace:
   ```python
   raise HTTPException(exc_status_code, str(exc)) from exc
   ```
   with:
   ```python
   raise HTTPException(
       exc_status_code,
       scrub_error_for_user(exc, request_id, exc_status_code),
   ) from exc
   ```
   The DB log call at `completions.py:1090` is **unchanged** — it continues to
   write `str(exc)` and `provider_for_error` into `api_logs`.

2. **`serving/servers/routers/anthropic_proxy.py:644-658` (`_forward_upstream_error`)**
   Today the function returns the raw upstream JSON body verbatim. Replace with:
   - Persist the raw body to `api_logs.error` via the existing DB log path
     (the route handler that calls `_forward_upstream_error` must be the one
     to schedule the DB log — `_forward_upstream_error` itself returns the
     scrubbed response).
   - Return `_anthropic_error(exc.status, scrubbed_msg)` where `scrubbed_msg`
     comes from `scrub_error_for_user(exc, request_id, exc.status)`.

   Implementer note: trace the callers of `_forward_upstream_error` in
   `anthropic_proxy.py` to confirm a `request_id` is in scope and that DB
   logging is wired up at each call site. If a call site doesn't have a
   request_id, add one (use the same request_id generation pattern used in
   `completions.py`).

3. **`serving/adapters/claude.py:140-144`**
   No code change required. The `RuntimeError(f"Upstream API error: {error_msg}")`
   is caught by the HTTP boundary in `completions.py` / `anthropic_proxy.py`
   and scrubbed there. Verify by reading the call chain — if any direct
   user-facing return path exists from this adapter that bypasses the HTTP
   boundary, scrub at that boundary too.

### Logging invariant

Every code path that previously sent provider-specific error text to the user
MUST also be writing the original `str(exc)` to `api_logs.error` keyed by the
same `request_id` returned to the user. If a path is missing the DB log,
**add it** as part of this work — otherwise the request_id we hand the user
will be useless.

## Testing

### Unit tests (`test/test_error_scrubbing.py`, new file)

- `scrub_error_for_user` returns the right message for each status code class
  (401, 403, 429, 400, 422, 500, 502, 503, 418).
- Output never contains the substrings `anthropic`, `openai`, `openrouter`,
  `claude`, `https://`, `api.` regardless of input exception text. Use a
  parametrized test with realistic upstream error bodies.
- `request_id` is appended when provided, omitted when empty/None.
- `UserFacingError` subclasses pass through their own message, with
  `request_id` appended.

### Integration tests

Existing `test/` integration tests that exercise the completions route should
gain coverage:
- Mock an upstream 500 → assert response body contains no provider tokens AND
  `api_logs` row for the same request_id contains the original error text.
- Mock an upstream 429 → response says `Rate limit exceeded`, DB has the raw
  upstream rate-limit message.

If no such integration harness exists in the repo, add a focused test using
the existing FastAPI test client setup (search `test/` for `TestClient`).

## Rollout

Single PR to `dev`. No feature flag — error message format is observable but
not contractual. Staging deploy on merge; verify against
`https://staging.freeinference.org` by triggering an auth failure and
confirming the response body lacks provider info.

## Open questions

None — design approved.
