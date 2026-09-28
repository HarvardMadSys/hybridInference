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

## What you can customize

The filenames below follow the example layout; manifest paths can name other
files in your distribution.

| What you can customize | File or interface | Rebuild requirement |
|---|---|---|
| Site name, public URL, support email and organization identity | `distribution.yaml` plus the `branding.yaml` selected by `site.branding` | No frontend rebuild; restart the backend after changing mounted files |
| Logo/favicon, links, API example, team/sponsors and data-policy notice | `branding.yaml`; optional mounted `site-assets/` images served at `/site-assets/*` | No frontend rebuild for mounted configuration/assets; packaged asset changes need a rebuild |
| Complete home page `/` | Module `client.tsx` → `Landing`, with `styles.css` and module assets | Build a frontend image with the module |
| Appearance and supported wording of `/login`, `/signup`, `/forgot-password`, `/reset-password`, `/verify-email` | Module `client.tsx` → `AuthFrame` / `fieldLayout` / `authMessages`, plus `styles.css` and shared form hooks | Build a frontend image with the module; authentication controllers stay shared |
| Legal text at `/terms` and the confirmations sign-up asks for | Module `client.tsx` → `TermsFrame`, `TermsContent` and `consentItems` together, preserving required terms/privacy anchors | Build a frontend image with the module |
| Titles and descriptions of the public pages, and their document language | Module `server.ts` → `metaMessages` and `locale` | Build a frontend image with the module |
| Existing console identity and feature settings | `branding.yaml`, `distribution.yaml` and supported environment/admin settings | No frontend rebuild; Site UI cannot replace dashboard, admin, Playground, `/chat`, `/team` or `/authorize` layouts |
| Models, endpoints, aliases, weights and supported router settings | `config/models.yaml`, `config/routing.yaml`, environment and supported admin APIs | No code rebuild for configuration; file edits require backend restart, admin edits apply at runtime |
| Public registration and configured RAG policy | `distribution.yaml` → `features`, plus each feature's environment/admin settings | No frontend rebuild; restart/recreate services for file/environment changes |
| Existing agent link/proxy integration | Frontend container environment: `AGENT_PUBLIC_URL`, `AGENT_WEB_INTERNAL_URL`, `AGENT_CONTROL_PLANE_INTERNAL_URL` | Recreate the frontend for runtime environment changes; `/agents` is not a Site UI slot |
| Trusted adapters, agent-access rules and quota reporting | Backend environment `BACKEND_EXTENSIONS` plus importable extension `.py` modules using documented hooks | Restart for already-mounted code; rebuild when code is packaged in the backend image |
| Additional routes, a different console layout, or a new authentication flow | Application source, such as `apps/frontend/src/app/`, in upstream or an explicitly maintained fork | Rebuild affected application images; these are outside Site UI API v1 |

