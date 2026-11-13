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

### pgAdmin over mutual TLS

The optional `/pgadmin/` console proxy checks the caller's administrator session
before connecting to pgAdmin. By default it uses `http://pgadmin:80`; setting
`PGADMIN_INTERNAL_URL` overrides that address. For a separately hosted pgAdmin
behind a gateway that requires client certificates, configure the **frontend
server's runtime environment** with:

```text
PGADMIN_INTERNAL_URL=https://pgadmin-gateway.internal:8443
PGADMIN_MTLS_ENABLED=true
PGADMIN_MTLS_CA_FILE=/run/pgadmin-tls/ca.crt
PGADMIN_MTLS_CERT_FILE=/run/pgadmin-tls/client.crt
PGADMIN_MTLS_KEY_FILE=/run/pgadmin-tls/client.key
```

The URL hostname must match the gateway server certificate. The CA file verifies
that server; the client certificate and key authenticate the frontend to the
gateway. Supply the files through a read-only secret mount accessible to the
frontend process, and restrict who may read or mount the client private key.
The console runs as uid 1000, not root, so the files must be readable by that
user: for example group 1000 with mode `0440`, or a Kubernetes `fsGroup: 1000`
with `defaultMode: 0440`. A file only root can read makes `/pgadmin/` answer
502, and the console logs that the credentials are not readable by its user.
These are file paths inside the frontend container, not certificate contents.
The upstream gateway must enforce mTLS itself and restrict access to pgAdmin;
the console's administrator check does not protect a separately exposed backend.

The example Compose file does not forward these variables or mount these files;
a deployment using Compose must explicitly add the frontend environment and
secret volumes in an override. Setting only the root `.env` is not sufficient.
With mTLS enabled, an HTTPS URL and all three files are required; invalid or
unreadable settings make `/pgadmin/` return 502 without falling back to HTTP.
Unset `PGADMIN_MTLS_ENABLED` or set it to the literal `false` to retain the
existing proxy behavior. Values other than `true` or `false` fail closed.

The proxy rereads credentials for each new request and supports Kubernetes
Secret volume projections that place all three files in the same projection
directory. Ordinary files must not be modified while a request reads them;
independent file updates cannot provide an atomic three-file snapshot.
Certificate rotation across the frontend and gateway requires a coordinated
procedure: their Secret projections do not update at the same time, and the
gateway may need a reload. Existing in-flight requests can continue using the
credentials they already acquired.

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
backend is a separate question, and the answer is in `deploy/public-paths.json`
and the console's own `next.config.js` rather than in any proxy config. See
[The public path table](public-path-table.md).

#### What your proxy still has to do

Three protections are commonly left to the reverse proxy, and nothing inside
this stack provides them. A deployment that exposes the published ports without
a proxy — a tunnel daemon connecting to `127.0.0.1:3001`, for instance — has
none of them, and nothing warns you:

| Protection | What a proxy in front typically does |
|---|---|
| Next.js Server Action guard | Refuse requests carrying a `Next-Action` header (`if ($http_next_action) { return 403; }`), so console server actions cannot be invoked from outside the console |
| Request body cap on `/v1/` | Bound completion request bodies (commonly `client_max_body_size 50m`), so oversized ones never cross your network. The gateway refuses bodies over `REQUEST_MAX_BODY_MB` (32 MiB) itself, and the console streams `/v1/` bodies to it without holding them |
| `X-Forwarded-For` rewriting | Overwrite a client-supplied chain, so only hops the proxy inserted reach the gateway |

The gateway reads forwarded headers only from the proxies you authorize (see
[Trusted proxies and client IPs](trusted-proxies-and-client-ips.md)), but it
never rewrites them. Without a proxy that does, the leftmost `X-Forwarded-For`
entry is whatever the caller sent — which is why `CF-Connecting-IP` is
preferred when Cloudflare is in front.

### Splitting inference traffic at the entry

By default every request reaches the console, which forwards the gateway's
paths to it. A long stream then passes through two processes that are each
released and restarted, and a console release or restart interrupts it as
surely as a gateway one does. An entry that can route by path — an
Ingress path rule, a tunnel's path routing, a load balancer — may instead send
the paths the public path manifest marks `entry_split`, `/v1` and
`/anthropic`, straight to the gateway, and everything else to the console as
before.

