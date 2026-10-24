# User Dashboard: Display Max Allowed Concurrency

Date: 2026-05-09

## Goal

Show authenticated user their per-user max concurrent inflight request cap on
the user dashboard. Read-only display. No editing, no live in-flight count.

## Context

Per-user concurrency caps already enforced server-side by
`UserConcurrencyLimiter` in `apps/backend/serving/servers/concurrency.py`.
Caps are role-keyed (`trial`/`free`/`pro`/`internal`/`admin`) and tunable via
runtime settings (`user_concurrency_{role}` keys in
`apps/backend/serving/config/runtime_settings.py`). Admin tab already
displays/edits these. End user has no visibility today.

User-facing dashboard renders `UsageStats.tsx`, fetched via `/user/usage`
endpoint that returns `UsageResponse(quota=QuotaInfo, usage=UsageStats)`.

## Design

### Backend

1. **Schema** — `apps/backend/serving/schemas_auth.py`:
   Add to `QuotaInfo`:

   ```python
   max_concurrency: int | None = None
   ```


2. **Helper** — `apps/backend/serving/servers/routers/user_routes.py`:
   Add `get_user_concurrency_for_role(role, runtime_settings)` mirroring
   the existing `get_default_daily_quota_for_role` pattern:
   - Look up `user_concurrency_{role}` in `RUNTIME_SETTINGS_REGISTRY`.
   - If present and `runtime_settings is not None`: return
     `await runtime_settings.get_int(key)`.
   - Else fall back to `_FALLBACK_LIMITS` from
     `serving.servers.concurrency`; if role itself missing from the
     fallback dict, use `_FALLBACK_LIMITS["free"]`.

3. **Endpoint** — `apps/backend/serving/servers/routers/user_routes.py::get_usage`:
   - Read role from `current_user["role"]` (already populated by
     `get_current_user` dep, as used elsewhere in this file).
   - Resolve `RuntimeSettings` via `get_runtime_settings_instance()` with
     try/except `RuntimeError` (existing pattern).
   - Call helper, populate `QuotaInfo.max_concurrency` on **both** the
     no-key early return and the main return (cap applies regardless of
     key presence).

4. **Tests** — `tests/unit/`:
   - Add `test_user_routes_usage_max_concurrency.py` (or extend existing
     usage test if any) covering:
     - Each defined role returns its registry default.
     - Runtime override value is reflected.
     - Unknown role → free-tier fallback.
     - Runtime read raises → constant fallback.
     - `has_key=False` branch still includes the field.

### Frontend

1. **Type** — `apps/frontend/src/lib/api/user.ts`:
   Add `max_concurrency?: number;` to the `quota` object inside
   `UsageStats` interface.

2. **UI** — `apps/frontend/src/components/features/dashboard/UsageStats.tsx`:
   Inside the existing blue "Daily quota remaining" banner, after the
   `Resets at …` line, render an additional subline:

   ```tsx
   {quota.max_concurrency != null && (
     <div className="mt-1 text-sm text-blue-800">
       Max concurrent requests: {quota.max_concurrency}
     </div>
   )}
   ```

   No layout restructure. Hide when undefined to keep older client/server
   pairings safe.

3. **Test** — extend or add a small render test verifying the line appears
   when value present and is hidden when absent.

## Out of scope

- Editing caps from user dashboard.
- Showing live in-flight request count.
- Showing per-tier comparison (trial vs pro).
- Admin SettingsTab changes — already shows tunables.

## Risks / edge cases

- Role string casing: registry keys are lowercase (`trial`, `free`, `pro`,
  `internal`, `admin`). Lower-case the resolved role before formatting the
  setting key to avoid `KeyError` on stored mixed case.
- Runtime cache TTL already covers churn — no new caching needed.

## Acceptance

- `GET /user/usage` includes `quota.max_concurrency` for any authenticated
  user, matching that user's role-resolved runtime setting.
- User dashboard shows `Max concurrent requests: N` line in quota banner.
- All new and existing unit tests pass; `make format` and `make test`
  clean.
