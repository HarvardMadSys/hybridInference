# Configuration

Configure the gateway in **Admin → Settings**, **Providers** and **Routing**.
Application configuration is stored in PostgreSQL. The gateway does not load
model, routing, alert, distribution or branding YAML at runtime. Docker Compose
and GitHub Actions still use their infrastructure YAML files.

A fresh database has no assumed provider or model. The console remains available
for first-run setup and shows **Ask admin to configure** when required
configuration or models are missing. Users never see secret values or private
configuration paths.

## Where configuration lives

| Configuration | Owner |
|---|---|
| Application settings and documents | `app_config`, edited in Admin Settings |
| Providers, credentials, live routes and overrides | Admin Providers and Routing |
| Database connection, host ports and container networking | `.env` and Compose |
| Persistent lifecycle options | `.ops.env`, copied from `.ops.env.example` |

### Application documents

The Models settings sub-tab includes these validated JSON documents. They use
the same conditional saves, audit, security confirmation and backup controls as
other settings. No JSON file is a runtime fallback either.

| Setting | Purpose | Application |
|---|---|---|
| `MODEL_CATALOG` | Model metadata, aliases, router parameters and base routes | Backend restart |
| `ROUTING_POLICY` | Optional deployment routing policy | Backend restart |
| `ALERT_RULES` | Optional alert thresholds and rule switches | Backend restart |
| `SITE_CONFIGURATION` | Public site identity and feature restrictions | Settings refresh |
| `SITE_BRANDING` | Validated public branding | Settings refresh |

Empty routing and alert documents use the built-in policy and thresholds.
Empty site documents use neutral branding. An empty model catalog supplies no
models; models created in Admin Routing remain available independently.

A stored site document that no longer validates is passed over: the explicit
`SITE_*` settings and then the neutral defaults apply, public signup and RAG are
reported off, and the console reports the configuration as incomplete until an
administrator corrects the document.

## How a gateway finds its config

Database settings win over environment seeds, then registered defaults. Stored
empty values are deliberate. Application file paths and distribution active/dark
modes no longer select runtime configuration.

## Model and routing documents in the database

The upgrade migration copies existing `route_catalog` rows into `MODEL_CATALOG`
and `ROUTING_POLICY` without overwriting settings an administrator already saved.
Embedded credentials become separate secret settings. The copy is held to the
previous release's own registry rules, so what it ran still registers: duplicate
aliases, a route without `kind`, an embedding model with `max_output_length: 0`
or a top-level anchor block. A catalog it could not have run either registers
no catalog model, and Admin and the log name the reason. Legacy rows remain for
previous-release compatibility, and later edits to those rows have no effect on
the new release.

Catalog and routing changes saved in this release are copied back to those
rows, as canonical JSON, so a rollback runs the latest catalog the previous
release can run. A catalog holding a credential reference where the previous
release would not expand one is not copied, nor are a reset and a removal: the
rows keep the last copy.

Site identity, branding and alert rules kept in files need one explicit import,
with the new image, after the migrations and before it serves. Until then the
new release runs with a neutral site and the built-in alert thresholds, so
public signup or RAG that the old manifest turned off is on. The standard
lifecycle commands import in that order when you name the old manifest as the
backend container sees it: the value of the deployment's
`DISTRIBUTION_CONFIG_PATH`, or its path in the checkout, such as
`distributions/<name>/distribution.yaml`.

```bash
make deploy BACKEND_IMAGE=<image@sha256:...> FRONTEND_IMAGE=<image@sha256:...> LEGACY_MANIFEST=distributions/<name>/distribution.yaml
make build LEGACY_MANIFEST=distributions/<name>/distribution.yaml
```

The second form is for a stack built from source. Both run
`python -m serving.config.manage import-legacy <manifest>` in a one-off container
of the new backend image, with the deployment's environment. With another
deployment method, run that command at the same point, where the backend can
read the old files.

The import takes only what the previous release had in force, judged as that
release judged it:

- The manifest's site identity, features, branding and documents, when the
  previous release ran it with `DISTRIBUTION_CONFIG_MODE=active`. A dark
  manifest was compared but never applied, so the import skips it with a
  warning. `--include-dark` (`LEGACY_IMPORT_OPTIONS=--include-dark`) imports it
  anyway, which applies those settings for the first time.
