# RouteWise option reference

The runnable catalog is `models.routewise.json`. Optional capacity route shapes are
in `route-capacity-reference.json`. Configure these values in the Admin model catalog.

| Option | Default |
|---|---|
| `budget_alpha` | `0.75` |
| `random_seed` | `None` |
| `reference_api_price` | `None` |
| `db_bootstrap_enabled` | `True` |
| `db_bootstrap_max_rows` | `50000` |
| `stateful_providers_single_worker_only` | `True` |
| `output_default_tokens` | `512.0` |
| `output_min_bucket_samples` | `3` |
| `output_min_model_samples` | `3` |
| `output_min_global_samples` | `3` |
| `quota_snapshot_refresh_interval_sec` | `60.0` |
| `envelope_window_hours` | `24` |
| `envelope_lower_percentile` | `10.0` |
| `envelope_upper_percentile` | `90.0` |
| `envelope_min_samples` | `1` |
| `envelope_max_samples` | `20000` |
| `envelope_cache_ttl_sec` | `1.0` |
| `envelope_bootstrap_donor_models` | `None` |
| `latency_slo_sec` | `3.0` |
| `latency_window_sec` | `900.0` |
| `latency_history_prior_window_sec` | `86400.0` |
| `latency_max_samples_per_profile` | `5000` |
| `latency_unprofiled_ttft_ms` | `5000.0` |
| `latency_hedge_mode` | `disabled` |
| `fallback_mode` | `policy` |
| `routewise_probe_enabled` | `False` |
| `routewise_probe_interval_sec` | `300.0` |
| `routewise_probe_timeout_sec` | `30.0` |
| `routewise_probe_idle_only` | `True` |
| `routewise_probe_idle_threshold_sec` | `900.0` |
| `routewise_probe_max_concurrency` | `1` |
| `decision_metadata_candidate_detail` | `False` |
| `prefix_cache_cost_adjustment_enabled` | `False` |

To start a source gateway after storing the catalog in Admin:

```bash
uv run uvicorn serving.servers.app:app --no-proxy-headers --host 127.0.0.1 --port 8080
```
