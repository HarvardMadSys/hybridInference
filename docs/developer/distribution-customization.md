# Distribution customization

A distribution can use the standard HybridInference website, apply its own
branding and operating policy, or build its own public pages while sharing the
console and authentication logic. Choose the smallest interface that covers
your requirements; each option has a different build and upgrade cost.

The standard application provides default home, account and terms pages,
dashboard, API-key management, Playground, request history and the admin console.
Authentication, persistence, email, model providers and optional services still
need their documented deployment configuration. The
[Router Tutorial](router-tutorial.md) runs a local example from inference through
the Web/Admin Console.

## 1. What you can customize

“Runtime configuration” below means configuration read by the running services;
it does not mean every file is watched or that an open browser updates live.
The filenames below follow the example layout; manifest paths can name other
files in your distribution.

| What you can customize | File or interface | Rebuild requirement |
|---|---|---|
| Site name, public URL, support email and organization identity | `distribution.yaml` plus the `branding.yaml` selected by `site.branding` | No frontend rebuild; restart the backend after changing mounted files |
| Logo/favicon, links, API example, team/sponsors and data-policy notice | `branding.yaml`; optional mounted `site-assets/` images served at `/site-assets/*` | No frontend rebuild for mounted configuration/assets; packaged asset changes need a rebuild |
| Complete home page `/` | Module `client.tsx` → `Landing`, with `styles.css` and module assets | Build a frontend image with the module |
| Appearance and supported wording of `/login`, `/signup`, `/forgot-password`, `/reset-password`, `/verify-email` | Module `client.tsx` → `AuthFrame` / `authMessages`, plus `styles.css` and shared form hooks | Build a frontend image with the module; authentication controllers stay shared |
| Legal text and frame at `/terms` | Module `client.tsx` → `TermsFrame`, preserving required terms/privacy anchors | Build a frontend image with the module |
| Existing console identity and feature settings | `branding.yaml`, `distribution.yaml` and supported environment/admin settings | No frontend rebuild; Site UI cannot replace dashboard, admin, Playground, `/chat`, `/team` or `/authorize` layouts |
| Models, endpoints, aliases, weights and supported router settings | `config/models.yaml`, `config/routing.yaml`, environment and supported admin APIs | No code rebuild for configuration; file edits require backend restart, admin edits apply at runtime |
| Public registration and configured RAG policy | `distribution.yaml` → `features`, plus each feature's environment/admin settings | No frontend rebuild; restart/recreate services for file/environment changes |
| Existing agent link/proxy integration | Frontend container environment: `AGENT_PUBLIC_URL`, `AGENT_WEB_INTERNAL_URL`, `AGENT_CONTROL_PLANE_INTERNAL_URL` | Recreate the frontend for runtime environment changes; `/agents` is not a Site UI slot |
| Trusted adapters, agent-access rules and quota reporting | Backend environment `BACKEND_EXTENSIONS` plus importable extension `.py` modules using documented hooks | Restart for already-mounted code; rebuild when code is packaged in the backend image |
| Additional routes, a different console layout, or a new authentication flow | Application source, such as `apps/frontend/src/app/`, in upstream or an explicitly maintained fork | Rebuild affected application images; these are outside Site UI API v1 |

