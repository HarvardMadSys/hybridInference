# Scrub Provider Error Messages — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace provider-specific text in user-facing HTTP error responses with generic, status-code-driven messages plus a `request_id`. Keep the original error in `api_logs.error` for operator debugging.

**Architecture:** Add `UserFacingError` marker base class and `scrub_error_for_user(exc, request_id, status_code)` helper to `serving/exceptions.py`. Mark existing intentionally user-facing exceptions (auth/quota/etc.) as `UserFacingError`. Replace raw `str(exc)` at three HTTP boundaries (`completions.py`, `anthropic_proxy._forward_upstream_error`, `anthropic_proxy._forward_raw_error`) with the scrubbed message, while ensuring the original error is persisted to the DB at each boundary.

**Tech Stack:** Python, FastAPI, pytest, aiohttp, SQLite (api_logs).

**Spec:** `docs/agents/specs/2026-05-02-scrub-provider-error-messages-design.md`

---

## File Structure

**Created:**
- `test/test_error_scrubbing.py` — unit tests for `scrub_error_for_user` and `UserFacingError`.

**Modified:**
- `serving/exceptions.py` — add `UserFacingError` marker, add `scrub_error_for_user`, make existing user-facing classes inherit from `UserFacingError`.
- `serving/servers/routers/completions.py` — replace `str(exc)` in `HTTPException` with scrubbed message (line 1022). DB log call (line 1001) stays unchanged.
- `serving/servers/routers/anthropic_proxy.py` — `_forward_upstream_error` and `_forward_raw_error` return scrubbed message; persist original to DB; thread `request_id` and `log_store` into both helpers and their call sites.

---

## Task 1: `UserFacingError` marker + `scrub_error_for_user` helper

**Files:**
- Modify: `serving/exceptions.py`
- Test: `test/test_error_scrubbing.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `test/test_error_scrubbing.py`:

```python
"""Unit tests for user-facing error scrubbing."""

import pytest

from serving.exceptions import (
    AuthenticationError,
    HybridInferenceError,
    QuotaExceededError,
    UserFacingError,
    scrub_error_for_user,
)


# Realistic provider error bodies (must NEVER appear in user-facing output).
PROVIDER_ERROR_SAMPLES = [
    "anthropic returned 500: internal_server_error",
    'OpenAI API error: {"error": {"message": "model overloaded"}}',
    "OpenRouter upstream timeout from https://openrouter.ai/api/v1",
    "claude-3-5-sonnet failed: token limit exceeded",
    "Connection refused to https://api.anthropic.com/v1/messages",
]

FORBIDDEN_SUBSTRINGS = ("anthropic", "openai", "openrouter", "claude", "https://", "api.")


@pytest.mark.parametrize("body", PROVIDER_ERROR_SAMPLES)
@pytest.mark.parametrize(
    "status_code,expected_prefix",
    [
        (401, "Authentication failed"),
        (403, "Authentication failed"),
        (429, "Rate limit exceeded"),
        (400, "Invalid request"),
        (422, "Invalid request"),
        (500, "Upstream service error"),
        (502, "Upstream service error"),
        (503, "Upstream service error"),
        (418, "Request failed"),
    ],
)
def test_scrub_replaces_message_by_status(body, status_code, expected_prefix):
    exc = RuntimeError(body)
    msg = scrub_error_for_user(exc, "req_abc", status_code)
    assert msg.startswith(expected_prefix), msg
    lowered = msg.lower()
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in lowered, (
            f"forbidden token {forbidden!r} leaked in {msg!r}"
        )


def test_scrub_appends_request_id_when_present():
    msg = scrub_error_for_user(RuntimeError("boom"), "req_xyz", 500)
    assert "(request_id: req_xyz)" in msg


@pytest.mark.parametrize("rid", ["", None])
def test_scrub_omits_request_id_when_blank(rid):
    msg = scrub_error_for_user(RuntimeError("boom"), rid, 500)
    assert "request_id" not in msg


def test_user_facing_error_passes_through():
    exc = QuotaExceededError(quota=1.0, spent=2.5)
    msg = scrub_error_for_user(exc, "req_q", 402)
    assert "Quota exceeded" in msg
    assert "(request_id: req_q)" in msg


def test_non_user_facing_subclass_is_scrubbed():
    class InternalUpstream(HybridInferenceError):
        pass

    msg = scrub_error_for_user(InternalUpstream("anthropic 500"), "req_i", 500)
    assert "anthropic" not in msg.lower()
    assert msg.startswith("Upstream service error")


