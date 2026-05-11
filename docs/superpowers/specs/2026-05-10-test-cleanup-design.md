# Test Fixture Cleanup

**Date:** 2026-05-10
**Status:** Approved (design)
**Author:** Juncheng Yang (with Kilo)

## Context

The test suite already has meaningful top-level separation across `tests/unit/`,
`tests/api/`, `tests/servers/`, `tests/observability/`, and `tests/external/`.
The main maintainability issue is not file layout. It is duplicated fixture and
setup logic, especially in server and auth-oriented tests.

The highest-value duplication cluster is the pair of server fixture modules:

- `tests/servers/conftest.py`
- `tests/servers/conftest_auth.py`

They currently duplicate several concerns:

- test auth/database environment defaults
- PostgreSQL test-database safety guards
- setup and cleanup helpers for auth-related DB state
- user seeding and login/header generation helpers

There is also repeated local setup in several server tests that rebuild the same
`FastAPI` + `AppServices` + `AsyncClient` pattern. That repetition is real, but
it is a secondary cleanup target because it spans more test-specific behavior.

## Goals

- Reduce duplicated auth/server test helper logic without changing test intent.
- Preserve the semantic difference between:
  - full-lifespan auth/server fixtures that exercise app startup behavior
  - narrower DB-backed auth fixtures that focus on route/storage behavior
- Make shared test helpers easier to discover and reuse in future test files.
- Keep the cleanup small enough that affected tests can be validated directly.

## Non-Goals

- Reorganizing the whole `tests/` directory.
- Renaming large groups of tests or fixtures purely for style consistency.
- Converting all server tests onto one universal app/client fixture.
- Broad refactors of test assertions unrelated to fixture duplication.

## Recommended Approach

Extract shared auth/server test helpers into a small helper module under the
existing test support area, then update both `tests/servers/conftest.py` and
`tests/servers/conftest_auth.py` to consume those helpers while preserving their
current fixture boundaries.

This is the best payoff/risk tradeoff because it removes the most obvious
duplication while avoiding a semantic merge of two fixture stacks that currently
serve different types of tests.

## Alternatives Considered

### 1. Shared app/client factory across many server tests

This would reduce repeated `FastAPI`/`AppServices`/`AsyncClient` boilerplate in
files such as `test_admin_api.py`, `test_admin_users.py`, `test_models.py`, and
`test_qdrant_proxy.py`.

Why not first:

- the reuse pattern is broader but less uniform than the auth helpers
- lifespan and dependency-override behavior vary between tests
- the touch surface is larger, so regression risk is higher

### 2. Fully unify auth fixtures into one canonical conftest stack

This would maximize deduplication.

Why not first:

- the existing fixture stacks intentionally exercise different behavior
- a full merge could hide important distinctions between startup tests and
  route-level tests
- it is more likely to change test runtime or semantics unexpectedly

## Design

### 1. Helper module placement

Add a focused helper module under the test support tree, preferably in an area
already used for reusable test code, such as:

- `tests/fixtures/auth_helpers.py`, or
- `tests/utils/auth_helpers.py`

The file should contain only reusable helper functions, not pytest fixtures.
Fixtures remain in the existing `conftest.py` files so fixture scope and naming
stay visible at the test layer.

Recommendation: prefer `tests/fixtures/auth_helpers.py` because the existing
repository already uses `tests.fixtures.auth_factories` and this keeps related
test support code colocated.

### 2. Shared helper responsibilities

Extract the following logic into helpers.

#### DB safety helpers

Centralize the logic that verifies tests only operate on dedicated test
databases.

Expected helpers:

- assert test DB name matches the required pattern
- verify an acquired pool/connection points at a test DB
- optionally probe database availability and return a skip-friendly failure

This replaces the near-duplicate guard logic currently split between
`tests/servers/conftest.py` and `tests/servers/conftest_auth.py`.

#### Auth environment defaults

Centralize default auth/database env values in a helper that returns a dict,
with optional overrides applied by the caller.

Expected behavior:

- one canonical source for auth-related test env defaults
- callers can request DB-backed or DB-disabled defaults as needed
- fixture-specific overrides remain explicit in the fixture body

The fixture itself should still own when and how environment variables are set.
Only the data source moves to the helper module.

#### Auth data helpers

Centralize common auth test operations:

- clean auth-related tables
- seed a test user from `create_test_user()` with optional overrides
- log in through a test client and return auth headers

These operations are currently implemented in more than one place and are good
fits for reusable helper functions because they do not need pytest scoping on
their own.

### 3. Fixture structure after cleanup

Keep both fixture stacks, but simplify them.

`tests/servers/conftest.py` remains responsible for:

- session-level auth test environment setup
- startup/lifespan-aware app wiring
- broader server test fixtures shared across the directory

`tests/servers/conftest_auth.py` remains responsible for:

- auth-specific DB-backed fixture composition
- auth test app/client fixtures that are intentionally narrower in scope
- data fixtures built from the shared helper functions

This preserves the current testing model while removing internal duplication.

### 4. Optional opportunistic follow-up

If the helper extraction makes a nearby test file obviously simpler, allow one
small opportunistic cleanup in the same pass. The likely candidate is a local
auth-user or login-header helper inside a server test file that becomes a thin
wrapper around the new shared helper.

This follow-up must stay small and should not expand into a suite-wide router
fixture refactor.

## Implementation Notes

- Prefer small helper functions with narrow names over one large test harness.
- Keep existing fixture names where practical to avoid unnecessary churn.
- Do not hide important fixture side effects behind generic wrappers.
- Reuse existing factories such as `tests.fixtures.auth_factories.create_test_user`
  rather than introducing new parallel factories.

## Risks And Mitigations

### Risk: helper extraction changes fixture semantics

Mitigation:

- keep fixture scopes unchanged
- keep fixture names unchanged where possible
- move logic, not ownership, out of conftest files

### Risk: over-generalized helpers become harder to read than duplication

Mitigation:

- extract only repeated logic with clear inputs/outputs
- avoid a catch-all builder object or multi-purpose test harness

### Risk: DB-backed auth tests and startup-aware tests diverge in subtle ways

Mitigation:

- keep the two fixture stacks separate
- share only low-level helpers such as env defaults, DB guards, and auth data
  setup/cleanup routines

## Validation

Run the tests directly affected by the cleanup.

Minimum validation target:

- the auth-related tests that consume `tests/servers/conftest.py`
- the auth-related tests that consume `tests/servers/conftest_auth.py`

Practical command selection can be finalized during implementation, but the
validation pass should confirm:

- fixtures still initialize correctly
- DB safety checks still behave as intended
- login/user-seeding helpers still produce the expected test data
- no server/auth tests regress due to changed fixture composition

## Success Criteria

- duplicated helper logic is removed from the two server auth conftest modules
- the new helper module has clear, narrow responsibilities
- existing test behavior remains unchanged from the caller's perspective
- targeted validation passes for the affected server/auth tests
