# Alert Management Cleanup — Design

**Date:** 2026-05-02
**Status:** Draft
**Owner:** Juncheng Yang

## Problem

The alerting setup has accumulated cruft across two parallel stacks:

1. **In-tree (main host).** `infrastructure/alertmanager/` runs an Alertmanager
   that routes only `ServiceDown`, `ServiceUnreachable`, and
   `DatabaseDisconnected` to a custom `alert-logger` Python webhook
   (`infrastructure/alertmanager/alert_logger.py`). The webhook appends
   payloads to `var/log/alert_history.jsonl`. **Nobody reads the JSONL.**
   Internal alerts are silent in practice.
2. **External monitor (separate host).** `infrastructure/external-monitor/`
   runs an independent Prometheus + Alertmanager + blackbox-exporter +
   llm-prober stack that posts to Slack `#free-inference-alert`. This works.

Two consequences:

- Notification asymmetry: external alerts are visible (Slack); internal alerts
  rot in a file. A `DatabaseDisconnected` event today would not page anyone.
- Rule corpses: `pipeline_health.yml` and `slo_burn_rate.yml` are entirely
  commented-out blocks (annotated 2026-02-11) due to bot/scanner traffic
  inflating metrics. The files exist but contribute nothing.
- Two Alertmanager configs to maintain when only one notification path is
  needed.

## Goals

- Internal alerts reach the same Slack channel as external alerts.
- Remove `alert-logger` webhook receiver and its custom Python service.
- Remove commented-out rule blocks from version control.
- Keep external monitor stack untouched (must stay independent for
  whole-host failure isolation).
- Keep an explicit allowlist of paging alerts to prevent future page-storms
  from accidentally re-enabled noisy rules.

## Non-goals

- Re-enabling SLO burn-rate or `ProviderAvailabilityLow` alerts. Root causes
  (bot/scanner traffic, broken `provider_availability` metric) are out of
  scope. Removal is permanent until those are fixed in separate work.
- Building a Grafana alert-history dashboard (Approach C). Slack channel
  history covers the audit need.
- Consolidating to a single Alertmanager instance (Approach B). External AM
  must remain on a separate host so a whole-host failure of the main server
  still pages.

## Approach

**Approach A: Unify notification path on Slack; delete `alert-logger`.**

The internal Alertmanager gains a Slack receiver mirroring the external one,
posting to the same `#free-inference-alert` channel. Title prefix is
`[INT-ALERT]` so internal vs external is visually distinct in Slack.

The `alert-logger` Python webhook, its systemd unit, and the JSONL audit
file are removed. Alertmanager's own UI (`:9093`) plus Slack history
covers operational visibility.

Routing keeps an explicit `match_re` allowlist for currently-active alert
names. Anything not in the allowlist falls to a `blackhole` receiver. This
guards against a future re-enabled noisy rule instantly paging the channel
before its noise level is verified.

Commented-out rule files are emptied or removed; an explanatory paragraph
in `infrastructure/prometheus/rules/README.md` (created if absent) points
to git history for the removed rules.

### Alternatives considered

- **B) Single Alertmanager.** Rejected: defeats external monitor's
  whole-host-failure isolation.
- **C) Slack + Grafana alert dashboard.** Defer. Slack channel history is
  good enough for now; revisit if audit requirements emerge.

## Files changed

**Delete:**

- `infrastructure/alertmanager/alert_logger.py`
- `infrastructure/systemd/alert-logger.service`
- `infrastructure/prometheus/rules/pipeline_health.yml` (entirely commented out)
- `infrastructure/prometheus/rules/slo_burn_rate.yml` (entirely commented out)

**Modify:**

- `infrastructure/alertmanager/alertmanager.yml` — replace `alert-logger`
  receiver with `slack-default` (see "Configuration" below).
- `infrastructure/alertmanager/alertmanager.yml.example` — drop the email
  receiver block; keep Slack-only as the canonical template.
- `infrastructure/alertmanager/README.md` — update to single-receiver
  Slack pattern; drop alert-logger mentions; remove email snippet.
- `infrastructure/systemd/alertmanager.service` — no change. (Originally
  considered `EnvironmentFile=`, but Alertmanager does not expand
  `${VAR}` in YAML; injection happens at deploy time via `envsubst`.
  See "Webhook URL injection" below.)
- `infrastructure/prometheus/prometheus.yml` — remove the now-deleted
  `rule_files` entries (`slo_burn_rate.yml`, `pipeline_health.yml`); add a
  top-level `global.external_labels: { source: internal }` so Slack
  messages can disambiguate origin if both stacks ever post the same
  alertname.
- `docs/source/developer/deployment.md` — update flow diagram
  (`prometheus → alertmanager → Slack`); remove alert-logger node.
- `docs/source/developer/freeinference.md` — drop alert-logger from infra
  list.

**Add:**

- `infrastructure/prometheus/rules/README.md` — short note recording that
  SLO and pipeline-health rules were removed 2026-05-02 due to
  bot/scanner-driven noise and broken metrics; link to git history.
- `/etc/freeinference/alertmanager.env` (deploy-side, not committed) —
  contains `SLACK_WEBHOOK_URL=...`. Document in deployment.md.

## Configuration

### `infrastructure/alertmanager/alertmanager.yml`

