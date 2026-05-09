# Signup ToS Agreement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require explicit Terms of Service agreement during signup and enforce it in both the frontend signup flow and the backend `POST /auth/signup` API.

**Architecture:** Keep the change on the existing signup validation boundary. The frontend Zod schema and signup page will require a checkbox linked to `/terms`, the frontend API client will send an explicit `accepted_tos` field, and the backend signup request model plus route will reject requests that do not include affirmative consent.

**Tech Stack:** Next.js, React Hook Form, Zod, Vitest, FastAPI, Pydantic, pytest, httpx

---

## File Map

- `apps/frontend/src/lib/schemas/auth.ts`
  Owns signup form validation. Add a required boolean field that must be `true`.
- `apps/frontend/src/lib/schemas/auth.test.ts`
  Owns focused schema validation coverage. Add failing and passing ToS tests here.
- `apps/frontend/src/app/signup/page.tsx`
  Owns the public signup UI. Render the checkbox, wire it to React Hook Form, and show the validation error.
- `apps/frontend/src/lib/api/auth.ts`
  Owns frontend auth request payloads. Extend `SignupRequest` and include `accepted_tos` in the signup body.
- `apps/backend/serving/schemas_auth.py`
  Owns backend auth request models. Extend `SignupRequest` with `accepted_tos: bool`.
- `apps/backend/serving/servers/routers/auth_routes.py`
  Owns backend signup behavior. Reject signup requests where `accepted_tos` is false.
- `tests/servers/test_auth_routes.py`
  Owns backend signup route behavior tests. Add focused signup tests for missing or false ToS consent.
- `tests/integration/test_signup_flow.py`
  Owns end-to-end signup-path coverage around allowlist behavior. Keep fixtures aligned with the new API contract so existing signup flow tests continue to pass.
- `tests/fixtures/auth_factories.py`
  Owns common signup request builders. Add `accepted_tos=True` to the default signup payload factory if it is not already present.

### Task 1: Frontend Schema TDD

**Files:**
- Modify: `apps/frontend/src/lib/schemas/auth.test.ts`
- Modify: `apps/frontend/src/lib/schemas/auth.ts`

- [ ] **Step 1: Write the failing frontend schema tests**

Add two tests to `apps/frontend/src/lib/schemas/auth.test.ts` inside the existing `describe('auth schemas', ...)` block:

```ts
  it('requires signup ToS acceptance', () => {
    const result = signupSchema.safeParse({
      email: 'user@example.org',
      password: 'SecurePass123',
      confirmPassword: 'SecurePass123',
      userName: 'Example User',
      acceptTerms: false,
    });

    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.flatten().fieldErrors.acceptTerms).toContain(
        'You must agree to the Terms of Service',
      );
    }
  });

  it('accepts signup input when ToS is agreed', () => {
    const result = signupSchema.safeParse({
      email: 'user@example.org',
      password: 'SecurePass123',
      confirmPassword: 'SecurePass123',
      userName: 'Example User',
      acceptTerms: true,
    });

    expect(result.success).toBe(true);
  });
```

- [ ] **Step 2: Run the frontend schema test to verify it fails**

Run: `npm run test -- src/lib/schemas/auth.test.ts`

Expected: FAIL because `signupSchema` does not yet define `acceptTerms`, so the rejection message is missing and/or the valid case is not shaped as expected.

- [ ] **Step 3: Write the minimal schema implementation**

Update `apps/frontend/src/lib/schemas/auth.ts` so `signupSchema` includes an `acceptTerms` boolean that must be `true`:

```ts
export const signupSchema = z
  .object({
    email: emailSchema,
    password: passwordSchema,
    confirmPassword: z.string(),
    userName: z
      .string()
      .trim()
      .min(2, 'Username must be at least 2 characters')
      .max(50, 'Username cannot exceed 50 characters'),
    useCase: z
      .string()
      .trim()
      .max(2000, 'Use case cannot exceed 2000 characters')
      .optional()
      .or(z.literal('')),
    acceptTerms: z.literal(true, {
      errorMap: () => ({ message: 'You must agree to the Terms of Service' }),
    }),
  })
  .refine((data) => data.password === data.confirmPassword, {
    message: 'Passwords do not match',
    path: ['confirmPassword'],
  });
```

If the local Zod version does not accept that `errorMap` shape on `z.literal`, use the equivalent minimal pattern that preserves the same field-level message, such as a boolean field refined to `true` on `path: ['acceptTerms']`.

- [ ] **Step 4: Run the frontend schema test to verify it passes**

