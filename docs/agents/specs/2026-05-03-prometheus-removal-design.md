# Prometheus Removal & Slack-Based Alerting — Design

**Date:** 2026-05-03
**Status:** Draft → ready for plan
**Author:** Architecture review follow-up (issue #1 of 6)

## Problem

The hybridInference codebase has near-zero working observability:

- All metrics in [serving/observability/metrics.py](../../../serving/observability/metrics.py) are no-op shims (~40 symbols left over after `prometheus-client` was removed). Callsites still emit labels but they are discarded.
- Prometheus scrape is disabled at [deploy/prometheus/prometheus.yml:33-40](../../../deploy/prometheus/prometheus.yml#L33-L40); rule files were dropped in commit `3d1721a`.
- Alertmanager 0.31.1 still runs in `docker-compose`, but only routes 3 hard-coded alerts (`ServiceDown|ServiceUnreachable|DatabaseDisconnected`); everything else blackholes.
- Recent direction (commit `c568fc1` "Slack alert when failed requests exceed threshold") shows the team is moving toward direct in-process Slack alerting.
- Production incidents are invisible until a human notices.

We want Prometheus and Alertmanager **gone**, replaced by a small in-process alerting framework that delivers Slack notifications based on rules over structured request logs and on direct state-change events.

## Goals

1. Delete all Prometheus, Alertmanager, and `alert-logger` artifacts (code, configs, Docker services, systemd units, docs).
2. Introduce a reusable in-process Slack alert helper any code path can call.
3. Introduce an `AlertEngine` that consumes structured request log records and runs rolling-window threshold rules, plus periodic SQL tickers for cost-based alerts.
4. Ship 9 initial alerts covering provider health, reliability, security, capacity, and cost.
5. Each step is mergeable as an independent PR; full rollout in 5 PRs.

## Non-goals

- Reintroducing Prometheus, OpenTelemetry, Datadog, or any external metrics backend.
- Distributed tracing.
- Replacing the existing structured request log pipeline.
- Cross-host alert state coordination (single-process alerter is fine for current scale).
- Designing the implementation order in detail — that belongs in the writing-plans output.

## Architecture

### Approach

**Hybrid (rules over logs + direct state-change calls + periodic SQL):**

Three alert sources, all funneling into the same `alert_slack(...)` helper:

- **Rule-based alerts** (5 of 9): `AlertEngine` consumes a stream of structured log records via a `logging.Handler`, maintains rolling-window counters keyed by `(rule, dimension)`, and fires when thresholds cross.
- **State-change alerts** (2 of 9): in-line callsites directly `await alert_slack(...)` — circuit-open transitions, DB disconnects.
- **Periodic SQL alerts** (2 of 9): `AlertEngine` runs scheduled tickers (every 5 min) that query stores and fire on threshold-crossing rows — user cost overrun, per-provider hourly spend.

All three share the single `alert_slack(...)` helper and its dedupe/cooldown machinery.

Approaches considered and rejected:

- **Pure log-rule** (everything as a log event the rule engine sees): forces state-change events into a log-record shape, which is awkward for things like circuit-state transitions.
- **Explicit in-process event bus** (typed `Event` classes + pub/sub): cleanest in theory, but adds a new abstraction with only one publisher pattern in practice. Not worth the maintenance.

### New modules

Three new modules under `serving/observability/`:

| Module | Responsibility | Approx LOC |
|---|---|---|
| `alerts.py` | `alert_slack(severity, title, context, dedupe_key=None, cooldown_sec=300)` async helper. Posts to Slack webhook, dedupes by key, severity → emoji/prefix. Single sink for all alerts. | ~120 |
| `log_handler.py` | `AlertingLogHandler(logging.Handler)` — captures structured log records, pushes onto bounded `asyncio.Queue` (drop-oldest on overflow). | ~60 |
| `alert_rules.py` | `AlertEngine` async task — drains the queue, maintains rolling-window counters, runs periodic SQL tickers, fires `alert_slack(...)` on threshold crossings. | ~250 |

### Lifecycle

- `AlertEngine` started in [serving/servers/bootstrap.py](../../../serving/servers/bootstrap.py) on app startup; cancelled on shutdown.
- `AlertingLogHandler` attached to root logger in [serving/servers/app.py](../../../serving/servers/app.py) after the structured-log JSON formatter is installed (so handler sees the same records that hit JSON output).
- If `SLACK_ALERTS_WEBHOOK_URL` is unset → log once at startup; `alert_slack(...)` becomes a no-op. Tests and dev environments work without configuring Slack.

### Data flow

**Rule-based:**

```
request → middleware emits structured log record
       → AlertingLogHandler captures → asyncio.Queue
       → AlertEngine drains queue → updates rolling windows
       → on threshold cross → alert_slack(...) → Slack webhook
```

**State-change:**

```
callsite (e.g. _CircuitBreaker.transition_to_open)
       → await alert_slack(severity, title, context, dedupe_key)
       → cooldown check → Slack webhook
```

**Periodic SQL (cost alerts):**

```
AlertEngine ticker (every 5 min)
       → SQL on operational store / hourly logs
       → for each row exceeding threshold → alert_slack(...)
```

## The 9 alerts

### Rule-based (consumed from request log records)

| # | Alert | Window | Threshold | Min samples | Cooldown | Severity | Context to Slack |
|---|---|---|---|---|---|---|---|
| 1 | Failed-request rate (port from #331) | 5 min | non-2xx > 5% | 50 | 15 min | error | rate, total, top error types, top providers |
| 2 | 5xx rate spike | 5 min | 5xx > 2% | 50 | 15 min | error | rate, total, top providers, top status codes |
| 3 | p95 latency per provider | 10 min | p95 > 30s (overridable per provider) | 30 | 30 min per provider | warn | provider, p95, p99, sample count |
| 4 | Auth failure spike | 1 min | count > 50 | n/a (count rule) | 10 min | warn | count, top source IPs, top key prefixes |
| 5 | Concurrency-exhausted | 5 min | count > 100 | n/a (count rule) | 30 min | warn | count, top users, top roles |

### State-change (direct in-line `alert_slack` calls)

| # | Alert | Where wired in | Trigger | Cooldown / dedupe key | Severity |
|---|---|---|---|---|---|
| 6 | Provider circuit-open | [routing/routers.py](../../../routing/routers.py) inside `_CircuitBreaker` state transition | CLOSED→OPEN or HALF_OPEN→OPEN | 5 min per `circuit_open:{endpoint_id}` | error |
| 7 | DB disconnect (port existing) | [serving/storage/database.py](../../../serving/storage/database.py) on conn failure | Health check fails / pool exhausted | 5 min per `db_disconnect:{db_kind}` | critical |

### Periodic SQL (AlertEngine tickers)

| # | Alert | Tick interval | Query target | Trigger | Cooldown / dedupe key | Severity |
|---|---|---|---|---|---|---|
| 8 | User cost overrun | 5 min | operational store | any user with `daily_cost > threshold[role]` | 24 h per `cost_overrun:{user_id}:{date}` | warn |
| 9 | Per-provider hourly spend | 5 min | hourly request logs | any provider with `hourly_spend > budget[provider]` | 1 h per `provider_spend:{provider}:{hour}` | warn |

### Slack message format (consistent across all alerts)

```
🚨 [error] Failed-request rate exceeded
At: 2026-05-03T12:34:56Z  Host: prod-1
Rate: 8.4% (42 of 500 requests, last 5 min)
Top providers: openai (60%), anthropic (30%)
Top errors: 502 Bad Gateway (28), 503 Service Unavailable (10)
```

Severity emoji: `critical`=🚨, `error`=❌, `warn`=⚠️, `info`=ℹ️.

## Configuration

### `config/alerts.yaml`

Loaded at startup, validated with Pydantic. Schema:

```yaml
rules:
  failed_request_rate:
    enabled: true
    window_sec: 300
    threshold_pct: 5.0
    min_samples: 50
    cooldown_sec: 900
  fivexx_rate:
    enabled: true
    window_sec: 300
    threshold_pct: 2.0
    min_samples: 50
    cooldown_sec: 900
  p95_latency_per_provider:
    enabled: true
    window_sec: 600
    threshold_ms: 30000
    min_samples: 30
    cooldown_sec: 1800
    overrides:
      some_slow_provider: { threshold_ms: 60000 }
  auth_failure_spike:
    enabled: true
    window_sec: 60
    threshold_count: 50
    cooldown_sec: 600
  concurrency_exhausted:
    enabled: true
    window_sec: 300
    threshold_count: 100
    cooldown_sec: 1800

state_changes:
  circuit_open:
    enabled: true
    cooldown_sec: 300
  db_disconnect:
    enabled: true
    cooldown_sec: 300

cost:
  user_overrun:
    enabled: true
    check_interval_sec: 300
    cooldown_sec: 86400
    thresholds_per_role:
      free: 5.00
      pro: 50.00
      internal: 500.00
  provider_hourly_spend:
    enabled: true
    check_interval_sec: 300
    cooldown_sec: 3600
    budgets:
      openai: 100.00
      anthropic: 50.00
      openrouter: 200.00
```

### Environment variables

| Var | Default | Purpose |
|---|---|---|
| `SLACK_ALERTS_WEBHOOK_URL` | unset | Slack incoming webhook. If unset, `alert_slack(...)` is a no-op. |
| `ALERTS_ENABLED` | `true` | Master kill switch (used during PR 1 dark-launch). |
| `ALERTS_CONFIG_PATH` | `config/alerts.yaml` | Path override for alerts config. |

## Strip plan

### Files / directories deleted

- [serving/observability/metrics.py](../../../serving/observability/metrics.py) — ~40 no-op shims
- [deploy/prometheus/](../../../deploy/prometheus/) — entire directory
- [deploy/alertmanager/](../../../deploy/alertmanager/) — entire directory (includes `alert_logger.py`)
- Any `alertmanager.service` / `alert-logger.service` units in [deploy/systemd/](../../../deploy/systemd/)

### Files modified

- `deploy/docker/docker-compose.yml` — drop `prometheus`, `alertmanager`, `alert-logger` services + their volumes.
- `deploy/nginx/freeinference.conf` — remove metrics-endpoint stanzas if any.
- `Makefile` — remove Prometheus-related targets if present.
- `.env.example` — remove Prometheus/Alertmanager vars; add `SLACK_ALERTS_WEBHOOK_URL`, `ALERTS_ENABLED`, `ALERTS_CONFIG_PATH`.
- Docs in `docs/developer/` and `docs/developer/developer/`.

### Callsite handling — three categories

| Category | Action | Examples |
|---|---|---|
| Pure no-op call (data already in request logs) | Delete the call | `PROVIDER_AVAILABILITY`, `PROVIDER_LATENCY` in [routing/routers.py:24-34](../../../routing/routers.py#L24) |
| Carries unique signal | Convert to structured log event with the same labels | `API_FALLBACKS` → `log.info("fallback_used", from=..., to=..., reason=...)`; `ROUTEWISE_CANARY_DECISIONS` → log event |
| State-change | Replace with `alert_slack(...)` call | `CIRCUIT_STATE` → on transition, call `alert_slack(severity="error", title="Provider circuit opened", context=...)` |

## Testing

- **Unit — `alerts.py`:** cooldown/dedupe under concurrent calls; severity formatting; webhook delivery against a mocked `httpx.AsyncClient`; no-op behavior when webhook URL unset.
- **Unit — `alert_rules.py`:** rolling-window counter insert/expire; threshold-crossing detection per rule; min-samples gate; periodic SQL ticker behavior with a mocked store and deterministic clock.
- **Unit — `log_handler.py`:** record capture; queue overflow drops oldest and increments dropped counter.
- **Integration:** drive a synthetic stream of structured log records through the handler → assert each rule fires the expected `alert_slack(...)` calls. Same for state-change paths (trigger a circuit open in `_CircuitBreaker` test, assert `alert_slack` called).
- **Smoke:** `AlertEngine.start()` / `.stop()` clean lifecycle (no zombie tasks, no leaked queues).

## Rollout — 5 PRs

This sequence will be turned into a detailed implementation plan by the writing-plans skill. Each PR is independently mergeable and revertable.

1. **PR 1 — Build framework, dark.** Add `alerts.py`, `log_handler.py`, `alert_rules.py`, `config/alerts.yaml`, all 9 alerts wired up, full unit + integration tests. Default `ALERTS_ENABLED=false`. Nothing fires.
2. **PR 2 — Enable in staging.** Set `SLACK_ALERTS_WEBHOOK_URL` + flip flag in staging only. Watch for 1–2 days; tune thresholds in `config/alerts.yaml`. No production change.
3. **PR 3 — Strip dead metric code.** Delete [serving/observability/metrics.py](../../../serving/observability/metrics.py) + all imports/callsites. Convert signal-carrying ones to structured log events / `alert_slack`. CI green (metrics were already no-ops).
4. **PR 4 — Strip Prometheus + Alertmanager infra.** Delete [deploy/prometheus/](../../../deploy/prometheus/) + [deploy/alertmanager/](../../../deploy/alertmanager/); remove `docker-compose` services; update env, nginx, systemd, docs.
5. **PR 5 — Enable in production.** Set webhook + flip flag in prod. Done.

**Stop / rollback:** any PR can be reverted independently. PR 4 (strip) is gated on PR 2 (staging proof) so we never remove Prometheus before alerts are validated.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Slack webhook outage / rate limits | Bounded send queue, drop-oldest on overflow, log dropped count. Alerts are best-effort. |
| `AlertEngine` task crash | Started under app lifespan; on cancellation, log + restart. `/health/ready` may include task-health if needed. |
| Cost-alert SQL slow under user growth | Indexed query on `(role, daily_cost desc)` with `LIMIT`. Cap loop work. |
| False-positive storm in early days | Conservative initial thresholds, built-in dedupe + cooldown, staging shake-out for 1–2 days before prod enable. |
| Removing Prometheus before alerts proven | PR 4 strictly depends on PR 2. Order enforces this. |
| Lost telemetry from deleted no-op metric calls | Architecture review confirmed all label data is already in structured request logs. PR 3 surfaces the few exceptions as log events. |

## Open questions

None at present. All forks resolved during brainstorming on 2026-05-03.

## Out-of-scope follow-ups (separate brainstorms)

These came up during the architecture review and have their own slots:

1. Decompose [serving/servers/routers/completions.py](../../../serving/servers/routers/completions.py) (1030 lines).
2. Adopt a schema migration framework (Alembic).
3. Make fire-and-forget side effects observable (cost increment + logging + dual-write shadow).
4. Decompose `frontend/src/app/dashboard/admin/page.tsx` (3343 lines).
5. Routing config expressiveness (per-model strategy via `routing.yaml`).
