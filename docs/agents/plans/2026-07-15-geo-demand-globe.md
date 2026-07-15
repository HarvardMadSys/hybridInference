# Geo-Temporal Demand Globe — Productization Plan (PR A + PR B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the standalone geo-temporal demand globe (research instrument, implemented as ops tooling on this branch) into the freeinference product as an admin page at `/dashboard/admin/analytics/geo`, backed by a live admin API — **without touching the `api_logs` schema or the request/logging write path**.

**Architecture:** Two sequential, separately reviewable PRs. **PR A (backend):** a read-only admin endpoint `GET /admin/analytics/geo` that scans the requested `api_logs` window, resolves `metadata->>'ip'` through offline GeoLite2 databases at query time, aggregates into the already-validated `data.json` contract, and caches the result in-process (hourly refresh). **PR B (frontend, based on PR A):** a Next.js sub-route `analytics/geo` hosting a React port of the globe (route-level code split keeps d3/atlas out of the base Analytics bundle), plus a lightweight Geography entry card on the Analytics landing. A future phase (deliberately deferred) would move geo resolution to log time via three `api_logs` columns; its triggers are listed at the end — do not implement it as part of this plan.

**Tech Stack:** Python 3.12, FastAPI, asyncpg/Postgres, `maxminddb` (new backend dependency), Next.js 15.5, React 18, TypeScript, d3 + topojson-client (new frontend dependencies), pytest, vitest.

**Spec:** inlined below (Background, Design decisions, Data contract). No separate spec file.

---

## Background & existing assets

Everything below already exists on branch `claude/cross-region-inference-survey-377dbd`
(worktree `.claude/worktrees/competent-curran-6886d3`); cherry-pick or rebase onto `dev` when starting.

| Asset | Where | Commit |
|---|---|---|
| Hourly geo exporter (offline research path) | [ops/db/analysis/geo_hourly_export.py](../../../ops/db/analysis/geo_hourly_export.py) | `0664fbae` |
| Standalone globe viewer (research instrument — **keep, do not delete**) | [ops/db/analysis/geo_globe.html](../../../ops/db/analysis/geo_globe.html) | `0664fbae` + restyle `8f6a3dbd` |
| Usage docs (workflow §5) | [ops/db/analysis/README.md](../../../ops/db/analysis/README.md) | `0664fbae` |
| Original single-file prototype (historical reference, synthetic data) | `~/.codex/visualizations/2026/07/15/019f645a-c69c-7fc1-913d-9072a2e508cb/global-traffic-globe.html` | not in repo |

Facts verified against the real system (2026-07-15):

- Client IP lives at `metadata->>'ip'` on `api_logs` for all three surfaces —
  [completions.py](../../../apps/backend/serving/servers/routers/completions.py) (`"ip": get_client_ip(request)`),
  [anthropic_messages.py](../../../apps/backend/serving/servers/routers/anthropic_messages.py) (also `ip_source`),
  [embeddings.py](../../../apps/backend/serving/servers/routers/embeddings.py).
  The admin requests page already reads it the same way ([admin/metrics.py](../../../apps/backend/serving/servers/routers/admin/metrics.py), `l.metadata->>'ip'`).
- Useful `api_logs` columns: `timestamp TIMESTAMPTZ`, `provider`, `served_endpoint_id`, `model_id`,
  `prompt_tokens`, `completion_tokens`, `latency_ms`, `ttft_ms`, `status_code`, `error`, `user_id`,
  `metadata JSONB` ([log_schema.py](../../../apps/backend/serving/storage/log_schema.py)).
- Exporter SQL runs clean against the real schema (validated on a schema-true dev DB).
- DB access conventions for scripts: `.env` / `DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME`
  (same as [ops/db/export_logs.py](../../../ops/db/export_logs.py)).
- GeoLite2 `.mmdb` files are **not** on any server yet. PR A adds `maxminddb` as a project
  dependency; deploying the database files remains an operational prerequisite.
- Admin tab nav supports sub-routes: active state uses `pathname.startsWith(href + '/')`
  ([AdminTabNav.tsx](../../../apps/frontend/src/components/features/admin/AdminTabNav.tsx)),
  and the Analytics tab is a thin wrapper
  ([analytics/page.tsx](../../../apps/frontend/src/app/dashboard/admin/(tabs)/analytics/page.tsx)) —
  so `analytics/geo/page.tsx` slots in with zero nav work.

## Design decisions (carried over from the research phase — do not silently reverse)

