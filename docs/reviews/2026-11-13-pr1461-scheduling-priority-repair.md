# PR #1461 scheduling-priority repair verification

The repair of [PR #1461](https://github.com/HarvardMadSys/hybridInference/pull/1461)
("propagate cold-prefill scheduling priority") was performed against the
maintainer reviews of 2026-11-05, 2026-11-06 and 2026-11-08, and re-verified
against the PR head `41085ca8` and current `dev` `f7142ef3`.

The PR head had advanced since the last review (commits `ae316b64`,
`fa1eb618` and the `41085ca8` dev merge landed on 2026-11-09 and 2026-11-10),
so every reported finding was re-checked against the code as it stood before
anything was changed. Three of the findings were already resolved at that
head; four were not.

All work was done in an isolated worktree. No PR was opened, nothing was
force-pushed, no branch was merged, and no deployment was changed.

## Findings already resolved at `41085ca8`

Verified by reading the code and by the existing regression tests, not
assumed from the review text:

- **The `AttributeError` crash (review finding 1).** The helper
  `_reserve_prefix_generation_for_dispatch` and its calls to
  `PendingPrefixCacheStore.scope_for` / `generation_for` / `set_generation`
  and `PrefixCache.reserve_generations` are gone. `TestPrefixCacheEnabledDispatch`
  covers a chat and a stream dispatch with the prefix cache enabled.
  `generation_for`, `set_generation` and `reserve_generations` now appear
  nowhere in the tree. The one surviving `scope_for` call is on
  `PrefixCacheCoordinator` (`prefix_cache.py`), a different class that owns
  that method, and it predates this PR.
- **The withdrawn prefill-load feature (review finding 2).** Not restored.
  `strategies/routewise.py` still lists `prefill_load_routing_enabled`,
  `prefill_load_scale_ms_per_1k` and `prefill_load_max_penalty_ms` as retired
  with no effect; `routewise/config.py` and `routewise/router.py` read none of
  them; dev's `TestWithdrawnPrefillLoadRouting` is kept. The LP and hedge
  choice in `routewise/lp.py`, `routewise/predictor.py` and
  `routewise/latency.py` are untouched.
- **The probe priority (review finding 6).** `_measure_probe_ttft_ms` stamps
  `PRIORITY_INTERACTIVE` on a priority-scheduling endpoint.
- **The backup lease leak (review finding 5).** The `except` around the
  `CheckpointBackupDispatch` construction releases `backup_lease` as well as
  the reservation.

## Defects repaired

Commits `c1fae836` and `602f5dc4` on the PR branch.

### 1. A failed hedge leg kept claiming a warm prefix

A hedged dispatch reports every leg outcome through `HedgedAdapter`, so
`RouteWiseRouter._on_failure` — the path that forgets an endpoint's
warm-prefix hints — never runs for it. `HedgedAdapter._invalidate_endpoint`
called an `endpoint_failure_hook` that no caller supplied, so no hint was
dropped for a failed leg.

The hint describes a prefix resident in the endpoint's radix cache. An
endpoint that restarted, was OOM-killed, or was replaced under the same id
lost that cache, and the hint survived it for up to the 20-minute TTL. The
dispatch priority is derived from that hint, so the next cold mega-prefill
was stamped interactive and preempted the interactive traffic the cold work
was supposed to yield to.

The router now hands the hedge legs the same forgetting hook its own failure
path uses, and classifies the failure the same way: an endpoint that answered
with a 4xx is up and still holds what it held, so its hints stay.

### 2. A streaming hedge backup that won never had its prefill confirmed

`chat_completion` confirmed the winning backup's prefill
(`prefill_confirmed=True`); `stream_chat_completion` only released the lease.
A plain release returns the load charge but writes no warm-prefix hint, so
the endpoint looked cold and the next turn on it was charged for prefill work
it had already done. The stream path now confirms the backup's prefill when
the backup wins the race.

### 3. Every RouteWise prefill lease ignored the route's context window

`FixedRouter._acquire_lease` passes `context_window` to
`PrefillLoadTracker.acquire`; the RouteWise lease did not. The tracker caps
a charge at the window because an engine refuses a prompt longer than its
window rather than prefilling it — so an uncapped charge described work no
engine would do, and the endpoint was over-charged for a prompt it would
refuse. The lease now takes the route's window at all four acquisition
sites.

### 4. `backup_dispatch_hook` was dead wiring

Added by #1417, withdrawn with #1663, re-added by this PR — and supplied by
no caller and asserted by no test. Removed.

## The prefill tracker is per-router, not shared

The previous PR description claimed bootstrap passes `FixedRouter`'s
`PrefillLoadTracker` into RouteWise through `RouterBuildDependencies`. It
does not. `RouterBuildDependencies` carries only `health_registry`;
`build_router` passes only `health_registry` and `params` to the router
class, so `RouteWiseRouter` constructs its own tracker
(`routewise/router.py:699`) while `FixedRouter` keeps its own
(`routers.py:1057`).

The claim was corrected in the PR description rather than the code changed.
Making the tracker shared would couple the two routers' selection and
admission accounting, which #1267 separated deliberately, so it is a
maintainer decision and wider than this PR.

RouteWise's lease does not use `acquire(dispatched=False)` plus
`mark_dispatched`, because that protocol exists to keep a request *waiting in
the gateway* from counting against another caller's affinity ceiling, and
`should_keep_affinity` — the only reader of the dispatched-only accounting —
is consulted by FixedRouter's selection, not RouteWise's. RouteWise's
tracker is private to it, so no other caller's ceiling can be moved by its
undispatched leases.

## Regression tests

Seven tests, each red on the unmodified PR head and green with the fix. Every
source change was reverted to the head version and the corresponding test
re-run to prove the red.

| Test | Pins |
|---|---|
| `test_a_failed_hedge_leg_forgets_the_endpoints_prefix_hints[chat]` | defect 1, chat |
| `test_a_failed_hedge_leg_forgets_the_endpoints_prefix_hints[stream]` | defect 1, stream |
| `test_a_client_error_keeps_the_endpoints_prefix_hints` | negative control for defect 1: a 4xx keeps the hints |
| `test_a_streaming_backup_winner_confirms_its_prefill` | defect 2 |
| `test_a_chat_backup_winner_confirms_its_prefill` | the control for defect 2: chat already did this |
| `test_a_routewise_lease_is_capped_by_the_context_window` | defect 3, unit |
| `test_a_dispatch_charges_no_more_than_the_route_window` | defect 3, end to end through a real dispatch |

## Validation

Run in an isolated worktree at `/tmp/claude/worktree/1461-repair` on the
delivered commit `602f5dc4`.

- `tests/unit/routing` on `dev` + this branch: **1764 passed, 2 skipped**
  (the 2 skips are the performance tests, disabled by default, and one
  pre-existing skip in `test_prefill_load.py`).
- The same suite on the PR head alone, before the repair: 1756 passed. The
  difference is the 7 new tests and the 1 removed dead hook.
- `dev` + the branch merged: 1764 passed, 2 skipped — the required
  current-upstream integration check.
- Ruff format check (1180 files), Ruff lint and pydocstyle: clean.
- Broader suites on the same tree: `tests/unit/adapters` 1285 passed,
  `tests/unit/servers` 913 passed, `tests/unit/test_*.py` 1133 passed,
  `tests/unit/observability` 684 passed, `tests/unit/config` 651 passed,
  `tests/unit/serving` 1426 passed, `tests/unit/storage` 736 passed,
  `tests/unit/ops` 391 passed, `tests/unit/rag` 149 passed,
  `tests/unit/middleware` 242 passed, `tests/unit/deploy` 163 passed,
  `tests/unit/analytics` 121 passed, `tests/unit/repo` 73 passed,
  `tests/unit/integrations` 37 passed, `tests/unit/auth` 11 passed,
  `tests/servers` 307 passed, `tests/api` 94 passed.

The failures and errors that remain were each reproduced on **unmodified
`dev`** and are unrelated to routing:

- `tests/unit/ops/test_classify_changes.py` — 3 failures, identical on `dev`.
- `tests/unit/serving/test_batch_review_webhooks.py` — 1 failure, identical on `dev`.
- `tests/unit/storage/test_bounded_log.py` — flaky on `dev`: failed once and
  passed on two subsequent runs of the same test.
- `tests/unit/storage/test_login_events.py` (7) and
  `tests/servers/test_account_id_backfill.py` (10) — errors on `dev` too.
  These are the `dbtest` tier, which needs a PostgreSQL this host did not
  have; `make test-db-local` was not run.

No test in the failing set exercises routing, the prefill tracker, or
hedging.

## Scope checks

- Weighted routing, RouteWise learning, the circuit breakers and the adapters
  are untouched: `routewise/lp.py`, `routewise/predictor.py`,
  `routewise/latency.py`, `routewise/candidates.py`,
  `routewise/effective_cost.py`, `endpoint_health.py` and
  `apps/backend/serving/adapters/` have no diff against `dev`.
- A client still cannot raise its own priority: `priority` is not in
  `validate_params`' whitelist, and that function is untouched.
- The delivered diff against the PR's own head `ef4e4d5c` is four files: two
  source files (65 insertions, 11 deletions) and two test files (339
  insertions).

## Mergeability and CI

- Delivered commit: `602f5dc4886201fa35e55ec8adfc5a427d37adf8`, pushed to the
  PR branch as a fast-forward (`ef4e4d5c..c1fae836`, then `c1fae836..602f5dc4`).
  No force-push; the review history is intact.
- The branch is 1 commit behind `dev` and merges into it cleanly
  (`git merge --no-commit --no-ff origin/dev`: "Automatic merge went well").
- GitHub reports the PR as `MERGEABLE`.
- The CI run for the delivered commit is `action_required`, not a failure: it
  is the approval a maintainer must give before the workflow runs on a fork
  push. It has not run yet, so **CI has not evaluated this candidate**.
- The PR still carries `CHANGES_REQUESTED` from the 2026-11-06 review. The
  four defects above are the items that review blocked on; the re-review is
  a maintainer decision.

## Remaining maintainer decisions

1. **Whether the shared tracker should be made shared.** The description now
   states the per-router fact; making it shared is a design change beyond
   this PR's scope.
2. **Whether `acquire(dispatched=False)` + `mark_dispatched` should be used
   for RouteWise's lease** if the tracker is ever shared. It has no effect
   while the tracker is private, because `should_keep_affinity` is
   FixedRouter's.
3. **Re-review of the four repaired defects**, and of the corrected
   description.
