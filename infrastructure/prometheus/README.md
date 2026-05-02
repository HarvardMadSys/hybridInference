Prometheus Stack
================

This directory contains Prometheus scrape configuration, recording/alerting rules, and Grafana dashboards (under `../grafana/`).

Layout
- `prometheus/`
  - `prometheus.yml`          Scrape configuration (defaults to `localhost:8000/metrics`).
  - `rules/`                  Recording and alerting rules (SLO/cost/availability).
- `../grafana/`
  - `dashboards/`             Importable Grafana dashboard JSON files.
  - `README.md`               Import instructions.
- `../alertmanager/`
  - `alertmanager.yml.example` Example Alertmanager routing (Slack).

Quick Start (local)
1) Run Prometheus:
   docker run --rm -p 9090:9090 \
     -v $(pwd)/infrastructure/prometheus:/etc/prometheus \
     -v $(pwd)/var/prometheus:/prometheus \
     prom/prometheus:latest \
     --config.file=/etc/prometheus/prometheus.yml

2) Import Grafana dashboards:
   - See `../grafana/README.md` for dashboard import steps and variables.

3) Alerts and notifications (optional, recommended):
   - Alertmanager setup: see `../alertmanager/README.md`.
   - Rules under `prometheus/rules/` are enabled by default:
     - `slo_burn_rate.yml`: error rate and P95 latency alerts.
     - `pipeline_health.yml`: provider availability and cost rate.

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