1. **Origin = network origin.** IP-based, never claimed as user residence. UI copy must say
   "origin (IP-based)"; local times derived from country centroid longitude are labeled `~approx`.
2. **The primary analysis is continent x time, across all traffic.** Traffic classes remain a
   secondary ASN-based robustness diagnostic: `nondc` / `dc` / `internal` / `unknown` (keyword
   list over GeoLite2-ASN org names). The default product view is `all`; the class filter belongs
   in an advanced/research control and must not compete visually with continent, time, or metric.
   `dc` means datacenter-origin network and `nondc` means non-datacenter-origin network — neither
   class identifies a human or an agent, and neither may be labeled as such in UI or paper claims.
3. **External API providers get no map location.** We do not know where DeepSeek/OpenRouter GPUs
   sit; they render in a side rail ("no location claimed"), never as globe nodes. Only `local`
   providers (hand-maintained site map) get nodes and inbound arcs.
4. **Pooling potential (range)** = `1 − global_peak / Σ per-continent peaks` on the selected
   metric+class. **Transferable now** = `min(Σ overflow, Σ slack) / Σ demand` with per-continent
   mean over the range as the capacity proxy (stated in the tooltip; replace with measured
   capacity when donated-GPU telemetry exists).
5. **Aggregates only ever leave the DB**: counts, token sums, latency percentiles, distinct-user
   counts. No raw IPs, no user ids, no prompts in any API response or exported file.
6. **Continent colors are fixed slots** of the CVD-validated dark categorical palette
   (AS `#3987e5`, NA `#199e70`, EU `#c98500`, SA `#008300`, AF `#9085e9`, OC `#e66767`,
   unknown `#898781`); color follows the entity across filters. Ribbon carries line-end direct
   labels as the secondary encoding. The globe stage stays dark ("instrument panel") in the
   product; surrounding chrome adopts the app theme.
7. **Why query-time GeoIP (PR A) instead of log-time columns (PR C):** the globe only needs a read
   endpoint; the column migration is the riskiest kind of change in this repo (the five-place
   `api_logs` sync — see PR C) and its real payoffs (SQL geo filters on the Requests tab, IP
   retention/TTL policy, frozen-at-observation resolution) are not prerequisites for this page.

## Data contract (existing `data.json` shape → PR A response body)

Produced today by `geo_hourly_export.py`; consumed today by `geo_globe.html`. PR A preserves this
columnar shape while adding serving-endpoint attribution to flow rows. The viewer resolves columns
by `flow_cols`, so it remains compatible via `geo_globe.html?data=/admin/analytics/geo`.

```jsonc
{
  "meta": {
    "source": "api_logs",              // or "synthetic-demo"
    "generated_at": "ISO-8601",
    "start": "ISO-8601 first hour",
    "hours": 720,
    "rows_total": 123456,
    "rows_with_ip": 120000,
    "geoip": {"country": true, "asn": true},
    "degraded": false,
    "degraded_reasons": [],
    "unmapped_alpha2": [],
    "notes": ["..."]
  },
  "classes": ["nondc", "dc", "internal", "unknown"],
  "bucket_cols": ["c","cc","cont","cls","n","err","users","tin","tout","gs","p50","p90"],
  "flow_cols": ["c","cls","p","e","n"],
  "providers": [{
    "id": "vllm",
    "label": "Local cluster (vLLM)",
    "kind": "local",
    "region": "us-east",
    "cont": "NA",
    "coord": [-71.09, 42.36]
  }],
  "hours_index": ["ISO-8601", "..."],   // one entry per hour, gaps included
  "hours": [{"b": [[...bucket rows]], "f": [[...flow rows]]}]
}
```

Row semantics: `c` ISO-3166 alpha-3 (`?XX` when unmapped, `?` when unknown), `cc` alpha-2, `cont`
MaxMind continent code, `cls` ASN-derived network class (retained for optional robustness analysis,
not user-type inference), `n` requests, `err` errored requests, `users` distinct non-null
`user_id`s, `tin`/`tout` prompt/completion tokens, `gs` Σ `latency_ms`/1000 (compute-time estimate),
`p50`/`p90` TTFT ms (nearest-rank, null when no samples). In flow rows, `p` is the provider label
and `e` is `served_endpoint_id` (falling back to `p` for older rows); flows from distinct serving
endpoints are not merged even when they share a provider.

---

## Conventions for this plan

- Branch off `dev`. Use a git worktree. PR target: `dev`; verify on staging before claiming done.
- Run `make format && make test` after each backend task; frontend tasks run
  `npm --prefix apps/frontend run lint`, `type-check`, and `test`.