This is optional and is the deployment's to set up: this repository ships no
entry configuration, and the example and local stacks keep the built-in
forwarding. The console's image is the same either way and still forwards
both prefixes, so removing the entry's two rules is the rollback and needs no
image change, provided the gateway still trusts the console as before and the
console's stop grace period has not been shortened. The split does not change
what a gateway release does to a stream (open requests still get 5 seconds
after the stop signal), and the console's model playground,
`/internal/playground`, still goes through the console.

On the split paths the entry has to meet these conditions:

| # | Condition |
|---|---|
| E1 | It sends to the gateway only the manifest's `entry_split` prefixes: a path equal to `/v1` or `/anthropic`, or beginning `/v1/` or `/anthropic/`. It matches after resolving `.` and `..` segments and percent-encoding, so `/v1/%2e%2e/openapi.json` is not under `/v1` |
| E2 | It does not buffer responses, and does not read a whole request body before forwarding it |
| E3 | Its idle timeout is longer than the gateway's `REQUEST_TIMEOUT_SECONDS`, and it sets no limit on a request's total duration shorter than `STREAM_REQUEST_TIMEOUT_SECONDS`. Streams send a keepalive every 15 seconds |
| E4 | Its request body limit is no lower than the gateway's own, file uploads under `/v1` included |
| E5 | When the client disconnects, it closes the request to the gateway |
| E6 | It overwrites the forwarding header the client sent (`X-Forwarded-For` or `CF-Connecting-IP`) |
| E7 | The gateway's port is reachable from the entry and the console only |
| E8 | It does not resend a `POST` once the request has been sent to the gateway. Retrying on another instance when the connection could not be established is fine |
| E9 | When an instance is taken out of rotation, it stops giving it new requests but does not close the connections already open to it. Not needed yet; the coexisting backend releases the design defers depend on it |

What clients see on the split paths is the gateway's own answer, the same as
through the console, with these differences:

- `X-Content-Type-Options: nosniff` comes from the gateway, which adds it to
  every response. The console's other two headers, `X-Frame-Options` and
  `Referrer-Policy`, only mean something for a page and are not sent on these
  paths.
- A path that ends in a slash, `/v1/models/`, gets the same `308` to the bare
  path `/v1/models` from the gateway as from the console.
- When the gateway cannot be reached, the error comes from the entry: a
  `502`, `503` or `504`, but with the entry's own body rather than the
  console's OpenAI-shaped JSON. SDKs act on the status; a client that parses
  error bodies will see different content. An entry that can be given a JSON
  error body may be configured to send one.

Which way a request went is shown by the gateway's request log, whose
`peer_ip` names the process that connected to it, read together with the
entry's own access log. A response header is not proof: anything in between
can rewrite one.

#### Client addresses with the entry in front

With the split, two peers forward requests to the gateway on clients' behalf:
the console, for everything that is not split, and the entry. Requests through
the entry must resolve to the same client address as before, and must do so
with the console stopped. Do not get there by trusting more: keep
`TRUST_PROXY_HEADERS` on, add no network that holds other workloads to a trust
list, and do not treat an `unknown` address as a normal result.

**Static trust** (Kubernetes proxy discovery not enabled). Readiness does not
depend on any proxy, so stopping the console leaves `/health/ready` as it was.
For the address, add the entry's exact peer address, as the gateway sees it,
to the setting for the header the entry writes: `TRUSTED_PROXIES` for an entry
that writes `X-Forwarded-For`, `TRUSTED_CLOUDFLARE_NETWORKS` for Cloudflare.
Keep the console's own entry. Add nothing wider than the entry itself: if the
gateway cannot tell the entry's connections from other processes' — behind a
loopback port mapping, every process on the host reaches the gateway from the
same address — solve that first, rather than trusting the shared address.
See [Trusted proxies and client IPs](trusted-proxies-and-client-ips.md).

**Kubernetes proxy discovery.** The split does not meet its goal in this mode
today, so a deployment that uses it stays on the console's forwarding:

- The discovered proxies are one Service's pods. An entry outside that Service
  is not one of them, so every request it sends resolves to `unknown`.
- With the frontend stopped the discovered set is empty, `/health/ready`
  answers `503 proxy_discovery_unavailable`, and the entry takes the gateway
  out of rotation: stopping the console would interrupt inference, the very
  coupling the split removes.
- The pinned identity is the frontend pods' direct controller. A frontend run
  as a Deployment gets a new ReplicaSet on every release, which requires
  updating that identity and restarting the gateway.

### Checking an entry split with the probe

`ops/dev/entry_split_probe.py` checks a deployment's inference path from
outside, through the address clients use, on either topology. It uses a raw
HTTP client and never retries, so every failed request counts:

- long streams on `/v1/chat/completions`, `/v1/responses` and
  `/anthropic/v1/messages`, with each frame's arrival time, the end marker
  (`[DONE]`, `response.completed`, `message_stop`) and the usage report
  checked;
- short streaming and non-streaming requests on the same three paths, at a
  fixed rate, for as long as the long streams run;
- the boundary, from `deploy/public-paths.json`: each split prefix reaches the
  gateway and carries `nosniff`, a trailing slash redirects to the same origin,
  the gateway's unpublished paths (`/docs`, `/openapi.json`, `/health/deep`,
  escapes such as `/v1/%2e%2e/openapi.json` and `//v1`) do not answer, and a
  missing or wrong API key gets `401`;
- `/health`, every few seconds, recorded but never counted.

Give the long streams a synthetic upstream with a known shape: the example's
fake provider (`distributions/example/fixtures/fake-openai-provider/server.py`)
started with `--stream-frames 600 --frame-interval-ms 100` streams 600
numbered frames 100 ms apart, and the probe then checks that every frame
arrived once, in order, at that interval. An entry that buffers shows up here
as frames arriving in bursts. Route one model to it for the long streams and,
optionally, a second to a default instance for the short requests:

```bash
PROBE_API_KEY=... python ops/dev/entry_split_probe.py \
  --base-url https://api.example.org --model probe-long --short-model probe-short \
  --expect-frames 600 --frame-interval-ms 100 --short-rate 2 --output probe.json
```

