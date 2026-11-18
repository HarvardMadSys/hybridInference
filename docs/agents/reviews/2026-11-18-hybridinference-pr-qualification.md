# HybridInference PR qualification evidence — 2026-11-18

This record qualifies the retained candidate-only admission and index-scan observations. It supplements the earlier run receipts and failure matrix; it does not replace the original logs. The raw log from the original eight-failure run was not retained, so the eight-row matrix remains a reconstruction from the earlier recorded report. No result below is described as an exact-head GitHub CI pass.

## Source identities

| Subject | Commit | Tree | State |
|---|---|---|---|
| Combined candidate | #1782 `c1d80bb0c17489e2039e8aec18a44f6c3f3b8752` + #1781 `6db28b1ec9dbb1becae5e625807e2b7c995615b3` + #1786 `32d4f1b4a37b66d793070f5dcf1c13bfc311ab5b` | `3154b2840b7154e8eb60b505b4d24ed8e261c1ca` | Integration tree constructed from the three exact heads; files under `candidate-tree` were staged, with base commit `91c73f0c2f30ea2bb88b0f4195963b061db1441b` as HEAD.
| Clean baseline | `91c73f0c2f30ea2bb88b0f4195963b061db1441b` | `fcd1deac12a1da8006fbd9cb14126656cd4e4efb` | Clean `dev` snapshot used for paired local runs.

Relevant admission implementation and test files, and the admin performance query/index helper and test, are byte-identical between the combined candidate and clean `dev`. Candidate migrations add hard-delete claim columns, a mergepoint, and alert-history indexes; they do not alter shared-spend tables, `api_logs`, or its source index. #1786 has no migration.

## Observation A — shared-spend admission timeout

**Exact node:** `tests/integration/storage/test_shared_spend_scopes_postgres.py::test_an_exhausted_request_pool_refuses_in_bounded_time_and_writes_nothing`.

The original retained occurrence is `candidate-broad-db.log` (worker `gw4`). The expected bounded rejection while both request-pool connections are deliberately held succeeds. After both are released, the test checks for no writes and retries admission; that final admission exceeded its 0.5-second total budget and raised `SpendAdmissionUnavailable`. The timeout wraps the local lock, request-pool acquisition, and SQL operation, so the original traceback cannot identify which stage consumed the budget.

The instrumented matched eight-worker, `loadfile` targeted run passed the exact node on both trees. The deliberate exhausted-pool call took about 0.50s and returned its expected pool-timeout error. The subsequent successful call acquired a connection in 0.0008s, completed the SQL admission in about 0.10s, and returned in about 0.11–0.12s on both candidate and clean `dev`. See `trace-candidate.txt` and `trace-dev.txt`; preflight records capture each tree, interpreter/import paths, plugin, command, workers, PostgreSQL version/configuration, and isolated DB namespace.

**Classification:** unresolved original observation; no PR regression is established. It did not reproduce under equivalent targeted parallel conditions, and the directly relevant code/test are unchanged from clean `dev`. The previous broad-run pass/fail variation does not establish flakiness or identify a cause. No timeout was increased and no implementation change was made.

## Observation B — index-only visibility assertion

**Exact node:** `tests/integration/test_admin_performance_views_postgres.py::test_a_source_filtered_count_is_an_index_only_scan[one-batch]` (original retained broad-run occurrence, at the visibility assertion before EXPLAIN). The parameterized variants are `interactive`, `batch`, and `one-batch`.

The test inserts 2,000 wide rows, ensures `idx_api_logs_source ON api_logs (timestamp DESC, batch_job_id)`, polls for WAL flush for at most five seconds, runs `VACUUM ANALYZE api_logs`, and asserts `pg_class.relallvisible = relpages`. The loop does not assert that the visibility-map precondition was achieved. The traced candidate run observed this assertion false for `interactive`: WAL flush succeeded after four checks (~0.067s), but `relpages=400`, `relallvisible=0`, `reltuples=2000`. The default plan used a sequential scan; with seq and bitmap scans disabled, PostgreSQL used a regular timestamp index scan, not an index-only scan, because no page was all-visible.

