# Distribution-owned public pages

A deployment can replace the public pages a visitor sees — the landing page, the
frame around the account pages, the legal page — without forking the console.
The seam is a **build-time UI module**: a small tree of React components that the
frontend image compiles in, alongside the shared application rather than instead
of it.

Start with the [Distribution customization guide](distribution-customization.md)
to choose between runtime configuration, a public UI module and a source fork.
Branding and existing feature switches need only
[configuration](configuration.md); the module described here changes public-page
components and their wording, not the shared console layout.

## 1. What a module can and cannot change

| The module owns | The shared application keeps |
|---|---|
| the home page, if it exports one | the console: dashboard, chat, admin, team, authorize, agents |
| the frame around the account pages | the account forms, their fields, validation and submit calls |
| the legal page, if it ships its own text | the terms anchors the footer and the sign-up step link to |
| its own wording for the account pages | the schemas, error codes, session handling and redirect rules |
| its own stylesheet and design assets | the runtime branding document and `/site-config` |

A module cannot change who gets in: the account pages' business logic, the
registration gate, the email-verification requirement and the permission rules
are all in the shared application and are not part of this interface.

## 2. A module is a directory

```
my-ui/
  manifest.json     identity: id, site_ui_api, locale
  client.tsx        the components (required)
  server.ts         document language (locale is optional)
  styles.css        the module's stylesheet (required, may be empty)
  public/           assets, under public/site-assets/<id>/
  ...               the module's own components, however it likes
```

`client.tsx` exports:

| Export | Required | What it means |
|---|---|---|
| `descriptor` | yes | `{ siteUiApi, id, locale }`. The id must match the manifest. |
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

## 3. Building an image with a module

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

### Local development

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

## 4. Assets

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

## 5. Styling the shared forms

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

## 6. Compatibility

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

`SITE_UI_API` is the version of this document. The resolver refuses a module
that declares a revision this checkout does not implement, rather than building
something neither side was written for.

The optional server `locale` sets the root document's `lang`, including console
routes; omitting it keeps `en`. Document titles, descriptions and icons come
from runtime branding. The module API has no metadata override or shared
public-layout export; modules can organize their own internal layouts freely.
