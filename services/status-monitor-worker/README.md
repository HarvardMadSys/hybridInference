# status-monitor-worker

A Cloudflare Worker version of the status monitor. A **Cron Trigger** probes
every FreeInference model every 5 minutes, stores results in **D1**, and the
Worker serves a live dashboard plus JSON endpoints.

```text
Cron (*/5)  ─►  scheduled()  ─►  GET ${GATEWAY}/models (role-filtered)
                                      │
                                      ▼  probe each (chat / embeddings, X-Probe: synthetic)
                                   D1 (probe_results)
                                      ▲
HTTP  ─►  fetch()  ─►  /  (dashboard)  +  /api/status  +  /api/health
```

Targets are discovered from the gateway's authenticated `/models` catalog, which
already applies role and runtime visibility rules — so the Worker probes exactly
what the prober key can actually call.

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
3 free/pro, 10 internal/admin), `PROBE_HEADER`, `RETENTION_DAYS`.

`PROBER_API_KEY` is a **secret**, not a var.

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

# 4. Deploy (registers the Worker and its 5-minute cron trigger).
npx wrangler deploy
```

Trigger a probe cycle without waiting for cron:

```bash
npx wrangler dev          # then, in another shell:
curl "http://localhost:8787/__scheduled?cron=*/5+*+*+*+*"
```

## Test / typecheck

```bash
npm test          # vitest unit tests (SSE parsing, dashboard rendering)
npm run typecheck # tsc --noEmit
```
