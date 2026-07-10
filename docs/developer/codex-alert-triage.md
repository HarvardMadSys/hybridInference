# Codex Alert Triage

The alert triage relay turns structured gateway and status-monitor alerts into
read-only Codex investigations. The original alert is delivered immediately;
analysis runs asynchronously and is posted as a reply in the same Slack thread.

```text
gateway / status-monitor
        │  POST /v1/alerts
        ▼
triage relay ──► Slack top-level alert
        │
        ├──► SQLite queue + incident dedupe
        │
        └──► codex exec (read-only) ──► Slack thread reply
```

This first phase does not hold GitHub credentials and cannot create issues,
branches, pull requests, or merges. The structured result only recommends
whether an issue or draft PR would be appropriate for human follow-up.

## Runtime boundaries

- Alert delivery never waits for Codex. The relay returns `202` after Slack
  accepts the original alert and the durable SQLite job is queued.
- Producers keep their existing incoming webhook as a fallback. A relay timeout
  or non-2xx response therefore does not drop the page.
- `codex exec` runs with a read-only sandbox, user config/rules disabled, web
  search disabled, and no hooks, apps, or subagents.
- Only `PATH`, `HOME`, `LANG`, and `LC_ALL` reach shell commands started by
  Codex. The Codex API key, Slack token, and relay token are not forwarded.
- Alert context is bounded and redacted before it reaches Codex. The original
  Slack text is never included in the model prompt.
- Codex sessions are persisted under `CODEX_TRIAGE_CODEX_HOME`; each Slack
  analysis includes its thread ID so an operator with relay-host access can
  resume the investigation deliberately.
- Fingerprints deduplicate transport retries. Firing alerts can be analyzed
  again after their producer cooldown; recovery events close the active
  incident and reply in its Slack thread.

## Relay configuration

Install the Codex CLI on the relay host and create a dedicated secret file:

```bash
sudo install -d -m 0750 /etc/hybrid-inference
sudo install -m 0600 /dev/null /etc/hybrid-inference/codex-triage.env
```

Populate `/etc/hybrid-inference/codex-triage.env`:

```text
CODEX_TRIAGE_RELAY_TOKEN=<random shared bearer token>
CODEX_TRIAGE_SLACK_BOT_TOKEN=xoxb-...
CODEX_TRIAGE_SLACK_CHANNEL_ID=C0123456789
CODEX_API_KEY=...

# Optional overrides
CODEX_TRIAGE_CODEX_BINARY=/absolute/path/to/codex
CODEX_TRIAGE_CODEX_MODEL=
CODEX_TRIAGE_TIMEOUT_SECONDS=600
CODEX_TRIAGE_MAX_ATTEMPTS=2
CODEX_TRIAGE_MAX_PENDING_JOBS=100
```

The Slack app needs `chat:write` and must be added to the target channel. The
service intentionally uses `chat.postMessage`, rather than an incoming webhook,
so it can capture the original message timestamp and post threaded replies.

Install and start the unit:

```bash
sudo cp deploy/systemd/codex-alert-triage.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now codex-alert-triage
curl http://127.0.0.1:8091/healthz
```

The unit binds only to loopback, mounts `/srv/hybridInference` read-only, and
writes queue/session data under `/var/lib/hybrid-inference-codex-triage`. Its
mount namespace also hides the relay secret file, `.env`, runtime data, and
server logs from the service and every Codex subprocess. Relay settings are
read from the systemd environment; the triage application does not parse the
repository `.env` file.

## Producer configuration

The backend producer reads these variables:

```text
CODEX_TRIAGE_RELAY_URL=http://127.0.0.1:8091
CODEX_TRIAGE_RELAY_TOKEN=<same shared token>
SLACK_ALERTS_WEBHOOK_URL=<existing fallback webhook>
```

`127.0.0.1` is correct for a bare-metal backend on the relay host. The
production backend currently runs in Docker, where loopback points at the
container; configure the same restricted TLS relay URL used by the Worker (or
another host-reachable reverse-proxy address) instead.

The Cloudflare status-monitor needs a TLS endpoint that forwards to the relay;
do not expose the loopback listener directly. Put it behind the existing
reverse proxy or a restricted Cloudflare Tunnel, then set Worker secrets:

```bash
cd services/status-monitor-worker
npx wrangler secret put CODEX_TRIAGE_RELAY_URL
npx wrangler secret put CODEX_TRIAGE_RELAY_TOKEN
npx wrangler secret put SLACK_WEBHOOK_URL
```

Keep `SLACK_WEBHOOK_URL` during the initial rollout. It is used only when the
relay cannot confirm delivery.

## Smoke test

Send a synthetic event from the relay host:

```bash
curl -i http://127.0.0.1:8091/v1/alerts \
  -H "Authorization: Bearer $CODEX_TRIAGE_RELAY_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "version":"1",
    "alert_id":"smoke-1",
    "fingerprint":"smoke:staging:provider",
    "source":"manual-smoke-test",
    "status":"firing",
    "severity":"warn",
    "title":"Synthetic provider warning",
    "environment":"staging",
    "occurred_at":"2026-07-10T00:00:00Z",
    "summary":"Synthetic event; no production impact",
    "context":{"provider":"example"},
    "slack_text":"Synthetic Codex triage smoke test"
  }'
```

A successful request returns `202`, posts the synthetic alert immediately, and
adds a Codex analysis reply after the queued job finishes. Reusing the same
fingerprint inside the dedupe window returns `duplicate: true` without posting
another top-level message.

## Rollout

1. Deploy the relay with only a staging producer configured.
2. Confirm raw-alert latency, analysis usefulness, timeout rate, and false
   conclusions for at least one week.
3. Enable the production gateway producer while retaining webhook fallback.
4. Add status-monitor delivery after the relay has a restricted public TLS
   endpoint.
5. Consider GitHub Issue and Draft PR actions in a separate change with
   separate credentials, explicit policy gates, and branch protection. Automatic
   merge remains out of scope.
