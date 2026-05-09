# Signup ToS Agreement Design

## Goal

Require users to explicitly agree to the Terms of Service during public signup, and enforce that requirement in both the frontend signup form and the backend `POST /auth/signup` API.

## Current State

The signup UI lives in `apps/frontend/src/app/signup/page.tsx` and is validated by `apps/frontend/src/lib/schemas/auth.ts`. It currently collects email, username, password, password confirmation, optional use case text, and an optional Turnstile token.

The backend signup contract is defined by `apps/backend/serving/schemas_auth.py` and handled in `apps/backend/serving/servers/routers/auth_routes.py`. The request body currently does not include any Terms of Service acceptance field, so a direct API caller can create an account without expressing consent.

An existing Terms of Service page already exists at `apps/frontend/src/app/terms/page.tsx`, so the signup flow can link to that route instead of introducing a new legal page.

## Goals

- Make ToS agreement a required part of signup UX.
- Prevent bypass by enforcing ToS acceptance in the backend signup API.
- Reuse the existing `/terms` page.
- Keep the change minimal and aligned with existing form and schema patterns.

## Non-Goals

- Versioning or storing the exact Terms of Service revision accepted by a user.
- Persisting acceptance metadata such as timestamp, IP, or document version.
- Adding separate Privacy Policy or marketing-consent checkboxes.
- Changing login, admin, or existing account-management flows.

## Approved Design

### 1. Frontend form requirement

Extend `signupSchema` in `apps/frontend/src/lib/schemas/auth.ts` with a required boolean field for ToS agreement. The schema should reject submissions where the field is not explicitly true and surface a field-level validation message such as `You must agree to the Terms of Service`.

This keeps validation consistent with the current React Hook Form plus Zod flow, so the submit handler only runs when the checkbox is checked.

### 2. Signup UI

Render a checkbox in `apps/frontend/src/app/signup/page.tsx` near the submit button, after the other form inputs and before Turnstile or submit handling. The label should clearly state that creating an account requires agreement to the Terms of Service and should link to `/terms`.

The link should open through the app's normal routing behavior. No modal or inline terms preview is required for this change.

### 3. API contract

Extend `SignupRequest` in `apps/frontend/src/lib/api/auth.ts` and `apps/backend/serving/schemas_auth.py` with an `accepted_tos` boolean.

The frontend signup request should always send `accepted_tos: true` for valid form submissions. Keeping the field explicit in the API contract makes backend enforcement straightforward and documents the policy at the boundary.

### 4. Backend enforcement

In `apps/backend/serving/servers/routers/auth_routes.py`, reject signup requests where `accepted_tos` is false with a `400` response and a clear error message such as `You must agree to the Terms of Service to create an account`.

This check should happen early in the signup flow before any user record is created. Placing the rule in the route ensures direct API callers cannot bypass the requirement even if they skip the web form.

### 5. Error handling and UX

Normal browser users should usually see the frontend schema error before any request is sent. API callers or malformed requests should receive the backend `400` response.

No additional success-response fields are needed. Existing signup success and approval messages remain unchanged.

## Files To Change

- `apps/frontend/src/lib/schemas/auth.ts`
- `apps/frontend/src/lib/schemas/auth.test.ts`
- `apps/frontend/src/app/signup/page.tsx`
- `apps/frontend/src/lib/api/auth.ts`
- `apps/backend/serving/schemas_auth.py`
- `apps/backend/serving/servers/routers/auth_routes.py`
- Backend auth-route tests for signup behavior, in the existing auth test module for that route

## Testing

Follow TDD for both frontend and backend validation:

1. Add or update frontend schema tests so signup fails when ToS is not accepted and passes when it is accepted.
2. Add or update backend signup tests so requests without `accepted_tos` or with `accepted_tos=false` are rejected.
3. Run the focused frontend and backend test targets covering the changed files.
4. If practical after the focused tests pass, run broader project validation for the affected areas.

## Rationale

This design is the smallest correct end-to-end enforcement.

- Frontend-only validation would be easy to bypass.
- Backend-only validation would protect the API but would produce avoidable UX friction.
- Reusing `/terms` avoids introducing new content or routing complexity.

The change intentionally does not store acceptance metadata because the current request is only to require agreement during signup, not to build a consent-audit system.
