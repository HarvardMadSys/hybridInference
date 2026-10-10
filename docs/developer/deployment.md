# Deployment Guide

Running HybridInference as a long-lived deployment: what starts, how to reach
it publicly or keep it private, how to create the first administrator, how to
operate it, and how to reset it without losing (or accidentally keeping)
data. A staging or scratch instance is the same stack; the sections on keeping
it private and on the first admin account matter most there.

First-time setup — cloning, the database login in `.env`, the first `make up`
and the console's setup page — is in
[Installation](installation.md#running-with-docker). This page assumes the
stack already comes up.

## What the stack is

`make up` starts three containers from `deploy/docker/docker-compose.yml`:

| Service | Image / build | Published on |
|---|---|---|
| `backend` | built from `deploy/docker/Dockerfile.backend` | `${BACKEND_HOST:-127.0.0.1}:${BACKEND_PORT:-8080}` |
| `frontend` | built from `deploy/docker/Dockerfile.frontend` | `${FRONTEND_HOST:-127.0.0.1}:${FRONTEND_PORT:-3001}` |
| `postgres` | `postgres:16` | `127.0.0.1:${DB_PORT:-5432}` |

All three published ports default to loopback. A reverse proxy on the same
host can reach the console at `127.0.0.1:3001`. The containers also join one bridge
network defined in the same Compose file, on which the backend reaches the
database as `postgres:5432`. Of the database address settings, only `DB_PORT`
from `.env` reaches this file, and only as the host half of the mapping (`127.0.0.1:${DB_PORT:-5432}:5432`); the
host-side bind address is hard-coded to loopback. `DB_HOST` is pinned to
`postgres` in the Compose file and is ignored under Compose — it matters only
for a backend started directly from source.

One more service exists in the same file but starts only when its profile is
named: `pgadmin` (profile `admin`).

`frontend` depends on `backend` with `condition:
service_started`, not `service_healthy` — deliberately, so that a backend
reporting unhealthy because its database logging is down does not stop the
console from starting.

### Putting it on the public internet

Nothing in the stack terminates TLS, and this repository ships no reverse-proxy
config to copy: certificates and the proxy in front of the two published ports
are yours to supply. A proxy running on the host can use `127.0.0.1:3001` for
the console or `127.0.0.1:8080` for direct gateway access. A proxy container on
the same Docker network can use `frontend:3001` or `backend:8080`.

If your proxy runs on another machine, set `FRONTEND_HOST` to a reachable host
interface in `.env`; `0.0.0.0` binds all IPv4 interfaces. Restrict access to the
intended proxy and run `make up` to recreate the port mapping. The console
forwards API routes as well as serving pages, so exposing its port also exposes
those routes. Set `BACKEND_HOST` separately only if direct gateway access is
needed.

Releases before the loopback default bound the console to `0.0.0.0`. If a
deployment relied on that, set `FRONTEND_HOST` explicitly before upgrading;
see [Releases and upgrades](releases.md).

Which public paths the console serves itself and which it forwards to the
backend is a separate question, and the answer is in the console's own
`next.config.js` rather than in any proxy config. See
[The public path table](public-path-table.md).

#### What your proxy still has to do

Three protections are commonly left to the reverse proxy, and nothing inside
this stack provides them. A deployment that exposes the published ports without
a proxy — a tunnel daemon connecting to `127.0.0.1:3001`, for instance — has
none of them, and nothing warns you:

| Protection | What a proxy in front typically does |
|---|---|
| Next.js Server Action guard | Refuse requests carrying a `Next-Action` header (`if ($http_next_action) { return 403; }`), so console server actions cannot be invoked from outside the console |
| Request body cap on `/v1/` | Bound completion request bodies (commonly `client_max_body_size 50m`). The gateway enforces no size limit of its own |
| `X-Forwarded-For` rewriting | Overwrite a client-supplied chain, so only hops the proxy inserted reach the gateway |

The gateway reads forwarded headers only from the proxies you authorize (see
[Trusted proxies and client IPs](trusted-proxies-and-client-ips.md)), but it
never rewrites them. Without a proxy that does, the leftmost `X-Forwarded-For`
entry is whatever the caller sent — which is why `CF-Connecting-IP` is
preferred when Cloudflare is in front.

### Keeping it private: an SSH tunnel

A staging or scratch instance, or any gateway you do not want on a network at
all, can keep the loopback defaults. Reach it from your workstation by
forwarding both ports:

```bash
ssh -L 3001:127.0.0.1:3001 -L 8080:127.0.0.1:8080 <user>@<your-server>
```

Then open `http://localhost:3001`. Keep the local end on `localhost` or
`127.0.0.1`: the refresh cookie is issued with the `Secure` flag by default
(`COOKIE_SECURE`), and browsers accept `Secure` cookies only over HTTPS or from
a loopback origin.

The console talks to the API at whatever `NEXT_PUBLIC_API_BASE` was baked in at
image build time — `http://localhost:8080` by default — which is why the tunnel
forwards 8080 as well. Changing it is a frontend rebuild, not a restart.

VS Code-family editors can manage the same forwards from their ports panel.

Check the stack answers:

```bash
curl -s http://localhost:8080/health
curl -s http://localhost:8080/v1/models
```

The default `CORS_ALLOWED_ORIGINS` already covers ports 3000, 3001 and 3002 on
`localhost` and `127.0.0.1`, plus HTTPS on port 8443, so a tunnelled instance
needs no CORS entry. Add one only when you serve the console from
another origin.

## Everyday operations

All from the repository root:

```bash
make up                  # start everything
make down                # stop everything (data survives; see below)
make restart             # restart everything
make restart s=backend   # restart one service
make ps                  # services and health status
make logs                # tail all logs
make logs s=backend      # tail one service
make build               # rebuild images and restart
make build s=frontend    # rebuild one service
```

`make up` and `make build` first run the `docker-volumes` target, which creates
the external volume `hybridinference_postgres_data` when it is missing.

To start an optional profile, pass it on the `make` command line — a variable
set there is exported into the environment of the recipe, and a shell variable
outranks every `--env-file` in Compose:

```bash
make up COMPOSE_PROFILES=admin
```

`COMPOSE_PROFILES` is a comma-separated list, so you can name several profiles
at once. It can also be set in `.env` (as `.env.example` notes), but the
command line is the form to reach for when you want certainty about which
profiles are active.

### What a change actually requires

Three different answers, and picking the wrong one looks like the change not
taking effect:

| You changed | Do this |
|---|---|
| A setting on the admin **Configuration** tab | Nothing, unless it is marked **Restart required**: then select **Restart backend** on that tab, or run `make restart s=backend` |
| A value in `.env` that stays in the environment — the database connection, ports, config paths, console values | `make up` — a container reads its `env_file` when it is *created*, so `docker compose restart` keeps the old environment |
| Any other value in `.env` | Nothing happens once the database has the setting: change it on the **Configuration** tab instead. See [Upgrading a deployment that kept its settings in `.env`](#upgrading-a-deployment-that-kept-its-settings-in-env) |
| A model registry or routing YAML | `make restart s=backend` — `config/` and `distributions/` are bind-mounted read-only, so no rebuild is needed |
| A distribution branding YAML | `make restart s=backend` — `/site-config` serves the validated snapshot loaded at backend startup |
| A file in the mounted site-assets directory | No image rebuild; replace the file in the deployment overlay |
| `AGENT_WEB_INTERNAL_URL` or `AGENT_CONTROL_PLANE_INTERNAL_URL` | `make up` — the `/agents` route handler reads them at runtime in the recreated frontend container |
| A true build-only `NEXT_PUBLIC_*` compatibility value | `make build s=frontend` — see below |
| Backend or frontend source | `make build`, or `make build s=<service>` |

The console gets its name and branding from the backend's `/site-config` at
runtime, and the `/agents` destinations from its own runtime environment, so
neither needs a new frontend image: restart the backend after editing the
branding file, and run `make up` after changing the console's environment.

The console will not render its normal pages without a valid `/site-config`
answer. If the backend is unreachable, slow (over three seconds) or returns
something invalid, the console shows a configuration error with a retry button
instead of guessing. Check the frontend logs and whether the backend is up,
then retry. The example deployment needs no extra settings for this.

A few older build-time options remain for existing build pipelines: the old
branding variables, which Compose still accepts as build arguments, and two
agent build arguments that it no longer sets. Standard builds do not need them.
Anything that really is a `NEXT_PUBLIC_*` build value is compiled into the
browser bundle and needs `make build s=frontend`; see
[The public path table](public-path-table.md).

## Configuration

### Environment

`.env` at the repository root holds the database connection and the container
settings; `.env.example` is the annotated list. Compose is invoked with
`--env-file .env` and the backend service also loads it as `env_file`. The
variables Compose itself requires are in
[Installation](installation.md#running-with-docker). Everything else is a
setting stored in the database and edited on the admin console's
**Configuration** tab; see
[Settings stored in the database](configuration.md#settings-stored-in-the-database).

### Upgrading a deployment that kept its settings in `.env`

Nothing has to change before the upgrade. At its first start the new backend
copies every setting that has a value in its environment into the database —
from `.env`, from an overlay's `deploy/*.env`, and from the values the Compose
file passes through — and logs
`Imported N setting(s) from the environment into the database`. The switches
on the **Settings** tab are copied into their own store the same way. The
existing `JWT_SECRET_KEY` and `API_KEY_SECRET` are among the copied values, so
sessions and API keys keep working, and a database that already has accounts
never shows the setup page. If the console then reports missing settings for
credentials the deployment leaves unset on purpose, mark those routes
`optional: true` in the model registry; see
[Missing settings](configuration.md#missing-settings).

From then on the database value wins. Editing such a line in `.env` changes
nothing — the **Configuration** tab marks it **Environment ignored** — so make
changes on the tab and delete the lines from `.env` when convenient. Keep a
copy until a rollback is no longer possible: a release from before the
settings moved into the database reads them only from the environment.

The `environment:` passthroughs in `deploy/docker/docker-compose.yml` work the
same way: they carry an overlay's values into the container, where they are
imported once. Their built-in defaults are imported too, so on a new
deployment `FRONTEND_URL`, `CORS_ALLOWED_ORIGINS`, `SITE_NAME` and the sender
address of outgoing mail start out as those defaults until you change them on
the tab.

### Config file resolution

The gateway picks each configuration file from an explicit
`MODELS_CONFIG_PATH` / `ROUTING_CONFIG_PATH` / `ALERTS_CONFIG_PATH`, then an
active distribution manifest, then the examples under `config/examples/`; see
[How a gateway finds its config](configuration.md#how-a-gateway-finds-its-config).

Note that the Compose file passes these through explicitly:

```yaml
ROUTING_CONFIG_PATH: ${ROUTING_CONFIG_PATH-}
MODELS_CONFIG_PATH: ${MODELS_CONFIG_PATH-}
DISTRIBUTION_CONFIG_PATH: ${DISTRIBUTION_CONFIG_PATH-}
```

An `--env-file` alone does not put a variable into a container's environment;
these lines are what carry it in. Without them the gateway would silently fall
back to the default files.

### Local inference servers

The backend container reaches servers on the host through
`host.docker.internal`, which the Compose file wires with
`extra_hosts: host.docker.internal:host-gateway`. Write that address explicitly
in the model registry:

```yaml
route:
  - kind: openai_compat
    base_url: http://host.docker.internal:8001/v1
```

For a backend running directly on the host, use `localhost` instead. The gateway
never rewrites provider URLs. See
[Adding a New Local Model](add-local-model.md).

## Health checks

```bash
curl -s http://localhost:8080/health
```

```json
{
  "status": "healthy",
  "routes_configured": 3,
  "database_configured": true,
  "database_connected": true,
  "stores": {
    "operational_store": {"status": "ok", "backend": "postgres", "cache": "in_memory"},
    "log_store": {"status": "ok", "backend": "postgres"}
  }
}
```

- `routes_configured` counts published route entries — one per model id in the
  active registry, plus one per alias. It is whatever *your* registry defines.
- `database_configured` distinguishes "this deployment asked for no database"
  from "the database is down": with `DB_ENABLED=false` it is `false` and the
  status is still `healthy`; with a database configured but unreachable at
  startup, `/health` answers **503** with `"reason":
  "database_unavailable_at_startup"`.
- `status` becomes `degraded` — still HTTP 200 — when one configured store is
  down but the other is serving. That shape is deliberate: the container
  `HEALTHCHECK` uses `curl -f /health`, so returning 503 for partial degradation
  would tear down backends that are still answering requests.

Use `/health/ready` for a strict readiness probe: it applies AND-logic across
configured stores and returns 503 unless every one of them is up.
`/health/deep` additionally reports per-endpoint health.

### Marking monitor traffic

A monitor that drives real inference — hitting `/v1/chat/completions` on a
schedule to measure a backend end to end, rather than just polling `/health` —
would otherwise land in `api_logs`, skew the dashboards, and feed RouteWise's
online learning as if it were user demand. Send `X-Probe: synthetic` on those
requests to keep them out: a marked request is left out of `api_logs` (and its
rejections out of the rejection log), does not record a routing observation,
and carries an `X-Provider` response header naming the backend that answered,
so the monitor can confirm which route it exercised.

The marker is honoured **only from an authenticated internal- or admin-role API
key** — never a free/pro key, an agent-sandbox grant, or, importantly, an
anonymous caller on a deployment running with `USER_AUTH_ENABLED=0` (auth-off
hands every caller the admin role, which is not the same as holding a monitor
identity). From any other caller the header is ignored and the request is
logged like ordinary traffic. A deployment that needs probes without auth wants
an explicit mechanism — a shared secret, a source allowlist — not this header.

The marker is about noise, not access. It cannot keep a monitor's own auth
failures from tripping the repeated-auth-failure blocklist, because that
decision is made before the presented key is read — see [A monitor or service
account is suddenly getting
429s](#a-monitor-or-service-account-is-suddenly-getting-429s).

The one thing the marker never touches is billing: cost and quota are
incremented unconditionally on every surface, for trusted and untrusted callers
alike, so a probe cannot be used to obtain unmetered inference. To keep marked
traffic in `api_logs` after all — to see a monitor's real latency and spend in
the requests dashboard — turn on the `log_synthetic_probes` runtime setting;
the routing-observation and `X-Provider` behaviour is unchanged.

## Alerting

The backend has an in-process alert engine that posts to a Slack webhook. It is
off unless you turn it on, with two settings under **Alerts** on the
**Configuration** tab:

| Setting | Value |
|---|---|
| `ALERTS_ENABLED` | on; applies after a backend restart |
| `SLACK_ALERTS_WEBHOOK_URL` | `https://hooks.slack.com/services/...` |

If `SLACK_ALERTS_WEBHOOK_URL` is empty it falls back to `SLACK_WEBHOOK_URL`, so
one webhook can serve both code paths.

Rules and thresholds are a deployment's own; this repository ships no alerts
file. Point `ALERTS_CONFIG_PATH` at yours, or leave it unset and the built-in
thresholds apply. The rule types and evaluation live in
`apps/backend/serving/observability/`.

Two auth rules are worth knowing apart, because their defaults differ on
purpose:

- `rules.auth_failure_spike` is **off**. Bad keys are internet background
  noise, and a count of them names nothing to act on. The `auth_failure` log
  records are emitted regardless.
- `rules.auth_ip_blocked` is **on**. This one fires when the blocklist starts
  *refusing* a source — a discrete decision at a much higher threshold, naming
  an address. It is on because the source is sometimes the deployment's own;
  see the troubleshooting entry below.

### What an auth alert tells you

An auth-failure card names the sources and, where it can, the accounts behind
them — the addresses they came from, how many distinct ones, the leading
characters of the keys presented, why each failed, and the paths being hit. The
recovery card carries the same picture of the incident that just closed, rather
than only the rule's name: by the time a spike resolves, the window it breached
on is empty, so the numbers have to come from a tally kept across the incident.

Two of those lines are worth reading carefully:

- **Known accounts.** Most auth failures are anonymous by construction — nobody
  was authenticated, which is the failure. A named account means a key this
  deployment *did* issue was presented and refused, with `credential_state`
  saying why (`revoked`, `expired`, `user_suspended`). That is the actionable
  case: a monitor, CI job or service account whose credential went stale.
  Resolving the owner costs one indexed lookup per failed auth, bounded by the
  shared rejection-enrichment budget and shed instantly under a flood; set
  `AUTH_FAILURE_IDENTIFY_CALLER=false` to spend nothing and lose the line. Read
  a named account as evidence and no named account as *unknown*, never as proof
  the traffic is external: the lookup is shed during exactly the flood you are
  investigating, and answers nothing on a timeout, a failed lookup, or with the
  setting off.
- **Arrived via peers.** Present only when the reported addresses did not come
  off the socket. They are then only as trustworthy as the proxy that set them,
  and a forged `X-Forwarded-For` is exactly how a source spreads its failures
  across the blocklist's buckets. The line names the sockets they actually
  arrived on.

Counts marked `(capped)` are floors, not totals: a source rotating addresses
faster than the tally tracks them stops being counted rather than being allowed
to grow it without bound. Do not size an incident from a capped number.

### Silencing alerts from the dashboard

The admin dashboard's Settings tab has two controls, and neither changes your
alerts file:

- **Slack Alerts** snoozes every alert for up to seven days.
- **Alert Types** mutes one type of alert, such as `auth_ip_blocked` or
  `circuit_open`, for an hour, a day, a week, or until it is unmuted. Every
  other type keeps sending.

A mute is stored in the database, so it survives a restart and reaches every
gateway process within a few seconds. While a type is muted nothing of it is
sent, and an incident that opens during the mute also closes without a recovery
message. An incident that was posted before the mute still gets its recovery,
and a breach still live when the mute lifts pages at its next evaluation. A mute
is for an alert you want back later; to retire a rule for good, turn it off in
your alerts file instead.

## The first admin account

A new deployment creates its first administrator on the console's setup page.
Until then the backend refuses sign-ups, so nobody else can claim the account:

1. Read the setup code from the backend's log. The code is kept in the
   database and stays the same, across restarts and worker processes, until
   setup is complete; every start logs it again, whatever `LOG_LEVEL` is set
   to:

   ```bash
   docker logs hybridinference-backend 2>&1 | grep 'setup code'
   ```

2. Open the console. While setup is pending, every page sends you to `/setup`.
3. Enter the code and choose a username and a password. This administrator has
   no email address and signs in with the username.

Whoever can read the backend log, or the database that holds the code, can
create this account, so keep both as private as the host. Wrong codes are
rate limited per client address, ten in 15 minutes; the right code is never
refused.

**Choose a username that is hard to guess.** Sign-in allows five attempts per
username in 15 minutes (`LOGIN_RATE_LIMIT_PER_15MIN`), and counts each attempt
before it checks the password, so anyone who knows a username can keep that
account locked out — and `admin` is the first name anyone tries. A lockout
lasts as long as the attempts keep coming and ends 15 minutes after they stop,
or when the backend restarts, because the counts are held in memory. Nothing
below needs a sign-in: `ADMIN_TOKEN`, when set, still reaches the admin API,
and the command-line tools work on the database directly.

**Without the browser.** `ops/admin/create_admin.py` writes the account
directly: it creates it with `role='admin'`, `status='active'`,
`email_verified=TRUE`, or promotes an existing account with the same address.
Run it from the repository root once the backend has started at least once
(the backend creates the schema):

```bash
python ops/admin/create_admin.py --email you@example.com
```

Run it inside the project environment (`source .venv/bin/activate` after
`make setup-dev`) so the `serving` package is importable. It reads `DB_HOST`,
`DB_PORT`, `DB_NAME`, `DB_USER` and `DB_PASSWORD` from `.env`, and Postgres
publishes on `127.0.0.1:5432`, so it works from the host shell. Omit
`--password` and it prompts, keeping the password out of your shell history.
Creating any account this way also completes first-run setup: a database that
has accounts never shows the setup page.

**A forgotten password.** The setup administrator has no email address, so
there is no reset link to send. Reset its password where the backend runs:

```bash
docker exec -it hybridinference-backend python -m serving.auth.reset_password <username>
```

It reads only the `DB_*` settings, asks twice for the new password — or, with
`--generate`, prints a strong one — and revokes the account's sessions, so
every browser must sign in again. It takes an email address as well, for any
other account. From a source checkout, run
`uv run python -m serving.auth.reset_password <username>` in the repository
root.

**A setting that breaks the console.** Settings live in the database, so a
value that stops sign-in from working cannot be fixed by editing `.env`, nor
from a console nobody can sign in to. Change it back from the command line:

```bash
docker exec -it hybridinference-backend python -m serving.config.manage reset JWT_ALGORITHM
```

`list`, `get KEY`, `set KEY VALUE` and `reset KEY` work like the
Configuration tab, with its validation, and never print a secret's value; see
[Without the console](configuration.md#without-the-console). The running
backend picks the change up within about ten seconds, or after a restart for a
setting marked **Restart required**.

With an administrator in place the instance can run with signup closed: turn
off `signup_enabled` on the **Settings** tab.

### Promoting accounts with `ADMIN_EMAILS`

`ADMIN_EMAILS`, under **Security** on the **Configuration** tab, makes a
listed address an administrator when that account signs in. It is not needed
for the first administrator, and it is unsafe without email verification:

```{warning}
Do not combine open signup (`signup_enabled`), disabled email verification
(`signup_require_email_verification` off) and `ADMIN_EMAILS` on an instance
anyone else can reach. Together they are a privilege-escalation recipe:

- with verification disabled, `POST /auth/signup` marks any address as
  verified without sending mail to it;
- on login *and* on every token refresh, the backend promotes any account whose
  address is listed in `ADMIN_EMAILS` from `free` to `admin`, with no check
  that the person signing up owns that address.

So a stranger who guesses or reads your `ADMIN_EMAILS` value signs up with that
address and is an admin on their first login.
```

Email verification is on by default, and with it on an account cannot log in
until it has followed a link sent to the address, which restores the
ownership check that `ADMIN_EMAILS` itself does not perform. This needs working
SMTP; without it nobody can complete a signup, and the console reports
`SMTP_USER` and `SMTP_PASSWORD` as missing settings.

`ADMIN_EMAILS` also picks the default recipients for signup approval mail. To
narrow the notification list without changing who holds the admin role, set
`SIGNUP_NOTIFY_EMAILS` (comma-separated); when it is empty, notifications fall
back to `ADMIN_EMAILS`.

Both `signup_enabled` and `signup_require_email_verification` are runtime
settings on the **Settings** tab. A `SIGNUP_ENABLED` or
`SIGNUP_REQUIRE_EMAIL_VERIFICATION` line in `.env` is copied into that store
at startup while it has no value of its own, and the stored value wins
afterwards.

## Database

PostgreSQL 16 runs in the `postgres` service with its data in the Docker volume
`hybridinference_postgres_data`. A psql shell:

```bash
docker exec -it hybridinference-postgres psql -U "${DB_USER}" -d "${DB_NAME}"
```

Schema details are in [Database](database.md).

```{warning}
The database holds every setting in plaintext: provider keys, the SMTP
password, webhook URLs, and the `JWT_SECRET_KEY` and `API_KEY_SECRET` that
sign sessions and protect API keys. A `pg_dump`, a backup, or a psql or
pgAdmin session therefore reveals all of them, and lets its holder call your
providers, decrypt users' stored API keys and sign administrator tokens.
Protect database backups and database access as you would a `.env` that held
those credentials.
```

### pgAdmin (optional)

```bash
make up COMPOSE_PROFILES=admin
```

pgAdmin then listens on `127.0.0.1:5050` with `SCRIPT_NAME=/pgadmin`, so an SSH
tunnel to that port is enough to reach it. The console can also proxy it at
`/pgadmin/`, gated on an admin session by
`apps/frontend/src/app/pgadmin/[[...path]]/route.ts`, which denies on every
unexpected condition, including a backend it cannot reach.

One thing to get right: whether pgAdmin *also* asks for its own login is set by
`PGADMIN_CONFIG_SERVER_MODE`, and the two defaults disagree. The Compose service
falls back to `False`, which serves pgAdmin with no login at all; `.env.example`
suggests `True`, which turns pgAdmin's own login on behind the console's gate.
`True` is the safer of the two.

## Troubleshooting

### A service will not start

```bash
make logs s=backend
make ps
```

- `required variable DB_NAME is missing a value: DB_NAME must be set in .env
  file` — Compose stopped at interpolation before starting anything. `DB_NAME`,
  `DB_USER` and `DB_PASSWORD` are declared required with the `${VAR:?message}`
  form, so the half after the colon is the Compose file's own text and the most
  greppable part of the line.
- Port already in use — override `BACKEND_PORT`, `FRONTEND_PORT` or `DB_PORT`.
- Database connection failed — check `make ps` for the `postgres` health status.
- `Configuration load failed after 3 attempts`, then
  `Authentication configuration incomplete` — the backend could not read its
  settings from the database and fell back to the environment, which holds no
  secrets. Fix the database connection rather than adding secrets to `.env`.

### A monitor or service account is suddenly getting 429s

`Too many authentication failures from this IP. Temporarily blocked.` is the
gateway's own abuse defense, not a provider error and not a quota. Once a source
accumulates `AUTH_FAILURE_BLOCK_THRESHOLD` failed authentications inside
`AUTH_FAILURE_BLOCK_WINDOW_SEC` (200 in a day, by default) it is refused for
`AUTH_FAILURE_BLOCK_DURATION_SEC` (a day).

The awkward case is a caller you own — a status monitor, a CI job, a service
account — whose key was rotated, revoked, or never reached its environment. It
retries on a schedule, crosses the threshold, and is then refused *ahead of the
key check*, which has two consequences:

- **Repairing the credential does not lift the block.** The blocklist is
  consulted before the presented key is read, so a corrected key gets the same
  429 until the deadline passes.
- **The 429 hides the original error.** Whatever the caller reports after the
  block is in place says nothing about whether the underlying 401/403 was fixed.

Which caller is it? Turn on the `log_rejected_requests` admin setting and the
refusals land in Recent Requests as `ip_blocked` rows. Each row names the
account behind the key the caller presented — including a key that was revoked
or expired, which is what a stuck monitor is presenting — and labels it with the
credential's state (`revoked`, `expired`, `user_suspended`) beside the user. A
row with no user is unresolved, not proof of a stranger: the key may be one this
deployment never issued, or the lookup may have been shed, since it runs on a
strict budget that gives up first under exactly the flood a block is holding
back.

To recover, first fix the credential, then clear the block:

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

`cleared: false` means there was nothing to lift — it lapsed, or that bucket was
never blocked. Clearing grants no immunity: a caller still presenting a bad key
is blocked again on crossing the threshold. For a source that should never be
blocked at all, list it in `AUTH_FAILURE_BLOCK_EXEMPT_IPS` (comma-separated
addresses or CIDRs) instead.

The blocklist is per-process, in-memory state. On the single-process default
both endpoints are exact, and a restart also clears every block. Run multiple
workers and each holds its own counts, so a listing shows only the worker that
answered and clearing may take more than one call.

### Resetting the stack

**Stop and start, keeping data:**

```bash
make down && make up
```

**Destroy the database and start clean.** `docker compose down -v` does *not* do
this. `postgres_data` is declared `external: true` in
`deploy/docker/docker-compose.yml`, and Compose never removes an external
volume — `down -v` returns success and leaves it fully intact, so `make up`
comes back on exactly the same data. Remove it by name:

```bash
make down
docker volume rm hybridinference_postgres_data
make up   # docker-volumes recreates it empty; Postgres re-initialises
```

The backend then starts as on a new deployment: it generates new secrets
unless `.env` supplies them, imports whatever settings `.env` still holds, and
prints a setup code for a new administrator.

```{warning}
`docker volume rm` is irreversible and takes every account, API key, request
log and stored setting with it. Take a `pg_dump` first if any of it matters.
```

pgAdmin's own volume (`hybridinference_pgadmin_data`) is an ordinary local
volume that `make down` leaves in place. To clear pgAdmin's saved state, remove
it by name as described in [Resetting pgAdmin](database.md#resetting-pgadmin).

### Rebuilding after code changes

```bash
make build               # all images
make build s=backend     # one service
```