The matched clean-`dev` traced run independently failed the same assertion on the `batch` parameter: WAL flush succeeded after eight checks (~0.184s), with the same 400 pages and zero all-visible pages. Its default plan was a sequential scan, and its forced plan was a regular `idx_api_logs_timestamp` index scan with heap buffers. Other parameter variants passed. Independent fresh-schema probes on both trees showed 400/400 pages all-visible after vacuum, and `Index Only Scan using idx_api_logs_source` with zero heap fetches for all three predicates. Thus the index/query supports an index-only plan when the visibility-map condition is actually true.

**Classification:** environmental/test-fixture issue reproduced on clean `dev`, not a candidate-specific regression. The exact reason PostgreSQL left the visibility map unset in a parallel-load run despite flushed WAL is not established. The assertion is sensitive to a planner/storage precondition the test does not verify reliably under load. A narrow future test improvement should make the visibility precondition deterministic or explicitly verify it before asserting the plan; it must retain the intended no-heap/index-only contract and must not pass merely by suppressing the assertion. No correction was made in these PRs.

## Broad and targeted run results

The original broad `make test-db-local` command was run twice on the combined tree and twice on clean `dev`, sequentially with PostgreSQL 15.19, Python 3.13.13, pytest 9.0.3, eight xdist workers (`--dist loadfile`), and isolated checkout DB namespaces. Candidate: 1,401 passed / 1 skipped / 3 failed, then 1,402 / 1 / 2; clean `dev`: 1,274 / 1 / 5, then 1,275 / 1 / 4. Every make run exited 2. Existing complete logs, preflights, and receipts are retained in this evidence directory. Those suites contain many different tests because the candidate includes additional PR tests; they are not pairwise-identical collections.

The paired focused five-file candidate run had 4 failed / 85 passed; its rerun with tracing had 2 failed / 87 passed. The matching clean-dev targeted run without tracing had 2 failed / 87 passed; the instrumented clean-dev run had 2 failed / 87 passed. Real-process DB-readiness timed out in both traced parallel runs. Other migration timing failures varied by tree/run. All logs record exact worker output and failures; do not treat serial-only passes as resolving parallel timing bounds.

The historical three #1782 helper regressions were confirmed and corrected: the synthetic commit fixtures now use the existing `legacy_org_id` instead of nonexistent `personal_org_id`; the corrected tests pass. The original eight-failure matrix is reconstructed because its original raw log was not retained. No currently unresolved failure has been established as a PR regression. Two candidate-only observations were recorded; the index assertion is now reproduced on clean `dev`, while the admission timeout remains unexplained and unreproduced in paired targeted runs.

## Environment and reproducibility

The original broad runs used `make test-db-local` with no path filter. The targeted run command, with diagnostic tracing enabled for the paired run, was:

```sh
PYTEST_PLUGINS=qualification_trace_plugin \
PYTHONPATH=<evidence-dir>:<checkout>/apps/backend:<checkout> \
QUAL_TRACE_LOG=<evidence-dir>/trace-<subject>.txt \
UV_PROJECT_ENVIRONMENT=<subject-venv> \
TEST_DB_LOCAL_NAME=hi_trace_<subject>_test_db \
make test-db-local ARGS='-vv tests/integration/storage/test_shared_spend_scopes_postgres.py tests/integration/storage/test_shared_spend_postgres.py tests/integration/test_schema_migration_upgrades_postgres.py tests/integration/test_admin_performance_views_postgres.py tests/integration/test_application_configuration_runtime.py'
```

Candidate and baseline used separate venvs, checkout/import paths, fresh database namespaces and worker databases, and ran sequentially. The local test PostgreSQL server was 15.19 with `fsync=off`, `synchronous_commit=off`, `full_page_writes=off`, `shared_buffers=512MB`, `max_connections=500`, and autovacuum enabled. The trace plugin was external to both source trees and only observed pool/SQL timing, WAL state, visibility stats, and query plans.

The index plan probe used the same locked project environment and PostgreSQL settings, a fresh schema for each source predicate, and captured `EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS)`. Complete command details and imported module paths are in each `.preflight.txt` file. Logs and preflights are sanitized copies: machine checkout prefixes are replaced with `$QUAL_ROOT` / `$SOURCE_REPO`, and any recognized credential fields are redacted. Original local logs remain unmodified.

## PR qualification matrix