Configuration cannot replace the home page's hero section or the console's
theme, sidebar or layout, and adding a navigation link does not create the page
it points to. A Site UI module translates only the public pages it supplies;
see [Document language and page titles](site-ui-modules.md#document-language-and-page-titles).

## Configuration-only customization

Start with the annotated `config/examples/distribution.example.yaml` and
`config/examples/branding.example.yaml`. Keep deployment-owned files in your
distribution repository or overlay, with credentials in the environment.
Use the standard frontend image with no Site UI selection to keep the default
page layouts. Reusing that image assumes its compiled backend network target
matches your deployment; see [When changes take effect](#when-changes-take-effect).

### Where your files live

A real deployment's configuration lives in a **distribution overlay**: one
directory under `distributions/` holding that deployment's manifest, config
files, Compose/env inputs, and branding.

```text
distributions/<name>/
├── distribution.yaml       # the manifest: identity + where the config files are
├── config/
│   ├── models.yaml
│   └── routing.yaml
└── deploy/
    ├── backend.env         # any *.env here; Compose reads them all
    └── docker-compose.yml  # optional overlay on deploy/docker/docker-compose.yml
```

The split exists so the source tree and the container image stay neutral. Model
IDs, upstream base URLs, ports, site identity and branding are properties of one
deployment, not of the project; keeping them in an overlay means the image can be
built once and a deployment's overlay mounted into it (`deploy/docker/docker-compose.yml`
mounts the overlay read-only rather than baking it into the image). It also means
a clone of this repository ships nobody's hosts.

For config-path precedence and admin overrides, see
[Configuration](configuration.md#how-a-gateway-finds-its-config).

### The distribution manifest

A manifest is one versioned YAML document that names a deployment and tells the
gateway where its config files are. `config/examples/distribution.example.yaml`
is the annotated reference. Both examples below and in that file close public
signup when activated; change `public_signup` to `true` or null if this
deployment should accept new accounts through the public signup API:

```yaml
schema_version: 1

distribution:
  id: example
  display_name: Example Router
  release: "1.0.0"

site:
  public_base_url: https://your-gateway.example
  support_email: support@your-gateway.example
  branding: branding.yaml        # copy branding.example.yaml beside this manifest

features:
  routers: [fixed]
  public_signup: false
  rag: false

paths:
  models: config/models.yaml      # relative to this file's directory
  routing: config/routing.yaml

deployment:
  target: local
```

`schema_version` must be `1`. `distribution.id` identifies the distribution;
`display_name` is its public name, and `release` records its version.
`deployment.target` records an operator label such as `local` or `staging`;
it does not select a host, build an image or deploy a service.

`paths.models`, `paths.routing` and `paths.alerts` select gateway configuration
files. Relative paths are resolved from the manifest directory; explicit gateway
environment overrides still take precedence. `paths.mcp` and the legal-document
fields are accepted but have the limitations described below.

Point the gateway at it with two environment variables:

```bash
export DISTRIBUTION_CONFIG_PATH=distributions/<name>/distribution.yaml
export DISTRIBUTION_CONFIG_MODE=active
```

**Public sign-up.** With an active manifest, `features.public_signup: false`
closes sign-up completely: the sign-up page disappears and `POST /auth/signup`
answers 403 without creating an account. Neither `SIGNUP_ENABLED=true` nor the
sign-up switch in the admin console can reopen it, and the admin API refuses
that change with 400. To allow sign-up, set the field to `true` or remove it,
restart the backend, and make sure the sign-up switch is on.

When the manifest allows sign-up, or no manifest is active, the admin console's
switch wins over `SIGNUP_ENABLED` (default `true`). `GET /site-config` reports
the result in `features.public_signup`, and the console follows it. Email
verification, domain-based approval and quotas still apply to every new
account.

If the gateway cannot read the sign-up switch from its database within one
second, it treats sign-up as closed rather than falling back to
`SIGNUP_ENABLED`, and tries again after five seconds. Saving the switch in the
admin console clears the error at once.

### Activating a manifest

Setting `DISTRIBUTION_CONFIG_PATH` alone changes nothing. By default the
gateway only does a dry run: it loads and checks the manifest and logs what it
would change, but keeps using the configuration it had. The setting for this
is `DISTRIBUTION_CONFIG_MODE`, and its default value, `dark`, means dry run:

```text
[distribution dark mode] models config stays config/examples/models.openrouter.yaml
(source=default, sha256=51fd6cde50fd); manifest would use
/srv/app/distributions/example/config/models.yaml (sha256=6117799cd0bf) — DIFFERENT
```

Read those lines, then set `DISTRIBUTION_CONFIG_MODE=active` to apply the
manifest. If an active manifest fails to load — a missing mount, a YAML error
— the gateway logs it at `CRITICAL` and comes up with no models, rather than
serving a different registry from the one you named. In a dry run the same
failure is only logged.

Leave the mode unset or set it to exactly `dark` or `active`. Any other value,
an empty one, or `active` without a manifest path is treated as a broken
configuration: sign-up closes (403), `/site-config` answers 503, and the
[docs assistant](rag-chat.md) endpoints answer 503. The warning appears once, in
the startup log, so when you see those errors check `DISTRIBUTION_CONFIG_MODE`
and `DISTRIBUTION_CONFIG_PATH`, fix them and restart the backend.

**Check a manifest before you activate or upgrade it.** Unknown keys at the top
level and under `features` are rejected, so a misspelled feature
(`publicSignup`, `public-signup`) or a feature placed at the top level fails
loudly. Keep operator-specific notes in a separate file. With
`DISTRIBUTION_CONFIG_PATH` set to the manifest you will deploy, run this with
the version you are about to deploy:

```bash
uv run python - <<'PY'
import os
from pathlib import Path
from serving.config.distribution import load_distribution_config

load_distribution_config(Path(os.environ["DISTRIBUTION_CONFIG_PATH"]))
print("Manifest valid.")
PY
```

The `paths`, `site`, `distribution` and `deployment` sections still ignore keys
they do not know, so a misspelled path key silently falls back to the default
registry. After activating, check that the startup log's
`Registered N routes from <path>` names the file you meant.

### Identity, and what the manifest must not contain

The manifest's `site:` and `features:` sections are public: `GET /site-config`
serves them to every visitor once the manifest is active. The site's name and
address also appear in the emails the gateway sends and in the headers it
sends to OpenRouter; those come from `SITE_NAME`, `SITE_PUBLIC_BASE_URL`,
`SITE_DOCS_URL` and `SITE_SUPPORT_EMAIL` when they are set, then from an active
manifest, then from neutral defaults. The standard Compose file sets
`SITE_NAME` to `HybridInference` unless you set it, so set it to your site's
name — or to an empty value to use the manifest's.

Manifests must not contain secrets. Credentials stay in the environment, and
`schema_version: 1` deliberately does not interpolate environment variables into
manifest values.

### Runtime branding

Copy the complete `config/examples/branding.example.yaml`, set its public
values, and point `site.branding` at it. The path is relative to the manifest's
directory. Set the site's name in `distribution.display_name`; a display name
alone, without a branding document, does not replace the frontend's default
name. The branding document is validated at backend startup and exposed through
`/site-config`, so every value in it must be safe for a visitor to read.

| Branding field | What the default UI uses it for |
|---|---|
| `schema_version` | Branding document version; currently `1` |
| `app_description`, `site_host` | Site description and displayed host |
| `organization.name`, `.url`, `.tagline` | Organization identity, link and tagline |
| `links.docs_url`, `.status_url`, `.github_url` | Named documentation, status and repository links; an empty string hides an optional link |
| `links.nav` | Extra HTTPS header links, each with `label` and `url`, in the listed order; does not replace existing navigation |
| `example.api_base`, `.api_key_env_var`, `.model` | Public quickstart example; an empty API base hides it. The environment-variable name is an example, not an API-key value |
| `analytics.statcounter_project_id`, `.statcounter_security_key` | Public identifiers for optional client-side analytics; empty values disable it |
| `signup.turnstile_site_key`, `.fast_track_domain`, `.fast_track_org` | Public widget key and affiliated-organization signup presentation; not server secrets or authorization rules |
| `storage_key_prefix` | Namespace for browser storage; changing it does not migrate existing stored preferences |
| `data_policy_notice` | Displayed data-policy notice; does not configure backend data handling |
| `assets.logo_url`, `.favicon_url` | Branding images, served from permitted HTTPS URLs or `/site-assets/*` |
| `team` | `/team` profiles: `name`, `affiliations`, and optional `badge`, `image`, `website` |
| `sponsors` | Default-home sponsor images: `name`, `alt`, `src`, `class_name`, `width`, `height` |

Use the template and the schema in `serving/config/branding.py` for permitted
values. Branding rejects unknown keys; required fields cannot simply be omitted.
For optional content, use the documented empty strings/lists. Sponsor
`class_name` accepts the schema's supported image-size classes, not arbitrary
CSS. No branding field supplies a universal palette, font or page layout.
Custom page components decide which runtime branding values they render.

Serve local branding images through the mounted site-assets directory and
`/site-assets/*`, or use permitted public asset URLs. Check the URLs over HTTP.
A failed or invalid `/site-config` response produces a configuration error
instead of silently rendering a working site with different defaults.

### Feature settings and their limits

- `features.rag: false` in an active manifest disables the RAG UI and backend
  access. Allowing RAG still requires the service's configuration; see
  [Docs RAG Assistant](rag-chat.md).
- `AGENT_PUBLIC_URL`, or the internal agent service destinations, configure the
  existing agent link/proxy integration. They do not install an agent service.
  See [The public path table](public-path-table.md).
- `features.routers`, `paths.mcp`, `site.terms_document`,
  `site.privacy_document` and `deployment.target` are accepted but have no
  effect yet; see
  [Settings that currently have no effect](configuration.md#settings-that-currently-have-no-effect).
  To publish your own terms, use a [Site UI module](site-ui-modules.md).

A public branding hint or hidden navigation item is not a substitute for
backend authorization. The registration rules above remain effective with a
custom UI module.

Model/provider changes made through supported admin APIs take effect on the
running gateway and are persisted in its database. They do not rewrite the YAML;
stored overrides are applied on top of it again at startup. Account for both
layers when investigating a configuration difference. See
[Runtime configuration](configuration.md#runtime-configuration-from-the-admin-console).

### When changes take effect

- Restart the backend after changing mounted manifest, branding, model/routing
  or alert files. Reload the page to check presentation; the backend serves the
  branding snapshot loaded at startup.
- Replace mounted site-assets files without rebuilding an image. The route
  allows five minutes of browser caching, so verify the served URL and account
  for browser/proxy caches. Changing `SITE_ASSETS_DIR` itself requires recreating
  the frontend container.
- Recreate the affected container after changing runtime environment values.
  A plain container restart retains its old environment. Server-only agent
  destinations are runtime values.
- Use the admin console/API for supported live edits, and check effective values
  and any feature-specific caching.
- Rebuild the frontend after changing true build-only `NEXT_PUBLIC_*` values or
  the backend network target compiled from `BACKEND_INTERNAL_URL`. These differ
  from runtime branding and are not changed by a backend restart.

Use runtime branding for identity rather than introducing new build-time
branding variables. The precise Compose commands are in
[Deployment](deployment.md#what-a-change-actually-requires). Rebuild the affected
image when configuration or assets are baked into it instead of mounted.

### The example distribution

`distributions/example/` is a complete, runnable distribution kept in the
repository as an example: a manifest, a one-model registry pointing at a
bundled fake provider that needs no credential, Compose overlays, and smoke
scripts.
Walk through it with the [Quickstart](router-tutorial.md):

```bash
make up DISTRIBUTION=example
make smoke DISTRIBUTION=example
```

It carries a marker file, `EXAMPLE_OVERLAY`, whose presence keeps it out of
automatic discovery. The Makefile picks a distribution like this:

- Overlays are discovered from `distributions/*/deploy/*.env`, minus any
  directory containing `EXAMPLE_OVERLAY`.
- Exactly one candidate: it is selected automatically, and `make` prints which
  identity it is compiling in.
- Several candidates: `make` fails and asks you to name one.
- None (the state of a fresh clone of this repository): no overlay is selected,
  and the stack is neutral.

`DISTRIBUTION=<name>` selects one by name — including the example, which is only
ever selected explicitly. `DISTRIBUTION=none` opts out.

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
