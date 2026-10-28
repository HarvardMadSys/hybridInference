# Creating batches in the console: implementation and verification record

- Date: 2026-10-28
- Author: Claude Code (Opus 5.5, high effort), for independent acceptance by Codex.
- Design: [2026-10-28-batch-ui-create-design.zh.md](../agents/specs/2026-10-28-batch-ui-create-design.zh.md)
- Scope: the first-release console Batch creation feature (Chat Completions and
  Embeddings from a JSONL file), with list search/status filtering, details,
  copy, stored and unfinished request exports, and the minimal uploaded-files
  clean-up. Messages/Responses creation and copy, trial runs, one-click
  retries, CSV/Excel input and cost estimates are out of scope, as designed.

The author checks and the subsequent independent Codex acceptance are recorded
separately below.

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
Review the initial feature-only commit with `git diff 319544fcf..22af7831`.

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

## Independent Codex integration and acceptance

Before delivery, Codex integrated `dev` at `453884a3` and refreshed #1687 to
`f7e5e987`. The latter retains HTTP 410 migration responses for retired Message
Batches paths, requires the Messages version in the `anthropic-version` header,
and preserves the legacy webhook event for old jobs. Both dependency PRs remain
open; the local integration does not merge either PR or deploy it.

Final executable code was checked at `4d23fa22`. Subsequent changes update this
record, the design and the verified Chinese translation catalog only. Codex also
fixed one remaining account-switch boundary: a pending IndexedDB clear must not
publish the old account's settled submission into a new account's state. A
deferred-clear hook regression fails before the guard and passes after it.
The public-asset guard now explicitly permits the two neutral JSONL templates,
and the deletion regression accepts a copy already removed by the background
collector while still requiring no usable copy or created job.

All independent execution used an isolated development machine, disposable
PostgreSQL databases, synthetic identities and a loopback fake provider. No
production data, real provider inference or shared deployment was used.

| Final check | Result |
|---|---|
| `make format`, `make lint` | passed |
| `make test` | 10,244 passed, 4 skipped |
| Complete PostgreSQL integration tier | 781 passed |
| Frontend type-check, lint, format, i18n and tests | passed; 168 files, 2,145 tests |
| Frontend production build | passed |
| `make docs-verify` | passed; English/Chinese catalog and local links checked |
| Secret scan and production dependency audit | no leaks or reported vulnerabilities |

The full live matrix ran on `860e41ce` (the feature plus current dev):

- Real Chat and Embeddings upload, automatic recognition, preview, submission,
  worker execution and result downloads. Preview, stored requests and provider
  input agree after overrides; preflight makes no provider calls.
- Copy, lost-response recovery, concurrent same-key submission, keyless guidance,
  cross-owner and administrator isolation, unfinished exports, deleted-source
  copy state, and retained management after role downgrade.
- Key revocation and regeneration after one completed, one in-flight and one
  queued request. The queued request never reached the provider; completed
  output remained downloadable. Work authorized before revocation could finish.
- Real desktop and 390px browser creation/download, copy defaults, committed
  response loss with automatic recovery, and disabled submission without a key.
  An old unknown intent performed a lookup without an automatic POST; only
  ambiguous input showed a type choice. No page errors or horizontal overflow.
- Independent regressions for source expiry during a row-lock wait (real
  PostgreSQL store-level), oversized JSON integers, empty model capabilities,
  rewritten total byte limits, and final-buffer storage exhaustion (real HTTP).

After the dependency refresh, `4d23fa22` was exercised again for Chat and
Embeddings creation through worker results, both original pre-feature API file
jobs, all 12 retired Message Batches method/path combinations returning 410,
and a real Chinese browser creation through completion. Desktop and 390px
Chinese screenshots were visually inspected. The frontend source is identical
to the source covered by the complete final frontend gates.

Browser acceptance used Next.js development mode; the production build was
verified separately. These checks do not establish real-provider billing or
production-load behavior. Raw logs, synthetic credentials and screenshots are
kept in task-local evidence, not committed with infrastructure details.

## Follow-up: four status categories for people (2026-10-28)

The ordinary list and details now show and filter by four categories plus
All -- Processing, Completed (every request succeeded), With issues and
Cancelled -- instead of the eight API statuses; the API, worker, storage and
admin views keep the eight. The mapping is defined once on the server (Python
and its SQL twin) and applied before pagination. Checks the author ran for this
change:

| Check | Result |
|---|---|
| `make test-db-local ARGS="tests/integration/storage/test_batch_console_categories.py"` | 3 passed (every lifecycle state in Python and SQL alike; grouped pagination with non-matching rows, search, ownership; exact `status` filter and `/v1` status unchanged). Removing the completed-count condition from the SQL made it fail |
| Frontend batch component tests | 55 passed (five filter choices and the request they make, Chinese labels, partial failure not green, pause note kept, no download while results are being prepared, list and details alike) |

Repository gates run for this change: `make format` (clean), `make lint`
(passed), `make test` (10,243 passed, 5 skipped), `make test-db-local` (784
passed), frontend type-check, lint, format, i18n (zh-CN 952/952) and tests
(168 files, 2,152 tests), and `make docs-verify` (passed). Live and browser
acceptance of this change is Codex's, in its own environment.
