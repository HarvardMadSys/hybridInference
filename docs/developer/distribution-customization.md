# Distribution customization

A [distribution](glossary.md#deployments) is one deployment's own files: its
name and branding, its model and routing configuration, and its feature
switches. This page is for operators setting one up. It covers what
configuration alone can change, how the manifest works, and how to upgrade.

A distribution can use the standard HybridInference website, apply its own
branding and operating policy, or build its own public pages while sharing the
console and authentication logic. Choose the smallest interface that covers
your requirements; each option has a different build and upgrade cost.

The standard application provides default home, account and terms pages,
dashboard, API-key management, Playground, request history and the admin console.
Authentication, persistence, email, model providers and optional services still
need their documented deployment configuration. The
[Quickstart](router-tutorial.md) runs a local example from inference through
the Web/Admin Console.


Application identity, feature restrictions and branding now live in the
`SITE_CONFIGURATION` and `SITE_BRANDING` settings in Admin. Model/routing/alert
configuration is database-backed too. Application manifests and branding files
are no longer read at runtime. The build-time Site UI/module mechanisms below
remain separate. See [Configuration](configuration.md) for migration and editing.

## What you can customize

Use Admin for application settings and a deployment overlay for code and assets.

| What you can customize | File or interface | Rebuild requirement |
|---|---|---|
| Site name, public URL, support email and organization identity | `SITE_CONFIGURATION` and `SITE_BRANDING` in Admin | Applies on settings refresh |
| Logo/favicon, links, API example, team/sponsors and data-policy notice | `SITE_BRANDING`; optional mounted `site-assets/` images served at `/site-assets/*` | Applies on settings refresh; packaged asset changes need a rebuild |
| Complete home page `/` | Module `client.tsx` → `Landing`, with `styles.css` and module assets | Build a frontend image with the module |
| Appearance and supported wording of `/login`, `/signup`, `/forgot-password`, `/reset-password`, `/verify-email` | Module `client.tsx` → `AuthFrame` / `fieldLayout` / `authMessages`, plus `styles.css` and shared form hooks | Build a frontend image with the module; authentication controllers stay shared |
| Legal text at `/terms` and the confirmations sign-up asks for | Module `client.tsx` → `TermsFrame`, `TermsContent` and `consentItems` together, preserving required terms/privacy anchors | Build a frontend image with the module |
| Titles and descriptions of the public pages, and their document language | Module `server.ts` → `metaMessages` and `locale` | Build a frontend image with the module |
| Existing console identity and feature settings | `SITE_BRANDING`, `SITE_CONFIGURATION` and supported Admin settings | No frontend rebuild; Site UI cannot replace dashboard, admin, Playground, `/chat`, `/team` or `/authorize` layouts |
| Models, endpoints, aliases, weights and supported router settings | Admin Routing, `MODEL_CATALOG` and `ROUTING_POLICY` | Follow restart markers on documents; ordinary route edits apply online |
| Public registration and configured RAG policy | `SITE_CONFIGURATION` → `features`, plus each feature's Admin settings | Follow the setting's refresh or restart marker |
| Existing agent link/proxy integration | Frontend container environment: `AGENT_PUBLIC_URL`, `AGENT_WEB_INTERNAL_URL`, `AGENT_CONTROL_PLANE_INTERNAL_URL` | Recreate the frontend for runtime environment changes; `/agents` is not a Site UI slot |
| Trusted adapters, agent-access rules and quota reporting | Backend environment `BACKEND_EXTENSIONS` plus importable extension `.py` modules using documented hooks | Restart for already-mounted code; rebuild when code is packaged in the backend image |
| Additional routes, a different console layout, or a new authentication flow | Application source, such as `apps/frontend/src/app/`, in upstream or an explicitly maintained fork | Rebuild affected application images; these are outside Site UI API v1 |

Configuration cannot replace the home page's hero section or the console's
theme, sidebar or layout, and adding a navigation link does not create the page
it points to. A Site UI module translates only the public pages it supplies;
see [Document language and page titles](site-ui-modules.md#document-language-and-page-titles).

## Configuration-only customization

Application settings are edited in Admin and stored in PostgreSQL. No
application manifest, model, routing, alerts or branding file is read at runtime.
Docker Compose remains the infrastructure definition, and a deployment can keep
its own overlay for containers, public assets and frontend build inputs.

### Where your files live

Container wiring and assets belong to the deployment repository. Application
configuration belongs in Admin Settings and is included in its configuration
backup. Checked-in JSON examples are optional import input, not runtime defaults.

### The distribution manifest

The former manifest is now the `SITE_CONFIGURATION` JSON setting:

```json
{
  "schema_version": 1,
  "distribution": {"id": "team", "display_name": "Team gateway", "release": "1"},
  "site": {"public_base_url": "https://gateway.example", "support_email": "support@example.test"},
  "features": {"public_signup": false, "rag": false},
  "deployment": {"target": "production"}
}
```

Model/routing file paths and a branding file path are not accepted here. Use
`MODEL_CATALOG`, `ROUTING_POLICY`, `ALERT_RULES` and `SITE_BRANDING` in the same
Admin category instead.

### Activating a manifest

Save the site document in Admin Settings. It applies when backend settings
refresh, normally within ten seconds. There is no active/dark mode to select.
For existing external YAML, use the explicit
[legacy importer](configuration.md#model-and-routing-documents-in-the-database),
then verify the stored documents before removing the old files.

### Identity, and what the manifest must not contain

The site and branding documents are public, strictly validated contracts. Keep
credentials in separate secret settings. The `SITE_*` operational settings retain
their existing precedence when explicitly configured; otherwise the site and
branding documents supply the identity. An unconfigured deployment is neutral.

### Runtime branding

Set the validated `SITE_BRANDING` JSON document in Admin Settings. The example
shape is in `config/examples/branding.example.json`. The console obtains the
public branding payload from `/site-config`; updating it requires no frontend
image rebuild. Public image assets can still be mounted read-only through
`SITE_ASSETS_DIR`; these are assets rather than application configuration.

### Feature settings and their limits

`features.public_signup: false` closes signup in both the API and console,
regardless of the ordinary signup switch. `features.rag: false` disables the RAG
surface. Public usage statistics remain opt-in through `public_stats`; schedule
`python -m serving.analytics.public_stats` for deployments that enable them.
Router selection still belongs to each model and its router parameters.

### When changes take effect

Site configuration and branding follow settings refresh. Catalog, routing policy
and alert-rule changes are marked Restart required. Ordinary Admin provider and
route changes synchronize between processes and report their applied status.
Frontend code and Site UI module changes still require a frontend build.

### The example distribution

`make up DISTRIBUTION=example` explicitly selects the local teaching fixture and
seeds its JSON settings. `make demo DISTRIBUTION=example` adds PostgreSQL and the
console; from then on the stored settings win. The `EXAMPLE_OVERLAY` marker keeps
the example out of automatic deployment discovery.

## Custom public pages and backend code

Configuration covers the site's identity, its branding, its models and its
feature switches. Two further interfaces go beyond that, each with a build or
deployment cost of its own:

- [Site UI Modules](site-ui-modules.md) replace the public pages — the home
  page, the frame around the account pages, the terms — with the
  distribution's own React components, compiled into its console image.
- [Backend Extensions](backend-extensions.md) load trusted Python code at
  startup to add an adapter, a Cloud Agent access rule or a quota source.

## Upgrade checklist

This repository maintains the application and the interfaces it publishes:
configuration, Site UI modules and backend extensions. Each distribution
maintains its own configuration, module, deployment and any fork, and decides
when to upgrade.

### An existing source fork

1. Inventory local changes by route and behavior. Separate branding values,
   public-page presentation, shared-console changes and backend logic.
2. Where they fit, move values into runtime configuration and public-page
   presentation into a Site UI module. Migration is optional; you can keep the
   fork, with its ongoing merge and verification work. A UI module keeps
   authentication controllers shared.
3. Identify remaining source changes explicitly. Console redesigns, extra
   routes and new authentication behavior do not become supported module
   features merely by moving files into the module directory.
4. Merge the intended upstream version in an isolated branch, resolve conflicts
   against your required behavior, and build the exact frontend/backend pair.
5. Verify the distribution in staging before promoting it. A successful merge
   or upstream CI result alone does not verify a fork's presentation or flows.

Adopting Site UI can reduce future conflicts; it does not automatically migrate
an existing fork or make arbitrary upstream source paths stable interfaces.

### Before promotion

- Pin the upstream version and, for custom UI, the module version and produced
  image digest. Keep the matching configuration and asset versions recoverable.
- Read configuration/API migration notes and validate against the selected
  version. Unsupported module API revisions must stop the build.
- Inspect the actual image's `/app/site-ui-manifest.json` to confirm the intended
  standard or custom build; test packaged and mounted assets over HTTP.
- Check public routes, configured links, narrow/wide layouts, keyboard access,
  form validation/loading/errors, login/logout and the registration policy.
- Check the authenticated console, user/admin permissions and an inference
  request against the deployed candidate, including any local fork behavior.
- Promote only the tested image/configuration combination and retain the prior
  combination for rollback.
