# status-monitor-worker

A Cloudflare Worker version of the status monitor. A **Cron Trigger** probes
every FreeInference model every 20 minutes, stores results in **D1**, and the
Worker serves a live dashboard plus JSON endpoints.

```text
Cron (*/20)  ─►  scheduled()  ─►  GET ${GATEWAY}/models (role-filtered)
                                      │
                                      ▼  probe each (chat / embeddings, X-Probe: synthetic)
                                   D1 (probe_results)
                                      ▲
HTTP  ─►  fetch()  ─►  /  (dashboard)  +  /api/status  +  /api/health
```

Targets are discovered from the gateway's authenticated `/models` catalog, which
already applies role and runtime visibility rules — so the Worker probes exactly
what the prober key can actually call.

## Slack alerts and Codex on-call analysis

When `CODEX_ONCALL_RELAY_URL` and `CODEX_ONCALL_RELAY_TOKEN` are set, legacy-owned
alerts are sent to the on-call relay first. The relay posts the original Slack
message, runs a read-only Codex investigation asynchronously, and replies in the
same thread. If relay delivery fails, the Worker falls back to
`SLACK_WEBHOOK_URL`.

An individual model is considered unavailable after
`ALERT_FAILURE_THRESHOLD` **consecutive** failed probes (default **2**, i.e.
~two 20-minute cycles — enough to distinguish a sustained outage from a single
transient blip). Alerts are **edge-triggered**: a model pages once when it
crosses the threshold and again only after it recovers and fails anew, so a
multi-hour outage doesn't repeat the page every 20 minutes. A short recovery
notice is posted when the model's next probe succeeds. The per-model alert state
lives in the D1 `meta` table, so it survives Worker restarts and is never raced
(alert evaluation runs while the cycle holds its lock).

Every transition is committed to alert state only after its selected writer
confirms delivery, so a transient failure is retried on the next cron cycle
rather than being silently dropped.

Individual model down/recovery transitions use the unified alert Control Plane
through an internal named Service Binding. `ALERT_DEFAULT_OWNER=control-plane`
applies only to new individual incidents: incidents opened by the legacy
relay/webhook remain pinned there until recovery, while existing Control Plane
incidents also finish through the Control Plane after rollback. Rejected,
ambiguous, or unavailable RPC attempts retain the exact persisted body and
`event_id` for retry and never fall back to the relay or incoming webhook.
Alert-state changes, successful pending cleanup, and resolved owner release
commit in one D1 batch transaction.

Two whole-deployment cases are also covered:

- **Gateway-level outage.** If the gateway is unreachable (model discovery
  fails) or the prober key is rejected account-wide (expired key, unverified,
  over quota), no model can be probed — so a single **"Monitoring cycle
  failing"** page is sent (edge-triggered, with a matching recovery notice)
  instead of nothing.
- **Mass outage.** Control-plane-owned incidents always open **one incident
  per model**, whatever the batch size, so each model keeps its own thread and
  recovers independently (D1 decision, 2026-07-27). Only in the
  `ALERT_DEFAULT_OWNER=legacy` rollback mode does a cycle with more than
  `ALERT_STORM_THRESHOLD` models (default **5**) still collapse into one
  **"N models down"** / **"N models recovered"** webhook summary, because one
  text per model would flood the channel there.

## History retention and D1 read cost

Probe history is kept for `RETENTION_DAYS` (**1024**), so `probe_results` grows
for years and every statement that scans it gets steadily more expensive — D1
bills `rows_read` per row *scanned*, not per row returned. Two places are
deliberately shaped around that:

- **Pruning by age** (`prune`) filters on `checked_at`, which
  `idx_probe_results_checked_at` serves as a range scan.
- **Evicting removed models** (`reconcileModels`) diffs the new active set
  against the previous one recorded in `meta.model_ids`, then deletes the
  departed ids with `model_id IN (...)` — an index seek on
  `idx_probe_results_model_id`. An unchanged catalog, which is the case on almost
  every cycle, issues no `probe_results` statement at all.

  The equivalent one-liner, `DELETE ... WHERE model_id NOT IN (active ids)`,
  cannot use either index (SQLite will not satisfy a negated equality set from
  one) and full-scans the table. It survives only as a backstop for rows the
  diff structurally cannot name — a cycle that dies between `recordResults` and
  `reconcileModels` stores no id for what it just wrote — and runs at most once a
  day, plus immediately when `meta.model_ids` is missing or corrupt.

Similarly, dashboard reads resolve the model list from the single
`meta.model_ids` row rather than `SELECT DISTINCT model_id`. When adding a query
here, check whether its predicate can actually be served by an index before
putting it on the cron path.

## Endpoints

| Path | Description |
|---|---|
| `/` | Auto-refreshing HTML dashboard |
| `/api/status` | Full snapshot (latest + history per model) as JSON |
| `/api/health` | `total` / `healthy` / `unhealthy` summary, plus `pendingControlPlaneTransitions` — a count that stays above zero across cycles (~20 min) means Control Plane submissions are being rejected and individual model alerting is stalled |

