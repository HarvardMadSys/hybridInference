# Alert history

The gateway keeps a structured history of what its alerting did: each breach it observed, each recovery, each decision not to notify (cooldown, mute, snooze, no channel, a policy switch), each delivery attempt and its result, and the admin console's test notifications. People and agents read it through a scoped, read-only API.

Alerting has two independent paths. The gateway's own alerts go through `alert_slack()` to one Slack or WeCom webhook, and the gateway records them as they happen. A status monitor and its alert control plane, deployed outside this repository, deliver their own alerts and can submit what they did to the [ingestion endpoint](#ingestion-api), which only records it.

History is on whenever the gateway has a database. It never changes what is sent: thresholds, cooldowns, mutes, snoozes, retries, routing and the `bool` that `alert_slack()` and `AlertSink.post()` return are the same with or without it.

## What "kept" means

- Only a committed PostgreSQL row is saved. Capture puts each fact in a bounded in-memory buffer without waiting, and a background writer commits it.
- History is best effort. A crash, a shutdown that runs out of time, or a buffer that fills during a long database outage loses uncommitted facts. Refused facts are counted and later written as a `history_gap` record, which can be lost too. Every read answer therefore carries `completeness: best_effort`, `unknown_gaps_possible: true` and `late_arrivals_possible: true`, and an empty gap list never proves completeness.
- History starts the first time the gateway runs with a database and capture on; earlier channel messages are not imported.
- Nothing deletes history on a timer. Plan database capacity and backups for it, and see [Erasure](#erasure).

PostgreSQL is the only store: no local file, disk spool, SQLite buffer or message broker.

## Turning it off

Without a database (`DB_ENABLED=false`) nothing is recorded and the read API answers "not configured". Credentials and the deployment id only scope who may read and which sources may submit; they don't decide whether the gateway records its own history. The settings are on the admin console's **Settings** tab, under **Alerts**, or in the environment:

| Setting | Default | Meaning |
|---|---|---|
| `ALERT_HISTORY_ENABLED` | `true` | Capture backend history and accept history from sources with a writer credential. `false` turns both off. Applies after a restart. |
| `ALERT_HISTORY_DEPLOYMENT_ID` | `default` | This deployment's id on every history row; reader credentials are scoped by it. Applies after a restart. |
| `ALERT_HISTORY_READ_CREDENTIALS` | empty | JSON list of read-only credentials (see [Credentials](#credentials)). |
| `ALERT_HISTORY_WRITER_CREDENTIALS` | empty | JSON list of source credentials. |

`ALERT_HISTORY_ENABLED` is read once at startup. With no stored value and no environment variable, capture is on. `false` (or `0`, `no`, `off`) turns it off, and a stored value wins over the environment; the default is never written to the database, so a stored `false` stays until someone changes it. A blank value, or a value in the environment that is not a boolean, also keeps capture off, with a warning; remove a blank stored value to return to the default. A stored value that is not a boolean is ignored with a warning, as for every setting.

With capture off, stored history stays readable, status reports the backend as `disabled`, and the ingestion endpoint answers `404` even to a valid writer credential. The environment on each record comes from `DEPLOYMENT_ENV`, or is inferred from `BASE_URL` as for alert cards.

The tables, `alert_history_sources` and `alert_history_events`, come from the schema migrations like every other table. History uses its own connection pool of at most four connections, opened on first use, so it never takes a connection the request path needs. If the database is unreachable, history waits, and the gateway still starts and alerts.

What it costs:

- Connections: up to four more PostgreSQL connections per gateway process.
- Writes: each process writes its source row every 30 seconds even when idle, plus one row per fact, in batches flushed every second.
- Storage: the tables grow with alert activity until you delete rows yourself.
- Shutdown: stopping a process waits up to about 10 seconds for history (see [Buffer, writer and shutdown](#buffer-writer-and-shutdown)); a process manager that kills it sooner loses what was queued.

## What a record holds

Each fact is one row with:

- its source identity (`source`, `source_namespace`, `source_instance_id`, `process_id`), the deployment and the environment;
- `occurred_at`, by the source's clock, and `recorded_at`, the database clock in the committing transaction;
- a `kind`, the alert type, a redacted alert key, the rule id, and `rule_version`, a short hash of the rule configuration in force;
- allowlisted `facts`;
- for decisions and deliveries: the reason, channel, outcome, a stable error code and the duration. Delivery attempts carry a redacted message snapshot: the sink's own rendering of the redacted title and fields, with the names of omitted fields and a truncation flag.

The kinds are `condition_triggered`, `condition_ended`, `notification_decision`, `suppression_summary`, `delivery_attempt`, `delivery_result`, `resolution_retry_terminated`, `history_gap` and `history_conflict` (written only by the store). A backend `condition_ended` has `end_basis` `observed_healthy` (the metric was seen to clear) or `no_samples` (the stale sweep closed it for lack of data), never conflated.

A delivery outcome is `accepted` (the channel confirmed; for WeCom, HTTP 2xx and `errcode == 0`), `rejected` (an explicit refusal), `not_sent` (nothing went out: rendering failed, or the connection was never made) or `unknown` (it may have been sent: a timeout after sending, a cancellation). An attempt without a result reads as `unknown`.

### What is never kept

Facts are copied only for an allowlist of field names: counts, rates, thresholds, provider, model and endpoint labels, and stable reasons. Every other field (upstream error text, request paths, IP addresses, key prefixes, account lists, request ids) is left out and named in `limitations` as `omitted:<field>`. An allowlisted name does not make a value safe: every kept string, title and snapshot goes through value redaction, which replaces credential headers, `Bearer` and `Basic` values, secret-named `name=value` pairs, well-known token shapes, URL paths and queries, e-mail and IP addresses and long opaque runs, and records `redacted:<field>`. The ingestion endpoint redacts again, whatever a source says it already did.

Account identifiers are never stored in the clear. A key that names an account (`cost_overrun:<user>:<day>`) is stored as `cost_overrun:[subject]:<day>`, with the account's erasure-fence digest in `subject_digests`. Without a fence secret the digest is not kept either, and the record says `subject_unfenced`.

### Episodes

A backend episode is one breach as one process observed it, from the first breached evaluation to the observation that ended it. Workers don't merge episodes, and one worker's recovery does not close another's.

Episodes are separate from the notification tracker. When a recovery card fails to send, the tracker re-arms the key so the card can be retried; the episode stays ended and the retries are attached to it. A send fixes its episode id before it starts, so a result that returns after a new breach never touches the new episode. The new breach opens a new episode and ends the old one's retries (`resolution_retry_terminated`, `superseded_by_new_episode`).

A decision not to notify is recorded once per episode and reason; repeats are counted in non-overlapping 30-second windows (`suppression_summary` with `count`, `bucket_start`, `bucket_end` and the first and last times). A breach its producer held back before the alert path (a circuit page turned off by policy, the failed-request detector's cooldown, a disabled `db_disconnect` report) is still recorded, with that decision. Admin test notifications are recorded with `origin: test`, joined to the audit row by their `test_id` (`audit_ref`), and never open an episode.

## Reading

Admins use their normal login or `ADMIN_TOKEN`; agents use a scoped reader token. Every endpoint is read-only.

| Endpoint | Returns |
|---|---|
| `GET /admin/alert-history/status` | One entry per source and target environment (see [Status](#status)). Admins also see the answering worker's own buffer under `this_process`; other workers' buffers are known only from their last written snapshot. |
| `GET /admin/alert-history/events` | Events that occurred in `[since, until)` (default the last 24 hours, at most 7 days per query), filtered by `source`, `source_namespace`, `deployment_id`, `target_environment`, `kind`, `alert_type`, `model_id`, `endpoint_id`, `channel`, `outcome`, `origin`. |
| `GET /admin/alert-history/episodes/{episode_id}` | One backend episode's records. |
| `GET /admin/alert-history/source-incidents/{source}/{incident_id}?source_namespace=…` | A source incident across its generations. |
| `GET /admin/alert-history/request-evidence/{request_id}` | Time, model, endpoint, status code, latency and error class of a request that a record in your scope references; nothing else, and nothing for an unreferenced request. |

A page holds at most 200 records: 50 by default for events, 200 for timelines. Pages are keyed on `(recorded_at, record_id)` behind an opaque cursor bound to the query. `recorded_at` is the insert time inside the writing transaction, not the commit order, so the API is not a change feed: a record whose transaction commits after a reader's cursor has passed its `recorded_at` appears on no later page. To collect a window completely, rescan its `occurred_at` range from the start and deduplicate by `record_id`. Even that cannot guarantee a record that arrives arbitrarily late, such as a buffered backend record or a source's retry hours later. Sort by `occurred_at` for display.

Timelines also return `end_recorded` and a `summary` of the whole timeline in your scope, not just the page. `records`, `generation_count`, `trigger_recorded` and `end_recorded` come from one exact aggregate; `generations` lists at most 100 groups, with `generations_truncated` when there are more. `end_recorded: false` means no end record had been committed when the read ran. It does not mean the condition is still on, and never that it recovered.

When the database cannot be read, list endpoints answer `503 history_unavailable` and status says `database: unavailable`; neither answers with an empty history.

Request evidence is checked against the request's owner, whom the record holds only as an erasure-fence digest. The read takes the owner's shared fence lock, so it waits for an erasure in progress and finds nothing after one. Request logs have their own retention, so evidence can be gone while the alert record remains.

### Status

`sources` has one entry per `(source, source_namespace, target_environment)`: each target the configuration expects (the backend's environment, and each environment of each writer credential), plus any other target that has reported. Each is looked up on its own, so a busy source or a healthy environment cannot hide another. An entry has:

- `connection`: `reporting` (last report within `stale_after_sec`), `stale`, `never_reported` (no report for this target), `disabled` (capture off), or `unknown` when the database cannot be read;
- `stale_after_sec`: 180 for the backend, which reports every 30 seconds; for a source, its writer credential's value. A source that reports less often, such as a 20-minute cron job, must set it higher or it shows `stale` between runs;
- `first_recorded_at` and `last_recorded_at` (committed records about this target), `last_successful_write_at` and `last_report_at`;
- `instance_count` (a lower bound when `instance_count_is_floor`), the 20 most recently reporting `instances`, and `instances_truncated`. An instance is `running`, `stopped` (it sent a stop record: a backend shutdown, or a completed invocation with `stop_reason: invocation_complete` and its `lost_at_stop`), or `unknown` (no stop record and no recent report; it may have crashed).

The answer also has `targets_truncated` when there are more than 200 targets, and `known_gaps`: at most 50 gap and conflict records from the last 7 days, with `known_gaps_truncated` and a pointer to the events endpoint for the rest.

## Credentials

Both lists store only the SHA-256 of each token. Generate a token, keep it in the consumer's own secret store, and put only its hash here:

```bash
TOKEN="ahw_$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf %s "$TOKEN" | sha256sum   # the 64 hex digits go into "sha256"
```

A reader credential fixes what its token may read:

```json
[{"id": "agent-readonly-1",
  "sha256": "<64 hex digits>",
  "deployment_ids": ["example-deployment"],
  "target_environments": ["production"],
  "expires_at": "2027-01-31T00:00:00Z"}]
```

A writer credential fixes the identity of everything its token submits; the request cannot change it:

```json
[{"id": "monitor-writer-1",
  "sha256": "<64 hex digits>",
  "source": "status_monitor",
  "source_namespace": "example-monitor",
  "deployment_id": "example-deployment",
  "target_environments": ["production", "staging"],
  "stale_after_sec": 1800,
  "expires_at": "2027-01-31T00:00:00Z"}]
```

`stale_after_sec` is optional (default 180, from 60 to 86400): how long the source may be silent in an environment before status calls it stale. Set it to the reporting interval plus a margin.

The rules:

- Tokens shorter than 32 characters are refused, and `expires_at` needs an explicit offset.
- `source: backend` is reserved for the gateway.
- Ids and hashes are unique across both lists, and credentials for the same `(source, source_namespace)` must name the same deployment.
- A reader token works only on the read endpoints and a writer token only on the ingestion endpoint; no other admin or internal route accepts either.

Credentials fail closed. A list that does not parse refuses every history token (an admin still gets in with their own credential). So does a configuration this worker cannot confirm is current: settings are re-read every 10 seconds, and when the snapshot in force was last confirmed more than 30 seconds ago, no history token is accepted until a read succeeds. A revoked credential therefore stops working everywhere within about 30 seconds, or the endpoint refuses everyone.

To rotate, add the new credential beside the old one (same `source` and `source_namespace` for a writer), give the consumer the new token, confirm it is used (status shows the source reporting), then remove the old entry. To revoke at once, remove the entry. Keep `expires_at` short to limit exposure.

## Ingestion API

```
POST /internal/alert-history/events
Authorization: Bearer <writer token>
Content-Type: application/json
```

A request carries at most 50 events and 256 KiB, and each event at most 64 KiB. The answer is `200` only after the transaction holding the batch has committed. Any other answer, or none, means "not known to be saved": resend the same events with the same ids. The console proxy forwards this one path, not the whole `/internal` prefix (see [Public path table](public-path-table.md)). It adds no authentication; the gateway admits only a configured writer credential and answers `404` while history is off or no writer is configured.

A minimal request, with one event and the sending instance's status:

```json
{"schema_version": 1,
 "events": [{
  "origin_record_id": "inc:inc_7f3a:g2:triggered",
  "kind": "condition_triggered",
  "occurred_at": "2026-10-26T08:15:30.125Z",
  "target_environment": "production",
  "source_instance_id": "monitor-2026-10-26T08:00:00Z-a1b2"}],
 "source_status": {
  "source_instance_id": "monitor-2026-10-26T08:00:00Z-a1b2",
  "target_environment": "production",
  "observed_at": "2026-10-26T08:15:31.200Z",
  "pending_count": 0}}
```

An event requires `origin_record_id` (1–200 characters of `[A-Za-z0-9._:@/-]`, one per fact, fixed by the source before the first submission), `kind`, `occurred_at` (an RFC 3339 string with an explicit offset, at most 400 days in the past and 5 minutes in the future) and `target_environment` (one of the credential's). Optional fields: `origin` (`live`, `test`), `source_instance_id`, `severity`, `alert_type`, `rule_id`, `rule_version`, `source_event_id`, `source_incident_id`, `generation`, `action_id`, `attempt_id`, `observation_scope`, `model_id`, `endpoint_id`, `end_basis`, `decision_reason`, `count`, `bucket_start`/`bucket_end`, `channel`, `outcome`, `error_code` (a stable code, never free text), `duration_ms`, `facts` (a flat object of at most 32 keys with string, number, boolean or null values), `message` (`title`, `body`, `format`, `format_version`, `redacted`, `truncated`, `omitted_fields`) and `external_ref` (`system`, `ref`).

Any other field is a `422`, including `source`, `source_namespace` and `deployment_id`, and so is an identifier that contains a credential or an address. Sources cannot send request references, because the gateway cannot prove who owns them. An event without `source_instance_id` (for a fact a source observes again in later runs, so a replay stays a byte-identical duplicate) is kept with `association_incomplete: true` and `no_source_instance` in `limitations`.

### Facts

Numbers, booleans and nulls are kept under any well-formed name; strings pass validation up to 500 characters. A string fact is kept only under a recognized name, of one of three kinds:

- Codes: the value must be a lowercase code token, `[a-z0-9][a-z0-9_.:-]{0,99}` (for example `control_plane_rpc`, `stale_result_not_applied`, `manual_reconciliation_required`); anything else under these names is dropped: `action_status`, `action_status_after`, `action_type`, `basis`, `check`, `claim_mode`, `commit`, `commit_state`, `control_plane_action`, `cycle_owner`, `db_kind`, `default_owner`, `delivery_owner`, `end_trigger`, `endpoint_id_omitted`, `error_type`, `gate`, `handoff_outcome`, `incident_key_form`, `incident_state`, `lifecycle_action`, `lifecycle_state`, `mode`, `model_id_omitted`, `notification`, `observation_basis`, `phase`, `previous_state`, `probe`, `producer_source`, `reason`, `record_role`, `recovery_notification`, `region`, `role`, `rule_version`, `source_event_id_form`, `state`, `status`, `store`, `task_name`, `transition`, `transport`, `trigger`, `window`.
- Identifiers: kept as sent, never partly redacted, in the identifier characters `[A-Za-z0-9._:@/-]`, mixed case, at most 200 characters; dropped if they carry an e-mail address, a URL, `bearer`/`authorization`/`password`/`secret`/`api_key` or a known token shape: `monitor_incident_key` (for example `status-monitor:model:Example-Org/Example-Model-1`, `fp:4f2185d2122b562b`).
- Labels: printable, cut to 200 characters, redacted: `alert`, `availability`, `budget`, `endpoint_id`, `first_at`, `first_lost_at`, `hour`, `hourly_spend`, `last_at`, `last_lost_at`, `model`, `over_by`, `provider`, `quota`, `rate`, `spend`, `top_endpoints`, `top_error_types`, `top_providers`, `top_status_codes`.

Every other string fact (a note, an error message, a prompt, a request body) is dropped and named in `limitations` as `omitted:<name>`, and so is any fact whose name marks a secret, whatever its type. The same rules apply to `source_status.rules_snapshot`, which holds at most 64 keys and 16 KiB. The names follow the technical field inventory of the status monitor and its alert control plane, so adding one is a contract change on both sides. A handoff to an intermediary (`channel: other`, `outcome: unknown`) keeps the facts that explain it, such as `transport`, `delivery_owner`, `handoff_outcome`, `control_plane_action`, `lifecycle_state` and `commit`.

### Identity, deduplication and the answer

An event's identity is `(source, source_namespace, origin_record_id)`, the first two from the credential. The server computes `payload_digest`, the SHA-256 of the canonical JSON of the validated, redacted event (keys sorted, timestamps normalized to UTC microseconds, absent and null fields dropped). The same identity and digest is a duplicate: nothing is written and the original `record_id` comes back. The same identity with another digest is a conflict: the stored fact stays, and the losing version is kept as a `history_conflict` row with only its digest and kind. Validation and scope errors fail the whole request; duplicates and conflicts are reported per item.

Derive ids from the source fact, for example `inc:<incident>:g<generation>:triggered` or `act:<action>:a<n>:result`, never from the submission time, a random value per retry, or the message text.

```json
{"committed": true, "recorded_at": "2026-10-26T08:15:31.402113Z",
 "source": "status_monitor", "source_namespace": "example-monitor",
 "results": [{"index": 0, "origin_record_id": "inc:inc_7f3a:g2:triggered",
              "status": "inserted", "record_id": "…"}],
 "source_status": "recorded"}
```

Errors have the shape `{"error": {"type": …, "message": …}}`:

| Status | `type` | When |
|---|---|---|
| `401` | `unauthorized` | Missing, unknown or expired token |
| `403` | `scope_denied`, `wrong_credential_kind` | A target environment outside the credential, or a reader token |
| `404` | `alert_history_not_configured` | History is off, or no writer credential is configured |
| `408` | `request_timeout` | The body did not arrive within 10 seconds |
| `413` | `payload_too_large` | The body is over 256 KiB |
| `415` | `unsupported_media_type` | The body is not `application/json` |
| `422` | `invalid_request` | Invalid or unparseable body, JSON nested deeper than 16 levels, or an event over 64 KiB. `error.details` gives the index, field path and code, never the submitted value |
| `429` | `too_many_requests` | Too many concurrent submissions |
| `503` | `history_unavailable`, `history_auth_unavailable` | The batch could not be committed, or credentials cannot be verified. Both carry `Retry-After`; resend the same body |

A source keeps no undeclared local copy, bounds its retries by its own execution budget, and later reports what it gave up on as a `history_gap`. The endpoint runs in the gateway, so a gateway outage is also an ingestion outage, which sources see as failed or unknown submissions.

### Source status

`source_status` is the sending instance's heartbeat; `events` may be empty. It requires `source_instance_id`, `target_environment` and `observed_at`; `release_id`, `started_at`, `sync_watermark`, `pending_count`, `pending_reported_at`, `dropped_count`, `last_error_code`, `rules_snapshot_hash` and `rules_snapshot` (flat, like `facts`) are optional. Stored timestamps never move backwards, and the read API presents the snapshot as last reported, not as live state.

A short-lived producer (one cron run, one RPC, one alarm) closes its instance in its final status with `stopped_at` (RFC 3339 with an offset), `stop_reason: "invocation_complete"` and `lost_at_stop` (0 to 10^9, the records it knows it lost). Send all three or none, and only with `pending_count: 0`: a stop with work still queued, or without `pending_count`, is a `422`. `stopped_at` may not precede `started_at` or lie more than 5 minutes ahead. Only a committed `200` with `source_status: "recorded"` confirms the stop. The instance then reads as `stopped`, not as a crashed `unknown`; unknown gaps stay possible.

Example lifecycle event:

```json
{"schema_version": 1, "events": [{
  "origin_record_id": "inc:inc_7f3a:g2:triggered",
  "kind": "condition_triggered",
  "occurred_at": "2026-10-26T08:15:30.125Z",
  "target_environment": "production",
  "source_instance_id": "monitor-2026-10-26T08:00:00Z-a1b2",
  "source_incident_id": "inc_7f3a", "generation": 2,
  "observation_scope": "model", "alert_type": "model_unavailable",
  "severity": "error", "rule_id": "model_probe", "rule_version": "3",
  "model_id": "example-model-1",
  "facts": {"failed_probes": 3, "probe_window_sec": 180}}]}
```

Example delivery result:

```json
{"schema_version": 1, "events": [{
  "origin_record_id": "act:act_91:a1:result",
  "kind": "delivery_result",
  "occurred_at": "2026-10-26T08:15:31.010Z",
  "target_environment": "production",
  "source_incident_id": "inc_7f3a", "generation": 2,
  "action_id": "act_91", "attempt_id": "act_91:1",
  "channel": "wecom", "outcome": "rejected",
  "error_code": "wecom_errcode_45009", "duration_ms": 412}]}
```

## Buffer, writer and shutdown

Each process queues at most 2,000 records or 8 MiB, counting the batch being written; a full buffer refuses new records and keeps what it has. The writer flushes batches of at most 50 records or 256 KiB every second. On failure, records stay queued and the writer backs off with jitter from 1 to 30 seconds. Each record is written in its own savepoint, so one unwritable record is dropped and counted without failing its batch, and a lost `COMMIT` response is resent and answered as duplicates. A heartbeat updates the instance's source row every 30 seconds even with nothing to write; its pending snapshot (`pending_count`, `pending_bytes`, `oldest_pending_at`) describes what stays queued once the batch written with it commits.

Startup installs the recorder before any producer starts. Shutdown runs after the producers stop:

1. It waits up to 5 seconds for every task still in the alert path or about to enter it: request handlers, the circuit breaker's and the health route's detached alert tasks, scheduler jobs, and sends waiting for a result.
2. It writes the open summary windows, closes the buffer and drains it. The write in progress, the drain, the stop record and closing the pool share a 5-second budget.

What remains is logged, counted in the instance's stop record (`lost_at_stop`), and lost. Allow about 10 seconds for these waits in the process manager's stop timeout. A new process is a new source instance, and an instance with no stop record shows as `unknown`, never as cleanly stopped.

## Erasure

Hard-deleting an account deletes, in the same transaction that sets its erasure fence, every history row whose `subject_digests` include the account. Every history write, however long it waited in a buffer, takes the fence's shared lock on its subjects and checks the fence in its own transaction, so a record about an erased account is never written afterwards, and a write during an erasure waits for it. Request evidence reads take the same lock. Database backups and restores carry history like any other table and follow the same deletion practice.

## Limitations

- Buffers, open episodes and summary windows live in each worker's memory and are lost on a crash.
- The suppression summary has no metric peak: the alert path sees decisions, not the metric.
- There is no commit-ordered feed; rescan and deduplicate by `record_id` (see [Reading](#reading)).
- Request references are kept only for an authenticated caller, and only with a fence secret.
