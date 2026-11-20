# Draft issue: parallel `VACUUM ANALYZE` leaves `api_logs` visibility map unset in index-only-scan test

**Status:** prepared for maintainer filing; this is separate from PRs #1781, #1782, and #1786. It records a reproducible clean-`dev` test/environment observation, not a PR regression.

## Test and expected contract

Failing node: `tests/integration/test_admin_performance_views_postgres.py::test_a_source_filtered_count_is_an_index_only_scan[batch]` on clean `dev` `91c73f0c2f30ea2bb88b0f4195963b061db1441b`, tree `fcd1deac12a1da8006fbd9cb14126656cd4e4efb`.

The parameterized test inserts 2,000 wide `api_logs` rows, ensures `idx_api_logs_source ON api_logs (timestamp DESC, batch_job_id)`, waits up to five seconds for WAL flush, executes `VACUUM ANALYZE api_logs`, and asserts `pg_class.relallvisible = relpages` before checking for an index-only count. The test does not assert that its WAL-flush polling loop reached the target. In the clean-dev failure recorded below, the trace shows the WAL flush did reach the target; visibility-map state still remained unset.

## Matched clean-dev reproduction

The targeted run used the same five-file command as the combined candidate, eight pytest-xdist workers with `--dist loadfile`, PostgreSQL 15.19, isolated per-worker test databases, and a separate clean-dev venv. The PostgreSQL service was shared across worker databases. The exact command and environment are in `trace-dev.preflight.txt`; the full output and targeted instrumentation are in `trace-dev.log` and `trace-dev.txt` in [evidence PR #1828](https://github.com/HarvardMadSys/hybridInference/pull/1828).

On the failing `[batch]` case:

- WAL flush was reached after 8 checks (~0.184 seconds).
- Table stats immediately after `VACUUM ANALYZE`: `relpages=400`, `relallvisible=0`, `reltuples=2000`, table size 3,276,800 bytes, zero dead tuples.
- The normal count plan was a sequential scan with 400 shared blocks hit (about 1.9 ms in this run).
- With `enable_seqscan=off` and `enable_bitmapscan=off`, PostgreSQL used a regular `idx_api_logs_timestamp` index scan, not an index-only scan; it touched 407 shared blocks (about 1.9 ms).

A matched candidate trace failed the same visibility assertion on `[interactive]`, also with WAL flush reached and `relpages=400`, `relallvisible=0`. The query and index helper are unchanged from clean `dev`; the three PR migrations do not change `api_logs` or this index.

## Control probes

Independent fresh-schema probes on both candidate and clean `dev`, with the same PostgreSQL settings, showed 400/400 pages all-visible after vacuum. For `interactive`, `batch`, and `one-batch`, `EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS)` used `Index Only Scan using idx_api_logs_source` with zero heap fetches. Probe output and preflights are in #1828's sanitized archive.

## Assessment and possible test correction

This is a baseline-reproduced parallel-load test/visibility-state issue, not evidence of a candidate query-planning regression. The exact reason vacuum leaves the visibility map unset in the failing run despite flushed WAL is not established.

A narrow test correction should make the fixture precondition explicit and diagnosable: assert the WAL-flush loop reached its LSN; after vacuum, verify the visibility-map condition with bounded diagnostic retries or fail setup with the observed relation stats; then retain the expected `Index Only Scan` and zero-heap-fetch assertions. Do not skip the plan assertion or accept a heap-reading plan merely to make the test pass.

## Evidence provenance

The original eight-failure broad-run raw log was not retained. The matched targeted clean-dev log and trace are retained and checksummed in the sanitized bundle linked from #1828. This draft issue's reproduction above comes from that later retained targeted run.