```yaml
route:
  receiver: blackhole
  group_by: ['alertname']
  group_wait: 1m
  group_interval: 5m
  repeat_interval: 4h
  routes:
    # Allowlist: only forward currently-vetted actionable alerts to Slack.
    # Anything not listed here falls to the blackhole receiver. Re-enabled
    # rules must be added explicitly to avoid page-storms during ramp-up.
    - match_re:
        alertname: "ServiceDown|ServiceUnreachable|DatabaseDisconnected"
      receiver: slack-default

receivers:
  - name: blackhole
  - name: slack-default
    slack_configs:
      - send_resolved: true
        api_url: ${SLACK_WEBHOOK_URL}
        channel: '#free-inference-alert'
        title: '{{ if eq .Status "resolved" }}[RESOLVED] {{ end }}[INT-ALERT] {{ .CommonAnnotations.summary }}'
        text: >-
          {{ range .Alerts }}
          {{ if eq .Status "resolved" }}:white_check_mark:{{ else }}:rotating_light:{{ end }}
          *{{ .Labels.alertname }}* ({{ .Labels.severity }})
            {{ .Annotations.description }}
          {{ end }}
```

### `infrastructure/prometheus/prometheus.yml` — additions

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 30s
  external_labels:
    source: internal

rule_files:
  - rules/service_availability.yml
```

### Webhook URL injection

Alertmanager does NOT expand `${VAR}` syntax in its YAML at runtime. The
existing external monitor handles this with deploy-time substitution; the
internal stack must do the same. Two viable options:

1. **`envsubst` at deploy time (recommended).** Keep `alertmanager.yml`
   in git with `${SLACK_WEBHOOK_URL}` literal. Add a deploy step:
   ```
   envsubst < infrastructure/alertmanager/alertmanager.yml \
     > /etc/alertmanager/alertmanager.yml
   ```
   pointing AM's `--config.file` at the rendered output. Document in
   the README.
2. **`sed` replacement.** Same idea, less elegant. Acceptable fallback
   if `envsubst` (gettext-base) is not installed on the host.

### `/etc/freeinference/alertmanager.env` (deploy-side, not in git)

```
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

`chmod 600`, owned by `freeinference:freeinference`. Sourced by the
deploy script that runs `envsubst`. (Not loaded by Alertmanager
directly — AM has no env-var support in YAML.)

## Deployment + rollout

1. Create `/etc/freeinference/alertmanager.env` on main host with the same
   webhook URL the external monitor uses (or a new one — same channel
   either way). `chmod 600`.
2. Render the live config:
   `set -a && source /etc/freeinference/alertmanager.env && set +a && envsubst < /srv/hybridInference/infrastructure/alertmanager/alertmanager.yml > /etc/alertmanager/alertmanager.yml`.
   (Bake into the existing deploy script so future config edits redo
   this automatically.)
3. Reload Alertmanager:
   `curl -X POST http://127.0.0.1:9093/-/reload`.
4. Apply `prometheus.yml` change (removed rule files + external_labels);
   reload: `curl -X POST http://127.0.0.1:9090/-/reload`.
5. Verify with synthetic alert (see Testing below).
6. Stop and disable `alert-logger`:
   `sudo systemctl disable --now alert-logger && sudo rm /etc/systemd/system/alert-logger.service && sudo systemctl daemon-reload`.
7. `git rm` the deleted files; commit; PR.
8. Optional: archive existing `var/log/alert_history.jsonl` if any
   forensic value, then delete.

## Testing

Pre-merge:

- `promtool check config infrastructure/prometheus/prometheus.yml`
- `amtool check-config infrastructure/alertmanager/alertmanager.yml`
  (note: `${SLACK_WEBHOOK_URL}` may need a placeholder value during the
  check; document the trick in the README)
- `uv run ruff format --check .` (project requirement)

Post-deploy:

- Synthetic `ServiceDown` trigger: temporarily lower the rule's `for: 2m`
  to `for: 10s`, stop the app process briefly, watch Slack for an
  `[INT-ALERT] API Service is Down` message and a `[RESOLVED]` follow-up
  when restarted. Revert the rule.
- `systemctl status alert-logger` on the host should report "Unit not
  found".
- Confirm Slack message includes the `source: internal` external_label
  somewhere (or is at least disambiguated by the `[INT-ALERT]` prefix).

## Risks + mitigations

- **Slack page-storm from re-enabled noisy rule.** Allowlist routing
  guards against this; new alertnames must be added to `match_re`
  explicitly.
- **Audit-trail loss (no JSONL).** Slack channel history covers normal
  ops needs. If compliance later demands durable logs, revisit
  Approach C (Grafana panel) or have Alertmanager log to a file via
  `--log.format=json`.
- **Webhook URL leak.** Already mitigated by env-var substitution; no new
  secret material is committed.
- **Deploy ordering.** If alertmanager.yml is applied before the
  EnvironmentFile exists, AM restart fails. Mitigation: create the env
  file in step 1, before any config change.

## Out-of-scope follow-ups

- Re-enable `ProviderAvailabilityLow` after fixing the
  `provider_availability` metric implementation.
- Re-enable SLO burn-rate alerts after route normalization collapses
  bot/scanner-driven 4xx noise.
- Add a Grafana alert-history dashboard if audit needs grow.
