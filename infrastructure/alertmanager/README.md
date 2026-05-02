# Alertmanager (internal)

Alertmanager receives alerts from the local Prometheus and forwards an
allowlisted subset to Slack `#free-inference-alert`. Anything not in the
allowlist falls to a `blackhole` receiver.

This is the internal stack. The external monitor (separate host) lives
in `infrastructure/external-monitor/` and runs its own Alertmanager.

## Files

- `alertmanager.yml` — live config. Contains `${SLACK_WEBHOOK_URL}`
  literal; substituted at deploy time.
- `alertmanager.yml.example` — copyable template.

## Webhook URL

The Slack incoming webhook URL is **not** in git. Store it in a
deploy-side env file:

```bash
sudo install -m 600 -o freeinference -g freeinference /dev/null /etc/freeinference/alertmanager.env
echo 'SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...' | sudo tee /etc/freeinference/alertmanager.env
```

At deploy time, render the live config with `envsubst`:

```bash
set -a
source /etc/freeinference/alertmanager.env
set +a
envsubst < /srv/hybridInference/infrastructure/alertmanager/alertmanager.yml \
  > /etc/alertmanager/alertmanager.yml
```

Point the systemd unit at the rendered file
(`--config.file=/etc/alertmanager/alertmanager.yml`). Reload:

```bash
curl -X POST http://127.0.0.1:9093/-/reload
```

If `envsubst` is unavailable, install `gettext-base` (Debian/Ubuntu) or
substitute with `sed -i "s|\${SLACK_WEBHOOK_URL}|$SLACK_WEBHOOK_URL|g" ...`.

## Adding a new alert to the Slack route

1. Define the rule in `infrastructure/prometheus/rules/`.
2. Add the `alertname` to the `match_re` allowlist in `alertmanager.yml`.
3. Validate: `amtool check-config /etc/alertmanager/alertmanager.yml`.
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
