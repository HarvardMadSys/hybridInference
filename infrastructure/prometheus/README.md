Prometheus Stack
================

> **Note:** Prometheus has been removed from the active deployment stack (see
> `docs/source/developer/deployment.md`). The configuration files here are
> preserved for potential re-introduction.

This directory contains Prometheus scrape configuration, recording/alerting rules,
and Alertmanager integration.

Layout
- `prometheus/`
  - `prometheus.yml`          Scrape configuration (defaults to `localhost:8000/metrics`).
  - `rules/`                  Recording and alerting rules (SLO/cost/availability).
- `../alertmanager/`
  - `alertmanager.yml.example` Example Alertmanager routing (Slack).

Quick Start (local)
1) Run Prometheus:
   docker run --rm -p 9090:9090 \
     -v $(pwd)/infrastructure/prometheus:/etc/prometheus \
     -v $(pwd)/var/prometheus:/prometheus \
     prom/prometheus:latest \
     --config.file=/etc/prometheus/prometheus.yml

2) Alerts and notifications (optional, recommended):
   - Alertmanager setup: see `../alertmanager/README.md`.
   - Rules under `prometheus/rules/` enabled by default:
     - `service_availability.yml`: `ServiceDown`, `ServiceUnreachable`,
       `DatabaseDisconnected`. Routed to Slack via the allowlist in
       `../alertmanager/alertmanager.yml`.
   - Previously-shipped `slo_burn_rate.yml` and `pipeline_health.yml`
     rules were removed on 2026-05-02. See `prometheus/rules/README.md`
     for context and how to re-enable.

Remote service (free inference server)
- Add your remote `/metrics` target into `scrape_configs`:
  ```yaml
  scrape_configs:
    - job_name: remote-app
      metrics_path: /metrics
      static_configs:
        - targets: ["your-server:8000"]
  ```
- Consider `external_labels` to tag `env` or `instance` for dashboard filtering and alert ownership.

Tips
- Tune `scrape_interval` based on load (e.g., 30s in production).
- Control label cardinality: set `METRICS_MODEL_LABEL=family` to avoid model-dimension explosion.
- Keep rules and dashboards as code to prevent UI drift.

Data directory
- If running the Prometheus binary directly (not in a container), explicitly set the data directory to `var/prometheus`.
- Examples:
  - Native binary:
    - `prometheus --config.file=infrastructure/prometheus/prometheus.yml --storage.tsdb.path=var/prometheus`
  - Docker (the quick-start command already mounts this to `/prometheus`):
    - `-v $(pwd)/var/prometheus:/prometheus`
- Keeping runtime data under `var/` makes cleanup/backup easier and avoids polluting the repository root.