There is no configuration field for an arbitrary home-page hero and no public
interface for replacing the console's overall theme, sidebar or layout.
Adding a navigation link does not register a route. A localized home page does
not imply a fully localized console; module wording and document language are
covered under [Your public pages](#your-public-pages).

## 2. Configuration-only customization

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

With an active manifest, `features.public_signup: false` disables both the
signup UI and `POST /auth/signup` (HTTP 403, without creating an account).
Neither `SIGNUP_ENABLED=true` nor a runtime `signup_enabled=true` override can
reopen it. To enable public signup, change the manifest to `true` or leave the
field unset/null and restart the backend, then ensure the effective
`signup_enabled` setting is enabled.

The admin API rejects attempts to set `signup_enabled=true` while the active
manifest disables signup (HTTP 400, without saving the setting). Manifest root
and `features` fields reject unknown keys, including `public-signup`,
`publicSignup`, or a `public_signup` field at the manifest root.

When the manifest allows signup, the runtime `signup_enabled` setting takes
precedence over `SIGNUP_ENABLED` (default `true`). The same rule applies with
no manifest or in dark mode; dark-mode feature values have no effect.
`GET /site-config` reports the resulting boolean in `features.public_signup`,
so the console follows the backend policy. Email verification, domain-based
approval, and quotas remain separate checks; allowing registration does not
bypass them.

If a runtime signup-setting read fails or exceeds one second, the gateway
temporarily disables public signup. A valid `/site-config` response still
returns HTTP 200 with its identity and branding intact and
`public_signup: false`; registration returns HTTP 403 without creating an
account. It does not fall back to an environment value that could reopen
registration.

Because `/site-config` is unauthenticated and read while rendering every
console page, the gateway does not repeat a failing read for every request.
Callers that arrive together while the setting's cache is cold share one
store round-trip, and a failed read is reused for five seconds, so an outage
costs one timeout per window rather than one per request. Recovery needs no
restart: the window expires on its own, and writing `signup_enabled` through
the administrator API clears it at once.

### Dark mode is the default, and that is deliberate

`DISTRIBUTION_CONFIG_MODE` defaults to `dark`. In dark mode the manifest is
loaded and validated, and the resolver logs what it *would* change, but current
resolution stays effective:

```text
[distribution dark mode] models config stays config/examples/models.openrouter.yaml
(source=default, sha256=51fd6cde50fd); manifest would use
/srv/app/distributions/example/config/models.yaml (sha256=6117799cd0bf) — DIFFERENT
```

Setting only `DISTRIBUTION_CONFIG_PATH` therefore cannot change behaviour; you
get a warning telling you the mode defaulted to `dark`. Config-path resolution
treats an unrecognised mode as `dark` with a warning. Signup, `/site-config`, and
the RAG feature policy are stricter: an explicitly empty or unknown mode, or
`active` without a manifest path, closes signup (HTTP 403), makes `/site-config`
report an unavailable configuration (HTTP 503), and makes authenticated
`/v1/rag/status` and `/v1/rag/chat` requests return HTTP 503. Only an unset mode
defaults to `dark`, including in Compose. Enable a manifest by running dark
first, reading the comparison lines, and only then setting `active`.

Mode warnings are logged once per distinct value per process. When diagnosing
RAG configuration errors, check the startup logs and the backend's effective
`DISTRIBUTION_CONFIG_MODE` and `DISTRIBUTION_CONFIG_PATH`, rather than expecting
the warning to repeat on every request. Correct the values and restart the
backend. See [Docs RAG Assistant](rag-chat.md) for the feature restriction.

Failure behaviour differs by mode, on purpose. In dark mode a manifest that will
not load is logged and skipped. In active mode the manifest *is* where the paths
come from, so a manifest that will not load — a lost overlay mount, a YAML error
— refuses to start rather than quietly serving a different registry.

The manifest root and `features` are closed schemas: unknown keys are rejected,
including misspelled feature names or feature flags placed at the root. This
changes the earlier behavior that silently ignored these keys. Before upgrading
an active deployment, validate its actual overlay with the new backend code;
the repository's example manifests do not validate privately maintained
overlays. Correct misspellings and keep operator-specific metadata in a separate
file. With `DISTRIBUTION_CONFIG_PATH` set to the deployed manifest, run:

```bash
uv run python - <<'PY'
import os
from pathlib import Path
from serving.config.distribution import load_distribution_config

load_distribution_config(Path(os.environ["DISTRIBUTION_CONFIG_PATH"]))
print("Manifest valid.")
PY
```

This is not exhaustive typo detection: `paths`, `site`, `distribution`, and
`deployment` still ignore unknown section keys for compatibility. In particular,
check `paths.models` and the resolved model-registry log before activating an
overlay; an unknown path key is treated as an omitted path and can select a
legacy default. Strict validation of those sections is a separate compatibility
change.

If an invalid active manifest reaches the HTTP handlers, signup remains closed
and `/site-config` returns HTTP 503 without exposing file paths or parser
errors. It cannot return a valid identity until the manifest is repaired.

### Identity, and what the manifest must not contain

`site:` and `features:` are served as a public subset by `GET /site-config`, and
`distribution.display_name` / `site:` feed backend-rendered content (transactional
emails, attribution headers) through `get_site_identity()` in
`apps/backend/serving/config/site_identity.py`. Both are gated on
`DISTRIBUTION_CONFIG_MODE=active`; in dark mode `/site-config` returns a
neutral document and identity falls back to `SITE_NAME` / `SITE_PUBLIC_BASE_URL`
/ `SITE_DOCS_URL` / `SITE_SUPPORT_EMAIL` or to neutral defaults.

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
- `features.routers` is declared/public metadata. It does not select or restrict
  the model router; use the model's `router` and supported admin overrides in
  [Routing](routing.md).
- `AGENT_PUBLIC_URL`, or the internal agent service destinations, configure the
  existing agent link/proxy integration. They do not install an agent service.
  See [The public path table](public-path-table.md).
- `site.terms_document` and `site.privacy_document` are accepted manifest fields,
  but they do not load text into the current terms page. Use the legal-page
  interface in [Your public pages](#your-public-pages) to publish custom text.
- `paths.mcp` is accepted by the resolver, but no component consumes it at this
  revision. It does not enable a service.

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

`distributions/example/` is a complete, runnable overlay kept in the repository
as a teaching artifact: a manifest, a one-model registry pointing at a bundled
fake provider that needs no credential, Compose overlays, and smoke scripts.
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

(your-public-pages)=

## 3. Your public pages

A deployment can replace the public pages a visitor sees — the landing page, the
frame around the account pages, the legal page — without forking the console.
The seam is a **build-time UI module**: a small tree of React components that the
frontend image compiles in, alongside the shared application rather than instead
of it.

One Next.js application, one console, one session. A distribution ships a small
UI module; the build compiles it in.

```text
hybridinference @ C
  ├── neutral backend image
  └── generic Next.js frontend
        + distribution UI module @ U   →  the distribution's own frontend image
```

Here `C` is the upstream source commit and `U` is the distribution's UI source
commit. Pin both when building a release. The module is not fetched or swapped
at runtime; component, stylesheet and compiled-copy changes require a new
frontend image. Runtime branding read by the module retains its runtime behavior.

### What a module can and cannot change

| The module owns | The shared application keeps |
|---|---|
| the home page, if it exports one | dashboard, chat, admin, team and authorize pages, and the `/agents` proxy integration |
| the frame around the account pages | the account forms, their fields, validation and submit calls |
| the legal page, if it ships its own text | the terms anchors the footer and the sign-up step link to |
| its own wording for the account pages | the schemas, error codes, session handling and redirect rules |
| its own stylesheet and design assets | the runtime branding document and `/site-config` |

A module cannot change who gets in: the account pages' business logic, the
registration gate, the email-verification requirement and the permission rules
are all in the shared application and are not part of this interface.

The shared application also keeps the session store, proxy rules, and
`AuthField` / `AuthNotice` / `AuthLoading` accessibility behavior. A module
cannot register a route or take over an unlisted page. A new path such as
`/pricing`, a console redesign or a new authentication method needs an
application change beyond this interface.

A module may import `@site-ui/host`, `react`, `react/*`, `react-dom`, `next`,
`next/*` and its own files. Relative imports must stay inside the module.
Anything else — in particular `@/...` — fails the build.

The host facade exposes public site configuration, resolved branding, session
state and the supported presentation types/helpers. Session state lets a page
choose a sign-in or console link; the facade does not expose login/logout,
tokens or authentication operations. The normative TypeScript interfaces live
in `apps/frontend/src/site-ui/contract.ts`, and the supported imports in
`apps/frontend/src/site-ui/host.ts`.

### A module is a directory

```
my-ui/
  manifest.json     identity: id, site_ui_api, locale
  client.tsx        the components (required)
  server.ts         required entry; document language (locale is optional)
  styles.css        the module's stylesheet (required, may be empty)
  public/           assets, under public/site-assets/<id>/
  ...               the module's own components, however it likes
```

Import the stylesheet from the client entry so it reaches the application
bundle; declaring a `styles.css` file alone does not load it:

```tsx
import './styles.css';
```

`server.ts` must exist even when no locale is needed; it may contain only
`export {};`. Only its `locale` export is optional.

`client.tsx` exports:

| Export | Required | What it means |
|---|---|---|
| `descriptor` | yes | A named literal `{ siteUiApi, id, locale }`; all three values must match `site_ui_api`, `id` and `locale` in the manifest. |
| `Landing` | no | Rendered at `/`. Supplying one replaces the console's home page. |
| `AuthFrame` | no | The frame around `/login`, `/signup`, `/forgot-password`, `/reset-password`, `/verify-email`. |
| `TermsFrame` | no | Rendered at `/terms` instead of the console's legal text. |
| `authAppearance` | no | Deprecated class-map compatibility for the shared forms, supported throughout API v1. |
| `authMessages` | no | The deployment's wording for the account pages. |

**An optional export is a claim, not a gap.** `Landing: null` means "the
console's home page is correct here"; omitting `AuthFrame` means "the account
pages sit inside the console's container". A module that exports neither is a
module that replaced nothing, and that is a legitimate thing to be while it is
being written.

**A frame that exists owns the whole page.** `AuthFrame` and `TermsFrame` render
their own header, `<main>` and footer, because supplying one is a claim to draw
the page. A frame that draws only a card leaves five pages with no chrome — that
is not a styling choice the host can correct, so it is the first thing to get
right.

`AuthFrame` must render the supplied shared form `children` and preserve any
provided `topbar` and `legal` nodes. Its headings and page identifier let one
frame serve the five account routes. It supplies the complete outer page, so
the host does not add another header or footer around it.

`TermsFrame` supplies its own legal text; the host passes `null` as its children.
Preserve the `terms-s` section-anchor contract, particularly `/terms#terms-s5`
for privacy links from the footer and signup consent. Setting manifest file
paths does not substitute a legal document here.

`authMessages` accepts the declared message keys and plain strings; it changes
wording, not validation or the required field set. Missing keys retain the
shared defaults. Preserve each message's interpolation variables. A module may
localize its own pages and supported keys, but this does not translate every
console page.

### Building an image with a module

The official frontend Dockerfile takes the module as a Buildx named context:

```bash
# The neutral image. The command is unchanged, and needs no new arguments.
docker build -f deploy/docker/Dockerfile.frontend -t local/frontend .

# A module from this repository, for development and for the public example.
docker buildx build -f deploy/docker/Dockerfile.frontend \
  --build-context site-ui=./distributions/example/frontend/site-ui \
  --build-arg SITE_UI_API=1 \
  --load -t local/frontend:example .

# A distribution's own repository, at a pinned commit.
docker buildx build -f deploy/docker/Dockerfile.frontend \
  --build-context "site-ui=<UI_REPOSITORY>#<U>:frontend" \
  --build-arg SITE_UI_SUBDIR=site-ui \
  --build-arg SITE_UI_API=1 \
  --tag "<FRONTEND_IMAGE>" --push "<CORE_REPOSITORY>#<C>"
```

| Input | Default | Meaning |
|---|---|---|
| `site-ui` context | the built-in marker | no external module; the neutral UI |
| `SITE_UI_SUBDIR` | `.` | the module's directory inside the context |
| `SITE_UI_API` | unset | required with an external module; must be `1` |

A distribution may pass its whole `frontend/` tree as the context and select
the module with `SITE_UI_SUBDIR=site-ui`. This keeps the module and its related
build inputs under one pinned source tree.

An external context that carries no module **fails the build**. Falling back to
the neutral UI would publish a site whose home page reverted, and nothing in the
build log would say so.

#### Local development

The same staging step runs on a workstation, without Docker. Run a gateway
first and set `BACKEND_INTERNAL_URL` to its address if it is not
`http://backend:8080`; the existing runtime `/site-config` dependency is unchanged:

```bash
cd apps/frontend
node scripts/site-ui/prepare-module.mjs prepare \
  --app . --context ../../distributions/example/frontend/site-ui \
  --subdir . --into src/site-ui/external --api 1
SITE_UI_DIR="$PWD/src/site-ui/external" SITE_UI_API=1 \
  SITE_ASSETS_DIR="$PWD/src/site-ui/external/public/site-assets" npm run dev
# For build or type-check, pass the same module selection variables.
```

`SITE_UI_DIR` and `SITE_UI_API` name the module directly if you would rather not
stage a copy:

```bash
SITE_UI_DIR=/path/to/my-ui SITE_UI_API=1 \
  SITE_ASSETS_DIR=/path/to/my-ui/public/site-assets npm run dev
```

The variables above apply to that one command; no shell profile is changed. To
return to the neutral UI, stop the server and run:

```bash
env -u SITE_UI_DIR -u SITE_UI_API -u SITE_ASSETS_DIR npm run dev
```

Local dev serves module assets via `SITE_ASSETS_DIR`; the image recipe packages
them automatically.

The staging environment file is used by the Dockerfile, not automatically read
by npm. Leaving a staged directory behind does not select it for later commands.

Either way the resolver writes `src/site-ui/active/` and
`tsconfig.generated.json`, so the bundler, the type checker and the test runner
all read the same decision. Those files are generated; do not edit them.

### Assets

A module's images live in `public/site-assets/<module-id>/`, and the build merges
them into the image. Three rules, all enforced:

- **Under the module's own id.** An asset elsewhere fails the build. This is what
  makes "two modules cannot write the same path, and neither can shadow the
  application's" true.
- **Never a collision.** A module asset at a path the application already ships
  fails the build rather than overwriting it.
- **The route is what serves them.** `/site-assets/*` is a Next route handler,
  and a route handler shadows static file serving for the whole prefix — so
  "the file is in `public/`" is not evidence that it is reachable. Test the URL.

At runtime, `SITE_ASSETS_DIR` is read first and the image's own copy second. A
deployment that mounts its branding directory keeps overriding the module's
files, and one that mounts nothing still serves a complete site.

### Styling the shared forms

The shared account forms render their own markup and default styling. Stable
semantic hooks provide styles and state; a narrow layout slot provides structural
variation. The deprecated class map remains supported throughout API v1.

**`data-auth` attributes for styling and state.** Shared form elements expose:

| Attribute | On |
|---|---|
| `data-auth="form"` | the `<form>` |
| `data-auth="field"` | each field's wrapper |
| `data-auth="label"` / `"control"` / `"password-control"` | label and inputs |
| `data-auth="reveal"` | the show-password button |
| `data-auth="hint"` / `"error"` | the hint and the error paragraph |
| `data-auth="notice"` | an error or success notice |
| `data-auth="submit"` / `"field-action"` / `"secondary-actions"` | the buttons and links |
| `data-auth="consent"` / `"consent-section"` | the sign-up confirmations |
| `data-auth="loading"` | the resolving-session region |

State is carried by the attributes the application already sets — `aria-invalid`,
`disabled`, `aria-busy`, `data-auth-error` — so a stylesheet never has to infer
"this field is in error" from a class name. The set is part of the contract: an
attribute is added, never repurposed.

**Structural layout.** The optional `fieldLayout` receives the shared control,
hint, error and action nodes, and the label's text and `htmlFor` id. It must
render every supplied node, associate the label with that id, and preserve the
`data-auth="field"` and `data-auth="label"` hooks. The neutral layout places the
field action after the control and validation; a module may place it beside its
label. This replaces layout booleans without moving validation or controllers
into a distribution.

**Deprecated `authAppearance` class map.** The class-map export, including its
nested `fieldLayout` entry, remains a supported compatibility surface throughout
API v1. New styling should prefer scoped `data-auth` and state selectors; class
names are module-owned details. The semantic-style migration is not complete.
Removing this adapter requires the next API revision, a documented migration and
comparison of normal, invalid, loading and keyboard states. Deprecation does not
allow breaking existing v1 modules.

All customization is subject to the same rule: **the module's CSS may only affect
the pages it owns.** A stylesheet that reaches `body`, `:root` or a bare element
selector will restyle the console too, and no check will catch it for you —
scope everything under a root class the module sets on its own shell.

### Compatibility

- **Adding a module does not change the console.** The five account pages sit in
  the console container when no `AuthFrame` is exported, and the shared pages,
  schemas and session handling are untouched either way.
- **Removing an optional export is safe.** A module that stops exporting
  `Landing` gets the console's home page back.
- **Renaming or removing an export, changing a prop's meaning, or repurposing a
  `data-auth` value is a breaking change** and belongs behind an API revision
  bump. So does renaming a key in `authMessages` or changing its interpolation
  variables.
- **Adding an optional key or attribute is compatible.**

`SITE_UI_API` selects the Site UI interface revision. The resolver refuses a
module that declares a revision this checkout does not implement, rather than building
something neither side was written for.

The optional server `locale` sets the root document's `lang`, including console
routes; omitting it keeps `en`. Document titles, descriptions and icons come
from runtime branding. The module API has no metadata override or shared
public-layout export; modules can organize their own internal layouts freely.

### Testing a module

The shared repository owns validation and the image recipe. These checks use
the built-in neutral module and the public example.

```bash
cd apps/frontend
npm run lint
npm run type-check
npm test
```

When checking your own module, pass the same module selection variables used
for local development. Exercise normal, invalid, loading and keyboard states of
the shared forms, every public page your module supplies, and the console to
check for style leakage.

From the repository root, build the neutral image and the public example through
the same upstream recipe:

```bash
docker buildx build -f deploy/docker/Dockerfile.frontend \
  --load -t local/frontend:neutral .
docker buildx build -f deploy/docker/Dockerfile.frontend \
  --build-context site-ui=./distributions/example/frontend/site-ui \
  --build-arg SITE_UI_API=1 --load -t local/frontend:example .
docker run --rm --entrypoint cat local/frontend:neutral /app/site-ui-manifest.json
docker run --rm --entrypoint cat local/frontend:example /app/site-ui-manifest.json
```

The first manifest must report `kind: "neutral"`; the second must report
`id: "example"`. The **Site UI Containers** CI job builds these two shapes and
checks the example asset over HTTP. These image builds need Docker and network
access for dependencies. Serving the full application also needs the gateway's
runtime `/site-config` endpoint.

Build and test the distribution's own image and assets as well. An upstream
example passing does not validate a privately maintained module.

(backend-extensions)=

## 4. Backend extensions

`BACKEND_EXTENSIONS` is an optional comma-delimited list of trusted, local Python
module names. It is empty by default. Each module must expose a synchronous
`register()` function that takes no arguments. The gateway imports and registers
each module once per process, after loading dotenv and before constructing
runtime routes or consuming their registries. An import or registration failure
aborts startup; the gateway does not silently use a different adapter.

Extensions register factories through
`serving.servers.registry.register_adapter_factory(kind, factory, *, override=False)`.
A factory receives a configuration dictionary and returns an adapter, before
built-in provider defaults are applied. Registering an existing extension kind
is an error. Replacing a built-in kind requires `override=True` and emits a log
entry. Registered kinds also become reserved provider labels. The startup
`register()` function may populate the existing runtime-setting and provider
metadata dictionaries before their consumers run; it must mutate those shared
dictionaries rather than replace them.

The deployment must make these modules importable, for example through its
read-only overlay mount. This executes trusted server code, not user-supplied
configuration: do not derive module names from requests or allow an admin form
to choose them. The loader does not fetch remote modules or load code per
request. Import all extension code during startup, avoid later lazy imports or
live code reloads, and restart the backend when changing the mounted extension.
See [Deployment-local adapters](adding-models.md#deployment-local-adapters) for
a minimal factory example.

### Cloud Agent access

The gateway keeps its fixed role hierarchy and resolves current account state.
The deployment owns the rule for who can use or administer its Cloud Agent.
A trusted extension registers one synchronous callback during `register()` with
`serving.agent_access.register_agent_access_policy(policy)`. The callback receives
a read-only mapping containing exactly `user_id` and `role`, after the gateway
has verified that the user exists and has status `active`. It receives no
profile, credentials, or database connection.

The callback returns a `list[str]` or `tuple[str, ...]` of explicit permissions:
`agent.use` permits Agent use, and `agent.admin` permits Agent administration.
Administration requires both permissions. Unknown, repeated, or non-string
permissions are invalid; values are not coerced. Registering a second policy is
an error. With no registered policy, Agent access is denied for every role.
The gateway imposes no deployment-specific role-to-permission rule.

The standalone Agent reads `GET /internal/users/{user_id}/agent-access` using
the existing `GATEWAY_GRANT_DISPATCH_TOKEN` bearer token. A successful response
contains exactly `user_id`, `allowed`, and `permissions`; `allowed` is true
exactly when `agent.use` is present. Permissions are returned in the order
`agent.use`, `agent.admin`. An intentional denial is HTTP 200 with
`allowed: false` and `permissions: []`. The endpoint reads the gateway's
operational store and evaluates the policy on each request; it does not cache
permission decisions. Account reads retain the existing user-cache semantics:
`CachedOperationalStore` caches user rows for 60 seconds, and account changes
through that wrapper invalidate the affected entry. Direct database edits or
writes in another process may remain unseen until that cache entry expires.

Unknown or inactive accounts receive HTTP 403 with error type
`subject_unavailable`, before the callback runs. A callback exception or invalid
result receives HTTP 503 with error type `agent_access_unavailable`, without
policy details. Both use the gateway's standard `{"error": {"type": ...,
"message": ...}}` envelope. Dispatch authentication and unavailable internal
API configuration retain their existing 401 and 404 responses. The legacy
user-status and model-catalog endpoints retain their existing contracts.

### Quota reporting

The gateway keeps quota reporting and quota-aware routing independent of the
service that measures usage. The public framework supplies the Providers
tab's result rows, disabled-key cards, key discovery and `gather_all`, plus
RouteWise's quota snapshots. No quota source is enabled by default. A trusted
backend extension connects an authorized usage API or the operator's own
metering service; the framework does not require a particular supplier,
website login or cookie.

An extension's `register()` calls
`serving.admin.provider_quotas.register_quota_fetcher(provider, display_name, fetch)`;
`fetch` is awaited as `fetch(operational_store, services)` and returns one
`ProviderQuotaResult` per configured key, converting its own failures into
results rather than raising. The admin endpoint supplies the store and services;
RouteWise currently calls it with `(None, None)`, so the fetcher must also work
without those objects. Registration runs at startup, not when the module is
merely imported. A fetcher queries usage; it does not send user inference
requests or change the route's inference protocol.

Each result contains `ProviderQuotaUsage` rows with `label`, `used`, `limit`,
`unit` and an optional timezone-aware `reset_at`. The deployment's provider
identifier links the source to its routes; it is not a hard-coded vendor
enum. `quota_source.provider`, `usage_label` and `unit` must exactly match a
returned result and usage row. Report the account/window shared by that quota
pool, and keep independent accounts in distinct sources. Do not sum unrelated
keys or present a failed query as zero usage.

The UI can display percentages, currencies and other units, but RouteWise's
quota admission consumes one **request** at a time and requires a compatible
count-based source. A percentage alone is not a request allowance. The source
owns window boundaries and reset times; `quota.limit` is a cross-check against
its reported limit, not a replacement measurement. A source with no successful
snapshot stays unready. A failed refresh does not manufacture a fresh balance;
an existing pool retains its last successful snapshot and local increments.

#### Run a local quota source

The complete [example extension](../../distributions/example/quota_extension.py)
reads `/usage` from the bundled fake provider. Its in-memory daily counter is
only a teaching fixture, resets on process restart or at UTC midnight, and is
not production accounting. It uses no real account or credential and is loaded
only when explicitly selected through `BACKEND_EXTENSIONS`.

After the developer setup, run these commands from the repository root in
three terminals. First, start a simulated quota provider:

```bash
uv run python distributions/example/fixtures/fake-openai-provider/server.py \
  --port 18353 --response-text ROUTED_TO_QUOTA --quota-limit 100
```

Then start a slower, priced fallback (its prices in the example are fictional):

```bash
uv run python distributions/example/fixtures/fake-openai-provider/server.py \
  --port 18352 --response-text ROUTED_TO_FALLBACK --ttft-delay-ms 400
```

Finally start the gateway with the opt-in
[quota registry](../../config/examples/models.routewise.quota.yaml). Name the
demo token once so the admin call below can reuse it:

```bash
export DEMO_ADMIN_TOKEN=local-quota-demo-only

PYTHONPATH=.:apps/backend \
  PYTHON_DOTENV_DISABLED=1 \
  BACKEND_EXTENSIONS=distributions.example.quota_extension \
  EXAMPLE_QUOTA_BASE_URL=http://127.0.0.1:18353 \
  MODELS_CONFIG_PATH=config/examples/models.routewise.quota.yaml \
  ROUTING_CONFIG_PATH=config/examples/routing.minimal.yaml \
  DB_ENABLED=false USER_AUTH_ENABLED=false ADMIN_TOKEN="${DEMO_ADMIN_TOKEN}" \
  JWT_SECRET_KEY=local-quota-demo-signing-secret-not-for-production \
  uv run uvicorn serving.servers.app:app --no-proxy-headers --host 127.0.0.1 --port 18080
```

These settings disable dotenv loading and user authentication and use a public
demo-only admin token and JWT signing secret: keep the listener on loopback and
never use them for a real deployment. The admin endpoint needs the signing
secret even when authenticating with the demo token. Read the same result the
console uses:

```bash
curl http://127.0.0.1:18080/admin/provider-quotas \
  -H "Authorization: Bearer ${DEMO_ADMIN_TOKEN}"
```

Send several requests to calibrate the cost envelope and allow the first
five-second probe/snapshot cycle to finish:

```bash
curl http://127.0.0.1:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"quota-demo","messages":[{"role":"user","content":"hi"}]}'
```

The response identifies the selected fixture. Initial requests use the fallback
until the quota route has both a usage snapshot and cost-envelope evidence.
Accepted chat requests, including active probes, consume the mock quota;
health, model discovery and `/usage` reads do not. To simulate exhaustion,
restart only the quota fixture with `--quota-limit 100 --quota-used 100`; after
the next snapshot refresh, requests continue through the fallback. Stop each
local process with Ctrl+C when finished.

To see the card in the authenticated Web/Admin Console, configure this same
extension and registry on a local full-stack demo backend, then open
**Providers → Quotas**. Use a quota endpoint reachable from that backend:
inside Compose, `127.0.0.1` denotes the backend container, not the host. The
[Router Tutorial](router-tutorial.md) covers the full-stack demo and its
authentication. With no returned results the Quotas tab stays visible and
links back here; Overview, Keys, Availability and Performance are independent.

For your own integration, copy the extension and replace its local HTTP query
and mapping with your authorized data source. Preserve the result contract, bound network
requests with a timeout, handle failures explicitly and keep credentials out
of responses and logs. Supplier-specific authorization and service terms still
apply; using an extension is a software boundary, not an exemption from them.

## 5. Upgrade checklist

Upstream maintains the standard implementation, configuration consumers and
published Site UI contract. Each distribution maintains its configuration,
module, deployment and any fork changes. Upstream merging a change does not
itself define when a distribution upgrades; that is its release pipeline's
responsibility.

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
