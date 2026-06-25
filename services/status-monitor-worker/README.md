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

## Slack alerts

When the optional `SLACK_WEBHOOK_URL` secret is set, each cron cycle pages a
Slack incoming webhook for any model that has failed `ALERT_FAILURE_THRESHOLD`
**consecutive** probes (default **2**, i.e. ~two 20-minute cycles — enough to
distinguish a sustained outage from a single transient blip). Alerts are
**edge-triggered**: a model pages once when it crosses the threshold and again
only after it recovers and fails anew, so a multi-hour outage doesn't repeat the
page every 20 minutes. A short recovery notice is posted when the model's next
probe succeeds. The per-model alert state lives in the D1 `meta` table, so it
survives Worker restarts and is never raced (alert evaluation runs while the
cycle holds its lock). Leave `SLACK_WEBHOOK_URL` unset to disable alerting
entirely.

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
`ALERT_FAILURE_THRESHOLD` (consecutive failed probes before a model pages Slack).

`PROBER_API_KEY` is a **secret**, not a var. `SLACK_WEBHOOK_URL` is an optional
**secret** — set it to enable Slack alerting (see above), leave it unset to
disable.

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
