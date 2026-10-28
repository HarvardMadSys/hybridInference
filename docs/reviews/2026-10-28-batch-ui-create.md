# Creating batches in the console: implementation and verification record

- Date: 2026-10-28
- Author: Claude Code (Opus 5.5, high effort), for independent acceptance by Codex.
- Design: [2026-10-28-batch-ui-create-design.zh.md](../agents/specs/2026-10-28-batch-ui-create-design.zh.md)
- Scope: the first-release console Batch creation feature (Chat Completions and
  Embeddings from a JSONL file), with list search/status filtering, details,
  copy, stored and unfinished request exports, and the minimal uploaded-files
  clean-up. Messages/Responses creation and copy, trial runs, one-click
  retries, CSV/Excel input and cost estimates are out of scope, as designed.

This record states what the author ran. It is not Codex's acceptance, which
is recorded separately.

## Baseline

The feature sits on a local integration of two pull requests that were open
and unmerged at the time:

| Ref | Commit |
|---|---|
| `dev` | `df40a9a82a6d5a39a9ffbdfebde3650f94cb5146` |
| PR #1687 (unified Batch API) head | `477f171ce3873415c19f2c66130b4634fc1d1416` |
| PR #1680 (Batch entry permissions) head | `aba29b25e611096ffaff28d034a7aade95939f9c` |
| Integration base (both merged locally) | `319544fcf9f79b66b8219956969565cede95297c` |

Conflicts (`batch_console.py`, `BatchesView.tsx`, `zh-CN.json`,
`batches.po`) were resolved keeping #1687's unified DTO, status and result
behaviour and #1680's capabilities and unavailable state. On the base the
batch backend tests and the frontend gates passed before any feature change.
Review feature-only changes with `git diff 319544fcf..HEAD`.

## Review rounds addressed

Two rounds of independent findings were fixed, each with a regression; most
regressions were also checked to fail with the fix reverted.

- Round 1: stale pending submissions (lookup by key; automatic resend only
  inside the guaranteed replay window; explicit new submission otherwise);
  replay decided before the active-batch cap under the admission lock; the
  account's own model access (disabled models) in preflight without a key;
  a source lock that conflicts with deletion; owner-only file routes for
  administrators too; derived-copy lifecycle; strict schema validation; a
  credential revision that detects in-place key regeneration.
- Round 2: source expiry re-judged after the lock wait (PostgreSQL barrier
  test); oversized integers as row errors; an explicitly empty sampling
  capability list refusing overrides (backend and UI); the stored file's
  total size limit; uploads bound to the starting account across refresh and
  retry; stale-submission actions bound to the key shown, with
  compare-and-replace in both persistence back ends; generation guards so
  late uploads, identifications and checks cannot change the page or what is
  submitted; overrides a newly chosen model does not take are cleared visibly;
  copy eligibility re-checked on submit.
- Final round: a storage cap hit by the buffered last chunk of the stored copy
  answers `413 storage_full` (it was a 500); uploads require the page's account
  explicitly; a new file's failed identification never leaves the previous
  file's report in use; a token refresh or 401 retry started for one session
  can neither install its token over, end, nor run as a session installed
  later in the same tab (session generation in the shared client).

## Checks run by the author

All on a development server, with synthetic accounts, a fake loopback
provider and disposable databases; no production, staging or shared
deployment, real credential or external provider was used.

| Check | Command | Result |
|---|---|---|
| Format | `make format` | clean |
| Lint | `make lint` | passed |
| Default suite | `make test` | 10,064 passed, 5 skipped |
| Database tier | `make test-db-local` | 767 passed |
| Console create, real PostgreSQL | `make test-db-local ARGS="tests/integration/test_batch_console_create_postgres.py"` | 37 passed (included in the tier) |
| Frontend | `npm run type-check`, `npm run lint`, `npm run format:check`, `npm run i18n:check`, `npm run test` (in `apps/frontend`) | passed; 163 files, 2,105 tests; zh-CN 941/941 |
| Frontend build | `npm run build` | passed |
| Docs | `make docs-verify` | passed |
| Live smoke | `ops/dev/batch_console_smoke.py` (`upstream`, `models`, `seed`, `check`) against a gateway and its in-process worker started from this branch, on a throwaway PostgreSQL container | 45 checks, `BATCH_CONSOLE_SMOKE_OK` |

The live smoke drives the session routes over HTTP end to end: recognition by
`url` and by body shape, preflight without model calls, submission under the
account's key, a run to completion on the fake provider, overrides on a copy,
problems by line without request content, type choice only when undetermined,
mixed files refused, disabled and explicitly parameter-less models,
`no_worker_credential`, `credential_changed`, `model_resolution_changed`,
idempotent replay, conflict and `410`, cancellation with the unfinished
export, owner-only access for another user and an administrator, refusal of a
derived copy by `/v1/batches`, and a deleted source.

Browser checks were not completed by the author: the console build used for
them needs a same-origin API base (`NEXT_PUBLIC_API_BASE=` empty), and Codex
runs the real-browser acceptance in its own environment. The component tests
cover the create flow, recovery, cross-tab and account-switch cases in jsdom.

## Known limitations

- Scan concurrency limits (two per process, one per user) are per process, not
  global. A retry that overlaps its own first attempt may get `429 scan_busy`;
  the page treats it as transient and retries with the same key.
- A submission whose answer never arrived and whose batch has since been
  purged cannot be told apart from one that never committed; after a day the
  page stops and asks rather than resending.
- The derived-copy cascade guarantees hold only once every gateway process runs
  this version (see the design's compatibility section).
- A request may still be billed more than once when an attempt's result is
  lost; the idempotency key prevents duplicate batches, not duplicate billing.
