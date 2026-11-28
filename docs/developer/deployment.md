# Deployment Guide

This page covers running HybridInference as a long-lived deployment: reaching
it, operating and releasing it, the first administrator, alerts, and recovery.
It assumes the stack already starts; first-time setup is in
[Installation](installation.md#running-with-docker).

## What the stack is

`make up` starts three containers from `deploy/docker/docker-compose.yml`:

| Service | Image / build | Published on |
|---|---|---|
| `backend` | built from `deploy/docker/Dockerfile.backend` | `${BACKEND_HOST:-127.0.0.1}:${BACKEND_PORT:-8080}` |
| `frontend` | built from `deploy/docker/Dockerfile.frontend` | `${FRONTEND_HOST:-127.0.0.1}:${FRONTEND_PORT:-3001}` |
| `postgres` | `postgres:16.16` | `127.0.0.1:${DB_PORT:-5432}` |

All three ports are published on loopback by default. The containers share the
`hybridinference` network, where the backend reaches the database as
`postgres:5432`. Under Compose, `DB_PORT` in `.env` sets only the host side of
the database's port, and `DB_HOST` is ignored (it matters only for a backend
started from source). The optional `pgadmin` service starts only with the
`admin` profile; see [pgAdmin](database.md#optional-pgadmin).

### Keeping it private: an SSH tunnel

A staging or scratch instance can keep the loopback defaults. Forward both
ports from your workstation:

```bash
ssh -L 3001:127.0.0.1:3001 -L 8080:127.0.0.1:8080 <user>@<your-server>
```

Then open `http://localhost:3001`. Keep the local end on `localhost` or
`127.0.0.1`: the refresh cookie is `Secure` by default (`COOKIE_SECURE`), and
browsers accept a `Secure` cookie only over HTTPS or from a loopback origin.
Port 8080 is forwarded because the console calls the API at the
`NEXT_PUBLIC_API_BASE` built into its image, `http://localhost:8080` by
default; changing it needs a frontend rebuild.

Check that the stack answers:

```bash
curl -s http://localhost:8080/health
curl -s http://localhost:8080/v1/models
```

The default `CORS_ALLOWED_ORIGINS` covers ports 3000, 3001 and 3002 on
`localhost` and `127.0.0.1`, and HTTPS on port 8443, so a tunnel needs no CORS
change.

### Putting it on the public internet

Nothing in the stack terminates TLS, and this repository ships no reverse-proxy
configuration. A proxy on the host uses `127.0.0.1:3001` for the console or
`127.0.0.1:8080` for direct gateway access; a proxy container on the same
Docker network uses `frontend:3001` or `backend:8080`.

For a proxy on another machine, set `FRONTEND_HOST` in `.env` to an interface it
can reach (`0.0.0.0` for all IPv4 interfaces), restrict access to that proxy,
and run `make up`. The console also forwards the API, so exposing its port
exposes the API. Set `BACKEND_HOST` only if you need direct gateway access.
Releases before the loopback default bound the console to `0.0.0.0`; if you
relied on that, set `FRONTEND_HOST` before upgrading.

Which paths the console serves and which it forwards is set by
`deploy/public-paths.json` and `next.config.js`, not by the proxy; see
[The public path table](public-path-table.md).

#### What your proxy still has to do

Nothing inside the stack provides these. Without a proxy (a tunnel daemon
connecting straight to `127.0.0.1:3001`, say) you have none of them, and nothing
warns you:

| Protection | What a proxy in front typically does |
|---|---|
| Next.js Server Action guard | Refuse requests carrying a `Next-Action` header (`if ($http_next_action) { return 403; }`), so nobody outside the console can invoke its server actions |
| Request body cap on `/v1/` | Bound request bodies (commonly `client_max_body_size 50m`). The gateway itself refuses bodies over `REQUEST_MAX_BODY_MB` (32 MiB) |
| `X-Forwarded-For` rewriting | Overwrite a client-supplied chain, so only hops the proxy added reach the gateway |

The gateway reads forwarded headers only from proxies you authorize, but never
rewrites them; see [Trusted proxies and client IPs](trusted-proxies-and-client-ips.md).

### Splitting inference traffic at the entry

By default every request goes through the console, so a release or restart of
the console interrupts long streams just as a gateway release does. An entry
that routes by path (an Ingress rule, a tunnel's path routing, a load balancer)
can instead send the paths the public path manifest marks `entry_split`, `/v1`
and `/anthropic`, straight to the gateway, and everything else to the console.

This is optional, and this repository ships no entry configuration. The console
image still forwards both prefixes, so removing the entry's two rules rolls the
split back, provided the gateway still trusts the console and the console's
stop grace period is unchanged. A gateway release still gives open requests 5
seconds after the stop signal.

On the split paths, the entry must meet these conditions:

| # | Condition |
|---|---|
| E1 | It sends to the gateway only a path equal to `/v1` or `/anthropic`, or starting with `/v1/` or `/anthropic/`, matched after resolving `.` and `..` segments and percent-encoding (`/v1/%2e%2e/openapi.json` is not under `/v1`) |
| E2 | It does not buffer responses, and does not read a whole request body before forwarding it |
| E3 | Its idle timeout is longer than the gateway's `REQUEST_TIMEOUT_SECONDS`, and any limit on a request's total duration is no shorter than `STREAM_REQUEST_TIMEOUT_SECONDS`. Streams send a keepalive every 15 seconds |
| E4 | Its request body limit is no lower than the gateway's own, including file uploads under `/v1` |
| E5 | When the client disconnects, it closes the request to the gateway |
| E6 | It overwrites the forwarding header the client sent (`X-Forwarded-For` or `CF-Connecting-IP`) |
| E7 | Only the entry and the console can reach the gateway's port |
| E8 | It does not resend a `POST` once it has reached the gateway; retrying when the connection could not be established is fine |
| E9 | Taking an instance out of rotation stops new requests but keeps open connections; see [Handing traffic to a new gateway process](#handing-traffic-to-a-new-gateway-process) |

Clients see the gateway's own answers, with two differences: the
`X-Frame-Options` and `Referrer-Policy` page headers are not sent (the gateway
adds `X-Content-Type-Options: nosniff` itself), and when the gateway is down the
`502`, `503` or `504` carries the entry's body, not the console's
OpenAI-shaped JSON. To see which way a request went, compare the gateway's
request log, whose `peer_ip` names the process that connected, with the entry's
access log.

#### Client addresses with the entry in front

Requests through the entry must resolve to the same client addresses as before,
with the console stopped. Do not get there by trusting more: keep
`TRUST_PROXY_HEADERS` on, add no network that holds other workloads to a trust
list, and do not treat an `unknown` address as normal.

With static trust, add the entry's exact peer address, as the gateway sees it,
to `TRUSTED_PROXIES` for an entry that writes `X-Forwarded-For`, or to
`TRUSTED_CLOUDFLARE_NETWORKS` for Cloudflare, and keep the console's own entry.
If the gateway cannot tell the entry's connections from other processes' (behind
a loopback port mapping every process on the host shares one address), solve
that first.

Kubernetes proxy discovery does not support the split: an entry outside the
discovered Service resolves to `unknown`, and with the frontend stopped the
discovered set is empty and `/health/ready` answers
`503 proxy_discovery_unavailable`. A deployment that uses discovery keeps the
console's forwarding.

### Checking an entry split with the probe

`ops/dev/entry_split_probe.py` tests the inference path from outside, through
the address clients use, on either topology. It never retries, so every failed
request counts. It runs long streams on `/v1/chat/completions`, `/v1/responses`
and `/anthropic/v1/messages`, checking each frame's timing, the end marker and
the usage report; short requests on the same paths at a fixed rate; and boundary
checks from `deploy/public-paths.json` (split prefixes reach the gateway,
unpublished paths such as `/docs` and `/v1/%2e%2e/openapi.json` do not answer,
a missing key gets `401`).

Point the long streams at a synthetic upstream: the example's fake provider
(`distributions/example/fixtures/fake-openai-provider/server.py`) started with
`--stream-frames 600 --frame-interval-ms 100` sends 600 numbered frames 100 ms
apart, and an entry that buffers shows up as frames arriving in bursts:

```bash
PROBE_API_KEY=... python ops/dev/entry_split_probe.py \
  --base-url https://api.example.org --model probe-long --short-model probe-short \
  --expect-frames 600 --frame-interval-ms 100 --short-rate 2 --output probe.json
```

The key comes from the variable `--api-key-env` names (`PROBE_API_KEY` by
default). The exit status is `0` only when nothing failed. Run it while you
release, restart or stop the console: with the split in place, nothing may
fail. Run against the gateway's own port, it reports `/docs` as reachable; that
is why E7 keeps the port private.

## Everyday operations

Run these from the repository root:

```bash
make up                  # start everything
make down                # stop everything (data survives; see below)
make restart             # restart everything
make restart s=backend   # restart one service
make ps                  # services and health status
make logs                # tail all logs
make logs s=backend      # tail one service
make build               # rebuild images, migrate, and restart
make build s=frontend    # rebuild one service
make migrate             # apply the schema migrations only
```

`make up` and `make build` create the external volume
`hybridinference_postgres_data` if it is missing, apply the schema migrations
with the backend image, and then start the services. `make restart` migrates
nothing.

Name optional profiles on the command line, which outranks every env file:
`make up COMPOSE_PROFILES=admin` (a comma-separated list). To keep host-local
choices (profiles, `PGADMIN_PUBLISH`) across commands, copy `.ops.env.example`
to `.ops.env` and uncomment what this host needs.

### Diagnostics, deployment and recovery

```bash
make doctor
make smoke MODEL=team-chat
make deploy BACKEND_IMAGE=registry/backend@sha256:<digest> FRONTEND_IMAGE=registry/frontend@sha256:<digest> MODEL=team-chat
make rollback MODEL=team-chat
make backup BACKUP=/private/path/gateway.dump
make restore-check BACKUP=/private/path/gateway.dump
```

Set `GATEWAY_API_KEY` for authenticated inference checks. `make smoke` runs the
distribution's own `smoke.py` when it ships one.

`make deploy` records the running artifacts, pulls the pinned images, runs the
migrations with the new backend image, imports the previous release's file
configuration when `LEGACY_MANIFEST` is set (see
[Model and routing documents in the database](configuration.md#model-and-routing-documents-in-the-database)),
then switches the services and runs the acceptance smoke. Any failure after the
switch, including an interruption, restores the previous images and runs the
acceptance on them. `make rollback` starts the recorded previous images without
running their migrations.

The record lives in `.ops/<distribution>/` (`.ops/none/` without a
distribution, in a subdirectory named after `COMPOSE_PROJECT_NAME` when the
shell sets it): `images.env` pins the images, and `previous.json` and
`current.json` name the releases. A deploy writes them only once its release is
accepted. While `images.env` exists, `make up`, `make migrate` and the other
lifecycle commands start the pinned images, and `make build` refuses; delete
`images.env` to return to source builds.

`make backup` writes to `.ops/<distribution>/gateway.dump` unless you set
`BACKUP`. The dump holds every stored secret, so keep it private and copy it off
the host. `make restore-check` is described under [Backup](database.md#backup).

### Handing traffic to a new gateway process

A release can start a new gateway process beside the old one and stop the old
one only when it has nothing left: no request cut short, no request-log row or
cost lost. The release asks the old process instead of waiting a fixed time.
Stopping with a signal is unchanged: open requests get five seconds, then are
cancelled.

The handoff routes exist only when the process has `HANDOFF_TOKEN` in its
environment, a random value of at least 32 characters; otherwise they answer
404. They live under `/internal/handoff`, which no public path list includes, so
neither the console nor an entry forwards them. Call each process at its own
address with `Authorization: Bearer <HANDOFF_TOKEN>`. This is not the
administrator token, which is read-only for changes wherever administrators
need a second factor.

| Call | What it does |
|---|---|
| `GET /internal/handoff` | The state (`serving`, `draining` or `sealed`), the work under way, `blocking` (why a seal would fail now) and `idle` |
| `POST /internal/handoff/drain` | Begins draining |
| `POST /internal/handoff/seal` | Seals if nothing is left (200); otherwise answers 409 with `blocking` and keeps draining |
| `POST /internal/handoff/resume` | Serves normally again, from draining or sealed; this abandons a release |

A release runs in this order:

1. Start the new process and accept it: `/health/ready` answers 200 and it
   serves the acceptance requests.
2. Take the old process out of the entry's rotation (E9).
3. Drain the old process.
4. Call seal until it answers 200. Polling never holds a drain open.
5. Stop the old process.

If the seal does not succeed in the time you expect, do not stop the process:
keep it, record the release as unfinished, and read `blocking`. The longest a
request may run is `STREAM_REQUEST_TIMEOUT_SECONDS`, an hour by default. To
abandon the release, resume the old process and remove the new one.

A draining process still serves every request that reaches it. Its
`/health/ready` answers 503 with `"reason": "draining"` (`/health` stays 200),
every response carries `Connection: close`, and idle connections are closed
after five seconds. It claims no new batch items and runs no scheduled jobs;
the other process does.

`blocking` lists what is left:

| `blocking` | What is left |
|---|---|
| `requests_in_flight` | A request in progress, streams included |
| `connections_open` | An open client connection, other than the release's calls and probes |
| `background_writes` | Work a request left behind, such as a cost increment or an alert being sent |
| `request_log_unwritten` | Request-log rows queued or being written |
| `request_log_lost` | A request-log row that was dropped or failed to write since the process started. Never clears |
| `stream_close_abandoned` | A stream whose close took more than ten seconds and was cut short, so its billing or logging may be incomplete. Never clears |
| `batch_work_running` | A batch item, validation, sealing, delivery or sweep still running here |
| `scheduled_jobs_running` | A scheduled job, or a broadcast still sending |
| `spend_unwritten` | A cost not yet added to the counters, or, in the shared spend mode, a reservation not yet settled |
| `spend_unconfirmed` | A cost that waiting will not confirm: one the counters refused, a write with an unknown outcome, or, in the shared spend mode, a quarantined reservation or refused increment. Never clears |

The three reasons that never clear keep the process draining for as long as it
lives. Keep it, record what the status reports, and reconcile before you stop
it. There is no way to force a seal, and clearing the records is not
reconciling. An empty `blocking` also means every cost the process recorded is
confirmed in the counters (each ledger in `spend` reports `confirmed`).

Checking and switching to `sealed` is one step. After it, every request is
refused with 503 and the code `instance_sealed` before any upstream call, and
counted in `requests.refused_after_seal`; a release that saw any did not hand
over cleanly. At shutdown the process logs its state (event
`handoff_state_at_shutdown`).

Run one uvicorn worker per instance: with several workers behind one port, or
under gunicorn, drain and seal answer 409. While two processes run side by side,
each enforces per-user concurrency, login rate limits and auth blocks on its
own, so a user can briefly have twice their concurrent requests. Budgets are
shared only in the shared spend mode, which is switched on with every process
stopped.

### What a change actually requires

| You changed | Do this |
|---|---|
| A setting in **Admin → Settings** | Nothing, unless it is marked **Restart required**: then select **Restart backend**, or run `make restart s=backend` |
| A value in `.env` that stays in the environment: the database connection, ports, the configuration selectors a rollback needs, console values | `make up`. A container reads its `env_file` when it is created, so `docker compose restart` keeps the old environment |
| Any other value in `.env` | Nothing happens once the database has the setting; change it in Admin. See [Upgrading a deployment that kept its settings in `.env`](#upgrading-a-deployment-that-kept-its-settings-in-env) |
| The model catalog, routing policy or alert rules | Follow the **Restart required** marker; no rebuild |
| Site configuration or branding | Applies when settings refresh; no rebuild or restart |
| A file in the mounted site-assets directory | Replace the file in the deployment overlay; no rebuild |
| `AGENT_WEB_INTERNAL_URL` or `AGENT_CONTROL_PLANE_INTERNAL_URL` | `make up`; the console reads them at runtime |
| A build-time `NEXT_PUBLIC_*` value | `make build s=frontend`; it is compiled into the browser bundle |
| Backend or frontend source | `make build`, or `make build s=<service>` |

The console reads its name and branding from the backend's `/site-config` at
runtime; no build argument brands it. A gateway with no `SITE_BRANDING`
document gets the neutral console.

## Configuration

`.env` holds the database connection and the container settings; every other
setting lives in the database and is edited in **Admin → Settings**. See
[Settings stored in the database](configuration.md#settings-stored-in-the-database).

### Upgrading a deployment that kept its settings in `.env`

Nothing has to change before the upgrade. At its first start the new backend
copies into the database every setting that has a value in its environment
(from `.env`, an overlay's `deploy/*.env` and the Compose passthroughs) and logs
`Imported N setting(s) from the environment into the database`. The existing
`JWT_SECRET_KEY` and `API_KEY_SECRET` are copied too, so sessions and API keys
keep working, and a database that has accounts never shows the setup page.

From then on the database value wins: editing the line in `.env` changes
nothing, and Admin marks the setting **Environment ignored**. Delete the lines
when convenient, but keep a copy of `.env` until a rollback is no longer
possible, because an earlier release reads its settings only from the
environment.

The log warns about the values it does not copy:

- A value that conflicts with a stored setting, such as `TRUST_X_REAL_IP=1`
  while the stored `TRUST_PROXY_HEADERS` is off; the stored value wins. Check
  proxy trust after an upgrade: Compose passes `TRUST_PROXY_HEADERS` with a
  default of `0`, so a database that stored that `0` ignores a `1` in `.env`.
  See [Trusted Proxies and Client IPs](trusted-proxies-and-client-ips.md).
- `ERASURE_FENCE_SECRET`, once the erasure fence uses `API_KEY_SECRET`. Remove
  the variable: while it is set, the backend refuses to start; see
  [Database](database.md#secrets-for-deleting-accounts).

The Compose passthroughs' built-in defaults are imported as well, so a new
deployment starts with the default `FRONTEND_URL`, `CORS_ALLOWED_ORIGINS`,
`SITE_NAME` and mail sender until you change them.

### Application configuration

Models, routing, alert rules, site identity and branding live in the database.
This release ignores the retired `*_CONFIG_PATH`, `DISTRIBUTION_CONFIG_PATH` and
`DISTRIBUTION_CONFIG_MODE` variables, and warns at startup while one is set.
Keep them, and the files they name, through the rollback window: a rolled-back
release reads its files through them, and the legacy import uses them. See
[Configuration](configuration.md).

### Local inference servers

The backend container reaches servers on the host at `host.docker.internal`
(mapped with `extra_hosts: host.docker.internal:host-gateway`). Write that
address in the model's route; the gateway never rewrites provider URLs:

```json
{
  "route": [
    {
      "kind": "openai_compat",
      "base_url": "http://host.docker.internal:8001/v1"
    }
  ]
}
```

A backend running directly on the host uses `localhost`. See
[Adding a New Local Model](add-local-model.md).

## Health checks

Anyone may call `/health` and `/health/ready`, and gets the status code and
`{"status": ...}`. An administrator's credential (a console token or
`ADMIN_TOKEN`) also shows the reason, the process's `instance`, the route count
and each store's state:

```bash
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8080/health
```

```json
{
  "status": "healthy",
  "routes_configured": 3,
  "instance": "8c1d4a7e2f6b9035",
  "database_configured": true,
  "database_connected": true,
  "stores": {
    "operational_store": {"status": "ok", "backend": "postgres", "cache": "in_memory"},
    "log_store": {"status": "ok", "backend": "postgres"}
  }
}
```

| Endpoint | Answers 503 when |
|---|---|
| `/health` | Every configured store is down, or the configured database was unreachable at startup (`database_unavailable_at_startup`). One store down gives `"status": "degraded"` with 200, so the container `HEALTHCHECK` does not kill a backend that still serves |
| `/health/ready` | Any configured store is down, the process is draining, or Kubernetes proxy discovery has no fresh proxy set. Use it for readiness |
| `/health/deep` | Every configured store is down. It also reports each endpoint's health |

With `DB_ENABLED=false`, `database_configured` is `false` and the status is
`healthy`.

### Marking monitor traffic

Send `X-Probe: synthetic` on a monitor's inference requests to keep them out of
`api_logs`, the dashboards and RouteWise's learning. A marked request also
records no routing observation and gets an `X-Provider` response header naming
the backend that answered.

The marker is honoured only from an authenticated API key with the internal or
admin role, never from a free or pro key, an agent grant, or an anonymous
caller on a gateway with `USER_AUTH_ENABLED=0`. It never affects billing, and
it does not protect a monitor from the auth-failure blocklist (see
[below](#a-monitor-or-service-account-is-suddenly-getting-429s)). To log marked
requests anyway, turn on the `log_synthetic_probes` runtime setting.

## Alerting

The backend sends alerts to one Slack or WeCom (企业微信) group webhook. In the **Alerts** section of **Admin → Settings → Operations**, choose
a **Notification channel** and enter its webhook URL; saved URLs are
write-only. Choose **None** to turn notifications
off.

| Setting | Value |
|---|---|
| `ALERTS_ENABLED` | Turns the in-process engine on; applies after a backend restart |
| `SLACK_ALERTS_WEBHOOK_URL` | Dedicated Slack webhook; falls back to `SLACK_WEBHOOK_URL` when empty |
| `SLACK_WEBHOOK_URL` | Slack fallback; also enables the failed-request detector |
| `WECOM_ALERTS_WEBHOOK_URL` | WeCom group webhook; also enables the failed-request detector |

Slack and WeCom are mutually exclusive. Webhook changes apply after a backend
restart, or within about ten seconds once an operator activates the
`alerts.runtime` live group; see
[Settings that apply without a restart](configuration.md#settings-that-apply-without-a-restart).
**Send test notification** sends a marked test without saving anything. A URL
typed into the test field must be the platform's official HTTPS endpoint
(`hooks.slack.com`, `hooks.slack-gov.com` or
`qyapi.weixin.qq.com/cgi-bin/webhook/send`), so an admin session cannot make the
server call an arbitrary address. A saved HTTPS URL may point elsewhere, such as
a Slack-compatible Mattermost.

Rules and thresholds are the `ALERT_RULES` document, under **Advanced** in
**Admin → Settings → Site & Setup** (empty means built-in thresholds; changes
need a restart). Two auth rules have deliberate defaults:
`rules.auth_failure_spike` is off, because bad keys are background noise, and
`rules.auth_ip_blocked` is on, because the source the blocklist refuses is
sometimes the deployment's own monitor.

### What an auth alert tells you

An auth-failure card names the sources, how many distinct addresses, the key
prefixes presented, why each failed and the paths hit; the recovery card
summarizes the whole incident. **Known Accounts** names accounts whose issued
keys were refused, with `credential_state` (`revoked`, `expired`,
`user_suspended`): usually a stale monitor or CI credential. The lookup is
skipped under load or with `AUTH_FAILURE_IDENTIFY_CALLER=false`, so no named
account does not prove the traffic is external. **Arrived Via Peers** appears
when the addresses came from forwarding headers, and names the sockets they
arrived on. Counts marked `(capped)` are lower bounds.

### Silencing alerts from the dashboard

**Admin → Settings → Operations** has two controls, neither of which changes
`ALERT_RULES`. **Alert Notifications** snoozes every alert for up to 24 hours
(the admin API accepts up to seven days). **Alert Types** mutes one type, such
as `auth_ip_blocked`, for an hour, a day, a week or until unmuted. Mutes are
stored in the database and reach every process within seconds. An incident that
opens while muted or snoozed closes without a recovery message; one posted
before still gets its recovery.

## The first admin account

A new deployment creates its first administrator on the console's setup page.
Until then the backend refuses every sign-up, by email or phone:

1. Read the setup code from the backend's log. It is stored in the database,
   stays the same across restarts until setup is complete, and is logged at
   every start whatever `LOG_LEVEL` is:

   ```bash
   docker logs hybridinference-backend 2>&1 | grep 'setup code'
   ```

2. Open the console; while setup is pending every page sends you to `/setup`.
3. Enter the code and choose a username and password. This administrator has
   no email address and signs in with the username. It can add one later in
   **Account Settings → Email Address** once mail delivery works.

Anyone who can read the backend log or the database can create this account, so
keep both private. Wrong codes are limited to ten per client address in 15
minutes; the right code is never refused. Once an administrator exists, you can
close signup with the `signup_enabled` runtime setting.

**Choose a username that is hard to guess.** Sign-in counts attempts before it
checks the password:

| Limit | Default | Setting |
|---|---|---|
| Per username from one address | 5 in 15 minutes | `LOGIN_RATE_LIMIT_PER_15MIN` |
| Per username from all addresses | 50 in 15 minutes | `LOGIN_ACCOUNT_RATE_LIMIT_PER_15MIN` |
| Per address | 20 in an hour | `LOGIN_RATE_LIMIT_PER_HOUR_PER_IP` |

A browser that has signed in to the account before holds a device cookie
(`<REFRESH_TOKEN_COOKIE_NAME>_device`, valid 180 days) and gets five attempts of
its own per 15 minutes, so other people's guesses do not lock it out. Changing
`JWT_SECRET_KEY` invalidates device cookies. Other browsers share the limits
with their address, and behind the bundled console every browser has the same
address unless the gateway trusts your edge's header (see
[Behind the console](trusted-proxies-and-client-ips.md#behind-the-console)).
`admin` is the first name anyone tries.

The recovery tools below need no sign-in. `ADMIN_TOKEN`, when set, also still
reaches the admin API.

### Creating an administrator without the browser

`ops/admin/create_admin.py` creates an account with `role='admin'`,
`status='active'` and `email_verified=TRUE`, or promotes the existing account
with that address. Run it from the repository root, in the project environment
(`source .venv/bin/activate` after `make setup-dev`), once the migrations have
run:

```bash
python ops/admin/create_admin.py --email you@example.com
```

It reads the `DB_*` settings from `.env` and prompts for the password when you
omit `--password`. Any account created this way also completes first-run setup.

### Resetting a forgotten password

The setup administrator has no email, so reset its password where the backend
runs:

```bash
docker exec -it hybridinference-backend python -m serving.auth.reset_password <username>
```

It reads only the `DB_*` settings, asks for the new password twice (or prints a
strong one with `--generate`), and signs the account out everywhere. It also
accepts an email address. From a source checkout, run
`uv run python -m serving.auth.reset_password <username>` at the repository
root. It works on a database whose migrations are not yet applied; name the
account by email there.

### Undoing a setting that breaks sign-in

A setting that stops sign-in cannot be fixed in `.env` or from the console.
Reset it from the command line:

```bash
docker exec -it hybridinference-backend python -m serving.config.manage reset JWT_ALGORITHM
```

`list`, `get KEY`, `set KEY VALUE` and `reset KEY` validate like Admin and never
print secrets; see [Without the console](configuration.md#without-the-console).
The backend picks the change up within about ten seconds, or after a restart for
a setting marked **Restart required**.

### Promoting accounts with `ADMIN_EMAILS`

`ADMIN_EMAILS` (under **Security** in **Admin → Settings → Access & Limits**)
makes a listed address an administrator when that account signs in. You do not need it
for the first administrator.

```{warning}
Never combine open signup (`signup_enabled`), disabled email verification
(`signup_require_email_verification` off) and `ADMIN_EMAILS` on an instance
others can reach. Without verification, `POST /auth/signup` marks any address
verified, and the backend promotes a listed address to `admin` on login and on
every token refresh. Anyone who guesses an `ADMIN_EMAILS` value becomes an
administrator.
```

Email verification is on by default and needs working mail: set `SMTP_HOST`,
`SMTP_USER` and `SMTP_PASSWORD` with `EMAIL_PROVIDER=smtp`, or
`CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_EMAIL_API_TOKEN` with `cloudflare`. There
is no default mail service; see
[Email and SMS delivery providers](configuration.md#email-and-sms-delivery-providers).
`ADMIN_EMAILS` is also the default recipient list for signup approval mail;
`SIGNUP_NOTIFY_EMAILS` overrides it.

### Requiring two-factor authentication of administrators

Any account can add an authenticator app (any TOTP app) in
**Settings → Two-Factor Authentication** and then confirms lasting changes, such
as raising a role, minting a key for another account or exporting secrets, with
a six-digit code. It gets ten recovery codes, shown once.

`ADMIN_TWO_FACTOR_REQUIRED=true` in `.env` makes an authenticator a condition of
administrator access. It is read from the environment only, so an administrator
session cannot switch it off. With it on:

- a console session reaches the Admin API only after giving a code, and again
  when it appears in a different kind of client;
- an administrator without an authenticator has no administrator access until
  they add one, and only an account with one can be given the `admin` role;
- `ADMIN_TOKEN` is read-only, so move automation that writes with it first.

Enroll the administrators before you turn it on: until an account has an
authenticator, anyone with its password can add theirs. **Admin → Users** shows
whether an account has one and removes a lost one.
`TWO_FACTOR_STEP_UP_MINUTES` (default 10) is how long a code keeps confirming
changes.

```{warning}
An administrator who loses both the device and the recovery codes cannot reach
the Admin API. Another administrator removes the authenticator in
**Admin → Users** (`POST /admin/users/{user_id}/two-factor/reset`). If there is
no other administrator, delete it in the database and add a new one with the
account's password:

    DELETE FROM user_two_factor WHERE user_id = '<the account id>';
```

## Database

PostgreSQL runs in the `postgres` service with its data in the volume
`hybridinference_postgres_data`. The schema, backups, pgAdmin and what the
database holds are in [Database](database.md).

### Applying schema migrations

Run the migrations with the image you are about to start, while the old one
still serves, and start the new one only when they succeed:

| Deployment | The step |
|---|---|
| The bundled Compose stack | `make migrate`; `make up` and `make build` run it for you |
| Pinned images (`make deploy`) | `make deploy` runs it before switching over; `make rollback` never runs an older image's migrations |
| A systemd unit (`deploy/systemd/`) | `.venv/bin/python -m migrations upgrade` in the unit's working directory, with its environment, before `systemctl restart` |

Rules for your own deploy script:

- Migrate with the same image, by digest, that you start. Images with versioned
  migrations carry the label `org.hybridinference.schema.migrations=alembic`.
- Use a one-off container with `--no-deps`, and check yourself that the
  database is up; otherwise Compose may recreate the database first. If the
  backend service pins an `ipv4_address`, give the migrations their own service
  (same image and `DB_*` settings, the database's network only, a profile so
  `up` never starts it), or Docker refuses with `Address already in use`.
- If the script arrives on stdin (`ssh host bash -s`), run the one-off with
  `</dev/null`; `compose run` reads stdin even with `-T` and swallows the rest
  of the script.
- Do not run migrations on every start (a `service_completed_successfully`
  dependency, an init container): every restart would wait on table locks.
- If the step fails, stop. Exit status 75 means a lock was not available in
  time; run it again later.

The backend refuses to start on an unmigrated database, so a missed step shows
up as a failed start. Have the step working before the first image with the
label arrives.

## Troubleshooting

### A service will not start

```bash
make logs s=backend
make ps
```

| Message | Fix |
|---|---|
| `required variable DB_NAME is missing a value: DB_NAME must be set in .env file` | Compose stopped before starting anything. Set `DB_NAME`, `DB_USER` and `DB_PASSWORD` in `.env` |
| Port already in use | Override `BACKEND_PORT`, `FRONTEND_PORT` or `DB_PORT` in `.env` |
| Database connection failed | Check the `postgres` health in `make ps` |
| `The database records no schema version` or `The database is at revision …, behind this code's …` | Run `make migrate` (or [your deployment's step](#applying-schema-migrations)), then start the backend |
| `Configuration load failed after 3 attempts`, then `Authentication configuration incomplete` | The backend could not read its settings from the database. Fix the connection; do not add secrets to `.env` |
| The console shows a configuration error with a retry button | The console could not get a valid `/site-config` from the backend within three seconds. Check the frontend logs and the backend, then retry |

### The backend's memory after large requests

At startup the backend fixes glibc's mmap threshold at 128 KiB, glibc's own
starting value, and logs `Allocator: glibc mmap threshold fixed at 128 KiB`.
Without this, the threshold rises with every large request body or reply the
backend frees, and the process keeps a burst's memory long after the burst
ends. `serving/servers/allocator.py` has the details. An operator's own
`MALLOC_MMAP_THRESHOLD_`, or `glibc.malloc.mmap_threshold` in `GLIBC_TUNABLES`,
takes precedence. With any other C library the backend changes nothing.

### A monitor or service account is suddenly getting 429s

`Too many authentication failures from this IP. Temporarily blocked.` is the
gateway's own abuse defense. A source with `AUTH_FAILURE_BLOCK_THRESHOLD`
failed authentications within `AUTH_FAILURE_BLOCK_WINDOW_SEC` (200 in a day by
default) is refused for `AUTH_FAILURE_BLOCK_DURATION_SEC` (a day).

It usually hits a caller you own whose key was rotated or revoked. The block is
checked before the key, so fixing the credential does not lift it, and the 429
hides whether the original 401 or 403 is fixed. To find the caller, turn on the
`log_rejected_requests` runtime setting: refusals appear in
**Recent Requests** as `ip_blocked` rows naming the account behind the key, with
its state (`revoked`, `expired`, `user_suspended`). A row without a user is
unresolved, not proof of a stranger.

Fix the credential, then clear the block:

```bash
# Which sources is this worker refusing?
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://localhost:8080/admin/auth-blocks

# Lift one. `ip` takes a raw address, or a bucket key exactly as listed
# (IPv6 sources are bucketed to their /64).
curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"ip": "203.0.113.7"}' \
  http://localhost:8080/admin/auth-blocks/clear
```

`cleared: false` means nothing was blocked. To exempt a source permanently, list
it in `AUTH_FAILURE_BLOCK_EXEMPT_IPS` (comma-separated addresses or CIDRs). The
blocklist lives in each process's memory: a restart clears it, and with several
workers each keeps its own.

### Resetting the stack

`make down && make up` keeps the data. To start with an empty database, remove
the volume by name. `docker compose down -v` does not remove it, because it is
declared `external: true`, and `make up` would come back on the same data:

```bash
make down
docker volume rm hybridinference_postgres_data
make up   # docker-volumes recreates it empty; Postgres re-initialises
```

The backend then starts as a new deployment: new secrets unless `.env` supplies
them, the settings `.env` still holds imported, and a new setup code.

```{warning}
`docker volume rm` is irreversible and takes every account, API key, request
log and stored setting with it. Take a `pg_dump` first if any of it matters.
```

pgAdmin's volume is separate; see [Resetting pgAdmin](database.md#resetting-pgadmin).