def test_authentication_error_is_user_facing():
    # AuthenticationError must inherit UserFacingError so its messages reach the user.
    assert issubclass(AuthenticationError, UserFacingError)


def test_none_exception_still_scrubs():
    msg = scrub_error_for_user(None, "req_n", 500)
    assert msg.startswith("Upstream service error")
    assert "(request_id: req_n)" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/juncheng/hybridInference-scrub-errors && uv run pytest test/test_error_scrubbing.py -v`
Expected: ImportError on `UserFacingError` and `scrub_error_for_user`.

- [ ] **Step 3: Implement `UserFacingError` and `scrub_error_for_user`**

Edit `serving/exceptions.py`. After the existing `HybridInferenceError` class (around line 12), add the marker class:

```python
class UserFacingError(HybridInferenceError):
    """Marker base for exceptions whose message is safe to surface verbatim
    to end users. Subclasses' str(exc) is passed through scrub_error_for_user
    unchanged (with a request_id suffix appended).
    """

    pass
```

Make these existing classes inherit from `UserFacingError` instead of (or in addition to) their current base. Update each `class` line:

- `AuthenticationError(HybridInferenceError)` → `AuthenticationError(UserFacingError)`
- `UserNotFoundError(HybridInferenceError)` → `UserNotFoundError(UserFacingError)`
- `DuplicateAPIKeyError(HybridInferenceError)` → `DuplicateAPIKeyError(UserFacingError)`
- `APIKeyNotFoundError(HybridInferenceError)` → `APIKeyNotFoundError(UserFacingError)`
- `QuotaExceededError(HybridInferenceError)` → `QuotaExceededError(UserFacingError)`

Subclasses of `AuthenticationError` (`UserAlreadyExistsError`, `WeakPasswordError`, `InvalidCredentialsError`, `EmailNotVerifiedError`, `AccountSuspendedError`, `TokenExpiredError`, `InvalidTokenError`, `TokenAlreadyUsedError`, `SessionNotFoundError`, `SessionRevokedError`) inherit `UserFacingError` transitively — no change needed.

Append the helper function at the end of `serving/exceptions.py`:

```python
# ----------------------------------------------------------------------
# User-facing error scrubbing
# ----------------------------------------------------------------------

_GENERIC_MESSAGES_BY_STATUS: dict[int, str] = {
    400: "Invalid request",
    401: "Authentication failed",
    403: "Authentication failed",
    422: "Invalid request",
    429: "Rate limit exceeded",
}


def scrub_error_for_user(
    exc: BaseException | None,
    request_id: str | None,
    status_code: int,
) -> str:
    """Return a user-safe error message containing no provider-specific info.

    The original exception text is intentionally NOT echoed back unless the
    exception is a `UserFacingError` subclass (whose message is, by author
    contract, free of provider info). Callers are responsible for persisting
    the full `str(exc)` to `api_logs.error` keyed by the same `request_id`.
    """
    if isinstance(exc, UserFacingError):
        base = str(exc)
    elif status_code in _GENERIC_MESSAGES_BY_STATUS:
        base = _GENERIC_MESSAGES_BY_STATUS[status_code]
    elif 500 <= status_code < 600:
        base = "Upstream service error"
    else:
        base = "Request failed"

    if request_id:
        return f"{base} (request_id: {request_id})"
    return base
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/juncheng/hybridInference-scrub-errors && uv run pytest test/test_error_scrubbing.py -v`
Expected: all 50+ parametrized cases PASS.

- [ ] **Step 5: Run formatter and full lint**

Run: `cd /home/juncheng/hybridInference-scrub-errors && uv run ruff format . && uv run ruff check serving/exceptions.py test/test_error_scrubbing.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
cd /home/juncheng/hybridInference-scrub-errors
git add serving/exceptions.py test/test_error_scrubbing.py
git commit -m "feat(errors): add UserFacingError marker and scrub_error_for_user helper