Run: `npm run test -- src/lib/schemas/auth.test.ts`

Expected: PASS, including the new rejection and acceptance cases.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/lib/schemas/auth.ts apps/frontend/src/lib/schemas/auth.test.ts
git commit -m "feat(auth): require terms acceptance in signup schema"
```

### Task 2: Signup UI And Frontend Payload

**Files:**
- Modify: `apps/frontend/src/app/signup/page.tsx`
- Modify: `apps/frontend/src/lib/api/auth.ts`

- [ ] **Step 1: Write the failing UI test or extend existing frontend coverage if a signup page test already exists**

If there is already a signup page test module, add a focused test there. If none exists, create `apps/frontend/src/app/signup/page.test.tsx` with a minimal rendering test that proves the checkbox and terms link exist:

```tsx
// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/script', () => ({
  default: () => null,
}));

import SignupPage from './page';

describe('SignupPage', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders a required terms agreement checkbox linked to /terms', () => {
    render(<SignupPage />);

    expect(
      screen.getByRole('checkbox', { name: /i agree to the terms of service/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /terms of service/i })).toHaveAttribute(
      'href',
      '/terms',
    );
  });
});
```

This step is intentionally UI-focused. Do not test submit behavior yet unless the file already has the surrounding test setup.

- [ ] **Step 2: Run the focused frontend UI test to verify it fails**

Run: `npm run test -- src/app/signup/page.test.tsx`

Expected: FAIL because the checkbox and link are not rendered yet.

- [ ] **Step 3: Implement the minimal signup page and API payload change**

In `apps/frontend/src/app/signup/page.tsx`:

1. Include `errors.acceptTerms?.message` in form rendering.
2. Register a checkbox field with `register('acceptTerms')`.
3. Render the checkbox near the submit button with copy similar to:

```tsx
          <label className="flex items-start gap-3 rounded-lg border border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-700">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
              {...register('acceptTerms')}
            />
            <span>
              I agree to the{' '}
              <a className="font-medium text-blue-600 hover:text-blue-700" href="/terms">
                Terms of Service
              </a>
              .
            </span>
          </label>
          {errors.acceptTerms && (
            <span className="block text-xs text-red-600">{errors.acceptTerms.message}</span>
          )}
```

In the submit handler, include the explicit consent flag in the request body:

```ts
      const result = await signup({
        email: data.email,
        password: data.password,
        user_name: data.userName.trim(),
        use_case: data.useCase?.trim() || undefined,
        accepted_tos: data.acceptTerms,
        turnstileToken: turnstileTokenRef.current ?? undefined,
      });
```

In `apps/frontend/src/lib/api/auth.ts`, extend the request type and serialized body:

```ts
export interface SignupRequest {
  email: string;
  password: string;
  user_name: string;
  use_case?: string;
  accepted_tos: boolean;
  turnstileToken?: string;
}
```

No additional transformation is needed beyond allowing `accepted_tos` to remain in `rest` so it is serialized into the JSON request body.

- [ ] **Step 4: Run the frontend UI and schema tests to verify they pass**

Run: `npm run test -- src/app/signup/page.test.tsx src/lib/schemas/auth.test.ts`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/app/signup/page.tsx apps/frontend/src/app/signup/page.test.tsx apps/frontend/src/lib/api/auth.ts
git commit -m "feat(auth): add signup terms consent UI"
```

### Task 3: Backend Signup Contract TDD

**Files:**
- Modify: `tests/servers/test_auth_routes.py`
- Modify: `tests/fixtures/auth_factories.py`
- Modify: `apps/backend/serving/schemas_auth.py`
- Modify: `apps/backend/serving/servers/routers/auth_routes.py`
- Modify: `tests/integration/test_signup_flow.py`
- Modify: `tests/servers/test_email_verification_required.py`

- [ ] **Step 1: Write the failing backend signup tests**

Add two focused tests under `class TestSignup` in `tests/servers/test_auth_routes.py`:

```py
    @pytest.mark.asyncio
    async def test_signup_missing_tos_acceptance(self, auth_app_client: AsyncClient):
        signup_data = create_signup_request()
        signup_data.pop("accepted_tos")

        response = await auth_app_client.post("/auth/signup", json=signup_data)

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_signup_rejects_false_tos_acceptance(self, auth_app_client: AsyncClient):
        signup_data = create_signup_request(accepted_tos=False)

        response = await auth_app_client.post("/auth/signup", json=signup_data)

        assert response.status_code == 400
        assert response.json()["detail"] == "You must agree to the Terms of Service to create an account"
```