| PR | Exact head | Local qualification | Candidate-only observation classification | Exact-head GitHub CI | Formal review | Maintainer decisions / merge condition |
|---|---|---|---|---|---|---|
| #1782 — Claim leases | `c1d80bb0c17489e2039e8aec18a44f6c3f3b8752` | The PR description reports exact-head `make check`: lint passed, 14,373 passed, 5 skipped, 6 failed. It attributes three synthetic-commit failures to this host's Git Identity Guard, notes two migration-hygiene failures fixed on this head, and one stream test that passed serially on both trees. Focused migration hygiene, workflow policy, and claim/migration/mixed-version PostgreSQL results are also recorded in the PR history; these exact-head checks were not rerun for this investigation. Full combined DB suites are independently retained above. | Admission timeout remains unresolved and was not reproduced in matched targeted candidate/dev traces. Historical three #1782 helper regressions were confirmed and corrected; their tests pass. | [Run 5900](https://github.com/HarvardMadSys/hybridInference/actions/runs/44169626341) is `action_required`, zero jobs; run 5897 is an earlier head. | Latest recorded formal state is `COMMENTED`; approval outstanding. | Confirm replacement of #1733 and migration ownership. Merge before #1781. |
| #1781 — Alert recovery | `6db28b1ec9dbb1becae5e625807e2b7c995615b3` | The PR description reports exact-head `make check`: lint passed, 14,408 passed, 5 skipped, 5 failed. It attributes three synthetic-commit failures to this host's Git Identity Guard; these exact-head checks were not rerun for this investigation. Focused lint, alert unit (135), alert-history PostgreSQL (19), and migration-hygiene (72) results are recorded in the PR history. Full combined DB suites are independently retained above. | Index-only visibility assertion reproduced on clean dev under the matched parallel target; test-fixture/environment state, not a verified PR regression. Root cause of vacuum visibility-map state under load remains unknown. | [Run 5898](https://github.com/HarvardMadSys/hybridInference/actions/runs/44138393056) is `action_required`, zero jobs. | Latest recorded formal state is `COMMENTED`; approval outstanding. | Decide best-effort startup restoration and record-only notification fallback. Merge after #1782; adds migration 0023. |
| #1786 — Served identity | `32d4f1b4a37b66d793070f5dcf1c13bfc311ab5b` | The PR description reports exact-head `make check`: lint passed, 14,495 passed, 5 skipped, 4 failed. It attributes three synthetic Git failures to this host's Git Identity Guard and records that the routewise example passed serially on both trees; these exact-head checks were not rerun for this investigation. Focused served-identity and PostgreSQL entitlement results are recorded in the PR history. Full combined DB suites are independently retained above. | Same shared combined-tree index observation as #1781; reproduced on clean dev under matched load, with unchanged query/test source. | [Run 5896](https://github.com/HarvardMadSys/hybridInference/actions/runs/44129629250) is `action_required`, zero jobs. | Latest recorded formal state is `COMMENTED`; approval outstanding. | Decide eligible keys, privileged-role requirement, and `X-Probe` endpoint disclosure. No migration dependency. |

The three PR heads remain unchanged. No PR was merged or closed. The release sequence is #1782 then #1781; #1786 is independent.

## Current GitHub qualification state

The upstream repository denied Git blob creation with HTTP 403 (`Resource not accessible by integration`), and configured `gh` authentication failed. The owner fork accepted an evidence-only branch based on the verified `dev` commit. The report and sanitized log archive are published there under `docs/agents/reviews/`; the archive is stored as base64 text with a separate SHA-256 file. Download the `.tar.gz.base64`, decode it with `base64 -d`, and verify the resulting archive against the adjacent `.tar.gz.sha256` before extraction. The three PR descriptions contain direct links to the report and archive. The evidence branch is not merged.

Maintainer decisions remain outstanding:

- #1782: confirm it replaces #1733 and confirm migration ownership/order.
- #1781: decide whether startup restoration is best-effort and whether record-only alerts reach the notification channel when history persistence fails.
- #1786: decide which API keys may receive `served_identity`, whether a privileged role is required, and whether `X-Probe` may disclose dedicated endpoint identifiers.

Migration sequence remains #1782 (`0016_hard_delete_claim_lease`, `0022_hard_delete_merge`) before #1781 (`0023_alert_preload_indexes`). #1786 is independent. No PR was merged or closed.

## Evidence integrity

`SHA256SUMS` contains a checksum for every sanitized, published evidence file except itself. Reconstructed information is identified above and in `failure-matrix.md`; no original eight-failure raw log is claimed. Repository-relative evidence is published for stable maintainer review, while the full original, unsanitized evidence remains local.
