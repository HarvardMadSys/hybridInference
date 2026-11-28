# Distribution customization

A [distribution](glossary.md#deployments) is one deployment of HybridInference:
its own code, assets and Compose settings, plus the application settings it
keeps in the database, such as its name, branding, models and feature switches.
This page is for operators setting one up: what each kind of customization
costs, the site configuration document, and how to upgrade.

The standard application provides default home, account and terms pages, a
dashboard, API-key management, the Playground, request history and the admin
console. Choose the smallest interface that covers your needs; the
[Quickstart](router-tutorial.md) runs a local example of the standard site.

## What you can customize

| What you can customize | Where | Cost |
|---|---|---|
| Site name, public URL, support email, and feature restrictions such as public registration and RAG | `SITE_CONFIGURATION` in Admin | Applies on settings refresh |
| Logo, favicon, links, API example, team and sponsors, data-policy notice | `SITE_BRANDING` in Admin; optional images mounted through `SITE_ASSETS_DIR` | Applies on settings refresh |
| Models, endpoints, aliases, weights and router settings | Admin Routing, `MODEL_CATALOG` and `ROUTING_POLICY` (see [Configuration](configuration.md)) | Catalog and policy changes need a backend restart; route edits apply online |
| Cloud Agent link and `/agents` proxy | Frontend environment: `AGENT_PUBLIC_URL`, `AGENT_WEB_INTERNAL_URL`, `AGENT_CONTROL_PLANE_INTERNAL_URL` | Recreate the frontend container |
| Link to another deployment's OpenInfra page | Frontend environment: `OPENINFRA_PUBLIC_URL` | Recreate the frontend container |
| The home page, the frame and wording of the account pages, the legal text and sign-up confirmations, public page titles and language | A [Site UI module](site-ui-modules.md) | Build a frontend image with the module |
| Adapters, Cloud Agent access rules and quota reporting | [Backend extensions](backend-extensions.md) listed in `BACKEND_EXTENSIONS` | Restart the backend; rebuild if the code is packaged in the image |
| Extra routes, a different console layout, a new authentication flow | Application source, in upstream or a fork you maintain | Rebuild the affected images; outside Site UI API v1 |

Configuration cannot change the console's theme, sidebar or layout, and a
navigation link does not create the page it points to.

A Site UI module ships inside the console image. Its components, stylesheet and
wording travel with the image, never through `/site-config`, so the pinned pair
of source commits decides what a running image shows. Its dependencies come
from the application's lockfile; a module's own `node_modules` is not used.

## Configuration-only customization

Keep container wiring and assets in the deployment's own repository, and edit
application settings in Admin; the configuration backup includes them. The JSON
examples in `config/examples/` are input to copy or import, never runtime
defaults.

### The distribution manifest

The distribution manifest is the `SITE_CONFIGURATION` setting: a JSON document
with the deployment's identity, public site and feature policy.

```json
{
  "schema_version": 1,
  "distribution": {"id": "team", "display_name": "Team gateway", "release": "1"},
  "site": {"public_base_url": "https://gateway.example", "support_email": "support@example.test"},
  "features": {"public_signup": false, "rag": false},
  "deployment": {"target": "production"}
}
```

- `features.public_signup: false` closes sign-up in both the API and the
  console, whatever the ordinary sign-up switch says.
- `features.rag: false` turns off the RAG endpoints.
- `features.openinfra: true` publishes, at `/openinfra`, the GPU fleet and
  engine metrics that OpenInfra reporters collect. Collecting is the separate
  `OPENINFRA_INGEST_TOKEN` setting; see [OpenInfra](openinfra.md).
- `features.public_stats: true` publishes usage statistics. Schedule
  `python -m serving.analytics.public_stats` with the backend's environment,
  `DB_*` and `BACKEND_EXTENSIONS` included (for example through
  `docker compose exec backend`); without a stored model catalog, its snapshot
  has no per-model section.
- `accounting.currency` defaults to `USD`. Switching to `CNY` needs an offline
  ledger migration first, and once the gateway runs on a ledger Admin refuses
  the change; see [Accounting Currency](accounting.md).

The document accepts no file paths. Use the `MODEL_CATALOG` setting
(**Admin → Routing → Models**) and the `ROUTING_POLICY`, `ALERT_RULES` and
`SITE_BRANDING` settings (**Admin → Settings → Site & Setup → Advanced
settings → Application**) instead. Router
selection belongs to each model in the catalog.

### Activating a manifest

Save the document in Admin Settings. It applies when backend settings refresh,
normally within ten seconds. An old YAML manifest is not imported: store its
settings as this document, as
[the upgrade notes](configuration.md#model-and-routing-documents-in-the-database)
describe.

If the stored document stops validating, the gateway passes over it: the
explicit `SITE_*` settings and then neutral defaults apply, public sign-up is
closed, the RAG endpoints answer `503`, and the console reports the
configuration as incomplete until an administrator corrects the document.

### Identity, and what the manifest must not contain

The site configuration and branding documents are public. Keep credentials out
of them; the site configuration refuses any credential, even a `${NAME}`
reference. Explicit `SITE_*` settings, such as `SITE_NAME` and
`SITE_SUPPORT_EMAIL`, win over the documents. A deployment that sets neither is
neutral.

### Runtime branding

Set the `SITE_BRANDING` document in Admin Settings;
`config/examples/branding.example.json` shows its shape. The console reads it
from `/site-config`, so a change needs no new image. Public images can be
mounted read-only through `SITE_ASSETS_DIR`.

### The example distribution

`make up DISTRIBUTION=example` selects the local tutorial overlay and seeds its
JSON settings. `make demo DISTRIBUTION=example` adds PostgreSQL and the console;
from then on, the stored settings win. The `EXAMPLE_OVERLAY` marker keeps the
example out of automatic distribution discovery, so you always select it by
name.

## Upgrade checklist

This repository maintains the application and the interfaces it publishes:
configuration, Site UI modules and backend extensions. Each distribution
maintains its own configuration, module, deployment and any fork, and decides
when to upgrade.

### An existing source fork

1. Inventory local changes by route and behavior. Separate branding values,
   public-page presentation, shared-console changes and backend logic.
2. Where they fit, move values into configuration and public-page presentation
   into a Site UI module. This is optional; you can keep the fork, with its
   ongoing merge and verification work.
3. List the source changes that remain. Console redesigns, extra routes and new
   authentication behavior do not become supported module features because you
   move their files into the module directory.
4. Merge the upstream version in a separate branch, resolve conflicts against
   your required behavior, and build the exact frontend and backend pair.
5. Verify the distribution in staging before promoting it. A clean merge or a
   green upstream CI does not verify a fork's pages or flows.

### Before promotion

- Pin the upstream version and, for a custom UI, the module version and the
  image digest. Keep the matching configuration and asset versions
  recoverable.
- Read the configuration and API migration notes for the target version.
- Check the image's `/app/site-ui-manifest.json` to confirm the standard or
  custom build, and test packaged and mounted assets over HTTP.
- Check the public routes, configured links, narrow and wide layouts, keyboard
  access, form validation, loading and errors, sign-in and sign-out, and the
  registration policy.
- Check the signed-in console, user and admin permissions, and an inference
  request against the deployed candidate, including any fork behavior.
- Promote only the tested image and configuration together, and keep the
  previous pair for rollback.
