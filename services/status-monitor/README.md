# status-monitor

A small status-monitoring website for FreeInference. It sends a synthetic
("dummy") chat request to **every model** in `config/models.yaml` on a fixed
schedule (5 minutes by default), records success/failure and latency, and
serves a live dashboard plus JSON endpoints.

```text
registry (models.yaml) ─┐
manual e2e_models ───────┴─► scheduler ──► gateway /v1/chat/completions ──► provider
                                  │
                                  ▼
                            StatusStore ──► dashboard (/) + /api/status + /api/health
```

The service intentionally does not import runtime code from the gateway; it
exercises the public API exactly as an external client would.

## Endpoints

| Path | Description |
|---|---|
| `/` | HTML status dashboard (auto-refreshing) |
| `/api/status` | Full snapshot (latest result + history per model) as JSON |
| `/api/health` | Lightweight summary (`total` / `healthy` / `unhealthy`) |

When `settings.base_path` is set (e.g. `/status-monitor`), the same routes are
also served under that prefix for reverse-proxy deployments.

## Configuration

See [`config.yml.example`](config.yml.example). Probe targets are
auto-discovered from the registry; `e2e_models` entries override discovered
targets (matched by `model_id`) or add ones not in the registry. `${VAR}` and
`${VAR:-default}` env references are expanded at load time.

## Run

```bash
uv sync
# One probe cycle, print JSON, exit (good for a smoke test):
uv run python -m status_monitor --config config.yml.example --run-once
# Serve the website:
uv run python -m status_monitor --config config.yml.example
```

Set `PROBER_API_KEY` to a valid FreeInference key before running against a real
gateway.

## Tests

```bash
uv run pytest
```
