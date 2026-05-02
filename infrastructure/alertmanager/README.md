# Alertmanager (internal)

Alertmanager receives alerts from the local Prometheus and forwards an
allowlisted subset to Slack `#free-inference-alert`. Anything not in the
allowlist falls to a `blackhole` receiver.

This is the internal stack. The external monitor (separate host) lives
in `infrastructure/external-monitor/` and runs its own Alertmanager.

## Files

- `alertmanager.yml` — live config. References the Slack webhook URL
  via `api_url_file:` (no env-var substitution needed).
- `alertmanager.yml.example` — copyable template.

## Webhook URL

The Slack incoming webhook URL is **not** in git. Alertmanager reads it
at runtime from the file referenced by `slack_configs[0].api_url_file`
(`/etc/alertmanager/secrets/slack-webhook-url` inside the container).

Provision the file on the host before bringing the stack up:

```bash
sudo install -d -m 700 -o root -g root /etc/freeinference
sudo install -m 600 /dev/null /etc/freeinference/slack-webhook-url
echo -n 'https://hooks.slack.com/services/...' | sudo tee /etc/freeinference/slack-webhook-url > /dev/null
```

Use `echo -n` so no trailing newline is appended — Alertmanager treats
the entire file content (minus a single trailing newline) as the URL.

The compose service in `infrastructure/docker/docker-compose.yml`
mounts this host path read-only into the container at the path
referenced by `api_url_file`. The mount source is overridable via the
`ALERTMANAGER_SLACK_WEBHOOK_FILE` env var (set in `.env` if needed),
defaulting to `/etc/freeinference/slack-webhook-url`.

After updating the webhook file, reload Alertmanager so it re-reads it:

```bash
curl -X POST http://127.0.0.1:9093/-/reload
```

## Adding a new alert to the Slack route

1. Define the rule in `infrastructure/prometheus/rules/`.
2. Add the `alertname` to the `match_re` allowlist in `alertmanager.yml`.
3. Validate: `amtool check-config /path/to/alertmanager.yml`.
4. Reload Alertmanager.

The allowlist is intentional: a bare top-level Slack receiver would
page on any rule that fires, including freshly re-enabled rules whose
noise level has not been verified.

## Verify

- UI: `http://localhost:9093`
- Health: `/-/ready`, `/-/healthy`
- Synthetic test: temporarily lower a `for: 2m` rule to `for: 10s` in
  `infrastructure/prometheus/rules/service_availability.yml`, stop the
  app process, confirm a Slack message arrives. Revert the rule.

## Related

- Prometheus rules: `infrastructure/prometheus/rules/`.
- External monitor: `infrastructure/external-monitor/`.
