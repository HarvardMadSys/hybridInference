# Configuration

Configure the gateway in Admin, under **Settings**, **Providers** and
**Routing**. Application configuration is stored in PostgreSQL; the gateway
reads no model, routing, alert, distribution or branding YAML at runtime.

## Where configuration lives

| Configuration | Where to change it |
|---|---|
| Model catalog (`MODEL_CATALOG`) | **Admin → Routing → Models** |
| Other application documents and settings (`app_config`) | **Admin → Settings** |
| Providers, credentials, live routes and overrides | **Admin → Providers** and **Admin → Routing** (operational store) |
| Database connection, host ports and container networking | `.env` and Compose; `.env.example` lists them |
| Persistent lifecycle options | `.ops.env`, copied from `.ops.env.example` |

A fresh database has no provider or model. The console stays available for
first-run setup and shows **Ask admin to configure** until the required
configuration exists; users never see secret values or private configuration
paths. A provider key alone installs no example models; the
local tutorial (`make up DISTRIBUTION=example`) is a separate, opt-in fixture.

### Application documents

Seven settings hold validated JSON documents, with the same conditional saves,
audit, security confirmation and backup as every other setting:

| Setting | Holds | Applies |
|---|---|---|
| `MODEL_CATALOG` | Model metadata, aliases, router parameters and base routes | After a backend restart |
| `ROUTING_POLICY` | Optional deployment routing policy | After a backend restart |
| `ALERT_RULES` | Optional alert thresholds and rule switches | After a backend restart |
| `SITE_CONFIGURATION` | Public site identity and feature restrictions | On settings refresh |
| `SITE_BRANDING` | Public branding | On settings refresh |
| `SSO_PROVIDERS` | Identity providers for [single sign-on](sso.md) | On settings refresh |
| `MCP_SERVERS` | MCP servers the Playground offers as a store | On settings refresh |

**Admin → Routing → Models** edits `MODEL_CATALOG`; **Admin → Settings → Site &
Setup → Advanced settings → Application** edits the rest. Admin shows a document as one plain-string field
per value, named by its path, such as `models[0].route[0].base_url`. Numbers
and `true`/`false` keep their type, and a list of strings is one
comma-separated field. To add or remove a key or a list item, switch to
**JSON**.

