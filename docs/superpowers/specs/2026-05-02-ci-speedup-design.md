# CI Speedup — Design Spec

**Date**: 2026-05-02
**Status**: Approved
**Owner**: jason

## Problem

CI wall time dominated by `Test` job at ~144s (1553 pytest tests serial @ 99s + 45s setup). Other jobs (lint 18s, frontend 40s, security 32s) run in parallel and aren't on the critical path. Goal: reduce wall time to ~60–75s.

## Goals

- Cut `Test` job runtime by ~50% via test parallelization.
- Skip CI entirely on documentation-only PRs.
- Drop coverage overhead on PR runs (still produce coverage on push to `dev`/`main`).
- Reduce dependency-install overhead via venv caching.

## Non-Goals

- Restructuring `Frontend Lint & Test` job (40s, off the critical path).
- Splitting `Security` job (32s, off the critical path).
- Refactoring tests beyond what xdist isolation requires.
- Switching test runner, DB engine, or CI provider.

## Design

### 1. Pytest parallelization with `pytest-xdist`

- Add `pytest-xdist` to `[dependency-groups].dev` in `pyproject.toml`.
- Update CI test command to `pytest -n auto --dist=loadfile -m "not external" ...`.
  - `-n auto` → one worker per CPU (4 on `ubuntu-latest`).
  - `--dist=loadfile` → all tests in same file go to same worker. Reduces cross-file DB row collisions and minimizes need for fixture refactor.
- **DB isolation**: `test/servers/conftest.py` and `conftest_auth.py` currently share a single `freeinference_test_db`. With `loadfile`, parallel workers can still write the same tables across files. Mitigation: introduce per-worker DB name using xdist's `worker_id` fixture.
  - At session start, derive DB name `${TEST_DB_NAME}_${worker_id}` (e.g. `freeinference_test_db_gw0`). Worker `master` (when `-n 0` or running serially) keeps the base name unchanged.
  - Add a session-scoped fixture that creates the worker-specific database if missing and points session env / engine config at it.
  - Existing safety check (`TEST_DB_NAME` must contain `test`) continues to pass since the suffix is appended to a name that already contains "test".
- Validation: run full suite locally with `-n auto --dist=loadfile`. If flaky, fall back to `--dist=loadscope` or reduce to `-n 2`.

### 2. Skip CI on docs-only changes

Add `paths-ignore` to the `push` and `pull_request` triggers in `.github/workflows/ci.yml`:

```yaml
paths-ignore:
  - 'docs/**'
  - '**/*.md'
  - 'LICENSE'
  - '.gitignore'
```

Note: GitHub treats `paths-ignore` as "no relevant change" → workflow doesn't run. If branch protection requires the workflow to actually execute on every PR, replace with a path-filter job that returns success without running tests.

### 3. Conditional coverage

- PR runs (`github.event_name == 'pull_request'`): `pytest -n auto --dist=loadfile -v -m "not external"`.
- Push runs (`github.event_name == 'push'`): include `--cov=. --cov-report=term-missing --cov-report=xml`.
- Codecov upload step gated on `if: github.event_name == 'push'`.
- Implementation: split into two `Run tests` steps with mutually exclusive `if:` conditions, OR build the command via a step that sets a `PYTEST_ARGS` env var.

### 4. Cache uv-managed venv

- Add `actions/cache@v4` keyed on `runner.os`, Python version, and `hashFiles('uv.lock')`, restoring `.venv`.
- Place between `Set up Python` and `Install dependencies`. `uv sync` becomes near-instant on cache hit.
- Apply to both `lint` and `test` jobs (`security` uses `uvx` so skip).

## Expected Impact

| Job | Before | After | Notes |
|---|---|---|---|
| Test | 144s | ~60–75s | xdist on 4 workers + cached venv + no coverage on PR |
| Lint | 18s | ~10s | venv cache |
| Frontend | 40s | 40s | unchanged |
| Security | 32s | 32s | unchanged |
| **Wall** | **144s** | **~70s** | dominated by Test |

## Risks

- **DB collisions under xdist**: Per-worker DB mitigation above is the primary defense. If schema setup is expensive, session-level fixture amortizes it.
- **Required status checks + `paths-ignore`**: Branch protection that requires CI to "actually run" will block merges of doc-only PRs. If observed, swap to a path-filter no-op job.
- **Test ordering assumptions**: Some tests may rely on sequential execution. xdist randomizes file→worker assignment. Catch via local run before merge.
- **Coverage gap on PR**: Reviewers lose per-PR coverage signal. Acceptable since coverage still tracked on `dev`/`main` push.

## Rollback

Single-commit revert restores serial test runs and prior CI.

## Out of Scope

- Splitting `Frontend Lint & Test` into parallel lint vs test.
- Sharding pytest across multiple GitHub runners (matrix). Only worth it if single-runner xdist saturates.
- Replacing pip-audit with cached audit DB.