- The alert rules from `ALERTS_CONFIG_PATH` when the environment names it, as it
  won over the manifest, and otherwise from the manifest. `--alerts=<file>`
  names a file explicitly; `LEGACY_IMPORT_OPTIONS` runs the import without a
  manifest too.

The command validates every document and stores the missing settings in one
conditional update. Settings already stored remain unchanged, so running it
again changes nothing. Check Admin Settings; a manual import into a running
backend needs a restart for catalog and rule changes. Keep the old files and the
deployment's `DISTRIBUTION_CONFIG_PATH`, `DISTRIBUTION_CONFIG_MODE` and
`*_CONFIG_PATH` values through the rollback window: the import reads them, and a
rolled-back release needs them. Normal startup never invokes this importer.

## What a fresh clone does with no configuration at all

It starts an empty, neutral gateway. Finish first-run setup, then add a provider
and a model in Admin. Supplying a provider key alone does not install example
models. The explicitly selected local tutorial is a separate, opt-in fixture.

## The model registry

Use **Admin → Routing → Create model** for ordinary additions. It supports
pricing, context/output limits, modalities, tool support, router choice and an
initial verified route. A new runtime model starts restricted to administrators.

Use `MODEL_CATALOG` when aliases or additional catalog metadata are needed:

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
credentials: use `${NAME}` references so ordinary backups contain the editable
catalog while withholding secret values. The model registry expands whole-value
references; routing policy references retain their existing expansion rules.
See [Adding a model](adding-models.md) for the field reference.

## The routing file

There is no runtime routing file. Set the optional `ROUTING_POLICY` document in
Admin when deployment-wide policy is needed. Per-model `router` and
`router_params` remain in the catalog; runtime strategy changes belong in Admin
Routing. Fixed and RouteWise retain their existing selection and accounting.

## Settings that currently have no effect

The retired model/routing/alert path variables and distribution active/dark modes
no longer select configuration. Use the application documents instead. The
standalone cloud agent owns its own settings; obsolete gateway `AGENT_*` values
do not configure it.

The backend still receives `DISTRIBUTION_CONFIG_PATH`, `DISTRIBUTION_CONFIG_MODE`,
`MODELS_CONFIG_PATH`, `ROUTING_CONFIG_PATH` and `ALERTS_CONFIG_PATH` from Compose
when the deployment sets them, and warns at startup that they do nothing. They
are kept for the rollback window: a failed deploy or `make rollback` starts the
previous release, which reads its manifest and files through them. Remove them,
and the files, once no earlier release will run.

## Runtime configuration from the admin console

Provider/key changes and ordinary route edits are persisted in the operational
store. Backend processes observe committed changes and converge on their scoped
model transitions, normally within ten seconds. The Models settings sub-tab shows
**Applied in each backend process**, including failed, stale or unsupported
processes. A stale process is never counted as successfully updated. Compare the
registered process count with your deployment inventory.

Catalog changes marked **Restart required** still need a controlled backend
restart. A save is not evidence that every process has applied the change.

A process that cannot apply one model's change keeps serving that model's
previous routes, lists the model as not applied, and retries it; other models
and key changes still apply. A route edit keeps the model's RouteWise router,
and what it has learned, unless the edit changes the strategy.

Disabling a key that routes pin takes it out of service in every process until
it is enabled again, and Admin refuses a disabled key when a route is created
or edited. Deleting a pinned key leaves running processes on the route without
that key, and a restarted process leaves the route out: repoint the routes
before deleting the key.

### Providers tab

Add and verify providers and keys here. Credentials remain write-only. Built-in
adapter kinds and catalog-owned provider names remain reserved.

### Routing tab

Create models, verify routes, edit their targets and capabilities, adjust
weights, and disable or remove routes. Resetting a catalog-route override returns
it to its database catalog baseline. The legacy API source label `yaml` and
`yaml_weight` field remain compatibility names for that baseline.

The **Reasoning effort levels** summary shows the route's client choices and
where they come from. Open **Advanced mapping** to change what each public
level sends upstream. **Use default effort settings** clears the effort
override while keeping other capability changes. A route without declared
effort capability can still run a model that thinks.