- Keep implementation commits focused. PR A = Tasks 1–3 plus the Task 4 staging gate; PR B =
  Tasks 5–7. Task 4 is operational evidence and does not require a code commit.
- Update the route snapshot test deliberately in the endpoint commit when adding the route (a new
  route changes the count — the PR #888 lesson; do not leave it as an unexplained drive-by).

---

## PR A — live admin geo endpoint (backend only, no schema change)

### Task 1: `GeoResolver` as a serving util

**Files:**
- New: `apps/backend/serving/utils/geo_resolver.py`
- Modify: `pyproject.toml` and `uv.lock` (add `maxminddb` as a project dependency)
- Test: `tests/unit/utils/test_geo_resolver.py` (new)

- [x] Port `GeoResolver`, `ALPHA2_TO_ALPHA3`, and `DC_ASN_KEYWORDS` from
  [ops/db/analysis/geo_hourly_export.py](../../../ops/db/analysis/geo_hourly_export.py) into the
  util (the exporter should import from the util afterwards — one implementation, two callers).
- [x] Lazy-open readers from `GEOIP_COUNTRY_DB` / `GEOIP_ASN_DB` env vars; missing files or missing
  `maxminddb` degrade to `country='?' / cls='unknown'` and set a `degraded` flag (never crash the
  admin page).
- [x] Unit tests with an injected fake reader: private/loopback → `internal`; DC keyword match →
  `dc`; unmapped alpha-2 → `?XX` + recorded in `unmapped_a2`; cache hit path.

### Task 2: aggregation service + `GET /admin/analytics/geo`

**Files:**
- New: `apps/backend/serving/analytics/geo_demand.py` (window scan → contract dict)
- Modify: `apps/backend/serving/servers/routers/admin/analytics.py` (or sibling router file)
- Test: `tests/unit/analytics/test_geo_demand.py`, `tests/api/admin/test_geo_endpoint.py`

- [x] Aggregation: stream `SELECT date_trunc('hour', timestamp), metadata->>'ip', provider,
  served_endpoint_id, prompt_tokens, completion_tokens, latency_ms, ttft_ms,
  (error IS NOT NULL OR COALESCE(status_code,200) >= 400), user_id FROM api_logs
  WHERE timestamp >= $1 AND timestamp < $2`
  with a server-side cursor; resolve IPs via `GeoResolver` (per-IP cache); emit the contract above.
  Reuse/port `_Bucket`, `_percentile`, `_finalize` from the exporter rather than re-deriving.
- [x] Query params: `days` (default 14, max 90), optional `since`/`until` ISO. Admin-role auth,
  same dependency as the sibling admin analytics endpoint.
- [x] **In-process cache**: key `(since_floor, until_floor)` quantized to the hour; TTL 1h;
  a single-flight task so concurrent dashboard opens don't fan out N scans. Response carries
  `meta.generated_at` so the UI can show staleness.
- [x] Provider site metadata: move `PROVIDER_SITES` from the exporter into the util module
  (single source; exporter imports it). Follow-up (out of scope): promote to `config/`.
- [x] Update the route snapshot test deliberately with the endpoint.

### Task 3: exporter dedup

**Files:**
- Modify: `ops/db/analysis/geo_hourly_export.py`

- [x] Replace the exporter's inlined resolver/site tables with imports from
  `serving.utils.geo_resolver`; `--demo` and CSV behavior unchanged. Re-run
  `uv run python ops/db/analysis/geo_hourly_export.py --demo --out /dev/null` as regression.

### Task 4: staging verification (gate for merging PR A)

- [ ] Provision GeoLite2 on staging: free MaxMind account → download `GeoLite2-Country.mmdb` +
  `GeoLite2-ASN.mmdb` to `/srv/geoip/`; set `GEOIP_COUNTRY_DB` / `GEOIP_ASN_DB` in the service env.
  **The `.mmdb` files must not enter the repo** (MaxMind EULA); add a weekly refresh cron later.
- [ ] Hit `/admin/analytics/geo?days=7` on staging (test account `admin@admin.com`); confirm
  latency of the cold scan and the cached hit; confirm `meta.geoip` flags true.
- [ ] Fetch the aggregate with an authenticated request (for example, `curl -H 'Authorization:
  Bearer ...' '<staging>/admin/analytics/geo?days=7' -o data.json`), then serve `data.json` beside
  `ops/db/analysis/geo_globe.html` locally. A localhost viewer cannot rely on staging cookies and
  its bare cross-origin `fetch()` may be blocked by CORS. This is the zero-frontend real-data
  go/no-go artifact.

## PR B — frontend Geography page

### Task 5: dependencies + vendored atlas

**Files:**
- Modify: `apps/frontend/package.json` (`d3`, `topojson-client`, and their types)
- New: `apps/frontend/public/atlas/countries-110m.json` (vendored — no esm.sh/CDN at runtime)

- [ ] Vendor the world atlas topology (alpha-3 `properties.id`, same as the standalone viewer
  verified) and load it with `fetch('/atlas/countries-110m.json')`.

### Task 6: `GeoGlobe` component + sub-route

**Files:**
- New: `apps/frontend/src/app/dashboard/admin/(tabs)/analytics/geo/page.tsx`
- New: `apps/frontend/src/components/features/admin/geo/GeoGlobe.tsx` (+ small subcomponents:
  `TrafficRibbon`, `ExternalApiRail`, `GeoStatCards`)
- Modify: `apps/frontend/src/lib/api/admin.ts` (typed fetcher for `/admin/analytics/geo`)
- Test: vitest for the pure helpers (series/pooling/transferable math), snapshot-light for markup

- [ ] Port from [ops/db/analysis/geo_globe.html](../../../ops/db/analysis/geo_globe.html): d3 owns
  the SVG interior inside a ref'd `<svg>`; React owns state (hour, class, metric, selection) and
  the chrome. Pooling/transferable/series builders move to a pure TS module (unit-testable).
- [ ] Keep: timeline scrub + play, declination-correct day/night terminator, metric select, fixed
  continent palette + ribbon direct labels, external-API rail, detail panel, DEMO badge honored
  from `meta.source`, and `prefers-reduced-motion`. Keep the traffic-class filter as a visually
  subordinate advanced/research control, defaulted to `all`; do not describe it as human/agent.
- [ ] Adapt: cards/typography/buttons to the app's design tokens; the globe stage may stay dark.
- [ ] Loading/error/staleness states (`meta.generated_at`), and an "unlocated %" stat — keep the
  honesty affordances.

### Task 7: Analytics landing entry card

**Files:**
- Modify: `apps/frontend/src/app/dashboard/admin/AnalyticsTab.tsx`

- [ ] Add a compact Geography entry card with an "Open globe →" link to `analytics/geo`. Do not
  fetch the full 14-day globe payload from the Analytics landing merely to show a KPI: the server
  cache avoids a repeated DB scan but not cold-scan latency, serialization, or network transfer.
  Add a KPI later only with a dedicated lightweight summary/rollup endpoint.

---

## Operational prerequisites (not code)

- [ ] GeoLite2 on prod + staging (`/srv/geoip/`, env vars, weekly refresh cron; EULA: files stay
  out of the repo).
- [ ] Deploy the updated project environment containing the Task 1 `maxminddb` dependency.
- [ ] **Go/no-go before investing in PR B:** run the offline exporter (or the PR A endpoint) on
  prod for ≥14 days of history and check whether all-traffic demand shows time-zone-separated
  peaks across continents. Then repeat after excluding `dc` and `unknown` as a robustness check;
  report any difference as a network-origin effect, not a human/agent effect. If the primary
  continent x time signal collapses, stop at PR A (the endpoint still powers future geo analytics)
  and reassess.

## Future phase — deferred: log-time geo enrichment (do NOT do now)

Triggers that justify it (any one): Requests-tab **filtering** by origin (`WHERE origin_continent=…`),
an IP retention/TTL policy (keep coarse geo, delete raw IPs), reproducible frozen-at-observation
resolution for the paper dataset, or scan cost outgrowing the hourly cache.

Scope when triggered: three columns `origin_country_code` / `origin_continent_code` /
`origin_asn_class`; enrichment at the single logging choke point (not per-router); backfill script
reusing `GeoResolver`. **Five-place sync checklist** (the historical split-brain incident):
[log_schema.py](../../../apps/backend/serving/storage/log_schema.py) (CREATE TABLE + migration
list) + both hand-duplicated INSERTs (`postgres_log.py`, `database.py`) + the LogStore ABC +
`ops/db/export_logs.py` alignment.

## Out of scope (all phases)

Public impact page (needs k-anonymity), Providers-tab geography, what-if routing simulator,
`geo_stats_hourly` rollup table, donated-GPU capacity overlay, real per-request serving-region
attribution for external APIs.