Also inspect `tests/fixtures/auth_factories.py`. If `create_signup_request()` does not already include `accepted_tos`, add it in the default payload so existing signup tests continue to describe a valid request:

```py
def create_signup_request(**overrides):
    data = {
        "email": f"user_{secrets.token_hex(4)}@signuptest.dev",
        "password": "SecurePass123!",
        "user_name": "Test User",
        "accepted_tos": True,
    }
    data.update(overrides)
    return data
```

For direct inline payloads in `tests/servers/test_email_verification_required.py`, add `"accepted_tos": True` to each signup body because those tests do not use the factory.

- [ ] **Step 2: Run the focused backend tests to verify they fail**

Run: `pytest tests/servers/test_auth_routes.py -k "signup and tos" -v`

Expected: FAIL because the request factory or direct payloads do not yet align with the API contract and the route does not yet reject `accepted_tos=False`.

- [ ] **Step 3: Implement the minimal backend contract and route enforcement**

Update `apps/backend/serving/schemas_auth.py`:

```py
class SignupRequest(BaseModel):
    """User signup request."""

    email: EmailStr
    password: str = Field(..., min_length=8)
    user_name: UserName
    use_case: str | None = Field(default=None, max_length=2000)
    accepted_tos: bool
    turnstile_token: str | None = None
```

Update `apps/backend/serving/servers/routers/auth_routes.py` near the top of the signup handler, after signup-enabled checks and before user creation logic:

```py
    if not body.accepted_tos:
        raise HTTPException(
            status_code=400,
            detail="You must agree to the Terms of Service to create an account",
        )
```

Update `tests/fixtures/auth_factories.py` and any direct signup payloads in `tests/servers/test_email_verification_required.py` and other touched signup tests so normal signup scenarios continue to send valid consent.

Update `tests/integration/test_signup_flow.py` only as needed to keep `create_signup_request(...)`-based calls aligned; if the factory is the sole source there, no further assertions are needed.

- [ ] **Step 4: Run the focused backend tests to verify they pass**

Run: `pytest tests/servers/test_auth_routes.py -k "signup and tos" -v`

Expected: PASS, with one 422 case for missing `accepted_tos` and one 400 case for explicit false consent.

- [ ] **Step 5: Run adjacent signup tests to guard regressions**

Run:

```bash
pytest tests/servers/test_auth_routes.py -k signup -v
pytest tests/servers/test_email_verification_required.py -v
pytest tests/integration/test_signup_flow.py -v -m dbtest
```

Expected: PASS. Any failures should be caused by missing `accepted_tos` in direct test payloads and fixed without broadening scope.

- [ ] **Step 6: Commit**

```bash
git add tests/servers/test_auth_routes.py tests/servers/test_email_verification_required.py tests/integration/test_signup_flow.py tests/fixtures/auth_factories.py apps/backend/serving/schemas_auth.py apps/backend/serving/servers/routers/auth_routes.py
git commit -m "feat(auth): enforce signup terms acceptance"
```

### Task 4: Final Verification

**Files:**
- No code changes expected

- [ ] **Step 1: Run the focused frontend verification suite**

Run:

```bash
cd apps/frontend && npm run test -- src/lib/schemas/auth.test.ts src/app/signup/page.test.tsx
```

Expected: PASS.

- [ ] **Step 2: Run the focused backend verification suite**

Run:

```bash
pytest tests/servers/test_auth_routes.py -k signup -v
pytest tests/servers/test_email_verification_required.py -v
pytest tests/integration/test_signup_flow.py -v -m dbtest
```

Expected: PASS.

- [ ] **Step 3: Inspect the final diff**

Run: `git diff --stat`

Expected: only the signup form, auth request model, auth route, auth fixtures, and directly related tests or plan/spec files are changed.

- [ ] **Step 4: Commit any remaining verification-only adjustments**

```bash
git add -A
git commit -m "test(auth): cover signup terms acceptance"
```

Only do this step if verification required an additional code or test fix after the earlier commits. If no files changed after the previous commit, skip this commit.

## Self-Review

- Spec coverage: the plan covers frontend checkbox UX, `/terms` link reuse, frontend payload changes, backend `accepted_tos` contract enforcement, and both frontend/backend tests.
- Placeholder scan: removed vague test instructions by naming exact files, exact assertions, and exact commands.
- Type consistency: the same field names are used throughout the plan: frontend `acceptTerms`, API/backend `accepted_tos`, backend request model `accepted_tos`, route check `body.accepted_tos`.
