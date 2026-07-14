# Codex Alert Triage with DeepSeek

The alert triage relay turns structured gateway and status-monitor alerts into
read-only Codex investigations. The original alert is delivered immediately;
analysis runs asynchronously and is posted as a reply in the same Slack thread.

```text
Cloudflare status-monitor ── restricted HTTPS ─┐
                                               │
Docker Compose network                         ▼
gateway backend ───────────────────────► codex-triage ──► Slack alert
                                               │              │
                                               │              └─ thread reply
                                               ▼
                                          codex exec
                                               │ Responses API
                                               ▼
                                      backend:8080/v1
                                               │
                                               ▼
                                       deepseek-v4-flash
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
- The image runs as an unprivileged user with a read-only root filesystem, all
  Linux capabilities dropped, and `no-new-privileges` enabled. Only the named
  queue/session volume and a bounded `/tmp` tmpfs are writable.
- The repository is copied into the image at build time through a dedicated
  Docker ignore policy. Host `.env` files, runtime data, local dependencies, and
  test fixtures are excluded; no host repository or secret directory is mounted.
- The relay image skips the private RouteWise package, which it does not import,
  so external users can build it without repository credentials.
- Only `PATH`, `HOME`, `LANG`, and `LC_ALL` reach shell commands started by
  Codex. A dedicated HybridInference API key is available to the Codex HTTP
  client but is not forwarded to those commands. The upstream DeepSeek key,
  Slack token, and relay token never enter the Codex process.
- On Linux the relay marks itself non-dumpable before starting its worker, so a
  Codex child cannot read the parent's secrets through `/proc`.
- Alert context is bounded and redacted before it reaches Codex. The original
  Slack text is never included in the model prompt.
- Codex sessions and the SQLite queue persist in the `codex_triage_data` volume.
  Each Slack analysis includes its thread ID for deliberate operator follow-up.
- Fingerprints deduplicate transport retries. Recovery events close the active
  incident and reply in its original Slack thread.

## Configure the relay

Create the relay-only environment file. Do not put these secrets in the shared
backend `.env`, because the backend container loads that whole file.

```bash
cp .env.triage.example .env.triage
chmod 0600 .env.triage
openssl rand -hex 32
```

Populate `.env.triage` with the generated token and credentials:

```text
CODEX_TRIAGE_RELAY_TOKEN=<random shared bearer token>
CODEX_TRIAGE_SLACK_BOT_TOKEN=xoxb-...
CODEX_TRIAGE_SLACK_CHANNEL_ID=C0123456789
CODEX_API_KEY=hyi-...
CODEX_TRIAGE_CODEX_MODEL=deepseek-v4-flash
CODEX_TRIAGE_HYBRID_BASE_URL=http://backend:8080/v1
```

`CODEX_API_KEY` must be a HybridInference `hyi-...` key owned by an `internal`
or `admin` user, because `deepseek-v4-flash` is internal-only. Use a dedicated
service key for production; an admin's personal key is suitable only for a
one-off smoke test. Do not put an OpenAI key or `DEEPSEEK_API_KEY` in this file.
HybridInference owns the upstream DeepSeek credential and applies its routing
and fallback policy.

`deepseek-v4-flash` is the default analysis model because it has a local H200
sglang route with the official DeepSeek API as fallback, and it is an order of
magnitude cheaper per token than `deepseek-v4-pro` — each triage run sends tens
of thousands of prompt tokens through an agentic loop. Override with
`CODEX_TRIAGE_CODEX_MODEL` when a stronger model is worth the cost.

The Slack app needs `chat:write` and must be added to the target channel. The
relay uses `chat.postMessage` so it can post the analysis in the original alert
thread.

## Enable the Compose profile

Set the producer values in the shared backend `.env`. The relay token must match
`.env.triage`.

```text
COMPOSE_PROFILES=triage
CODEX_TRIAGE_RELAY_URL=http://codex-triage:8091
CODEX_TRIAGE_RELAY_TOKEN=<same shared token>
SLACK_ALERTS_WEBHOOK_URL=<existing fallback webhook>
```

Set `ALERTS_ENABLED=true` as well when enabling the gateway's rule-based alert
engine. Other existing gateway alert producers use the relay automatically when
the URL and token are present.

Build and start the existing Compose stack:

```bash
make build
curl -fsS http://127.0.0.1:8091/healthz
docker compose -f deploy/docker/docker-compose.yml --env-file .env \
  exec codex-triage codex --version
```

The health response must contain `"ready":true`. The image pins its Codex CLI
version through `CODEX_TRIAGE_CODEX_CLI_VERSION` in `.env`; updating that value
and rebuilding upgrades the CLI. Each rebuild also refreshes the read-only
repository snapshot inspected by Codex.

## Configure the status monitor

The Cloudflare Worker cannot reach the internal Compose hostname. The relay
publishes port `8091` only on host loopback; expose it through a restricted TLS
reverse-proxy route or Cloudflare Tunnel, then set Worker secrets:

```bash
cd services/status-monitor-worker
npx wrangler secret put CODEX_TRIAGE_RELAY_URL
npx wrangler secret put CODEX_TRIAGE_RELAY_TOKEN
npx wrangler secret put SLACK_WEBHOOK_URL
```

Use the restricted HTTPS URL for `CODEX_TRIAGE_RELAY_URL` and retain
`SLACK_WEBHOOK_URL` during rollout. The webhook is used only when the relay
cannot confirm delivery.

## Smoke test

Send a synthetic event from the Compose host, using the token from
`.env.triage`:

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
    "slack_text":"Synthetic DeepSeek-backed Codex triage smoke test"
  }'
```

A successful request returns `202`, posts the synthetic alert immediately, and
adds a Codex analysis reply after the queued job finishes. Reusing the same
fingerprint inside the dedupe window returns `duplicate: true` without another
top-level message.

## Rollout

1. Enable the `triage` profile on staging and configure only one producer.
2. Confirm raw-alert latency, analysis usefulness, timeout rate, and false
   conclusions for at least one week.
3. Enable the production gateway producer while retaining webhook fallback.
4. Add status-monitor delivery after the restricted public TLS route is ready.
5. Consider GitHub Issue and Draft PR actions in a separate change with
   separate credentials, explicit policy gates, and branch protection.
   Automatic merge remains out of scope.
