# Signup abuse protection — implementation plan

## Goal
Stop bots from flooding `/auth/signup` and burning Resend quota. Three layers, one PR:

1. **Cloudflare Turnstile** challenge on the signup form (verified server-side before any DB write).
2. **IP-based rate limit** on `/auth/signup`: 5 / hour and 10 / day per client IP.
3. **Email domain blocklist** rejecting obvious throwaway/test domains at signup.

Branch already created: `jason/claude/signup-abuse-protection` (worktree at `/srv/hybridInference/.claude/worktrees/signup-abuse-protection`). Base = `origin/dev` (e174d06).

## Files to create / modify

### Backend
- **NEW** `serving/utils/turnstile.py` — `async def verify_turnstile_token(token: str, remote_ip: str) -> bool`. Calls `https://challenges.cloudflare.com/turnstile/v0/siteverify` with `httpx`. If `settings.turnstile_secret_key` is empty, treat as disabled and return `True` (so dev/test environments work without keys).
- **NEW** `serving/utils/email_blocklist.py` — `BLOCKED_EMAIL_DOMAINS: frozenset[str]` (lowercase) and `def is_email_domain_blocked(email: str) -> bool`. Initial list: `example.com`, `example.org`, `example.net`, `test.com`, `test.org`, `mailinator.com`, `guerrillamail.com`, `guerrillamail.net`, `tempmail.com`, `tempmail.net`, `10minutemail.com`, `yopmail.com`, `trashmail.com`, `throwawaymail.com`, `fakeinbox.com`, `getairmail.com`, `dispostable.com`, `maildrop.cc`. Reject any address whose domain is in the set OR ends with `.test` / `.example` / `.invalid` / `.localhost` (RFC 2606 reserved TLDs).
- **NEW** `serving/utils/signup_rate_limit.py` — purpose-specific sliding-window IP limiter, sqlite-backed at `data/db/signup_rate_limits.db`, two windows (1h, 24h). Function: `async def check_and_record_signup(ip: str) -> tuple[bool, str | None]` — returns `(allowed, reason_if_blocked)`. Avoid coupling to existing `PersistentRateLimiter` (that's token-bucket for inference, not a fit). Use `aiosqlite` if available; otherwise `sqlite3` in a `to_thread`.
- **MODIFY** `serving/config/settings.py`:
  - Add `turnstile_site_key: str = ""`
  - Add `turnstile_secret_key: str = ""`
  - Already has `signup_rate_limit_per_hour: int = 5` — add `signup_rate_limit_per_day: int = 10`
- **MODIFY** `serving/schemas_auth.py` `SignupRequest`: add `turnstile_token: str | None = None` (optional so existing API consumers + tests without keys work).
- **MODIFY** `serving/servers/routers/auth_routes.py` `signup()`:
  - Get client IP via existing `get_client_ip(request)`.
  - Check rate limit FIRST (before any DB read). On block: HTTP 429 with `Retry-After`.
  - Verify Turnstile token if `settings.turnstile_secret_key` set. On fail: HTTP 400 `"Captcha verification failed"`.
  - Check email blocklist after Pydantic validation. On block: HTTP 400 `"This email domain is not allowed"`. Use generic message — do not enumerate blocked domains.
  - Order: rate limit → Turnstile → blocklist → existing password validation → existing duplicate check → existing insert.
  - Record the rate-limit hit AFTER all validation passes (so a bot probing with bad inputs doesn't get free tries against the same IP — actually, NO: record on every attempt that reaches the endpoint, including failures, so bots can't bypass by varying the payload). **Decision: record on entry, before any other check.**

### Frontend
- **MODIFY** `frontend/src/lib/api/auth.ts` `signup()` request type — add `turnstileToken?: string`. Send as `turnstile_token` in body.
- **MODIFY** the signup page (find with `grep -rn "signup" frontend/src/app/`) — add Turnstile widget. Use `@marsidev/react-turnstile` if it can be added, OR implement directly with the official `<script src="https://challenges.cloudflare.com/turnstile/v0/api.js">` and a callback. Site key from `process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY`. If env is empty, render the form without the widget (dev mode).
- **MODIFY** `frontend/.env.example` (or create) — document `NEXT_PUBLIC_TURNSTILE_SITE_KEY`.

### Config / docs
- **MODIFY** `.env.example` — add `TURNSTILE_SITE_KEY=`, `TURNSTILE_SECRET_KEY=`, `SIGNUP_RATE_LIMIT_PER_HOUR=5`, `SIGNUP_RATE_LIMIT_PER_DAY=10`.

### Tests
- **MODIFY** `test/servers/test_auth_routes.py`:
  - `test_signup_rate_limited_per_hour` — 6th attempt within an hour returns 429.
  - `test_signup_rate_limited_per_day` — 11th attempt within a day returns 429.
  - `test_signup_blocked_domain_example_com` — returns 400, generic message.
  - `test_signup_blocked_reserved_tld_test` — `foo@bar.test` returns 400.
  - `test_signup_turnstile_missing_when_required` — when secret key set in fixture, missing token returns 400.
  - `test_signup_turnstile_valid` — happy path with mocked verifier.
- The autouse `auth_test_env` fixture in `test/servers/conftest.py` already empties SMTP — keep that. ADD `TURNSTILE_SECRET_KEY: ""` to `_AUTH_VARS` so Turnstile is disabled by default; tests that need it on can `monkeypatch.setenv` per-test.
- For rate-limit tests, use a per-test sqlite DB path via fixture (env var `SIGNUP_RATE_LIMIT_DB`) so tests don't share state. Default code path uses `data/db/signup_rate_limits.db` in production.
- Use the existing `mock_email_service` pattern as needed.

## Out of scope
- Purging existing `@example.com` rows from prod DB (manual review later).
- Redis-backed limiter.
- Migrating broadcast email plan.

## Acceptance criteria
- All new tests pass.
- `make lint` (or equivalent — check `pyproject.toml` for ruff/black) clean.
- `mypy` / type-check clean.
- Existing test suite still passes.
- Frontend builds (`npm run build` from `frontend/`).
- No secrets committed; `.env.example` updates only.

## Conventions
- Follow existing code style: type hints, no docstrings on trivial functions, errors via `HTTPException`.
- No new comments unless explaining a non-obvious "why".
- Match existing import ordering and module layout.
- Commit message style: `feat(auth): add signup abuse protection (Turnstile + rate limit + blocklist)`.

## Verification commands
```bash
cd /srv/hybridInference/.claude/worktrees/signup-abuse-protection
# Backend lint + tests
uv run ruff check . 2>&1 | tail -20
uv run ruff format --check . 2>&1 | tail -20
uv run pytest test/servers/test_auth_routes.py -v 2>&1 | tail -40
# Frontend
cd frontend && npm run lint 2>&1 | tail -20 && npm run build 2>&1 | tail -10
```