### Dashboard zoom-in

Each model card shows the latest metrics, a latency sparkline, and a TTFT trend.
**Click a card (or focus it and press Enter/Space) to zoom in** — this opens a
detail overlay with full-size time-series charts for **latency** (ms),
**throughput** (tok/s), and **time to first token** (ms), each with
min / avg / max / latest summaries and per-point hover tooltips. Down-probes are
highlighted in red and gaps are left where a probe was skipped. Close with the
✕ button, by clicking outside the card, or with `Esc`.

The history is embedded from the same snapshot the cards use, so the zoom view
needs no extra request. The page's periodic refresh is paused while a detail
view is open and resumes on close (a `<noscript>` fallback keeps auto-refresh
working without JavaScript).

## Configuration

`wrangler.toml` `[vars]`: `GATEWAY_BASE_URL`, `PROBE_PROMPT`, `PROBE_MAX_TOKENS`,
`MAX_CONCURRENCY` (keep at/below the prober account's gateway concurrency cap —
3 free/pro, 10 internal/admin), `PROBE_HEADER`, `RETENTION_DAYS`,
`ALERT_FAILURE_THRESHOLD` (consecutive failed probes before a model pages Slack),
`ALERT_STORM_THRESHOLD` (models changing state in one cycle before pages collapse
into a summary), and `ALERT_DEFAULT_OWNER` (`control-plane` for new individual
model incidents; set to `legacy` to roll back new ownership).

`CF_VERSION_METADATA` and `ALERT_CONTROL_PLANE` are non-secret platform
bindings. The latter targets the Control Plane's
`StatusMonitorProducerEntrypoint`; it does not add a public URL or bearer token.

`PROBER_API_KEY` is a **secret**, not a var. `CODEX_ONCALL_RELAY_URL`,
`CODEX_ONCALL_RELAY_TOKEN`, and `SLACK_WEBHOOK_URL` are optional secrets. Both
relay values are required to enable on-call analysis; retain the Slack webhook as its
delivery fallback.

## Deploy

CI deploys this Worker automatically: `.github/workflows/deploy-status-monitor.yml`
runs `npm test` and `wrangler deploy` whenever a change under
`services/status-monitor-worker/**` lands on `dev` (or via manual
**workflow_dispatch**). It needs a single repository secret,
`CLOUDFLARE_API_TOKEN` (a Cloudflare API token with Workers Scripts edit +
D1 access), the staging environment variable
`ALERT_CONTROL_PLANE_STAGING_URL`, and GitHub's job-scoped OIDC token. After
deployment it reads the exact Wrangler `version_id`, registers that immutable
version as `service=status-monitor`, `source=status-monitor`,
`principal=staging-monitor`, then runs a firing/resolved RPC through a
runner-local Service Binding gate. The gate listens only on `127.0.0.1`; only
its binding is proxied to the deployed staging Control Plane, and neither the
gate nor a public route is deployed.

Wrangler is pinned to a reviewed 4.x release (remote bindings became stable in
4.37.0) so gate code stays local while its Service Binding reaches staging.
Do not replace the gate with legacy `wrangler dev --remote`, which uploads the
disposable gate Worker to a preview environment. The workflow does not run D1
migrations — apply schema changes once, manually (step 2 below), before they
ship.

An out-of-band manual deployment is safe while the owner remains `legacy`, but
its version is not trusted to submit Control Plane events until the reviewed CI
workflow attests that exact version.

To deploy by hand (first-time setup or out-of-band):

```bash
cd services/status-monitor-worker
npm install

# 1. Create the D1 database (once) and copy the printed database_id into
#    wrangler.toml under [[d1_databases]].
npx wrangler d1 create freeinference-monitor

# 2. Apply the schema.
npx wrangler d1 migrations apply freeinference-monitor --remote

# 3. Set the prober API key (internal/admin key recommended).
npx wrangler secret put PROBER_API_KEY

# 3a. (Optional) Set the Slack incoming-webhook URL to enable failure alerts.
npx wrangler secret put SLACK_WEBHOOK_URL

# 3b. (Optional) Send alerts through Codex on-call before the Slack fallback.
npx wrangler secret put CODEX_ONCALL_RELAY_URL
npx wrangler secret put CODEX_ONCALL_RELAY_TOKEN

# 4. Deploy (registers the Worker and its 20-minute cron trigger).
npx wrangler deploy
```

Trigger a probe cycle without waiting for cron:

```bash
npx wrangler dev          # then, in another shell:
curl "http://localhost:8787/__scheduled?cron=*/20+*+*+*+*"
```

## Test / typecheck

```bash
npm test          # vitest unit tests (SSE parsing, dashboard rendering)
npm run typecheck # tsc --noEmit
```
