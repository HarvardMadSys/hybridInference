# Alert Management Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify internal alert notifications on the same Slack channel as external alerts, delete the unused `alert-logger` Python webhook + JSONL sink, and remove fully-commented-out Prometheus rule files.

**Architecture:** The internal `infrastructure/alertmanager/` config gains a Slack receiver mirroring the external monitor's, gated by an explicit `match_re` allowlist of vetted alert names. The `alert-logger` webhook (`alert_logger.py` + systemd unit) is removed. Empty rule files (`pipeline_health.yml`, `slo_burn_rate.yml`) are deleted, with a short README in `infrastructure/prometheus/rules/` recording the removal and pointing to git history. Webhook URL injection uses `envsubst` at deploy time, matching the existing external-monitor pattern. The external monitor stack is untouched.

**Tech Stack:** Prometheus 2.x, Alertmanager 0.28, Slack incoming webhooks, systemd, `envsubst` (gettext-base).

**Spec:** `docs/superpowers/specs/2026-05-02-alert-management-cleanup-design.md`

## Post-implementation deviations from this plan

The plan was written assuming a deploy-time `envsubst` would render the
Slack webhook URL into `alertmanager.yml`. Code review surfaced that the
Docker compose deploy path mounts `alertmanager.yml` from the source
tree unmodified, so the `${SLACK_WEBHOOK_URL}` literal would never be
expanded — `make up` would fail. The implementation pivoted to
Alertmanager's native `api_url_file:` directive, which avoids the
rendered-vs-source split entirely. Concretely:

- `alertmanager.yml` and `alertmanager.yml.example` use
  `api_url_file: /etc/alertmanager/secrets/slack-webhook-url` instead of
  `api_url: ${SLACK_WEBHOOK_URL}`.
- The compose `alertmanager` service mounts a host-side webhook URL file
  (default `/etc/freeinference/slack-webhook-url`, override via
  `ALERTMANAGER_SLACK_WEBHOOK_FILE`) read-only into the container.
- `infrastructure/alertmanager/README.md` documents the file
  provisioning + reload flow (no `envsubst`).
- The plan's Task 5 was expanded to also delete the
  `Dockerfile.alert-logger` and the `alert-logger` service block +
  `alert_log_data` volume in `docker-compose.yml`.
- `Makefile`'s `DOCKER_VOLUMES` list dropped
  `hybridinference_alert_log_data` (originally missed by the plan's
  grep audit, which only swept `docs/` and `infrastructure/`).