The key is read from the environment variable `--api-key-env` names and is
never printed. The report is JSON; the exit status is `0` only when nothing
failed. Run it before and while you release, restart or stop the console: with
the split in place, no long stream may break and no short request may fail.
Run it once on the console's own path while stopping the console as well, to
see that it does report the broken streams. Run against the gateway's port
directly, it reports `/docs` and the other unpublished paths as reachable:
that port is not an entry, which is why E7 keeps it private.

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
make build               # rebuild images, migrate, and restart
make build s=frontend    # rebuild one service
make migrate             # apply the schema migrations only
```

`make up` and `make build` first run the `docker-volumes` target, which creates
the external volume `hybridinference_postgres_data` when it is missing, and then
apply the schema migrations with the backend image before they start the
services. `make restart` starts the existing containers again and migrates
nothing. On a deployment that `make deploy` pinned to published images, these
commands start the pinned images, and `make build` refuses until you return to
source builds; see [Diagnostics, deployment and recovery](#diagnostics-deployment-and-recovery).

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

A deploy records the running artifacts, pulls the pinned pair and migrates with
the new backend image. With `LEGACY_MANIFEST` set it then imports the previous
release's file-based configuration with that image; see
[Model and routing documents in the database](configuration.md#model-and-routing-documents-in-the-database).
Only then does it switch the services and run the acceptance smoke. Any failure
after the switch, an interrupted command included, restores the previous images
and runs the acceptance on them again; the command then exits with the deploy's
own error and whether the restore passed. A rollback starts the recorded
previous images without running their migrations.

Each deployment keeps its record in `.ops/<distribution>/` (`.ops/none/` without
a distribution, in a subdirectory named after `COMPOSE_PROJECT_NAME` when the
shell sets it): `images.env`, the images it is pinned to, and `previous.json`
and `current.json`. A deploy writes them only once its release is accepted, so
a failed attempt changes neither the pin nor the release a rollback returns to;
a rollback pins the images it starts. While `images.env` exists, that
deployment's `make up`, `make migrate` and other lifecycle commands start the
pinned images instead of building the checkout, and `make build` refuses. To
return to source builds, delete that `images.env` and run `make build`. No other
distribution, the example included, uses the pin.

Copy `.ops.env.example` to `.ops.env` and uncomment the options this host needs
to persist optional Compose profiles, cloud-agent networking and the pgAdmin
host-port choice across commands; the copy alone changes nothing.

Without `BACKUP`, the dump goes to `.ops/<distribution>/gateway.dump`. It holds
every stored secret: keep it private and copy it off the host. A restore check
never touches the deployment's database server. It restores the dump into a
throwaway PostgreSQL container of the same image, with its own volume and
internal network, migrates the copy to this release, as a deploy would migrate a
backup taken before the upgrade, and removes the container, volume and network
afterwards. For its duration it adds a second database server's CPU and I/O,
and it needs free disk where Docker keeps volumes, normally the disk of the
deployment's own database: it refuses to start unless about twice the
database's size plus 1 GiB is free there.

### What a change actually requires

Three different answers, and picking the wrong one looks like the change not
taking effect:

| You changed | Do this |
|---|---|
| A setting on the admin **Configuration** tab | Nothing, unless it is marked **Restart required**: then select **Restart backend** on that tab, or run `make restart s=backend` |
| A value in `.env` that stays in the environment — the database connection, ports, the configuration selectors a rollback needs, console values | `make up` — a container reads its `env_file` when it is *created*, so `docker compose restart` keeps the old environment |
| Any other value in `.env` | Nothing happens once the database has the setting: change it on the **Configuration** tab instead. See [Upgrading a deployment that kept its settings in `.env`](#upgrading-a-deployment-that-kept-its-settings-in-env) |
| Model catalog, routing policy or alert rules in Admin Settings | Follow the **Restart required** marker; no image rebuild is needed |
| Site configuration or branding in Admin Settings | Applies when settings refresh; no rebuild or restart |
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
`Imported N setting(s) from the environment into the database`, with the
exceptions below. The switches
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

Besides a value that is not valid on its own, two kinds of value are not
copied, and the log names each with a warning:

- A value that breaks a rule with a setting already stored, such as
  `TRUST_X_REAL_IP=1` while the stored `TRUST_PROXY_HEADERS` is off. The
  stored value wins and the environment's counts as unset. Check proxy trust
  after an upgrade: the Compose file passes `TRUST_PROXY_HEADERS` with a
  default of `0`, so a database that stored that `0` keeps the gateway off
  forwarded headers whatever `.env` says. Change it on the Settings tab;
  see [Trusted Proxies and Client IPs](trusted-proxies-and-client-ips.md).
- `ERASURE_FENCE_SECRET`, once the erasure fence uses `API_KEY_SECRET` in its
  place. Leave the variable out: while it is set, the backend refuses to
  start; see [Database](database.md#secrets-for-deleting-accounts).

The `environment:` passthroughs in `deploy/docker/docker-compose.yml` work the
same way: they carry an overlay's values into the container, where they are
imported once. Their built-in defaults are imported too, so on a new
deployment `FRONTEND_URL`, `CORS_ALLOWED_ORIGINS`, `SITE_NAME` and the sender
address of outgoing mail start out as those defaults until you change them on
the tab.

### Application configuration

Models, routing, alert rules, site identity and branding are database settings,
not runtime files. Retired `*_CONFIG_PATH` and active/dark manifest selections
have no effect on this release, which warns at startup while one is set. Keep
them in the deployment's env files through the rollback window anyway: Compose
passes them to the backend, where a rolled-back previous release reads its files
through them and the legacy import learns what that release had in force. See
[Configuration](configuration.md) for the document controls and the import an
existing deployment runs during its upgrade.

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

The backend sends alerts to one Slack or WeCom (企业微信) group webhook. In
**Admin → Settings → Alerts**, choose a **Notification channel** and enter
its webhook URL. Only the selected channel's field is shown; saved URLs remain
write-only. Choose **None** to turn off notifications.

The in-process engine is off unless you enable `ALERTS_ENABLED`. Its underlying
configuration keys are:

| Setting | Value |
|---|---|
| `ALERTS_ENABLED` | on; applies after a backend restart |
| `SLACK_ALERTS_WEBHOOK_URL` | optional dedicated Slack webhook |
| `SLACK_WEBHOOK_URL` | Slack fallback; also enables the failed-request detector |
| `WECOM_ALERTS_WEBHOOK_URL` | WeCom group webhook; also enables the failed-request detector |

If `SLACK_ALERTS_WEBHOOK_URL` is empty it falls back to `SLACK_WEBHOOK_URL`, so
one webhook can serve both code paths. The console preserves existing dedicated
Slack settings and uses the fallback key for a new Slack configuration.

Slack and WeCom are mutually exclusive. Saving a channel switch clears the old
channel in the same update. Webhook changes apply after a backend restart until
an operator activates the `alerts.runtime` live group; from then on they apply
in every backend process within about ten seconds, without a restart. See
[Settings that apply without a restart](configuration.md#settings-that-apply-without-a-restart).
Until a change applies, normal alerts continue using the previous channel.
**Restart pending**, or the **Applied in each backend process** panel once the
group is active, shows what is still waiting.

**Send test notification** sends a marked test using the URL currently entered,
or the latest saved URL when no draft is entered. It works before a restart and
does not save changes, enable alerting or change incident/cooldown state. The
success message includes a test ID to check in the receiving group.

A URL typed into the test field must be the selected platform's official HTTPS
endpoint (`hooks.slack.com` / `hooks.slack-gov.com`, or
`qyapi.weixin.qq.com/cgi-bin/webhook/send`); that restriction exists so an admin
session cannot make the server request an arbitrary address. A **saved** URL is
not held to that host list: normal delivery already posts to it, so a
Slack-compatible destination (Mattermost, Rocket.Chat, an egress proxy) tests
normally. It must still be `https`. Discord, Telegram and arbitrary webhook
formats are not supported by this selector.

Set rules and thresholds in the `ALERT_RULES` JSON document in Admin Settings.
Leave it empty to use built-in thresholds; changes are marked Restart required. The rule types and evaluation live in
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

A snooze treats recoveries the same way: an incident that opens while alerts are
snoozed closes without a recovery message, and one that was posted before the
snooze still gets its recovery.

## The first admin account

A new deployment creates its first administrator on the console's setup page.
Until then the backend refuses every sign-up, by email or by phone, so nobody
else can claim the account:

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

Once email delivery is configured, this administrator can bind an email in
**Account Settings → Email Address**. Confirm the current password and follow
the verification link sent to the new address. The username continues to work
after binding; the same settings section also lets you change a bound email.

Whoever can read the backend log, or the database that holds the code, can
create this account, so keep both as private as the host. Wrong codes are
rate limited per client address, ten in 15 minutes; the right code is never
refused.

**Choose a username that is hard to guess.** Sign-in counts each attempt
before it checks the password: five per username from one address in 15
minutes (`LOGIN_RATE_LIMIT_PER_15MIN`), fifty per username from all addresses
together (`LOGIN_ACCOUNT_RATE_LIMIT_PER_15MIN`, counting only the attempts the
other limits let through), and twenty per address in an hour
(`LOGIN_RATE_LIMIT_PER_HOUR_PER_IP`). A browser that has signed in to the
account before, including the one you used for setup, holds a device cookie
(`<REFRESH_TOKEN_COOKIE_NAME>_device`, sent only to `/auth`, kept on sign-out,
valid for 180 days) and gets five attempts per 15 minutes of its own, so other
people's guesses never lock it out. Together, the account's browsers that hold
one get `LOGIN_ACCOUNT_RATE_LIMIT_PER_15MIN` attempts per 15 minutes, a budget
only requests carrying such a cookie count against, and once it is spent they
are limited like any other browser. Each password has its own budget: cookies
from before a password change or reset cannot spend the one your browsers share
after it. Changing `JWT_SECRET_KEY` invalidates every device cookie until that
browser's next sign-in.

Other browsers share the limits with their address, and behind the bundled
Compose file's console every browser has the same address unless the gateway
trusts the header your edge sets, which is safe only while the console is
reachable through that edge alone (see
[Behind the console](trusted-proxies-and-client-ips.md#behind-the-console));
`admin` is the first username anyone tries. The pages that mail a link to an
address anyone types, password reset and resending the verification email,
are limited too: three of each kind an hour per address, whether or not an
account has it, and ten an hour per client address when the backend can tell
clients apart (without that, the per-address limit alone). The counts are held
in memory, so a backend restart clears them. Nothing below needs a sign-in: `ADMIN_TOKEN`,
when set, still reaches the admin API, and the command-line tools work on the
database directly.

**Without the browser.** `ops/admin/create_admin.py` writes the account
directly: it creates it with `role='admin'`, `status='active'`,
`email_verified=TRUE`, or promotes an existing account with the same address.
Run it from the repository root once the schema migrations have run
(`make migrate`; `make up` runs them):

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
root. It also works on a database the schema migrations have not brought up to
date — while a migration waits for the users table's lock and the previous
release goes on serving, say — and changes no schema. Such a database may have
no login names yet, so name the account by its email address there.

**A setting that breaks the console.** Settings live in the database, so a
value that stops sign-in from working cannot be fixed by editing `.env`, nor
from a console nobody can sign in to. Change it back from the command line:

```bash
docker exec -it hybridinference-backend python -m serving.config.manage reset JWT_ALGORITHM
```

`list`, `get KEY`, `set KEY VALUE` and `reset KEY` work like the
Settings tab, with its validation, and never print a secret's value; see
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

### Applying schema migrations

Every deployment method has the same step: run the migrations with the image
you are about to start, while the old one still serves, and start it only when
they succeed.

| Deployment | The step |
|---|---|
| The bundled Compose stack | `make migrate`; `make up` and `make build` run it for you |
| Compose with a pinned image (`docker-compose.digest-backend.yml`) | `docker compose ... run --rm --no-deps backend python -m migrations upgrade` once PostgreSQL is up, with the same files and `BACKEND_IMAGE` as the `up` that follows |
| A systemd unit (`deploy/systemd/`) | `.venv/bin/python -m migrations upgrade` in the unit's working directory, with its environment, before `systemctl restart` |

Rules for a deploy script that does this itself:

- Run the migrations with the same image, by digest, that you then start. The
  image carries the label `org.hybridinference.schema.migrations=alembic`; an
  image without it predates versioned migrations, builds its schema at
  startup, and has no step to run.
- `run --rm backend` gives the one-off container the backend service's
  networks, fixed addresses included. If the service pins an address
  (`ipv4_address`), the running backend holds it and Docker refuses the
  one-off with `Address already in use`. Give the migrations a service of
  their own instead: the same image and `DB_*` settings, only the database's
  network, and a Compose profile so that no `up` starts it.
- Do not let the one-off start or recreate the database. Pass `--no-deps` and
  check yourself that the database is up: Compose recreates a dependency whose
  configuration changed, before a migration that may then fail.
- If the deploy script itself arrives on stdin (`ssh host bash -s`), run the
  one-off with `</dev/null`. `compose run` attaches stdin even with `-T`, so it
  reads the rest of the script, and the deploy ends with status 0 having done
  nothing more.
- Do not make the migrations a step that every start runs (Compose's
  `depends_on: condition: service_completed_successfully`, a Kubernetes init
  container). Every restart would migrate first and wait on table locks before
  it serves. Where they are wired that way anyway, a restored earlier image
  finds nothing to apply on the newer schema: the command exits 0, or 1 when
  that schema no longer supports the earlier code.
- If the step fails, stop: do not restart the service. Exit status 75 means a
  lock was not available in time (a backup holding a table, or another
  migration run); the same command can be run again later.
- Have the deploy in place, and seen working once on an image without the
  label, before the first image with it arrives. The backend refuses to start
  on a database that has not been migrated, so a missed step shows up as a
  failed start and, where the deploy restores the previous image, an outage
  while it does.

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

Set `PGADMIN_PASSWORD` in `.env` first, then:

```bash
make up COMPOSE_PROFILES=admin
```

The console serves pgAdmin at `/pgadmin/` (`SCRIPT_NAME=/pgadmin`), gated on an
admin session by `apps/frontend/src/app/pgadmin/[[...path]]/route.ts`, which
denies on every unexpected condition, including a backend it cannot reach.
pgAdmin publishes no host port, since one would skip that gate;
`PGADMIN_PUBLISH=1` adds one on `127.0.0.1` for an SSH tunnel.

Behind the console's gate pgAdmin asks for its own login, `PGADMIN_EMAIL` and
`PGADMIN_PASSWORD`: `PGADMIN_CONFIG_SERVER_MODE` defaults to `True`, and
`False` serves pgAdmin with no login at all. There is no fallback password:
without one pgAdmin will not set up its volume. It keeps the login it first
started with, so a volume first started without a password, or with server
mode off, needs the steps in [Authentication](database.md#authentication).

pgAdmin is on a network of its own, `pgadmin`, shared only with the console and
the database. The backend and any other service on `hybridinference` cannot
reach it. The first `make up` after this change recreates the `postgres`
container to attach it to that network, a restart of a few seconds.

The Compose service runs an exact pgAdmin release (`dpage/pgadmin4:9.19`).
Moving from the earlier `dpage/pgadmin4:8` upgrades pgAdmin's settings
database in the `pgadmin_data` volume on the first start, which an 8.x image
cannot read afterwards.

```{note}
pgAdmin and the agent web app (`/agents`) are served on the console's own
origin. Any script either app serves runs with the authority of the console
session open in that browser, because the session's access token and
`/auth/refresh` are same-origin. The proxies keep each app's cookies under its
own path and refuse any cookie named like one of the console's, which stops an
app from replacing or clearing the console's session, but only a separate
origin removes the shared authority. Serving each app from its own hostname
behind the same admin gate needs DNS and tunnel routes per deployment and is
tracked as follow-up work.
```

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
- `The database records no schema version` or `The database is at revision …,
  behind this code's …` — the migrations have not been applied for this image.
  Run `make migrate` (or your deployment's step, see
  [Applying schema migrations](#applying-schema-migrations)), then start the
  backend again.
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
