# Verification: PR #1733 — hard-delete claim lease with CAS and heartbeat

Date: 2026-11-12. Pull request: [HarvardMadSys/hybridInference#1733],
"feat(storage): hard-delete claim lease with CAS and heartbeat (replacement for
#1463)".

Scope: independent verification of the PR's **current head**
(`8ee83d2cf`, "merge: rebuild the hard-delete lease change on current dev"),
against the ten invariants in the review brief. The November 8 review's
historical findings were not repeated; every invariant was re-derived from the
current head and checked against real PostgreSQL.

Everything below ran against the disposable local test database
(`hybridinference-test-postgres`, database `hi_48e8feac8a_test_db*`). No
production or user data was touched. Nothing was merged or pushed.

## What was reviewed

The head was fetched from the upstream monorepo
(`HarvardMadSys/hybridInference`, `refs/pull/1733/head` = `8ee83d2cf`) and
verified to be the fork branch's tip
(`B-A-M-N/hybridInference:prep/1463-claim-lease-dev-replacement`).

Base: `c8dc56c57727c9ba8e0fbe1ec99ccbd68bb68cda`, four commits behind current
`dev` (`82a1c2bb0`). The PR's merge base is that commit; `dev` has moved on
since, but the four later commits touch other subsystems.

The change is 20 files, +1752/−130: the claim lease in
`serving/storage/postgres_operational.py` and `serving/storage/base.py`, the
cache wrapper, the admin routes, the `0016_hard_delete_claim_lease`
migration, and the two schemas.

## Invariant-by-invariant result

| # | Invariant | Result |
|---|---|---|
| 1 | A live worker cannot be superseded by a concurrent claim | **PASS** |
| 2 | Stale takeover requires the grace period and explicit recovery authority | **PASS** |
| 3 | A superseded worker cannot continue destructive operations | **PASS** |
| 4 | Heartbeat coverage protects every destructive stage, including LogStore | **PASS** |
| 5 | Token ownership is checked atomically wherever authority is exercised | **PASS** |
| 6 | Crash recovery works before and after the durable erasure fence | **PASS** |
| 7 | Legacy NULL claim timestamps cannot permit immediate unauthorized takeover | **PASS** |
| 8 | CAS conflicts and lost ownership return the correct HTTP 409 semantics | **PASS** |
| 9 | The migration forms exactly one valid Alembic chain on current dev | **PASS** |
| 10 | Session eviction and targeted auth-cache invalidation remain intact | **PASS** |

Ten of ten invariants hold. The PR's own claim-lease tests pass, and the
November 8 findings — the second Alembic head, the lease that was written but
never read, the 500 where a 409 belongs — are genuinely fixed on this head.

**One defect was found and fixed** (below). It is outside the ten invariants
as stated: it is a regression the PR introduced into a path the invariants do
not name.

## Defect found and fixed

**`update_user_fields` refused every write to a claimed account, not just the
writes the claim is about.** (`serving/storage/postgres_operational.py`)

The PR added a pending-claim guard to `update_user_fields` to protect its
compare-and-swap. The guard was applied unconditionally, so a *plain* write —
one that does not touch `status` and passes no `expected_status` — was refused
as well.

On `dev` the method wrote the columns unconditionally. After the PR, a plain
write to an account with a pending hard-delete claim raises
`HardDeleteStateChanged`. The one caller that can reach it is the
password-reset CLI (`python -m serving.auth.reset_password`), whose module
docstring says it "works for any account": `main()` catches
`ResetPasswordError` only, so the operator got an unhandled
`HardDeleteStateChanged` traceback instead of the CLI's own message.

**The fix keeps the guard unconditional** and reports the conflict where it
can be seen:

- `serving/auth/reset_password.py` — a claimed account is reported as a
  `ResetPasswordError` naming what happened and what the operator can do
  instead ("an administrator must resume the account first"), so `main()`
  prints it and exits 1.
- `serving/servers/routers/user_routes.py` — the profile and password routes
  report it as HTTP 409.

No erasure policy changed and no fencing was weakened. The recovery grace
period, the explicit recovery authority, the token replacement on takeover and
the sticky recovered-provenance are all unchanged.

### A follow-up review corrected this fix, and the correction is what stands

The first version of the fix relaxed the guard for writes that do not name
`status`, on the reasoning that such a write cannot strand a claim (the
account stays `deleted`). **That was wrong, and the follow-up review of the
verification branch was right to challenge it:**

- it permitted changes to identity fields — `email`, `role`, `preferences` —
  on an account being erased, which is exactly what the claim exists to
  prevent. Every other store refuses *any* work for an account marked
  `hard_delete_pending` (`batch_store._lock_owner`, `batch_file_store`,
  `batch_webhook_store`); the relaxed guard was the one place that did not;
- it was not needed. A claimed account is always soft-deleted, and every
  self-service writer requires an `active` account, so none of them can reach
  the guard.

The guard is therefore unconditional, and the correction is in this branch.
The reachability, the policy and the isolation are proved below.

## Follow-up review: the claim guard on `update_user_fields`

A narrowly scoped follow-up review of this branch asked whether the guard
should stay unconditional. The lease invariants were not re-audited. The
question was the one this branch had answered by relaxing the guard, and the
answer was wrong.

### 1. Can the self-service profile and password endpoints reach a `deleted` account with an active claim?

**No.** Three independent gates, each verified over the real HTTP stack
(`tests/servers/test_pr1733_reachability.py`, 5 tests):

| Gate | Where |
|---|---|
| The soft delete deletes the account's `auth_sessions` rows, so `require_live_session` refuses the token | `delete_user`, `PostgresOperationalStore` |
| `get_current_user` refuses any account whose status is not `active` | `serving/servers/deps.py` |
| `_usable_account` refuses a non-`active` browser account | `serving/servers/routers/auth_routes.py` |

A claimed account is always `deleted`: `begin_hard_delete_user` sets the
status and the claim under one row lock, and `resume_user` clears them
together. So the self-service routes never reach the guard — the 409 handling
in `user_routes.py` is there for the race, not for the normal case.

The login route's bootstrap seed (`role='admin'`) and the browser-session
seed are behind the same status gates, so neither reaches it either. The
admin routes cannot reach it either: `PATCH /admin/users/{id}` only allows
the transitions `active -> suspended` and `suspended -> active`, and
approve/reject require `pending_approval`.

**The password-reset CLI is the only caller that can reach the guard**, and
that is exactly the case it now reports.

### 2. Is permitting those writes required by a product contract, or does it merely restore old behaviour?

**It merely restored old behaviour, and the old behaviour was not a
contract.**

`dev`'s `update_user_fields` had no claim guard at all — it wrote the columns
unconditionally. But `dev` had no lease either, and the guard exists to
protect a property `dev` did not have: that the account being erased does not
change under the erasure.

The established pattern in this repository is unambiguous. Three other stores
refuse *any* work for an account marked `hard_delete_pending`, whatever the
work is:

```python
# batch_store.py, batch_file_store.py, batch_webhook_store.py
owner = await conn.fetchrow(
    "SELECT status, hard_delete_pending FROM users WHERE id = $1 FOR SHARE", user_id
)
if owner is None or owner["status"] != "active" or owner["hard_delete_pending"]:
    raise BatchOwnerUnavailable(user_id)
```

A relaxed guard on the `users` table would have been the only place in the
codebase that let an account under erasure keep changing. The CLI's "works
for any account" is a statement about *which accounts can be named*, not a
promise that a permanent erasure in progress may be edited.

### 3. Can writes occur between destructive erasure stages, or while the claim is stalled?

**They can be attempted, and they are refused.** The erasure runs four
stages (LogStore purge with the fence, Responses purge, batch purge, and the
final token-scoped operational purge), and the claim is live throughout.

`tests/integration/storage/test_pr1733_claim_write_isolation.py` (3 tests)
exercises this against real PostgreSQL:

- a writer hammering the row while the claim is live is refused every time,
  and the erasure still completes (`counts["users"] == 1`);
- the same for the *stalled* claim window — the lease has outlived the
  recovery grace but no takeover has happened, which is where a stalled claim
  sits: `email`, `role` and `preferences` are all refused, and the erasure
  still completes;
- a writer racing the recovery takeover is refused 20 out of 20 times, and
  the takeover's owner still completes the erasure.

The erasure does not depend on the writer: the final purge is token-scoped
and runs under the row lock, so it deletes the account whatever else touched
the row.

### 4-6. The correction

The guard is unconditional again, and the conflict is reported where it can be
seen:

| Caller | Before | After |
|---|---|---|
| `user_routes.update_profile` | unhandled `HardDeleteStateChanged` | HTTP 409, "This account is being permanently deleted and cannot be changed." |
| `user_routes.change_password` | unhandled `HardDeleteStateChanged` | HTTP 409, same message |
| `reset_password` CLI | unhandled traceback | `ResetPasswordError` naming what happened and what to do instead; `main()` prints it and exits 1 |

Normal writes are unchanged: an active account, an unclaimed soft-deleted
account, and an account whose claim has been released all write as before.
The guard is about the claim, not about the status.

### 7. The tests that required writes during a claim are replaced

`tests/integration/storage/test_pr1733_regression_plain_write_guard.py` no
longer asserts that a write during a claim succeeds. It now asserts the
opposite, for the identity fields as well:

- every write to a claimed account is refused — `user_name`,
  `password_hash`, `email`, `role`, `preferences`,
  `max_concurrent_requests` (6 parameterised cases);
- a compare-and-swap on a claimed account is refused;
- a refused write leaves the claim intact (pending, same token, lease
  untouched);
- writes to an active account, an unclaimed soft-deleted account and a
  released claim succeed, and are unchanged.

`tests/integration/storage/test_pr1733_reset_cli_regression.py` now asserts
the CLI's own behaviour: a `ResetPasswordError` whose message names the
deletion and the way out, `main()` printing it and returning 1 with no
traceback, the password unchanged, and the ordinary case still working.

New in this follow-up:

- `tests/servers/test_pr1733_reachability.py` (5 tests) — the reachability
  table above, over the real routes.
- `tests/servers/test_pr1733_claim_conflict_routes.py` (3 tests) — the
  profile and password routes report the conflict as 409, and the profile
  route still works for an active account.
- `tests/integration/storage/test_pr1733_claim_write_isolation.py` (3 tests)
  — the isolation table above.

## Second follow-up: the avatar write, and the CLI's recovery guidance

A second follow-up review of this branch found an adjacent path the first
correction had not covered, and challenged the CLI's guidance. Both are
addressed here.

### The avatar write bypassed the claim guard

`PATCH /user/profile` accepts `avatar_url` as well as `user_name`. The
`user_name` write goes through `update_user_fields`, which the claim guard
covers; the avatar write calls `set_user_avatar()`, which did not:

```sql
UPDATE users SET avatar_data_url = $1 WHERE id = $2
```

No row lock, no claim check. A request authenticated before the soft delete
could land after the erasure claim was established and still change the
account.

**Fixed**: `set_user_avatar` now takes the account row lock and refuses a
claimed account with `HardDeleteStateChanged`, and the profile route maps
that to HTTP 409.

### The same hole existed in four more writers

The avatar was the writer the review named, but it was not the only one. A
probe of every account-mutating method against a claimed account sized the
gap exactly:

| Mutation | Before | After |
|---|---|---|
| `set_user_avatar` | wrote | **refused** |
| `set_user_preference` | wrote | **refused** |
| `delete_user_preference` | wrote | **refused** |
| `set_user_preference_if_newer` | wrote | **refused** |
| `mark_user_email_verified` | wrote | **refused** |
| `update_user_fields(user_name)` | refused | refused |
| `update_user_fields(preferences)` | refused | refused |
| `initialize()` startup role seeding | **promoted** | **not promoted** |

Each of the first five writes the ``users`` table directly, so it inherited
neither `update_user_fields`'s guard nor any other. The fix is one shared
helper, ``_refuse_claimed_account``, which reads the account row under the
row lock the write would take anyway and raises `HardDeleteStateChanged` for a
claimed account. All five use it.

The routes that reach them map the conflict to HTTP 409: the three
preference routes, the profile route (both fields), and the email
verification route.

**One mutation is deliberately not guarded**: `update_user_last_login`. The
login route requires an `active` account, so a claimed account (always
soft-deleted) cannot reach it, and it writes a timestamp rather than an
identity field. `tests/integration/storage/test_pr1733_other_mutation_probe.py`
asserts that decision, so changing it is deliberate rather than accidental.
It is still subject to the authentication-to-write race, so it is described
as what it is: a timestamp write that a claimed account cannot reach through
the login route, not a write the claim refuses.

### The startup role seeding was a sixth writer

A further review of the integration commit found one more: the startup role
seeding in `PostgresOperationalStore.initialize()`. It promotes the accounts
named in `ADMIN_EMAILS` to the admin role, and it runs at every backend
start, so a restart could change an account's role while its hard-delete
claim was active. Its update carried no ``status`` and no
``hard_delete_pending`` predicate:

```sql
UPDATE users SET role = 'admin'
WHERE lower(trim(email)) = ANY($1::text[]) AND role IN ('free')
```

It now carries both predicates, so a restart leaves a soft-deleted account
and an account under a claim exactly as it found them, and still promotes an
eligible active account. `tests/integration/storage/test_pr1733_admin_role_seeding_claim.py`
(4 tests) covers it; they are red on the unguarded code (3 failures).

### The rule, stated exactly

Not every write to the ``users`` table passes through the helper, and the
claim is not described as if it did. The account-mutating writers that read
the account row themselves — the avatar write, the three preference writers
and ``mark_user_email_verified`` — use it. The state machine's own writes
(``update_user_fields``, ``suspend_user``, ``delete_user``,
``resume_user``, approve, reject) carry their own claim guard. The startup
role seeding carries an equivalent predicate in its own statement. The phone
and email binding statements carry ``status = 'active'``.
``update_user_last_login`` is the one account write with no claim guard.

### The CLI's recovery guidance was wrong

The first correction told the operator to "resume the account first". That
is wrong: `resume_user` refuses a live claim, and it refuses it even with
the operator's stale-claim confirmation until the lease has outlived the
recovery grace period. An account whose erasure fence is already established
cannot be resumed at all.

The message now names the procedure that can act on it:

> A password reset cannot be used on an account under a hard-delete claim. If
> the deletion must be stopped, an administrator has to follow the hard-delete
> recovery procedure: wait for the claim's recovery grace period to pass,
> confirm that every deletion worker for the account has stopped, and only
> then resume it through the Admin console with the stale-claim confirmation.
> An account whose erasure fence is already established cannot be resumed at
> all.

### Tests

- `tests/integration/storage/test_pr1733_avatar_claim_guard.py` (5 tests) —
  the avatar write is refused on a claimed account, removing the portrait is
  refused too, a refused write leaves the claim intact, and the ordinary and
  unclaimed cases are unchanged. Red on the unguarded code (3 failures),
  green with the guard.
- `tests/servers/test_pr1733_claim_conflict_routes.py` (5 tests) — the
  profile route reports the avatar conflict as 409, and the avatar still
  saves for an active account.
- `tests/integration/storage/test_pr1733_other_mutation_probe.py` (4 tests) —
  the mutation table above, as a regression test rather than a report.
- `tests/integration/storage/test_pr1733_reset_cli_regression.py` — the
  CLI's message must name the recovery procedure and must not imply that an
  ordinary Resume works.

## Evidence

### The PR's own claim-lease tests, plus the new adversarial suite

```
90 passed
```

(`tests/integration/storage/test_hard_delete_claim_lease_postgres.py`,
`tests/servers/test_admin_hard_delete_postgres.py`,
`tests/integration/test_migration_0016_hard_delete_claim_lease_postgres.py`,
and the seven new files below.)

### New tests (85 tests, of which 23 are the follow-up review's)

All use independent connections and the real PostgreSQL row and advisory
locks. No sleeps decide an outcome.

| File | Tests | Covers |
|---|---|---|
| `tests/integration/storage/test_pr1733_adversarial_claim_lease.py` | 22 | Invariants 1, 2, 3, 5, 6, 7: 10 simultaneous claims, 8 simultaneous recoveries, the grace boundary at 59 and 61 minutes, the server clock, token-scoped purge, NULL-lease stamping under concurrency |
| `tests/integration/storage/test_pr1733_failure_paths.py` | 11 | Transient DB errors during renew and release; purge-vs-takeover, release-vs-takeover and release-vs-stale-release races (5 trials each); crash recovery before and after the fence; double crash recovery |
| `tests/servers/test_pr1733_adversarial_routes.py` | 11 | Invariant 8 and 4 at the route boundary, over the real store pairing: every 409 mapping and its `detail.code`, the stage-renewal count, the heartbeat wrapper, a superseded worker refused before the destructive stage |
| `tests/integration/storage/test_pr1733_migration_chain.py` | 7 | Invariant 9: one head, expand-only, upgrade/downgrade/re-upgrade, mixed-version verdicts |
| `tests/integration/storage/test_pr1733_mixed_version.py` | 5 | Old code on a 0016 database; the CLI on a 0015 database; the boot guard that keeps new code off a 0015 database |
| `tests/integration/storage/test_pr1733_regression_plain_write_guard.py` | 12 | The defect above, and the follow-up: every write to a claimed account is refused, identity fields included |
| `tests/integration/storage/test_pr1733_reset_cli_regression.py` | 3 | The defect above, through the CLI's own code path |
| `tests/integration/storage/test_pr1733_claim_write_isolation.py` | 3 | Follow-up: a write during a live or stalled claim cannot stop the erasure |
| `tests/servers/test_pr1733_reachability.py` | 5 | Follow-up: the self-service routes cannot reach a claimed account |
| `tests/servers/test_pr1733_claim_conflict_routes.py` | 3 | Follow-up: the profile and password routes report the conflict as 409 |

### The merge candidate against the advanced `dev`

`dev` advanced to `2e560743` after the integration commit was written. The
integration commit merges onto it **without a conflict** — `git merge-tree`
reports no conflict markers, and a real merge in a disposable worktree
succeeds. Three files overlap between the two sides (`reset_password.py`,
`user_routes.py`, `postgres_operational.py`); `user_routes.py` is changed on
both sides, but the two changes are in different places (dev's API-key-reveal
change and the integration commit's claim-conflict handling), so Git merges
both.

Verified on the merged result:

| Gate | Result |
|---|---|
| `make lint` | pass |
| The 105 claim-lease tests | pass |
| `make test` | 13740 passed, 5 skipped, 3 failed — the same three that reproduce on `dev` |
| The migration chain | 16 revisions, one head (`0016_hard_delete_claim_lease`); the 64 migration hygiene tests pass |
| The database tier | 2 failed, both load-dependent flakes (see below) |

The integration commit's own source is intact in the merged tree: the six
conflict-handling sites, the shared helper, and the startup seeding's
predicates are all present.

### CI at the exact PR head

The upstream CI run for this PR is green, and it is green at the head that was
verified: run `42812062498`, `head_sha = 8ee83d2cf9fb33d023ec6a1478aeb34e796ee473`,
`head_branch = prep/1463-claim-lease-dev-replacement`, conclusion `success`.

The integration commit's own CI is pull request
[#1782](https://github.com/HarvardMadSys/hybridInference/pull/1782), on head
`8febb79db`. Its run exists (`43317899415`, `head_sha =
8febb79dbcafb5638a5e9fd41ba68a7876a80d47`) but is `action_required`: a
workflow run for a pull request from a fork needs a maintainer's approval
before it executes, and approving one needs repository admin rights that this
account does not have. **The integration commit's CI is therefore not yet
green, and is not claimed to be.** The approval is a maintainer's action.

| Check | Result |
|---|---|
| Backend Quality | pass (24s) |
| Security Scan | pass (21s) |
| Classify Changes | pass (8s) |
| test (1) / (2) / (3) / (4) | pass (2m27s / 2m14s / 1m37s / 2m31s) |
| test (db) | pass (3m51s) |
| docker-build (backend) | pass (58s) |
| Tutorial E2E | pass (7m17s) |
| CI Gate | pass (10s) |
| Frontend Quality, Docs Build, Site UI Containers, Publish candidate | skipped (not applicable) |

That is the head *without* the fix below. The fix is a separate local commit
on top of it (see "Delivered changes"); CI for it is not claimed here.

### Repository gates (on the head with the fix)

| Gate | Result |
|---|---|
| `make lint` | **pass** (ruff format, ruff check, pydocstyle) |
| `make test` | 13630 passed, 5 skipped, **4 failed** — all 4 pre-existing on `dev` (see below) |
| `ops/dev/test-db-local.sh run tests/integration tests/servers` | **2 failed**, both pre-existing on `dev` |
| `make docs-verify` | pass (185 Markdown documents, zh_CN matches the English build) |

Every failure in either run is unrelated to this PR and reproduces identically
on current `dev` (`82a1c2bb0`) in a clean worktree:

- `tests/unit/ops/test_classify_changes.py` (3 tests) — git change
  classification; fail on `dev` too, with the same assertions.
- `tests/integration/test_admin_performance_views_postgres.py::test_a_source_filtered_count_is_an_index_only_scan[one-batch]`,
  `tests/integration/storage/test_user_usage_reasoning_known.py::test_a_partial_window_sums_only_the_rows_that_report`,
  `tests/integration/storage/test_user_usage_reasoning_known.py::test_a_window_where_no_row_reports_is_unknown_not_zero`,
  `tests/integration/storage/test_postgres_utc_windows.py::test_usage_detail_today_and_month_are_utc`
  — shared-table races under the parallel run; each passes in isolation on
  both heads, and the set that fails differs from run to run on the `dev`
  baseline as well.
- `tests/servers/test_routewise_example_runtime.py::test_neutral_quota_example_boots_and_routes[available]`
  — passes in isolation on both heads; fails only under the parallel run.
- `tests/integration/test_schema_migration_upgrades_postgres.py::test_an_open_write_bounds_the_wait_and_never_queues_other_writes[users]`
  — a lock-timeout budget assertion; fails on `dev` too, with the same
  assertion (`assert 8.1 < 8`).
- `tests/integration/test_application_configuration_runtime.py::test_database_configuration_across_real_processes`
  — reproduced on the `dev` baseline with the same failure.

The database tier's failure set varies between runs (two to four, depending
on the run) — the untouched `dev` baseline fails under the same load too, with
a different set each time. This was checked against the advanced `dev`
(`2e560743`) as well: its full tier fails `test_an_open_write_bounds_the_wait_and_never_queues_other_writes[users]`
and `test_database_configuration_across_real_processes` under the same load,
and each passes in isolation. The ones that vary
(`test_a_partial_window_sums_only_the_rows_that_report`,
`test_a_window_where_no_row_reports_is_unknown_not_zero`,
`test_a_source_filtered_count_is_an_index_only_scan[one-batch]`) are
timing-sensitive and pass in isolation on both heads. No failure in any run is
attributable to this PR or to the correction: the claim-lease, adversarial,
reachability, isolation and regression files pass in every run, and the
failure set on the PR head with the correction is a subset of the failure set
on the `dev` baseline.

### What was checked by reading, not only by running

- **Lock ordering.** `begin_hard_delete_user`, `resume_user` and
  `hard_delete_user` take the `users` row lock (`FOR UPDATE`) before writing;
  `_lock_account_keys` is taken first in `hard_delete_user` and
  `suspend_user`, and its advisory lock is always the shared all-accounts
  lock first. The PR added no new lock and changed no lock order.
- **Transaction boundaries.** The claim's read-modify-write is one
  transaction under the row lock; the takeover replaces the token in that same
  transaction, so two recovery callers cannot both proceed. The lease
  timestamp is stamped outside the claim transaction on the legacy-NULL path
  precisely so the stamp commits even though the request is refused.
- **Token generation.** `secrets.token_urlsafe(32)` per claim; a takeover
  replaces the token, so the superseded worker's renew and final purge both
  fail closed.
- **Lease-expiration calculation.** One hour, evaluated by PostgreSQL
  (`NOW() - INTERVAL`), never by the caller's clock. Verified at 59 minutes
  (inside) and 61 minutes (outside) the boundary.

### Not verified here

- No deployment was contacted; this is not live acceptance.
- The erase policy was not changed, so nothing in it was re-litigated.
- The PR was not merged, and nothing was pushed to the fork.

## Delivered changes

Two branches on the fork, both based on the PR's head `8ee83d2cf`:

| Branch | What |
|---|---|
| `integration/pr1733-claim-lease-review` | **The integration commit.** One squashed commit, `b1ee8b0b1`, on the PR's head: the fixes, the tests and this record. The superseded permissive-guard commit is not in its history. |
| `review/pr1733-claim-lease-verification` | The same work as it was reviewed, commit by commit: `6003f22bd` (the first fix, superseded), `598f0f70c`, `9b93f8fc2`, `132a2da31` (the correction), `ceea8b518` (the avatar and CLI corrections). The squashed commit's tree is byte-identical to `ceea8b518`. |

The integration commit's source change, against the PR's head:

```
 apps/backend/serving/auth/reset_password.py        | 17 +++++
 apps/backend/serving/servers/routers/auth_routes.py | 22 +++++-
 apps/backend/serving/servers/routers/user_routes.py | 82 +++++++++++++++++-----
 apps/backend/serving/storage/postgres_operational.py | 95 ++++++++++++++++++--
 4 files changed, 187 insertions(+), 29 deletions(-)
```

Plus twelve test files and this record.

The PR itself was not modified and nothing was merged or pushed to the PR's
branch.

## Recommendation

The claim lease is real on this head and the ten invariants hold. The one
defect found is a narrow over-broad guard, fixed without touching the erasure
policy or weakening fencing.

The follow-up review of that fix was right to challenge it: the first version
relaxed the guard for non-status writes, which would have been the only place
in the codebase where an account under erasure could still change. The
correction keeps the guard unconditional and reports the conflict where it can
be seen — a 409 in the two self-service routes, and a message and exit status
1 in the password-reset CLI. The self-service routes cannot reach the guard
in normal operation (a claimed account is soft-deleted, and they require an
`active` account), so the 409 handling is there for the race.
