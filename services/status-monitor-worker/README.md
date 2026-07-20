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

Unified Alert Control Plane V2 is opt-in when both `ALERT_RELAY_V2_URL` and
`ALERT_RELAY_V2_TOKEN` are set. Its payload contains no producer environment or
Slack text. The V2 relay derives environment from the credential and owns Slack
rendering. While an incident stays down, each cycle sends a V2 repeat so the
relay can update the existing parent's count and last-seen time. A failed
repeat is never sent to V1 or the direct webhook, so it cannot create Slack
repeat noise. The URL must be credential-free HTTPS; an unsafe URL is treated
as unconfigured and leaves V1/webhook behavior unchanged.

When `CODEX_ONCALL_RELAY_URL` and `CODEX_ONCALL_RELAY_TOKEN` are set, each alert
uses the legacy V1 on-call relay. The relay posts the original Slack message,
runs a read-only Codex investigation asynchronously, and replies in the same
thread. If relay delivery fails, the Worker falls back to `SLACK_WEBHOOK_URL`.
When V2 is configured it is attempted before these legacy paths; a transition
that falls back is visibly prefixed `[Relay fallback]`.

With only the optional `SLACK_WEBHOOK_URL` secret set, each cron cycle pages the
Slack incoming webhook directly for any model that has failed `ALERT_FAILURE_THRESHOLD`
**consecutive** probes (default **2**, i.e. ~two 20-minute cycles — enough to
distinguish a sustained outage from a single transient blip). Alerts are
**edge-triggered**: a model pages once when it crosses the threshold and again
only after it recovers and fails anew, so a multi-hour outage doesn't repeat the
page every 20 minutes. A short recovery notice is posted when the model's next
probe succeeds. The per-model alert state lives in the D1 `meta` table, so it
survives Worker restarts and is never raced (alert evaluation runs while the
cycle holds its lock). Leave both delivery paths unset to disable alerting.

Every page is committed to the alert state only **after** either the relay or
fallback Slack POST is confirmed delivered, so a transient failure is retried
on the next cron cycle rather than being silently dropped.

Two whole-deployment cases are also covered:

- **Gateway-level outage.** If the gateway is unreachable (model discovery
  fails) or the prober key is rejected account-wide (expired key, unverified,
  over quota), no model can be probed — so a single **"Monitoring cycle
  failing"** page is sent (edge-triggered, with a matching recovery notice)
  instead of nothing.
- **Mass outage (storm cap).** When more than `ALERT_STORM_THRESHOLD` models
  (default **5**) change state in the same cycle — e.g. a provider-wide blip —
  the individual pages collapse into one **"N models down"** / **"N models
  recovered"** summary so the channel isn't flooded.

## Endpoints

| Path | Description |
|---|---|
| `/` | Auto-refreshing HTML dashboard |
| `/api/status` | Full snapshot (latest + history per model) as JSON |
| `/api/health` | `total` / `healthy` / `unhealthy` summary |

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
into a summary).

`PROBER_API_KEY` is a **secret**, not a var. `ALERT_RELAY_V2_URL`,
`ALERT_RELAY_V2_TOKEN`, `CODEX_ONCALL_RELAY_URL`,
`CODEX_ONCALL_RELAY_TOKEN`, and `SLACK_WEBHOOK_URL` are optional secrets. Both
values in either relay pair are required. `DEPLOYMENT_SHA` is optional immutable
build metadata included only in V2 events. No V2 value is present in the live
`wrangler.toml`; this PR therefore leaves deployed behavior unchanged.

## Deploy

CI deploys this Worker automatically: `.github/workflows/deploy-status-monitor.yml`
runs `npm test` and `wrangler deploy` whenever a change under
`services/status-monitor-worker/**` lands on `dev` (or via manual
**workflow_dispatch**). It needs a single repository secret,
`CLOUDFLARE_API_TOKEN` (a Cloudflare API token with Workers Scripts edit +
D1 access); the account is pinned via `account_id` in `wrangler.toml`. The
workflow does not run D1 migrations — apply schema changes once, manually
(step 2 below), before they ship.

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

# 3c. (Future opt-in) Enable V2 only after its Worker/resources are approved.
npx wrangler secret put ALERT_RELAY_V2_URL
npx wrangler secret put ALERT_RELAY_V2_TOKEN

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