An empty document means the built-in routing policy and alert thresholds,
neutral branding, no catalog models (models created in Admin Routing still
work) and no single sign-on. For a site document that stops validating, see
[Activating a manifest](distribution-customization.md#activating-a-manifest).

## How a gateway finds its config

A stored row wins over the environment, even when it is empty, and the
environment wins over the registered default. At first boot, registered
settings found in the environment seed their missing rows; later environment
edits do not replace a stored row. Resetting a setting makes it follow the
environment or default again. The database connection stays in the
environment, because it is needed before settings can be read. An application
document from the environment that does not validate is not imported: the log
names the setting and the reason, and the backend refuses to start unless a
valid stored value takes its place.

## Model and routing documents in the database

This section is for upgrading from a release that read YAML files.

The upgrade migration copies `route_catalog` rows into `MODEL_CATALOG` and
`ROUTING_POLICY`, unless an administrator already saved those settings, and
moves embedded credentials into secret settings (`CATALOG_SECRET_*`). A catalog
the previous release could run still registers; one it could not registers no
model, with the reason in Admin and the log. The legacy rows stay for a
rollback, and catalog changes saved later are copied back to them, so a rollback
runs the latest catalog it can.

Site identity, branding and alert rules kept in files need one explicit import,
with the new image, after the migrations and before it serves. Until then the
new release runs with a neutral site and built-in alert thresholds, so public
signup or RAG that the old manifest turned off is on. Name the old manifest as
the backend container sees it (its `DISTRIBUTION_CONFIG_PATH`, or its path in
the checkout), and the lifecycle commands import at the right point:

```bash
make deploy BACKEND_IMAGE=<image@sha256:...> FRONTEND_IMAGE=<image@sha256:...> LEGACY_MANIFEST=distributions/<name>/distribution.yaml
make build LEGACY_MANIFEST=distributions/<name>/distribution.yaml
```

Use the second form for a stack built from source. Both run
`python -m serving.config.manage import-legacy <manifest>` in a one-off
container of the new backend image; with another deployment method, run that
command at the same point. It imports what the previous release had in force:

- the manifest's identity, features, branding and documents, if that release
  ran it with `DISTRIBUTION_CONFIG_MODE=active`. A dark manifest is skipped;
  `--include-dark` (`LEGACY_IMPORT_OPTIONS=--include-dark`) imports it anyway,
  which applies it for the first time;
- the alert rules from `ALERTS_CONFIG_PATH` if set, otherwise from the
  manifest. `--alerts=<file>` names a file explicitly.

The import stores only missing settings, in one conditional update, so running
it again changes nothing; a running backend needs a restart for catalog and
rule changes. Keep the old files and the `DISTRIBUTION_CONFIG_PATH`,
`DISTRIBUTION_CONFIG_MODE` and `*_CONFIG_PATH` values through the rollback
window: the import reads them, and a rolled-back release needs them.

## The model registry

Use **Admin → Routing → Create model** for ordinary additions: it sets pricing,
limits, modalities, tool support, the router and a first verified route. A new
model starts restricted to administrators. Use `MODEL_CATALOG` for aliases and
other catalog metadata:

```json
{
  "models": [{
    "id": "team-chat",
    "name": "Team chat",
    "provider": "openai_compat",
    "base_url": "https://provider.example/v1",
    "api_key": "${TEAM_PROVIDER_KEY}",
    "context_length": 8192,
    "max_output_length": 4096,
    "route": [{"kind": "openai_compat", "weight": 1}]
  }]
}
```

Add `TEAM_PROVIDER_KEY` as a secret setting first. Documents refuse embedded
credentials and take `${NAME}` references, so an ordinary backup holds the
catalog without the secrets. [Adding a model](adding-models.md) is the field
reference.

## The routing file

There is no runtime routing file. Set the optional `ROUTING_POLICY` document for
a deployment-wide policy. Each model's `router` and `router_params` stay in the
catalog, and runtime strategy changes belong in Admin Routing.

Admin shows `ROUTING_POLICY` as one card, each value under a readable name with
its key beside it. An empty field leaves the key out, so its default applies:
the **Router** (`default_router`, Fixed by default) for models that name none,
the **Local share** (`routing_parameter.local_fraction`) between the **Local
deployments** and **Remote deployments**, and the local deployments' **Health
checks** (`health_check`, `timeout`). The card keeps any key it does not edit,
and switches to JSON for a policy it cannot show exactly. Every change applies
after a backend restart.

## Settings that currently have no effect

The backend warns at startup about each of these that is still set:

- `DISTRIBUTION_CONFIG_PATH`, `DISTRIBUTION_CONFIG_MODE`, `MODELS_CONFIG_PATH`,
  `MODELS_CONFIG`, `ROUTING_CONFIG_PATH`, `ROUTING_CONFIG` and
  `ALERTS_CONFIG_PATH`. Compose still passes the path and mode variables to the
  backend, because a failed deploy or `make rollback` starts the previous
  release, which reads its files through them. Remove them, and the files, once
  no earlier release will run.
- `BATCH_EXPANSION_ENABLED`. Every batch kind is available whenever
  `BATCH_ENABLED` is on.

These do less than their names suggest, without a warning:

- `routing_parameter.local_fraction` in `ROUTING_POLICY`. The local/remote
  split applies once, at startup, and only on a gateway without a database;
  with one, route weights come from Admin Routing.
- `health_check` in `ROUTING_POLICY`. A health check is logged and shown on
  `GET /routing`, but never changes where a request goes.

- `site.terms_document` and `site.privacy_document` in `SITE_CONFIGURATION`.
  The legal text comes from the console or a [Site UI module](site-ui-modules.md).
- `AGENT_*` variables other than those in `.env.example`. The standalone cloud
  agent owns its settings.

## Runtime configuration from the admin console

Provider, key and ordinary route changes are stored in the operational store,
and every backend process applies them, normally within ten seconds. **Admin →
Routing → Models** shows **Applied in each backend process**. A stale process
never counts as updated, so compare the registered processes with your own
list. Catalog changes marked **Restart required**
still need a backend restart.

A process that cannot apply one model's change keeps serving that model's old
routes, lists it as not applied and retries. A route edit keeps the model's
RouteWise router, and what it has learned, unless the edit changes the
strategy. A disabled key is out of service in every process, and Admin refuses
it for a new or edited route. Repoint the routes that pin a key before deleting
it: a restarted process drops a route whose key is gone.

### Providers tab

**Admin → Providers → Overview** lists the providers; add, verify, edit or
delete one there. Below the list are three sections:

| Section | What it does |
|---|---|
| **Keys** | Adds, verifies, disables and removes keys, reserves one for a user tier, and lists the upstream concurrency of every key in use |
| **Quotas** | Shows each key's use of its plan's limits, where a quota extension reports them |
| **Availability** | Takes a provider out of routing for every model, and puts it back |

**Admin → Providers → Performance** shows each model's providers' latency,
errors, cache hits and throughput.

Credentials are write-only. Built-in adapter kinds and catalog-owned provider
names are reserved.

A provider's Base URL may name any host, private and loopback addresses
included, so you can add a self-hosted engine (vLLM, SGLang, Ollama). Cloud
instance metadata addresses are always refused, in every spelling. A failed
probe shows the upstream's error message only when it is in the OpenAI error
format.

To keep Admin off the gateway's own network, set
`ADMIN_UPSTREAM_ALLOW_PRIVATE_NETWORKS=0` (**Admin → Settings → Site & Setup →
Advanced settings**, Network section). Base URLs saved or probed in Admin, and the model queue status
address, must then resolve to public addresses, at validation and at every
connection. Routes saved earlier keep serving, so restrict the gateway's egress
at the network layer as well.

### Routing tab

**Routes** creates models, verifies routes, edits their targets and
capabilities, adjusts weights, and disables or removes routes. Resetting a
route's override returns it to the catalog baseline, which the API labels
source `yaml` (`yaml_weight`). **Models** edits the model catalog and the
overrides on top of it: each model's required role, and whether it counts
toward the per-user concurrency limit.

The **Reasoning effort levels** summary shows the route's client choices and
where they come from. **Advanced mapping** changes what each public level sends
upstream, and **Use default effort settings** clears the effort override while
keeping other capability changes. In the API, omitting `reasoning_effort_map`
keeps the override and `null` clears it. See
[Mapping reasoning-effort levels on a route](adding-models.md#mapping-reasoning-effort-levels-on-a-route).

### Settings tab

**Admin → Settings** has four tabs, **Access & Limits**, **Providers &
Routing**, **Operations** and **Site & Setup**, each with its tuning settings
under **Advanced settings**. Each setting shows whether it is required and
whether it needs a restart.

### How the two layers combine

The model catalog is the baseline. Stored runtime models, routes, overrides,
weights and availability apply on top; changing them never edits the catalog.

## Settings stored in the database

### Confirming a security-relevant change

Changing a secret, an application document, or a switch that decides who may
sign in, whether inference needs a key, what is logged or what an account may
spend asks the administrator to confirm who they are: with an authenticator
code if the account has one, otherwise with its password. An account that
signs in only through single sign-on signs in there again, which confirms one
change within five minutes. `ADMIN_TOKEN` cannot confirm. A save that would
need two confirmations sends nothing; save the changes one at a time.

Saves are conditional: if someone else saved after you started editing, yours
is refused and the tab reloads the latest values, keeping your unsaved changes.

### Backup and restore

**Backup** exports every application document, setting, provider, runtime route
and override, without secret values; **Backup with secrets** includes them after
you confirm again. Restore previews the security-relevant changes and applies
the bundle in one transaction. A secret the bundle leaves out keeps its value.
A restore removes stored settings the bundle does not name, except the
application documents and the `CATALOG_SECRET_*` credentials; reset those
explicitly.

This bundle is not a database backup. For accounts, sessions and request
history too, run `make backup BACKUP=/private/path/gateway.dump` and check it
with `make restore-check BACKUP=/private/path/gateway.dump`; see
[Database](database.md#backup).

### When a change applies

Ordinary settings apply within about ten seconds. The model catalog, routing
policy, alert rules and credentials used by catalog routes apply after a
backend restart, as Admin marks them, and a restart interrupts requests in
flight, streams included.

### Settings that apply without a restart

A live group is a set of settings that every backend process applies together,
without a restart, once an operator has activated it. The one group,
`alerts.runtime`, holds the alert channel and the failed-request alert:

| Settings | What they choose |
|---|---|
| `SLACK_ALERTS_WEBHOOK_URL`, `SLACK_WEBHOOK_URL`, `WECOM_ALERTS_WEBHOOK_URL` | The one channel alerts go to |
| `FAILED_REQUEST_ALERT_THRESHOLD`, `FAILED_REQUEST_ALERT_WINDOW_MINUTES`, `FAILED_REQUEST_ALERT_COOLDOWN_MINUTES`, `FAILED_REQUEST_ALERT_RATE`, `FAILED_REQUEST_ALERT_RATE_MIN_COUNT` | When the failed-request alert pages |

Once the group is active, a save reaches every process within about ten
seconds. A process that finds two conflicting channels or a value out of range
keeps its current values and reports the group invalid. To move from Slack to
WeCom, clear the Slack webhooks and set the WeCom one in the same save.
Clearing every webhook pauses alerting. The failed-request alert runs only while
`SLACK_WEBHOOK_URL` or `WECOM_ALERTS_WEBHOOK_URL` is set.

The Alerts category shows, for each registered process, whether it has applied
the saved revision. A process silent for over a minute counts as unknown.
**Applied** does not mean a message got through; **Send test notification**
checks that. `ALERTS_ENABLED` and `ALERT_RULES` still apply only after a
restart.

#### Activating a live group

A group stays inactive, its settings marked **Restart required**, until an
operator activates it. Do so once every backend process runs a build that
supports it:

1. Deploy the build everywhere, and check that each process registered:
   `python -m serving.config.manage live status`.
2. Preview the activation:
   `python -m serving.config.manage live preview alerts.runtime`.
   Values saved while a restart was pending apply as soon as you activate.
3. Activate with the version the preview printed and your number of backend
   processes:
   `python -m serving.config.manage live activate alerts.runtime --expect <epoch>:<revision> --expect-instances <N>`.
   It refuses if the configuration changed since the preview, a process does
   not support the group, or the count differs.
4. Check `live status` until every process has applied the latest revision.

`live deactivate alerts.runtime --expect <epoch>:<revision>` turns the group
off; running processes keep their last values until they restart. After
restoring a whole database, run
`python -m serving.config.manage live reset-epoch --expect <epoch>:<revision>`
so that every process registers again.

### Without the console

The recovery CLI reads the same database and applies the same validation:

```bash
python -m serving.config.manage list
python -m serving.config.manage get SITE_NAME
python -m serving.config.manage set SITE_NAME "Team gateway"
python -m serving.config.manage document models /private/path/catalog.json
python -m serving.config.manage status
```

A file passed to `document` is imported once, not read at runtime. The CLI
never prints a secret value.

### Missing settings

The console shows **Ask admin to configure**. Administrators can open Settings
or Models from it, or create a model in Routing. The gateway never quietly
picks a borrowed provider, model or key. A provider key or URL the catalog
references never counts as missing: until it is set, the route that needs it is
skipped (with its model, unless the route is `optional: true`) and the backend
log names it.

## Running with your configuration

```bash
make up
make doctor
make smoke MODEL=team-chat
```

Set `GATEWAY_API_KEY` when the inference smoke needs authentication; it is real
inference and uses quota. Set `ADMIN_ACCESS_TOKEN` for `make doctor` to report
the running processes' pending restarts.

## Email and SMS delivery providers

`EMAIL_PROVIDER` defaults to `smtp` and `SMS_PROVIDER` to `none`. The built-in
mail transports are `smtp` and `cloudflare` (Cloudflare Email Sending); neither
comes configured, and setup and Admin show only the selected one's settings.
Other providers are [extension modules](#extension-providers); a gateway that
does not load one neither offers nor lists it. Selecting an unavailable
provider is a configuration error: delivery never falls back to another.

**Admin → Settings → Site & Setup → Email** sets up mail as one card: choose the
service, then fill in its settings. **Send test email** sends a fixed message
through the saved settings, to an address you enter or to your own. When mail
is not sent, the console says why (settings still missing, an SMTP server that
refused the login or TLS, or what a Cloudflare error code means), never
including a credential. Broadcasts record the same reason for each recipient
not reached.

### SMTP

The backend checks the server's certificate before it sends the password. Port
465 uses TLS from the first byte; any other `SMTP_PORT` must offer STARTTLS.
The certificate must be valid for `SMTP_HOST` and issued by an authority the
backend trusts; otherwise it is refused and the log says why. For a private
authority, set `SSL_CERT_FILE` in the backend's environment to a bundle that
includes it and the system's authorities.

### Cloudflare Email Sending

1. In the Cloudflare dashboard, onboard the sending domain under **Email
   Service > Email Sending**.
2. Create an API token with the **Email Sending: Edit** permission on that
   account.
3. Set `EMAIL_PROVIDER=cloudflare`, `CLOUDFLARE_ACCOUNT_ID` to the account's
   Account ID (32 hexadecimal digits) and `CLOUDFLARE_EMAIL_API_TOKEN` to the
   token.
4. Set `SMTP_FROM_EMAIL` to an address on that domain. `SMTP_FROM_EMAIL` and
   `SMTP_FROM_NAME` are the sender for both built-in transports.

The token can send mail from every domain on that account; the configuration
API redacts it. Both values are read on every send, so a rotated token applies
at once. A message counts as sent once Cloudflare reports it delivered or
queued; a refusal, bounce or suppressed recipient fails the send and is logged
with Cloudflare's error code. Sends are not retried, because a timeout can
follow acceptance. On the Workers Free plan, Cloudflare delivers only to
verified addresses. The `smtp` transport can also send through Cloudflare, at
`smtp.mx.cloudflare.net`, port 465, user `api_token`, with the token as the
password.

### Extension providers

A trusted module listed in `BACKEND_EXTENSIONS` can add an email or SMS
provider with `register_email_provider` or `register_sms_provider`
(`serving.utils.delivery`), and its settings with `register_config_entries`
(`serving.config.app_config_registry`). Those settings get the usual
precedence, validation and secret masking; the module reads them with
`config_value()`.

### Tencent Cloud SES and SMS

`serving.integrations.tencent_cloud` adds `EMAIL_PROVIDER=tencent_ses` and
`SMS_PROVIDER=tencent` when listed in `BACKEND_EXTENSIONS`. A deployment that
used Tencent before the module became optional adds it when upgrading; settings
are unchanged.

| For | Settings |
|---|---|
| Both | `TENCENT_CLOUD_SECRET_ID`, `TENCENT_CLOUD_SECRET_KEY`, and `TENCENT_CLOUD_TOKEN` for temporary credentials |
| Email | `TENCENT_SES_REGION`, `TENCENT_SES_FROM_EMAIL` (approved in that region), optional `TENCENT_SES_FROM_NAME` |
| Email templates | `TENCENT_SES_TEMPLATE_VERIFICATION` and `TENCENT_SES_TEMPLATE_PASSWORD_RESET` (required for account mail); `TENCENT_SES_TEMPLATE_APPROVAL`, `TENCENT_SES_TEMPLATE_REJECTION`, `TENCENT_SES_TEMPLATE_ADMIN_SIGNUP`, `TENCENT_SES_TEMPLATE_NOTICE` (unset disables) |
| SMS | `SMS_REGION`, `SMS_SDK_APP_ID`, `SMS_SIGN_NAME`, `SMS_TEMPLATE_ID` |

Reference templates are in
`apps/backend/serving/integrations/tencent_cloud/ses-templates/`. Replace the
example domain with your console's domain before submitting them for approval;
the verification and reset links must match `FRONTEND_URL`. Template data is
limited to 800 UTF-8 bytes, so an oversized or HTML-only notice fails; use SMTP
for long or free-form HTML mail. A failed SES send never falls back to SMTP and
is not retried.

The SMS template takes the code and its lifetime in minutes, in that order. It
sends to mainland China numbers only: set `SMS_ALLOWED_PREFIXES=+86`, and
`SMS_DEFAULT_COUNTRY_CODE=86` to accept numbers without a country code. Keep credentials, signatures and template IDs in
deployment configuration, outside source control.