- `docs/source/developer/architecture.md` had a spaced "alert logger"
  mention (also originally missed by the plan's hyphen-only grep) which
  was cleaned up.
- `infrastructure/prometheus/README.md` dropped the "Slack/Email"
  description for `alertmanager.yml.example` to match the trimmed
  template.

The spec was updated to match this final design; the per-task
breakdown below reflects the pre-pivot envsubst plan and is preserved
for traceability. See the spec's "Webhook URL injection" section for
the as-built mechanism.

---

## File Structure

**Modify:**
- `infrastructure/alertmanager/alertmanager.yml` — replace `alert-logger` receiver with `slack-default`; keep allowlist routing.
- `infrastructure/alertmanager/alertmanager.yml.example` — drop email block; Slack-only template.
- `infrastructure/alertmanager/README.md` — update for single Slack receiver; drop alert-logger references; document `envsubst` step.
- `infrastructure/prometheus/prometheus.yml` — remove `rules/slo_burn_rate.yml` and `rules/pipeline_health.yml` from `rule_files`; add `global.external_labels.source: internal`.
- `docs/source/developer/deployment.md` — remove `alert-logger` from infra list and flow diagram.
- `docs/source/developer/freeinference.md` — drop `alert-logger` from infra list.

**Delete:**
- `infrastructure/alertmanager/alert_logger.py`
- `infrastructure/systemd/alert-logger.service`
- `infrastructure/prometheus/rules/pipeline_health.yml`
- `infrastructure/prometheus/rules/slo_burn_rate.yml`

**Create:**
- `infrastructure/prometheus/rules/README.md` — record removal of SLO/pipeline-health rules + reasons.

Each task below produces an isolated, committable change.

---

## Task 0: Setup worktree + branch

**Files:** none (git plumbing).

- [ ] **Step 1: Pull latest dev**

```bash
cd /home/juncheng/hybridInference
git fetch origin
git checkout dev
git pull origin dev
```

- [ ] **Step 2: Create worktree on feature branch**

```bash
git worktree add ../hybridInference-alert-cleanup -b jason/claude/alert-management-cleanup
cd ../hybridInference-alert-cleanup
```

Expected: new directory `../hybridInference-alert-cleanup` checked out at branch `jason/claude/alert-management-cleanup`.

All subsequent tasks run inside this worktree directory.

---

## Task 1: Replace alert-logger receiver with Slack in internal Alertmanager config

**Files:**
- Modify: `infrastructure/alertmanager/alertmanager.yml`

- [ ] **Step 1: Verify current content**

Run:
```bash
cat infrastructure/alertmanager/alertmanager.yml
```

Expected: file contains `receiver: blackhole` top-level, with a child route to `alert-logger` receiver pointing at `http://alert-logger:5001/alerts`.

- [ ] **Step 2: Replace file contents**

Overwrite `infrastructure/alertmanager/alertmanager.yml` with:

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

- [ ] **Step 3: Validate config syntax with amtool**

Substitute the env var with a placeholder for the syntax check (amtool fails on unsubstituted `${VAR}`):

```bash
SLACK_WEBHOOK_URL='https://hooks.slack.com/services/PLACEHOLDER' \
  envsubst < infrastructure/alertmanager/alertmanager.yml > /tmp/am-check.yml
amtool check-config /tmp/am-check.yml
rm /tmp/am-check.yml
```

Expected output: `Checking '/tmp/am-check.yml'  SUCCESS` followed by a summary listing the receivers.

If `amtool` is not installed, install it:
```bash
# Debian/Ubuntu:
sudo apt-get install -y prometheus-alertmanager
# Or download from https://github.com/prometheus/alertmanager/releases
```

- [ ] **Step 4: Commit**

```bash
git add infrastructure/alertmanager/alertmanager.yml
git commit -m "feat(alertmanager): route internal alerts to Slack instead of alert-logger"
```

---

## Task 2: Add `external_labels` and remove deleted rule_files refs in prometheus.yml

**Files:**
- Modify: `infrastructure/prometheus/prometheus.yml`

- [ ] **Step 1: View current `global` block**

```bash
sed -n '1,10p' infrastructure/prometheus/prometheus.yml
```

Expected: `global:` block with `scrape_interval: 15s`, `evaluation_interval: 30s`, no `external_labels`.

- [ ] **Step 2: Add `external_labels` to `global`**

Edit `infrastructure/prometheus/prometheus.yml`. Replace:

```yaml
global:
  # Default for local/dev; consider 30s in production to reduce overhead.
  scrape_interval: 15s
  evaluation_interval: 30s
```

with:

```yaml
global:
  # Default for local/dev; consider 30s in production to reduce overhead.
  scrape_interval: 15s
  evaluation_interval: 30s
  external_labels:
    source: internal
```

- [ ] **Step 3: Remove `rule_files` entries for deleted rule files**

Replace:

```yaml
rule_files:
  - rules/slo_burn_rate.yml
  - rules/pipeline_health.yml
  - rules/service_availability.yml
```

with:

```yaml
rule_files:
  - rules/service_availability.yml
```

- [ ] **Step 4: Validate prometheus config**

```bash
promtool check config infrastructure/prometheus/prometheus.yml
```

Expected output ends with `SUCCESS: 1 rule files found`. (Will fail if `slo_burn_rate.yml` or `pipeline_health.yml` are still referenced because Task 3 has not yet deleted them — that's fine; promtool only complains if a referenced file is missing. The files still exist at this point, so SUCCESS is expected. After Task 3 deletes them, re-run this check.)

If `promtool` is not installed:
```bash
# Debian/Ubuntu:
sudo apt-get install -y prometheus
```

- [ ] **Step 5: Commit**

```bash
git add infrastructure/prometheus/prometheus.yml
git commit -m "feat(prometheus): tag internal stack with source=internal external label"
```

---

## Task 3: Delete fully-commented-out rule files

**Files:**
- Delete: `infrastructure/prometheus/rules/pipeline_health.yml`
- Delete: `infrastructure/prometheus/rules/slo_burn_rate.yml`

- [ ] **Step 1: Confirm both files contain only comments + empty `rules: []`**

```bash
grep -v -E '^\s*(#|$)' infrastructure/prometheus/rules/pipeline_health.yml
grep -v -E '^\s*(#|$)' infrastructure/prometheus/rules/slo_burn_rate.yml
```

Expected output for both: only the YAML scaffolding (`groups:`, `- name: ...`, `interval: 30s`, `rules: []`). No active rule definitions.

- [ ] **Step 2: Delete the files**

```bash
git rm infrastructure/prometheus/rules/pipeline_health.yml
git rm infrastructure/prometheus/rules/slo_burn_rate.yml
```

- [ ] **Step 3: Re-validate prometheus config**

```bash
promtool check config infrastructure/prometheus/prometheus.yml
```

Expected: `SUCCESS: 1 rule files found` (only `service_availability.yml` now).

- [ ] **Step 4: Commit**

```bash
git commit -m "chore(prometheus): remove empty pipeline_health and slo_burn_rate rule files

Both files contained only commented-out rule blocks (annotated 2026-02-11
as noisy). Removal documented in infrastructure/prometheus/rules/README.md
(added in next commit). Original rule definitions remain in git history."
```

---

## Task 4: Add `infrastructure/prometheus/rules/README.md` documenting removal

**Files:**
- Create: `infrastructure/prometheus/rules/README.md`

- [ ] **Step 1: Create the README**

Write `infrastructure/prometheus/rules/README.md`:

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add infrastructure/prometheus/rules/README.md
git commit -m "docs(prometheus): record removed rule files and rationale"
```

---

## Task 5: Delete `alert-logger` Python webhook + systemd unit

**Files:**
- Delete: `infrastructure/alertmanager/alert_logger.py`
- Delete: `infrastructure/systemd/alert-logger.service`

- [ ] **Step 1: Confirm no other code imports from `alert_logger`**

```bash
grep -rn "alert_logger" --include="*.py" --include="*.service" --include="*.yml" --include="*.yaml" .
```

Expected: only matches in the two files about to be deleted (`alert_logger.py` itself and `alert-logger.service`). Anything else means there's a hidden consumer — stop and investigate.

- [ ] **Step 2: Delete files**

```bash
git rm infrastructure/alertmanager/alert_logger.py
git rm infrastructure/systemd/alert-logger.service
```

- [ ] **Step 3: Commit**

```bash
git commit -m "chore(alertmanager): remove unused alert-logger webhook and systemd unit

The alert-logger Python webhook appended Alertmanager payloads to
var/log/alert_history.jsonl, which nobody read. Internal alerts now
flow to Slack via the slack-default receiver added in the previous
alertmanager.yml change."
```

---

## Task 6: Update `alertmanager.yml.example` to Slack-only

**Files:**
- Modify: `infrastructure/alertmanager/alertmanager.yml.example`

- [ ] **Step 1: Replace file contents**

Overwrite `infrastructure/alertmanager/alertmanager.yml.example` with:

```yaml
# Template for infrastructure/alertmanager/alertmanager.yml.
# Copy to alertmanager.yml and replace ${SLACK_WEBHOOK_URL} at deploy
# time (envsubst recommended; see infrastructure/alertmanager/README.md).

route:
  receiver: blackhole
  group_by: ['alertname']
  group_wait: 1m
  group_interval: 5m
  repeat_interval: 4h
  routes:
    # Allowlist: add new alertnames here explicitly to start paging on them.
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

- [ ] **Step 2: Commit**

```bash
git add infrastructure/alertmanager/alertmanager.yml.example
git commit -m "docs(alertmanager): trim example template to Slack-only with allowlist"
```

---

## Task 7: Rewrite `infrastructure/alertmanager/README.md`

**Files:**
- Modify: `infrastructure/alertmanager/README.md`

- [ ] **Step 1: Replace file contents**

Overwrite `infrastructure/alertmanager/README.md` with:

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add infrastructure/alertmanager/README.md
git commit -m "docs(alertmanager): rewrite README for Slack-only flow with envsubst deploy"
```

---

## Task 8: Remove `alert-logger` from developer docs

**Files:**
- Modify: `docs/source/developer/deployment.md`
- Modify: `docs/source/developer/freeinference.md`

- [ ] **Step 1: View current `alert-logger` mentions**

```bash
grep -n "alert-logger" docs/source/developer/deployment.md docs/source/developer/freeinference.md
```

Expected matches:
- `docs/source/developer/deployment.md:23`
- `docs/source/developer/deployment.md:42`
- `docs/source/developer/freeinference.md:27`

- [ ] **Step 2: Edit `docs/source/developer/deployment.md`**

Read line 23 context first:
```bash
sed -n '20,26p' docs/source/developer/deployment.md
```

Find the line listing infra components and remove `alert-logger,`. For example, replace:
```
Alertmanager, alert-logger, and Grafana. All ports bind to `127.0.0.1` only.
```
with:
```
Alertmanager and Grafana. All ports bind to `127.0.0.1` only.
```

Read line 42 context:
```bash
sed -n '40,46p' docs/source/developer/deployment.md
```

Replace the flow diagram line:
```
prometheus ──▶ alertmanager (:9093) ──▶ alert-logger (:5001)
```
with:
```
prometheus ──▶ alertmanager (:9093) ──▶ Slack #free-inference-alert
```

- [ ] **Step 3: Edit `docs/source/developer/freeinference.md`**

Read line 27 context:
```bash
sed -n '25,29p' docs/source/developer/freeinference.md
```

Remove `alert-logger,` from the infra component list. For example, replace:
```
Alertmanager, alert-logger, Grafana, plus pgAdmin behind the `admin` profile)
```
with:
```
Alertmanager, Grafana, plus pgAdmin behind the `admin` profile)
```

- [ ] **Step 4: Verify no remaining mentions**

```bash
grep -rn "alert-logger\|alert_logger" docs/ infrastructure/
```

Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add docs/source/developer/deployment.md docs/source/developer/freeinference.md
git commit -m "docs: drop alert-logger references from deployment + freeinference guides"
```

