# Phase 2 deployment evidence template

This is an operator worksheet, not evidence. The repository snapshot in
`phase2-wave0.repo.json` is also not deployment evidence: it proves only what
can be reproduced from source.

Evidence is accepted only when a separate JSON document:

- validates against `phase2-deployment-evidence.schema.json`;
- uses `evidence_status: captured`;
- identifies the exact source-built local image ID (and uses `null` for
  `repository_digest` when that image was not pushed);
- names the deployment target/environment and selected runtime manifest,
  including all three selectors and captured startup comparison/hash logs;
- records both the repository DDL-source fingerprint and a fingerprint
  captured from the live database, plus the live server and version;
- identifies a verified backup;
- links passed dark-load; hydrated frontend, API, SSE, auth, quota, model, and
  routing smoke; dimensioned latency/error/cost metrics; and rollback results;
- contains no credentials, cookies, tokens, connection strings, or raw request
  bodies.

Values such as `pending`, `template`, `TODO`, `TBD`, `unknown`, or angle-bracket
placeholders are not observations and are rejected where evidence is required.
Setting a planned run to `passed` without its immutable report reference is
also not evidence.

## Worksheet

The intentionally invalid YAML below is a checklist. Replace every placeholder,
convert it to JSON, and validate the result. Do not attach this template itself
to a release.

```yaml
schema_version: 1
evidence_status: template # must become "captured" only after every run exists
deployment_target: <target-identifier>
environment: staging

source_build:
  build_kind: source-build
  source_revision: <40-or-64-character-source-revision>
  repo_baseline_sha256: <sha256:...>
  local_image_id: <sha256:...>
  repository_digest: null # keep null unless the exact image was pushed
  built_at: <RFC3339>
  build_log_reference: <run-or-artifact-reference>

selected_runtime_manifest:
  distribution_id: <distribution-id>
  schema_version: 2
  sha256: <sha256:...>
  selectors:
    models: shadow
    routing: shadow
    alerts: shadow
  startup_result: pending
  startup_comparison_reference: <artifact-reference>
  startup_hash_log_reference: <artifact-reference>

live_database:
  repo_ddl_source_fingerprint: <sha256:...>
  live_schema_fingerprint: <sha256:...>
  server: <database-server>
  server_version: <database-server-version>
  captured_at: <RFC3339>
  capture_reference: <artifact-reference>
  backup:
    backup_id: <immutable-backup-id>
    captured_at: <RFC3339>
    location_reference: <backup-reference>
    integrity_sha256: <sha256:...>
    restore_readiness_verified: false

dark_load:
  result: pending
  window_start: <RFC3339>
  window_end: <RFC3339>
  request_count: 0
  success_count: 0
  success_rate: 0
  required_success_rate: 0
  production_traffic_mutated: false
  report_reference: <run-or-artifact-reference>

smoke_tests:
  overall_result: pending
  frontend_html:
    result: pending
    spec_sha256: <sha256:...>
    route_count: 0
    passed_route_count: 0
    failure_count: 0
    report_reference: <run-or-artifact-reference>
  api:
    result: pending
    report_reference: <run-or-artifact-reference>
  sse:
    result: pending
    report_reference: <run-or-artifact-reference>
  auth:
    result: pending
    report_reference: <run-or-artifact-reference>
  quota:
    result: pending
    report_reference: <run-or-artifact-reference>
  model:
    result: pending
    report_reference: <run-or-artifact-reference>
  routing:
    result: pending
    report_reference: <run-or-artifact-reference>

metrics:
  result: pending
  window_start: <RFC3339>
  window_end: <RFC3339>
  request_count: 0
  critical_alert_count: 0
  bucket_dimensions: [model, provider, client]
  bucket_baselines:
    model:
      latency_report_reference: <artifact-reference>
      error_report_reference: <artifact-reference>
      cost_report_reference: <artifact-reference>
    provider:
      latency_report_reference: <artifact-reference>
      error_report_reference: <artifact-reference>
      cost_report_reference: <artifact-reference>
    client:
      latency_report_reference: <artifact-reference>
      error_report_reference: <artifact-reference>
      cost_report_reference: <artifact-reference>
  abort_thresholds:
    latency_p95_ms: 0
    error_rate: 0
    cost_usd: 0
  observed_maxima:
    latency_p95_ms: 0
    error_rate: 0
    cost_usd: 0
  dashboard_reference: <dashboard-reference>

rollback:
  result: pending
  mode: rehearsal
  target:
    known_good_source_revision: <40-or-64-character-source-revision>
    local_image_id: <sha256:...>
    record_reference: <immutable-known-good-record>
  execution_reference: <run-reference>
  duration_seconds: 0
  recovery_verified: false
  database_restore_required: false

attestation:
  operator: <operator>
  reviewer: <reviewer>
  recorded_at: <RFC3339>
```

## Required run notes

For the hydrated frontend smoke, use
`frontend-html-smoke.v1.json`. Public routes must return HTTP 200 and expose
their declared visible marker after hydration. Protected routes must end at
`/login` without a session, via an accepted client- or server-side redirect,
and then return HTTP 200 with the login marker. The checked specification and
its hash are only a gate; a live or source-build run report is still required.

For database evidence, fingerprint the live catalog rather than copying the
repository DDL fingerprint into both fields. Record the backup before dark load
or cutover, and retain the server/version, separate integrity value, and
restore-readiness check.

For metrics evidence, every baseline report must be bucketed by model, provider,
and client. Record latency, error, and cost artifacts for each dimension and the
abort thresholds agreed before the observation window; a single aggregate
dashboard is not enough.

For rollback evidence, record the exact target, the execution/rehearsal run,
known-good source revision and local image ID, measured duration, and the
post-rollback recovery check. A command printed in a runbook is a template, not
a completed rollback.
