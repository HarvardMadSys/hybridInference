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

## Configuration

`wrangler.toml` `[vars]`: `GATEWAY_BASE_URL`, `PROBE_PROMPT`, `PROBE_MAX_TOKENS`,
`MAX_CONCURRENCY` (keep at/below the prober account's gateway concurrency cap —
3 free/pro, 10 internal/admin), `PROBE_HEADER`, `RETENTION_DAYS`.

`PROBER_API_KEY` is a **secret**, not a var.

## Deploy

```bash
cd services/status-monitor-worker
npm install

# 1. Create the D1 database (once) and copy the printed database_id into
#    wrangler.toml under [[d1_databases]].
npx wrangler d1 create status_monitor

# 2. Apply the schema.
npx wrangler d1 migrations apply status_monitor --remote

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