Generic status-code-driven messages for non-user-facing exceptions, with
request_id suffix. UserFacingError subclasses pass through verbatim."
```

---

## Task 2: Scrub `HTTPException` in completions router

**Files:**
- Modify: `serving/servers/routers/completions.py:1022`

- [ ] **Step 1: Inspect the current site to confirm context**

Read lines 990-1023 of `serving/servers/routers/completions.py`. Confirm the structure:
- `exc_status_code` is in scope.
- `request_id` is in scope.
- `_schedule_db_log_task` (line 1001) writes `"error": str(exc)` — leave untouched.

The existing unit tests in Task 1 already cover the scrubbing logic exhaustively. This task is a single-line replacement at the boundary; no additional route-level test is added.

- [ ] **Step 2: Edit `completions.py` to call the scrubber**

In `serving/servers/routers/completions.py`, locate the import block at the top of the file and add:

```python
from serving.exceptions import scrub_error_for_user
```

(Place it alphabetically with other `serving.` imports.)

Replace line 1022:

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

The DB log call above (the `_schedule_db_log_task(...)` block at line 1001) stays unchanged — `"error": str(exc)` continues to land in the DB.

- [ ] **Step 3: Run the relevant test suite**

Run: `cd /home/juncheng/hybridInference-scrub-errors && uv run pytest test/test_error_scrubbing.py test/ -k "completion or error" -v`
Expected: no regressions.

- [ ] **Step 4: Commit**

```bash
cd /home/juncheng/hybridInference-scrub-errors
git add serving/servers/routers/completions.py
git commit -m "fix(completions): scrub provider info from HTTPException detail

Route still persists original str(exc) and provider name to api_logs;
user response now carries a generic message plus request_id."
```

---

## Task 3: Scrub error helpers in `anthropic_proxy.py` and persist originals to DB

**Files:**
- Modify: `serving/servers/routers/anthropic_proxy.py` — `_schedule_db_log` (line 172), `_forward_upstream_error` (line 619), `_forward_raw_error` (line 636), and call sites at lines 444, 447, 525, 536, 541, 547.

- [ ] **Step 1: Extend `_schedule_db_log` to accept an `error` parameter**

Edit `serving/servers/routers/anthropic_proxy.py`. Update the `_schedule_db_log` signature (line 172):

```python
def _schedule_db_log(
    log_store,
    *,
    request_id: str,
    model_id: str,
    account_id: str | None,
    usage: dict[str, int],
    latency_ms: int,
    status_code: int,
    pricing: dict[str, str],
    metadata: dict[str, Any],
    error: str | None = None,
) -> None:
```

Inside `_log()`, pass `error=error` to `log_store.log_request(...)`. Confirm `log_store.log_request` accepts an `error` kwarg by reading its signature in `serving/storage/database.py` (search for `def log_request`). If it doesn't, add `error: str | None = None` to that signature too and persist it into the existing `api_logs.error` column (the column already exists per `serving/storage/database.py:216`).

- [ ] **Step 2: Add scrubbed error helpers**

Add an import at the top of `serving/servers/routers/anthropic_proxy.py`:

```python
from serving.exceptions import scrub_error_for_user
```

Replace `_forward_upstream_error` (lines 619-633) with:

```python
def _forward_upstream_error(
    exc: aiohttp.ClientResponseError,
    model_id: str,
    *,
    request_id: str,
    log_store=None,
    account_id: str | None = None,
    latency_ms: int = 0,
    metadata: dict[str, Any] | None = None,
) -> JSONResponse:
    """Return an Anthropic-format error with provider info scrubbed.

    Persists the raw upstream error body (if present) to api_logs.error so
    operators can debug from the request_id surfaced to the user.
    """
    API_MODEL_REQUESTS.labels(
        model=normalize_model_label(model_id),
        provider=normalize_provider_label(_PROVIDER_NAME),
        status_code=str(exc.status),
    ).inc()
    raw_error = getattr(exc, "error_body", None) or exc.message or str(exc)
    if log_store is not None:
        _schedule_db_log(
            log_store,
            request_id=request_id,
            model_id=model_id,
            account_id=account_id,
            usage={"input_tokens": 0, "output_tokens": 0},
            latency_ms=latency_ms,
            status_code=exc.status,
            pricing={},
            metadata=metadata or {},
            error=str(raw_error),
        )
    scrubbed = scrub_error_for_user(exc, request_id, exc.status)
    return _anthropic_error(exc.status, scrubbed)
```

Replace `_forward_raw_error` (lines 636-646) with:

```python
def _forward_raw_error(
    status: int,
    body: str,
    model_id: str,
    *,
    request_id: str,
    log_store=None,
    account_id: str | None = None,
    latency_ms: int = 0,
    metadata: dict[str, Any] | None = None,
) -> JSONResponse:
    """Return a scrubbed Anthropic-format error and persist raw body to DB."""
    API_MODEL_REQUESTS.labels(
        model=normalize_model_label(model_id),
        provider=normalize_provider_label(_PROVIDER_NAME),
        status_code=str(status),
    ).inc()
    if log_store is not None:
        _schedule_db_log(
            log_store,
            request_id=request_id,
            model_id=model_id,
            account_id=account_id,
            usage={"input_tokens": 0, "output_tokens": 0},
            latency_ms=latency_ms,
            status_code=status,
            pricing={},
            metadata=metadata or {},
            error=body or "",
        )
    scrubbed = scrub_error_for_user(None, request_id, status)
    return _anthropic_error(status, scrubbed)
