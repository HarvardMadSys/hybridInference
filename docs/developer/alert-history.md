# Alert history

The gateway can keep a structured history of what its alerting did: every
breach it observed, every recovery, every decision not to notify (cooldown,
mute, snooze, no channel, a policy switch), every delivery attempt and its
result, and the admin console's test notifications. Other alert producers —
the status monitor first — submit their own history through an authenticated
ingestion endpoint. People and agents read it back through a scoped, read-only
API.

It is **on whenever the gateway has a database** (see
[Turning it off](#turning-it-off)) and changes nothing about what is sent: thresholds,
cooldowns, mutes, snoozes, retries, routing and the `bool` that
`alert_slack()` / `AlertSink.post()` return are exactly as before. It is a
record, not an alert-noise feature.

## What "kept" means

- **Only a committed PostgreSQL row is saved.** Capture offers each fact to a
  bounded in-memory buffer without waiting; a background writer commits it.
  Being queued is not being saved.
- **Best effort, honestly reported.** A crash, a shutdown that runs out of
  time, or a buffer that fills during a long database outage can lose
  uncommitted facts. Refusals are counted and written later as a
  `history_gap` record, but that record can be lost too. Every read answer
  therefore carries `completeness: best_effort`, `unknown_gaps_possible: true`
  and `late_arrivals_possible: true`. An empty gap list never proves the
  history is complete.
- **History starts when capture starts**: the first gateway start with a
  database and capture on. Past channel messages are not turned into history.
- **No automatic deletion.** Nothing deletes history on a timer. Plan database
  capacity and backups for it, and see [Erasure](#erasure).

PostgreSQL is the only store. There is no local file, disk spool, SQLite
buffer or message broker.

## Turning it off

A gateway with a database records its own alert history from startup; there
is nothing to switch on. Without a database (`DB_ENABLED=false`) nothing is
recorded and the read API answers "not configured". Reader and writer
credentials and the deployment id do not decide whether the gateway records
its own history: they only scope who may read it and which sources may submit
to it.

Settings, under **Alerts** on the admin console's Configuration tab (or the
environment):

| Setting | Default | Meaning |
|---|---|---|
| `ALERT_HISTORY_ENABLED` | `true` | Capture backend history and accept history from sources with a writer credential. `false` turns both off. Applies after a restart. |
| `ALERT_HISTORY_DEPLOYMENT_ID` | `default` | This deployment's id on every history row; reader credentials are scoped by it. Applies after a restart. |
| `ALERT_HISTORY_READ_CREDENTIALS` | empty | JSON list of read-only credentials (see below). |
| `ALERT_HISTORY_WRITER_CREDENTIALS` | empty | JSON list of source credentials (see below). |

`ALERT_HISTORY_ENABLED` is a deployment-level opt-out, read once at startup:

- **Not set anywhere** (no stored value, no environment variable): capture is
  on.
- **`false`** (or `0`, `no`, `off`): capture is off. A value stored in the
  console wins over the environment, so a stored `false` stays off whatever the
  environment says, and an environment `false` stays off while nothing is
  stored. The built-in default is never written to the database, so a stored
  `false` stays in force until someone changes it.
- **Blank**, stored or in the environment: capture stays off, and the gateway
  logs a warning. A blank stored value is in force like any other stored
  value. Remove the value to return to the default.
- **Not a boolean in the environment**: capture stays off, with a warning.
- **Not a boolean in the database**: the stored value is ignored with a
  warning, as for every setting, and the environment value applies — or, with
  none, the default: capture on.

With capture off, the history already stored stays readable through the read
API, its status reports the backend as `disabled`, and the ingestion endpoint
answers `404` even to a valid writer credential.

The observed environment comes from `DEPLOYMENT_ENV` (or is inferred from
`BASE_URL`, as for alert cards).

With capture on, the gateway creates two tables on first use (`CREATE TABLE IF
NOT EXISTS`, catalog-gated, like the rest of the schema): `alert_history_sources`
and `alert_history_events`. History uses **its own small connection pool**
(at most four connections, created lazily), so it never takes a connection the
request path needs, and a database that is down at boot only delays history.
Neither a missing schema nor an unreachable database stops the gateway from
starting or alerting.

### What it costs

Recording uses database and process resources; plan for them:

- **Connections.** Each gateway process may hold up to four more PostgreSQL
  connections. Count them against the server's connection limit.
- **Writes.** Each process writes its source row every 30 seconds even when
  nothing happens, and one row per recorded fact, in batches flushed every
  second.
- **Storage.** Nothing is deleted automatically (see
  [What "kept" means](#what-kept-means)): the tables grow with alert activity
  until you delete rows yourself.
- **Shutdown.** Stopping a process waits up to about 10 seconds for history
  (see [Buffer, writer and shutdown](#buffer-writer-and-shutdown)). A process
  manager that kills the gateway sooner loses what was still queued.

## What a record holds

Each fact is one row with: its source identity (`source`, `source_namespace`,
`source_instance_id`, `process_id`), the deployment and observed environment,
`occurred_at` (when it happened, source clock) and `recorded_at` (database
clock, written in the committing transaction), a `kind`, the alert type and a
redacted alert key, the rule id and a short hash of the rule configuration in
force (`rule_version`), allowlisted `facts`, and for decisions and deliveries
the reason, channel, outcome, stable error code and duration. Delivery attempts
carry a redacted **message snapshot**: the sink's own rendering of the redacted
title and fields, with the names of omitted fields and a truncation flag.

Kinds: `condition_triggered`, `condition_ended` (with `end_basis`
`observed_healthy` or `no_samples` — never conflated), `notification_decision`,
`suppression_summary`, `delivery_attempt`, `delivery_result`,
`resolution_retry_terminated`, `history_gap`, and `history_conflict` (written
by the store only).

Delivery outcomes: `accepted` (the channel confirmed; for WeCom HTTP 2xx *and*
`errcode == 0`), `rejected` (an explicit refusal), `not_sent` (nothing left:
rendering failed, the connection was never made), `unknown` (it may have been
sent: a timeout after sending, a cancellation, no result recorded). An attempt
without a result reads as `unknown`.

### What is never kept

Facts are copied only for an allowlist of field names (counts, rates,
thresholds, provider/model/endpoint labels, stable reasons). Every other field —
upstream error text, request paths, IP addresses, key prefixes, account lists,
request ids — is left out and named in the record's `limitations`
(`omitted:<field>`). A field name being allowlisted does not make its value
safe: every kept string, title and snapshot passes value redaction that
replaces credential headers, `Bearer`/`Basic` values, secret-named
`name=value` pairs, well-known token shapes, URL paths and queries, e-mail and
IP addresses and long opaque runs, and records `redacted:<field>`. The same
redaction runs again at the ingestion boundary, whatever a source claims about
having redacted its text already.

Account identifiers are never stored in the clear. An alert whose key names an
account (`cost_overrun:<user>:<day>`) is stored as
`cost_overrun:[subject]:<day>` with the account's erasure-fence digest in
`subject_digests`. Without a fence secret the digest is not kept either and the
record says `subject_unfenced`.

### Episodes

A backend **episode** is one breach as one process observed it: opened by the
first breached evaluation, ended by the observation that ended it. Workers do
not merge episodes, and one worker's recovery does not close another's.

The episode is independent of the notification tracker. When a recovery card
fails, the tracker re-arms the key so the card can be retried; the episode
stays ended and the retries are attached to it — the episode id is fixed when
the send starts and carried by the retry, so a result that returns after a new
breach never touches the new episode. A breach after that opens a new episode
and ends the old one's retry (`resolution_retry_terminated`,
`superseded_by_new_episode`).

A decision not to notify is recorded once per episode and reason, then counted
in non-overlapping 30-second windows (`suppression_summary` with `count`,
`bucket_start`, `bucket_end`, first/last time). A breach held back by its
producer before it reached the alert path (a circuit page turned off by
policy, the failed-request detector's own cooldown, a disabled
`db_disconnect` report) is still recorded as a breach with that decision.

Admin test notifications are recorded with `origin: test`, joined to the audit
row by their `test_id` (`audit_ref`), and never open an episode.

## Reading

Admins use their normal login or `ADMIN_TOKEN`; agents use a scoped reader
token. Every endpoint is read-only.

| Endpoint | Returns |
|---|---|
| `GET /admin/alert-history/status` | One entry per **source and target environment** (see [Status](#status)). Admins also see the answering worker's own buffer under `this_process`; other workers' buffers are known only from their last written snapshot. |
| `GET /admin/alert-history/events` | Events that **occurred** in `[since, until)` (default the last 24 h, at most 7 days per query), filtered by `source`, `source_namespace`, `deployment_id`, `target_environment`, `kind`, `alert_type`, `model_id`, `endpoint_id`, `channel`, `outcome`, `origin`. |
| `GET /admin/alert-history/episodes/{episode_id}` | One backend episode's records. |
| `GET /admin/alert-history/source-incidents/{source}/{incident_id}?source_namespace=…` | A source incident across its generations. |
| `GET /admin/alert-history/request-evidence/{request_id}` | Time, model, endpoint, status code, latency and error class of a request that a history record in your scope references — nothing else, and nothing for an unreferenced request. |

Pages are at most 200 records (default 50), keyed on
`(recorded_at, record_id)` behind an opaque cursor that is bound to the query
(and carries its resolved time window). `recorded_at` is the database clock
when the row was **inserted**, inside its transaction — not commit order, and
the API is not a changefeed. Two concurrent writes can commit in the opposite
order to their `recorded_at`: a reader can page past the later one before the
earlier one becomes visible, and that row then appears on **no** later page of
that scan. Pagination is not a snapshot (`late_arrivals_possible: true`).

To collect everything for a window, rescan the `occurred_at` window from its
start and deduplicate by `record_id`. Rescanning helps only for what has
committed by the time of the rescan: no finite overlap or delay guarantees
completeness for a record that arrives arbitrarily late (a buffered backend
record, a source's retry hours later). Sort by `occurred_at` for display.

Episode and source-incident timelines also return `end_recorded` and a
`summary` over the **whole timeline in your scope**, not the page being read:
`records`, `generation_count`, `trigger_recorded` and `end_recorded` come from
one exact aggregate over every matching row; `generations` lists at most 100
per-generation groups, with `generations_truncated` when there are more — the
totals never depend on that list. `end_recorded: false` means no end record had
been committed when the read ran — not that the condition is still on, and
never that it recovered.

### Status

`sources` has one entry per `(source, source_namespace, target_environment)`:
every target configuration expects (the backend's environment; each
environment of each writer credential) plus any other target that has
reported. Each is looked up on its own, so a busy source or a healthy
environment cannot hide another one:

- `connection`: `reporting` (last report within `stale_after_sec`), `stale`
  (reported, but not within it), `never_reported` (no report for *this*
  target at all), `disabled` (capture off), or `unknown` when the database
  cannot be read.
- `stale_after_sec`: 180 for the backend (heartbeat every 30 s); for a source,
  its writer credential's `stale_after_sec` (default 180, 60…86400). A
  source that reports on a slower cadence — a 20-minute cron — must set it
  above that cadence, or it shows `stale` between runs.
- `first_recorded_at` / `last_recorded_at` (committed records about this
  target), `last_successful_write_at`, `last_report_at`.
- `instance_count` (a floor when `instance_count_is_floor`), the 20 most
  recently reporting `instances`, and `instances_truncated`. An instance's
  `state` is `running`, `stopped` (it sent a stop record — a backend shutdown,
  or a completed invocation with `stop_reason: invocation_complete` and its
  `lost_at_stop`), or `unknown` (no stop record and no recent report: it may
  have crashed).
- `targets_truncated` when more than 200 targets exist; `known_gaps` lists at
  most 50 gap and conflict records from the last 7 days, with
  `known_gaps_truncated` and a pointer to the events read for the rest.

When the database cannot be read, list endpoints answer `503
history_unavailable` and the status endpoint says `database: unavailable`;
neither ever answers with an empty history.

Request evidence is owner-checked: the record holds the request's owner only as
an erasure-fence digest, the lookup must match it, and the read takes the
owner's shared fence lock, so it waits for an erasure in progress and finds
nothing after one. Request logs have their own retention, so evidence may be
gone while the alert record remains.

## Credentials

Both lists store only the **SHA-256 of each token**. Generate a token, keep it
in the consumer's own secret store, and put only its hash here:

```bash
TOKEN="ahw_$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf %s "$TOKEN" | sha256sum   # the 64 hex digits go into "sha256"
```

Reader credential — what it may read is fixed here:

```json
[{"id": "agent-readonly-1",
  "sha256": "<64 hex digits>",
  "deployment_ids": ["example-deployment"],
  "target_environments": ["production"],
  "expires_at": "2027-01-31T00:00:00Z"}]
```

Writer credential — the identity of everything it submits is fixed here, not by
the request:

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

`stale_after_sec` (optional, default 180, 60…86400) is how long the source may
be silent per environment before status calls it stale: its reporting cadence
plus a margin.

Rules: tokens shorter than 32 characters are refused; `expires_at` needs an
explicit offset; `source: backend` is reserved; ids and hashes are unique
across both lists; credentials for the same `(source, source_namespace)` must
name the same deployment. A reader token works only on the read endpoints, a
writer token only on the ingestion endpoint; neither is accepted by any other
admin or internal route.

**Fail closed.** A list that does not parse refuses every history token (an
admin still gets in on their own credential). So does a configuration this
worker cannot confirm is current: settings are re-read every 10 seconds, and
when the snapshot in force was last confirmed more than 30 seconds ago, no
history token is accepted until a read succeeds. A revoked credential
therefore stops working everywhere within about 30 seconds, or the endpoint
refuses everyone.

**Rotation.** Add the new credential (same `source` and `source_namespace` for
a writer) alongside the old one, deploy the new token to the consumer, confirm
it is used (`status` shows the source reporting), then remove the old entry.
To revoke immediately, remove the entry; to limit exposure, keep `expires_at`
short.

## Ingestion API

```
POST /internal/alert-history/events
Authorization: Bearer <writer token>
Content-Type: application/json
```

At most 50 events and 256 KiB per request, 64 KiB per event. The answer is
`200` **only after the transaction holding the batch has committed**; anything
else, including no answer, means "not known to be saved", and the source
resends the same events with the same ids.

A minimal request — one event and the sending instance's status; the fields
are described below:

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

Event fields. Required: `origin_record_id` (1–200 chars of
`[A-Za-z0-9._:@/-]`, generated by the source, fixed before the first
submission, one per fact), `kind`, `occurred_at` (RFC 3339 **string** with an
explicit offset, within 400 days past and 5 minutes future), and
`target_environment` (must be in the credential's list). Optional: `origin`
(`live`, `test`), `source_instance_id`, `severity`, `alert_type`, `rule_id`,
`rule_version`, `source_event_id`, `source_incident_id`, `generation`,
`action_id`, `attempt_id`, `observation_scope`, `model_id`, `endpoint_id`,
`end_basis`, `decision_reason`, `count`, `bucket_start`/`bucket_end`,
`channel`, `outcome`, `error_code` (a stable code, never free text),
`duration_ms`, `facts` (a flat object of at most 32 keys; values string (≤500 accepted, see below),
number, boolean, null), `message` (`title`, `body`, `format`,
`format_version`, `redacted`, `truncated`, `omitted_fields`) and
`external_ref` (`system`, `ref`). Any other field — `source`,
`source_namespace`, `deployment_id` included — is a `422`. Identifiers that
contain a credential or address shape are a `422`. Request references are not
accepted from sources: the gateway cannot prove who owns them.

**Facts are typed; text facts are recognized names only.** Numbers, booleans
and nulls are kept under any well-formed name (strings up to 500 characters
pass validation, as on the sending side). A *string* fact is kept only under a
recognized name, of one of two kinds:

- **Codes** — the value must be a lowercase code token,
  `[a-z0-9][a-z0-9_.:-]{0,99}` (e.g. `control_plane_rpc`,
  `stale_result_not_applied`, `manual_reconciliation_required`); anything
  else under these names is dropped: `action_status`, `action_status_after`, `action_type`, `basis`, `check`, `claim_mode`, `commit`, `commit_state`, `control_plane_action`, `cycle_owner`, `db_kind`, `default_owner`, `delivery_owner`, `end_trigger`, `endpoint_id_omitted`, `error_type`, `gate`, `handoff_outcome`, `incident_key_form`, `incident_state`, `lifecycle_action`, `lifecycle_state`, `mode`, `model_id_omitted`, `notification`, `observation_basis`, `phase`, `previous_state`, `probe`, `producer_source`, `reason`, `record_role`, `recovery_notification`, `region`, `role`, `rule_version`, `source_event_id_form`, `state`, `status`, `store`, `task_name`, `transition`, `transport`, `trigger`, `window`.
- **Identifiers** — kept exactly, never partly redacted: the identifier
  charset `[A-Za-z0-9._:@/-]`, mixed case, at most 200 characters, and dropped
  if it carries an e-mail address, a URL, `bearer`/`authorization`/
  `password`/`secret`/`api_key` or a known token shape: `monitor_incident_key` (e.g.
  `status-monitor:model:Example-Org/Example-Model-1`, `fp:4f2185d2122b562b`).
- **Labels** — printable, cut to 200 characters, redacted: `alert`, `availability`, `budget`, `endpoint_id`, `first_at`, `first_lost_at`, `hour`, `hourly_spend`, `last_at`, `last_lost_at`, `model`, `over_by`, `provider`, `quota`, `rate`, `spend`, `top_endpoints`, `top_error_types`, `top_providers`, `top_status_codes`.

Every other string fact — a note, an error message, a prompt, a request body —
is dropped and named in the record's `limitations` (`omitted:<name>`); so is
any fact whose name marks a secret, whatever its type. The same rule applies to
`source_status.rules_snapshot` (at most 64 keys and 16 KiB, as on the sending
side). The names follow the status monitor's and alert control plane's
technical field inventory; adding one is a contract change on both sides. A
handoff to an intermediary (`channel: other`, `outcome: unknown`) keeps the
facts that explain it — `transport`, `delivery_owner`, `handoff_outcome`,
`control_plane_action`, `lifecycle_state`, `commit` and the like.

An event sent without `source_instance_id` (a fact a source re-observes in
later runs, so a replay stays a byte-identical duplicate) is kept with
`association_incomplete: true` and `no_source_instance` in `limitations`.

**Identity and dedup.** The key is `(source, source_namespace,
origin_record_id)`, the first two from the credential. The server computes
`payload_digest`, the SHA-256 of the canonical JSON of the validated,
server-redacted event (keys sorted, timestamps normalised to UTC
microseconds, absent and null fields dropped). Same key and digest: a
**duplicate**, nothing new written, the original `record_id` returned. Same
key, different digest: a **conflict**; the stored fact is not overwritten, the
losing version is kept as a `history_conflict` row (digest and kind only).
Validation and scope errors fail the whole request; duplicates and conflicts
are per item.

Derive ids from the source fact: `inc:<incident>:g<generation>:triggered`,
`act:<action>:a<n>:result`. Never from submission time, a per-retry random
value, or the message text.

Response:

```json
{"committed": true, "recorded_at": "2026-10-26T08:15:31.402113Z",
 "source": "status_monitor", "source_namespace": "example-monitor",
 "results": [{"index": 0, "origin_record_id": "inc:inc_7f3a:g2:triggered",
              "status": "inserted", "record_id": "…"}],
 "source_status": "recorded"}
```

Errors use `{"error": {"type": …, "message": …}}`: `401 unauthorized`,
`403 scope_denied` / `wrong_credential_kind`, `404
alert_history_not_configured`, `413 payload_too_large`, `415`, `422
invalid_request` (with `error.details`: index, field path and code — never the
submitted value; also for JSON nested deeper than 16 levels or not valid
JSON), `408 request_timeout` (the body did not arrive within 10 seconds; read
before any commit slot is taken), `429 too_many_requests` and `503
history_unavailable` /
`history_auth_unavailable` (both with `Retry-After`; resend the same body).

A source keeps no undeclared local copy, bounds its retries by its own
execution budget, and reports what it gave up on as a `history_gap` later.

Source status (heartbeat; `events` may be empty): `source_instance_id`,
`target_environment` and `observed_at` required; `release_id`, `started_at`,
`sync_watermark`, `pending_count`, `pending_reported_at`, `dropped_count`,
`last_error_code`, `rules_snapshot_hash`, `rules_snapshot` (flat, like
`facts`) optional. Stored timestamps never move backwards, and the read API
presents the snapshot as last reported, never as live state.

A short-lived producer (one cron run, one RPC, one alarm) closes its instance
in its **final** status: `stopped_at` (RFC 3339 with offset),
`stop_reason: "invocation_complete"` and `lost_at_stop` (0…10^9, the records
it knows it lost) — all three or none, and only with `pending_count: 0`; a
stop with work still queued, or with `pending_count` absent, is a `422`, and
`stopped_at` may not precede `started_at` or lie more than 5 minutes ahead.
Only a committed `200` with `source_status: "recorded"` confirms it. Such an
instance reads as `stopped`, not as a crashed `unknown`. It claims nothing
about records it did not know it lost: unknown gaps stay possible.

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

The ingestion endpoint runs in the gateway, so a gateway outage is also an
ingestion outage; sources report that as failed or unknown submissions.

## Buffer, writer and shutdown

Per process: at most 2,000 records or 8 MiB queued, **counting the batch being
written**; batches of at most 50 records / 256 KiB, flushed every second;
failures back off with jitter from 1 to 30 seconds while the records stay
queued; a full buffer refuses new records and keeps what it has. Each record is
written in its own savepoint, so one unwritable record is dropped and counted
without failing its batch. A lost `COMMIT` response is resent and answered as
duplicates. A heartbeat updates the instance's source row every 30 seconds even
with nothing to write; a heartbeat proves only that it was written. Its
pending snapshot (`pending_count`, `pending_bytes`, `oldest_pending_at`)
describes, consistently, what stays queued once the batch committed with it.

Startup installs the recorder before any producer starts. Shutdown runs after
the producers have stopped: it waits up to 5 seconds for every task still in
the alert path or scheduled to enter it (request handlers, the breaker's and
the health route's detached alert tasks, scheduler jobs, sends awaiting their
result), then writes its open summary windows, closes the buffer and drains it
— the write already in progress, its cancellation, the drain, the stop record
and closing the pool all inside a 5-second budget. What is left is reported in
the instance's stop record (`lost_at_stop`) and logged, and is lost. A new
process is a new source instance; an instance with no stop record is shown as
`unknown`, never as cleanly stopped. Each wait ends as soon as its work is
done, but together they can add about 10 seconds to a shutdown; give the
process manager's stop timeout room for that.

## Erasure

Hard-deleting an account removes, in the same transaction that establishes its
erasure fence, every history row whose `subject_digests` contain the account.
Every history write — however long it waited in a buffer — takes the fence's
shared lock on its subjects and checks the fence in its own transaction, so a
record about an erased account is never written afterwards, and a write during
an erasure waits for it. Request evidence reads take the same lock. Backups
and restores of the database carry history like any other table and are
subject to the same deletion practice.

## Limitations

- In-memory state per worker: buffers, open episodes and summary windows are
  lost on a crash.
- The suppression summary has no metric peak: the alert path sees decisions,
  not the metric.
- `recorded_at` is the insert-time clock inside the writing transaction, not
  its commit time; a transaction that commits late is invisible to a cursor
  that has already passed it. There is no commit-ordered feed; rescan and
  deduplicate by `record_id` (see [Reading](#reading)).
- Request references are kept only for an authenticated caller, and only with
  a fence secret.
- The ingestion endpoint is reachable through the console proxy at
  `/internal/alert-history/events` — that one path, not the `/internal`
  prefix (see [Public path table](public-path-table.md)). The proxy adds no
  authentication: the gateway admits only a configured writer credential, and
  answers `404` while alert history is off or no writer is configured.