The request field is `reasoning_effort_map`. Omitting it keeps an existing
override. Sending `null` clears the override and follows the route's catalog
or creation map, its legacy effort settings, or a matching preset. An
inherited preset is resolved when the adapter is built and is never copied
into the saved route or capability override. Existing database routes pick
it up on reload without rewriting their catalog. Route listings include
`reasoning_efforts_published` and `reasoning_effort_source`. See
[Mapping reasoning-effort levels on a route](adding-models.md#mapping-reasoning-effort-levels-on-a-route).

### Settings tab

Edit application documents, credentials, delivery settings, network policy,
runtime switches and quotas here. Required fields and restart requirements are
shown beside each setting.

### How the two layers combine

The model catalog provides the baseline. Stored runtime models, routes,
strategy/capability overrides, weights and availability choices apply on top.
Changing an override never edits the baseline document.

## Settings stored in the database

### Where a value comes from

A database row wins over the environment, even when empty. At first boot,
registered environment settings seed missing rows. Subsequent environment edits
do not replace them. Resetting a setting deliberately returns it to its
registered environment/default behavior. Database connection and wiring stay in
the environment because they are needed before settings can be read.

An application document from the environment that does not validate is not
imported: the log names the setting and the reason, never the value, and the
backend refuses to start on it unless a valid stored value takes its place.

### Analytics sections

Analytics visibility switches affect the console without a backend restart.

### On the Settings tab

Security-sensitive changes require the administrator's password. Conditional
saves prevent overwriting another administrator's concurrent edits.

### Backup and restore

**Backup** exports every application document, setting, provider, runtime route
and override. Secret values are omitted by default; references remain intact.
**Backup with secrets** includes credentials after administrator reauthentication.
Restore previews security-relevant changes and applies the confirmed bundle in
one transaction. Withheld secrets keep the destination's existing values.

A restore removes the stored settings the bundle does not name, except the
application documents and the `CATALOG_SECRET_*` credentials the upgrade lifted
out of the catalog, which a bundle from before the upgrade cannot name: those
keep their stored value or reset. Reset one explicitly to remove it.

This configuration bundle is distinct from a full database backup, which also
preserves accounts, sessions, request history and operational state. Use
`make backup BACKUP=/private/path/gateway.dump` and verify it with
`make restore-check BACKUP=/private/path/gateway.dump`; see [Database](database.md#backup).

### When a change applies

Ordinary settings refresh within ten seconds. Model catalogs, routing policy,
alert rules and credentials captured by catalog routes apply after a backend
restart, as explicitly marked in Admin. Restarting interrupts requests in flight,
including streams. Ordinary Admin route edits synchronize without that restart.

### Settings that apply without a restart

A live group is a set of settings the backend applies together, without a
restart, once an operator has activated it. The first one, `alerts.runtime`,
holds the alert channel and the failed-request alert:

| Settings | What they choose |
|---|---|
| `SLACK_ALERTS_WEBHOOK_URL`, `SLACK_WEBHOOK_URL`, `WECOM_ALERTS_WEBHOOK_URL` | The one channel alerts go to |
| `FAILED_REQUEST_ALERT_THRESHOLD`, `FAILED_REQUEST_ALERT_WINDOW_MINUTES`, `FAILED_REQUEST_ALERT_COOLDOWN_MINUTES`, `FAILED_REQUEST_ALERT_RATE`, `FAILED_REQUEST_ALERT_RATE_MIN_COUNT` | When the failed-request alert pages |

Once the group is active, a save reaches every backend process within about
ten seconds. Each process checks the whole group first: a pair of channels
that conflict, or a value out of range, keeps the values it already uses and
reports the group as invalid, while the saved values stay for you to correct.
To move from Slack to WeCom, clear the Slack webhooks and fill in the WeCom
one in the same save. Clearing every webhook pauses alert delivery and the
failed-request alert on purpose; it is not a failure. The failed-request alert
runs while `SLACK_WEBHOOK_URL` or `WECOM_ALERTS_WEBHOOK_URL` is set, as before;
`SLACK_ALERTS_WEBHOOK_URL` alone does not start it.

The Alerts category shows where the saved configuration stands. **Applied in
each backend process** names the saved version — for example
`app_config / epoch 3f1c… / revision 42` — and, for every process that has
registered, whether it has applied that revision, is still applying it, failed
or has not reported. A process whose heartbeat stopped more than a minute ago
counts as unknown, never as applied. A backend running an older build does not
register at all, so the panel cannot prove that every process is covered:
compare its list with your deployment's own list of processes. **Applied**
means the process uses the saved channel and thresholds; it says nothing about
whether a message reached the channel, which **Send test** checks. **Send
test** uses the saved webhook (or the address you typed), not necessarily the
one a process is using.

After a channel change, alerts already under way finish on the old channel,
and the recovery message of an incident opened earlier goes to the new one.
The failed-request alert keeps its cooldown and its open incident across the
change. It still checks once a minute, so a new threshold takes effect at the
next check after the change has applied.

The console saves conditionally: if someone else saved the configuration after
you started editing a category, your save is refused, nothing is stored, and
the tab reloads the latest values and keeps your unsaved changes for you to
review and save again. A reload, or a save in another category, never moves
unsaved changes onto values you have not seen.

`ALERTS_ENABLED`, the `ALERT_RULES` document and the alert engine still apply only
after a restart. Other settings join live groups later, one group at a time.

#### Activating a live group

A group stays inactive until an operator activates it, and a backend upgrade
never activates one by itself. Until then its settings behave as **Restart
required**. Activate it once every backend process runs a build that supports
it:

1. Deploy the build everywhere and check that each process registered:
   `python -m serving.config.manage live status`.
2. Preview what activation applies. It shows each setting's saved value (a
   secret only as set or not set) and the processes whose value in use differs
   — values saved while a restart was pending apply as soon as you activate,
   which can change where alerts go:
   `python -m serving.config.manage live preview alerts.runtime`.
3. Activate with the version the preview printed, and the number of backend
   processes your deployment runs:
   `python -m serving.config.manage live activate alerts.runtime --expect <epoch>:<revision> --expect-instances <N>`.
   It refuses when the configuration changed after the preview, when a running
   process does not support the group, or when the count differs.
4. Check `live status`, or the Alerts category, until every process has
   applied the latest revision.

If a process cannot apply the group when it is activated — a saved value is
invalid, say — the group stays active and the process keeps the values it was
using. Correct the setting and save it again: it applies without a restart.

`live deactivate alerts.runtime --expect <epoch>:<revision>` turns the group
off. A running process then keeps the values it last applied until it
restarts, instead of returning to the ones it started with, and a process that
starts afterwards uses the saved values. Until every process has restarted the
two can differ, and the status shows it. Going back to a build that does not
know the group is a deployment, not a configuration change.

A configuration backup does not include this status. Restoring a backup
applies its `app_config` rows to live groups like any other save, and writes
the other tables in the same transaction, so a failure anywhere restores none
of them. After restoring a whole database, run
`python -m serving.config.manage live reset-epoch --expect <epoch>:<revision>`:
it starts a new version history and makes every process register again.


### Without the console

The recovery CLI reads the same database and applies the same validation:

```bash
python -m serving.config.manage list
python -m serving.config.manage get SITE_NAME
python -m serving.config.manage set SITE_NAME "Team gateway"
python -m serving.config.manage document models /private/path/catalog.json
python -m serving.config.manage status
```

A JSON file passed to `document` is explicit import input, not a runtime source.
The CLI never prints a secret value.

### Missing settings

The console shows **Ask admin to configure**. Administrators can open Settings
or create a model in Routing. Other users should contact their administrator.
No borrowed provider, model, key or deployment identity is silently selected.

## Environment variables

Keep the database connection and infrastructure wiring in `.env`. Use Admin for
application changes after first boot. See `.env.example` for the remaining
infrastructure settings.

## Running with your configuration

```bash
make up
make doctor
make smoke MODEL=team-chat
```

Set `GATEWAY_API_KEY` in the shell when an inference smoke needs authentication.
The smoke is explicit real inference and consumes the usual quota.

## Email and SMS delivery providers

`EMAIL_PROVIDER` defaults to `smtp`; `SMS_PROVIDER` defaults to `none`. Every other provider is an optional module that a deployment loads explicitly. A gateway that does not load a provider neither offers it nor lists its settings in **Admin > Configuration**. Without a provider beyond the built-in one, it does not list `EMAIL_PROVIDER` or `SMS_PROVIDER` either, which would have only one value to choose, and without an SMS provider it lists none of the phone settings. Selecting an unavailable provider is a configuration error; delivery never silently falls back to another provider.

SMTP mail travels over TLS that checks the server's certificate before the password is sent. Port 465 uses TLS from the first byte; any other `SMTP_PORT` must offer STARTTLS. The certificate must be valid for `SMTP_HOST` and issued by an authority the backend trusts. A server with a self-signed or privately issued certificate is refused, and the backend log names the server and says why. Give it a certificate from a public authority, or set `SSL_CERT_FILE` in the backend's environment to a bundle that holds the system's authorities and the one that signed it.

Each distribution can add the providers its own inference hub needs, such as a regional email or SMS service. A trusted module selected through `BACKEND_EXTENSIONS` registers its email or SMS transport and configuration entries before database-backed settings load. The shared interfaces are `register_email_provider` and `register_sms_provider` in `serving.utils.delivery`, and `register_config_entries` in `serving.config.app_config_registry`. Extension settings keep the normal database → environment → registered default precedence, validation, secret masking and worker refresh. The extension reads them through `config_value()` instead of adding vendor fields to shared `Settings`.

### Tencent Cloud SES and SMS

The gateway ships one such module, `serving.integrations.tencent_cloud`, for Tencent Cloud transactional email and SMS. It is not loaded by default. Add it to `BACKEND_EXTENSIONS` to offer `EMAIL_PROVIDER=tencent_ses` and `SMS_PROVIDER=tencent`. A failed SES request or missing SES template does not fall back to SMTP.

Configure `TENCENT_CLOUD_SECRET_ID`, `TENCENT_CLOUD_SECRET_KEY`, optional `TENCENT_CLOUD_TOKEN`, `TENCENT_SES_REGION`, `TENCENT_SES_FROM_EMAIL` and optional `TENCENT_SES_FROM_NAME`. The sender must be approved in the selected region. Secret values remain server-side and are redacted in the configuration API. Keep production credentials and sender identities in deployment configuration.

Set positive IDs for `TENCENT_SES_TEMPLATE_VERIFICATION` and `TENCENT_SES_TEMPLATE_PASSWORD_RESET` before enabling SES account mail. Configure `TENCENT_SES_TEMPLATE_APPROVAL`, `TENCENT_SES_TEMPLATE_REJECTION`, `TENCENT_SES_TEMPLATE_ADMIN_SIGNUP` and `TENCENT_SES_TEMPLATE_NOTICE` for the notifications the deployment uses. Unset IDs disable those templates. Configuration health reports missing SES account-mail fields when public signup requires email verification, instead of asking for SMTP credentials.

Reference HTML lives in `apps/backend/serving/integrations/tencent_cloud/ses-templates/`. Before submitting templates for provider approval, replace the example domain with the deployment console domain and choose the desired language and wording. Verification and reset templates contain a fixed HTTPS origin and path; only the encoded `token` query value varies. Other templates use `site_name` and, where applicable, `reason`, `identifier` or `body`. Keep template links consistent with `FRONTEND_URL`; the gateway cannot inspect approved template contents.

SES receives approved templates with escaped scalar values, never arbitrary HTML variables. Serialized template data is limited to 800 UTF-8 bytes; oversized notices and HTML-only notices fail rather than being truncated. Successful submission means provider acceptance, not inbox delivery. Calls are not automatically retried because a timeout can follow acceptance. Deployments that require unrestricted HTML mail or long notices can continue to use SMTP.

The SMS provider requires `TENCENT_CLOUD_SECRET_ID`, `TENCENT_CLOUD_SECRET_KEY`, `SMS_REGION`, `SMS_SDK_APP_ID`, `SMS_SIGN_NAME` and `SMS_TEMPLATE_ID`. Temporary credentials also require `TENCENT_CLOUD_TOKEN`. Its approved template takes the code and its lifetime in minutes, in that order. It currently sends to mainland China mobile numbers only: set `SMS_DEFAULT_COUNTRY_CODE` to `86` if local input is desired and `SMS_ALLOWED_PREFIXES` to `+86`. Keep real credentials, approved signatures and template IDs in deployment configuration, outside source control.

A deployment that used the Tencent adapter before it became optional adds the module to `BACKEND_EXTENSIONS` when it upgrades; setting names and stored values are unchanged. On a gateway that does not load a provider, a stored row it once used lists as an ordinary custom setting that no code reads, keeping its secret flag, and an administrator can delete it.

Set `ADMIN_ACCESS_TOKEN` for `make doctor` to report the running process's pending restarts; without it, diagnostics explicitly identify saved-only configuration status.