```

- [ ] **Step 3: Update all call sites**

Update each call site to pass the new kwargs. Both `_forward_streaming` and `_forward_non_streaming` already have `request_id`, `log_store`, `account.id`, `start_time`, and `metadata` in scope — wire them through.

In `_forward_non_streaming`, lines 444 and 447:

```python
            except aiohttp.ClientResponseError as retry_exc:
                account_pool.report_failure(account.id, retry_exc.status)
                return _forward_upstream_error(
                    retry_exc,
                    model_id,
                    request_id=request_id,
                    log_store=log_store,
                    account_id=account.id,
                    latency_ms=int((time.time() - start_time) * 1000),
                    metadata=metadata,
                )
```

```python
        else:
            account_pool.report_failure(account.id, exc.status)
            return _forward_upstream_error(
                exc,
                model_id,
                request_id=request_id,
                log_store=log_store,
                account_id=account.id,
                latency_ms=int((time.time() - start_time) * 1000),
                metadata=metadata,
            )
```

Also update the bare `_anthropic_error` calls in `_forward_non_streaming` (line 454) and `_forward_streaming` (lines 525, 536) so they don't leak the `{exc}` interpolation. Replace each:

```python
return _anthropic_error(502, f"Upstream connection failed: {exc}")
```

with:

```python
if log_store is not None:
    _schedule_db_log(
        log_store,
        request_id=request_id,
        model_id=model_id,
        account_id=account.id,
        usage={"input_tokens": 0, "output_tokens": 0},
        latency_ms=int((time.time() - start_time) * 1000),
        status_code=502,
        pricing={},
        metadata=metadata,
        error=f"Upstream connection failed: {exc}",
    )
return _anthropic_error(502, scrub_error_for_user(exc, request_id, 502))
```

(Apply the same transform at lines 525 and 536. For line 536's "on retry" message, set `error=f"Upstream connection failed on retry: {exc}"`.)

In `_forward_streaming`, lines 541 and 547:

```python
return _forward_raw_error(
    resp.status,
    error_body,
    model_id,
    request_id=request_id,
    log_store=log_store,
    account_id=account.id,
    latency_ms=int((time.time() - start_time) * 1000),
    metadata=metadata,
)
```

Apply the same expansion to line 547.

- [ ] **Step 4: Manually trace one error path end-to-end**

In a comment **on the PR description (not in code)**, note the path you traced. Example: "Traced 502 connection-failed branch from `_forward_non_streaming` line 454: raw exc text persisted via `_schedule_db_log(error=...)`, user receives `Upstream service error (request_id: aprx_...)`."

- [ ] **Step 5: Run lint and existing tests**

Run: `cd /home/juncheng/hybridInference-scrub-errors && uv run ruff format . && uv run ruff check serving/servers/routers/anthropic_proxy.py && uv run pytest test/ -k "anthropic or proxy or error" -v`
Expected: no errors, no regressions.

- [ ] **Step 6: Commit**

```bash
cd /home/juncheng/hybridInference-scrub-errors
git add serving/servers/routers/anthropic_proxy.py serving/storage/database.py
git commit -m "fix(anthropic_proxy): scrub upstream error bodies before forwarding to user

_forward_upstream_error and _forward_raw_error now persist the raw
upstream body to api_logs.error and return a scrubbed Anthropic-format
error to the client. Bare _anthropic_error(502, f...) call sites moved
to the same pattern."
```

---

## Task 4: Final lint, full test pass, push branch

- [ ] **Step 1: Format check**

Run: `cd /home/juncheng/hybridInference-scrub-errors && uv run ruff format --check .`
Expected: no diffs.

- [ ] **Step 2: Lint check**

Run: `cd /home/juncheng/hybridInference-scrub-errors && uv run ruff check .`
Expected: no errors.

- [ ] **Step 3: Full test suite**

Run: `cd /home/juncheng/hybridInference-scrub-errors && uv run pytest test/ -x`
Expected: PASS.

- [ ] **Step 4: Push branch**

Run: `cd /home/juncheng/hybridInference-scrub-errors && git push -u origin jason/claude/scrub-provider-error-messages`
Expected: branch published.

(PR creation is handled outside this plan, by the parent agent.)