---

## Task 9: Run project lint check

**Files:** none (verification only).

- [ ] **Step 1: Run ruff format check (project requirement per CLAUDE.md)**

```bash
uv run ruff format --check .
```

Expected output: `XXX files already formatted`. No changes required (this PR removes a Python file, doesn't add one).

- [ ] **Step 2: If ruff reports drift, format and commit**

```bash
uv run ruff format .
git add -u
git commit -m "chore: ruff format"
```

If no drift, skip this step.

---

## Task 10: Final config sanity sweep

**Files:** none (verification only).

- [ ] **Step 1: Re-run prometheus config check**

```bash
promtool check config infrastructure/prometheus/prometheus.yml
```

Expected: `SUCCESS: 1 rule files found`.

- [ ] **Step 2: Re-run alertmanager config check**

```bash
SLACK_WEBHOOK_URL='https://hooks.slack.com/services/PLACEHOLDER' \
  envsubst < infrastructure/alertmanager/alertmanager.yml > /tmp/am-check.yml
amtool check-config /tmp/am-check.yml
rm /tmp/am-check.yml
```

Expected: `SUCCESS`. Receivers listed: `blackhole`, `slack-default`. No `alert-logger`.

- [ ] **Step 3: Confirm alert-logger fully gone from repo**

```bash
grep -rn "alert-logger\|alert_logger\|AlertLogger" \
  --include="*.py" --include="*.ts" --include="*.tsx" \
  --include="*.yml" --include="*.yaml" --include="*.service" \
  --include="*.md" .
```

Expected: no output.

- [ ] **Step 4: Verify allowlist matches active rules**

```bash
grep -E "^\s*- alert:" infrastructure/prometheus/rules/service_availability.yml | awk '{print $3}'
grep -A1 "match_re:" infrastructure/alertmanager/alertmanager.yml
```

Expected: each `alert:` name in `service_availability.yml` (`ServiceDown`, `ServiceUnreachable`, `DatabaseDisconnected`) appears in the allowlist regex. Any drift → fix `alertmanager.yml`.

---

## Task 11: Open PR to dev

**Files:** none (PR creation).

- [ ] **Step 1: Push branch**

```bash
git push -u origin jason/claude/alert-management-cleanup
```

- [ ] **Step 2: Create PR**

```bash
gh pr create --base dev --title "Alert management cleanup: route internal alerts to Slack, drop alert-logger" --body "$(cat <<'EOF'
## Summary

- Internal Alertmanager now routes `ServiceDown` / `ServiceUnreachable` / `DatabaseDisconnected` to Slack `#free-inference-alert` via a `slack-default` receiver, mirroring the external-monitor stack. Title prefix `[INT-ALERT]` distinguishes from `[EXT-ALERT]`.
- Allowlist routing preserved (`match_re`) so future re-enabled noisy rules can't page-storm without an explicit add.
- Deleted unused `alert_logger.py` Python webhook + its systemd unit. JSONL audit sink had no readers; Slack history covers ops needs.
- Deleted fully-commented-out `pipeline_health.yml` and `slo_burn_rate.yml` rule files (noisy since 2026-02-11). New `infrastructure/prometheus/rules/README.md` records why and points to git history.
- Added `external_labels.source: internal` on internal Prometheus to disambiguate against the external stack.
- Webhook URL injection uses deploy-time `envsubst`, matching the existing external-monitor pattern. Documented in the rewritten `infrastructure/alertmanager/README.md`.

Spec: `docs/superpowers/specs/2026-05-02-alert-management-cleanup-design.md`

## Test plan

- [ ] `promtool check config infrastructure/prometheus/prometheus.yml` → SUCCESS
- [ ] `amtool check-config` on `envsubst`-rendered `alertmanager.yml` → SUCCESS
- [ ] `uv run ruff format --check .` passes
- [ ] On staging: provision `/etc/freeinference/alertmanager.env` with the Slack webhook URL, run `envsubst` to render the live config, reload Alertmanager
- [ ] On staging: temporarily set `ServiceDown` `for: 10s`, stop the backend process, confirm `[INT-ALERT] API Service is Down` arrives in `#free-inference-alert`, then `[RESOLVED]` after restart
- [ ] On staging: confirm `systemctl status alert-logger` reports "Unit not found" after deploy
EOF
)"
```

Expected: PR URL printed. Save it for the user.

- [ ] **Step 3: Report PR URL**

Print the PR URL in the final message.

---

## Self-Review Notes

Spec coverage:
- Notification topology (Slack with `[INT-ALERT]` prefix) → Task 1.
- File deletes (`alert_logger.py`, systemd unit, two empty rule files) → Tasks 3, 5.
- File modifies (yml, yml.example, README, prometheus.yml, two doc pages) → Tasks 1, 2, 6, 7, 8.
- Add `infrastructure/prometheus/rules/README.md` → Task 4.
- Allowlist preserved → Task 1 (kept `match_re`, swapped receiver).
- `external_labels: source: internal` → Task 2.
- `envsubst` deploy step documented → Task 7.
- Lint per CLAUDE.md → Task 9.
- PR to dev per CLAUDE.md → Task 11.

Type/name consistency: receiver name `slack-default`, channel `#free-inference-alert`, env var `SLACK_WEBHOOK_URL`, label `source: internal` — used identically across all tasks.

No placeholders: every code/config/command block is concrete.
