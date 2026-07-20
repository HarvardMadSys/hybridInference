# Unified Alert Control Plane V2

V2 is an opt-in migration path for Slack incident lifecycle management and
read-only Codex analysis. This first migration PR adds code, tests, templates,
and a runbook only. It does not create Cloudflare resources, set secrets,
modify a live Wrangler configuration, deploy a Worker, or enable a staging or
production producer.

V1 remains unchanged and is the default whenever `ALERT_RELAY_V2_URL` and
`ALERT_RELAY_V2_TOKEN` are unset.

## Incident lifecycle

A producer sends a structured version `2` event. The relay derives
`STAGING`, `PRODUCTION`, or `LOCAL` solely from the matching producer bearer
token. It rejects producer-rendered Slack text and producer environment fields.

The lifecycle is:

1. First `firing`: create an internal incident ID, one Slack parent, and one
   analysis job.
2. Repeated `firing`: increment the D1 occurrence count and last-seen timestamp,
   then update that parent with Slack `chat.update`. Do not add a thread reply.
3. Codex completion: add one analysis or unavailable reply in the parent
   thread.
4. `resolved`: add one recovery reply and update the parent to Resolved.
5. A later `firing`: create a new incident ID and a new parent.

The parent carries trusted environment identity, the internal incident ID,
first/last seen, failure count, useful bounded context, and Codex state. It
shows deployment as `branch@pending` before ref validation, then either
`branch@validated-immutable-SHA` or
`branch@unavailable (analysis ref: branch)`. Recovery includes outage duration
and final failure count. Slack mention/control sequences from event values are
escaped. Bot display identity is configured on the Slack bot, not in producer
payloads.

## Producer contract

`POST /v2/alerts` accepts only:

- `version` (exactly `"2"`)
- `alert_id`, `fingerprint`, and `source`
- `status` (`firing` or `resolved`)
- `severity` (`critical`, `error`, `warn`, or `info`)
- `title`, `occurred_at`, `summary`, and `context`
- optional `deployment_sha` and `evidence_refs`

Fields are strictly validated, bounded, control-sanitized, and secret-redacted
before persistence or analysis. Direct user identifiers, remote IPs, and API
key prefixes are also redacted; producers should send aggregate affected-user
counts instead. `alert_id` is globally idempotent. The active incident key is
trusted environment plus fingerprint.

The gateway producer attempts V2 first only when both V2 variables are set. If
that attempt fails, existing V1 relay/webhook behavior remains available and
the legacy message begins with `[Relay fallback]`.

The status monitor has the same opt-in pair. Its initial and resolved
transitions can use existing fallbacks, while sustained firing updates are sent
only to V2. A failed sustained update therefore cannot produce direct Slack
repeat noise. D1 records whether V2 or a legacy path accepted firing; repeats
and recovery stay on that owner, and failed recovery retains state for the next
cycle. Existing unprefixed markers are legacy-owned.

## Analysis dispatch and callback

The relay places a generated `job_id` on a Cloudflare Queue. Its consumer
chooses the analysis ref:

- staging: the full deployment SHA only after GitHub proves it is an ancestor
  of `dev`; otherwise `dev`
- production: the full deployment SHA only after GitHub proves it is an
  ancestor of `main`; otherwise `main`
- local: `dev`

The chosen ref is stored in D1. GitHub `workflow_dispatch` receives only
`job_id`. The workflow definition runs from `dev` for staging/local and `main`
for production.

GitHub only registers a `workflow_dispatch` workflow when that workflow file
exists on the repository's default branch (`main`). Before the first staging
synthetic alert, merge a separate, dormant workflow-only bootstrap containing
`.github/workflows/codex-oncall-v2.yml` to `main`. Do not enable any producer
or Worker as part of that bootstrap. Staging dispatch still uses `ref=dev`, so
the executed workflow and checked-out application code come from the approved
`dev` revision.

The V2 workflow fetches the job with its separate workflow bearer token before
checkout. The response contains the sanitized alert, trusted environment,
trusted analysis ref, model, and model base URL. It contains no Slack token,
channel, or thread timestamp.

Checkout uses `persist-credentials: false` and repository permissions are
`contents: read`. Codex must pass its read-only sandbox self-check. The callback
helper rejects analysis unless the Codex JSONL proves both a successful
read-only command and a completed turn.

Success and failure callbacks go to
`POST /v2/jobs/{job_id}/complete`. A failure requires the Actions run URL. If
checkout or setup fails before repository code is available, the final
workflow step uses the runner's `curl` and `jq` to send the same failure shape.
The relay, not the workflow, writes the thread reply.

V2 has no issue, PR, merge, repository-write, configuration-write, or
operational-action capability.

All URLs that receive a V2 bearer token, GitHub token, or model API key must be
credential-free HTTPS URLs. Queue delivery uses three total attempts, matching
the Worker constant to Wrangler's `max_retries = 2`.

## Rollout

Follow `services/alert-relay-worker/README.md` to create D1 and Queue resources,
copy `wrangler.toml.example`, apply the migration, and set secrets. These are
manual future operations, not part of this PR.

After the dormant workflow bootstrap is present on `main` and the Worker and
workflow secrets exist, enable one producer environment by setting both
`ALERT_RELAY_V2_URL` and its environment-specific `ALERT_RELAY_V2_TOKEN`.
Validate:

1. one firing parent,
2. repeat parent updates with no thread noise,
3. one analysis reply,
4. one recovery reply and a Resolved parent,
5. a new parent after re-firing,
6. an explicit run-URL reply for a forced workflow failure.

Before rollback, let active V2 incidents resolve (or resolve them operationally)
while the V2 credential is still present. Removing the credential mid-incident
prevents the producer from closing the existing V2 Slack parent. Once no V2
incident is firing, unset either producer variable to return new alerts to the
V1-default path.
