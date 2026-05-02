# Prometheus Rules

Active rule files in this directory:

- `service_availability.yml` — `ServiceDown`, `ServiceUnreachable`, `DatabaseDisconnected`. Routed to Slack via the internal Alertmanager allowlist.

## Removed rules (historical)

The following rule files were removed on 2026-05-02. They had been
fully commented out since 2026-02-11 due to noise from upstream metrics.

- `pipeline_health.yml` — held `ProviderAvailabilityLow`. The
  `provider_availability` metric was unreliable; the alert fired
  continuously for 265h between 2026-01-28 and 2026-02-11. Re-add when
  the metric is reimplemented.
- `slo_burn_rate.yml` — held `APIErrorBudgetBurnFast`,
  `APIHighLatencyP95`, `APIErrorRateHigh`, plus their recording rules.
  Triggered exclusively by bot/scanner traffic against probe routes
  (`.env`, `.php`, `wp-login`, etc.). Re-add after route normalization
  collapses scanner routes and after the error ratio is restricted to
  5xx-only.

Original definitions are in git history:

```bash
git log --all --oneline -- infrastructure/prometheus/rules/pipeline_health.yml
git log --all --oneline -- infrastructure/prometheus/rules/slo_burn_rate.yml
```

## Adding a new rule

1. Add or edit a `*.yml` file in this directory.
2. Reference it under `rule_files:` in `infrastructure/prometheus/prometheus.yml`.
3. To page on the new alert, add the `alertname` to the `match_re`
   allowlist in `infrastructure/alertmanager/alertmanager.yml`.
4. Validate with `promtool check config infrastructure/prometheus/prometheus.yml`.
