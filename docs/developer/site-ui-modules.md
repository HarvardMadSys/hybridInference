# Site UI Modules

This page is for frontend developers building a distribution's own public
pages. If configuration and branding are enough for you, see
[Distribution customization](distribution-customization.md) instead.

A deployment can replace the public pages a visitor sees — the landing page, the
frame around the account pages, the legal page — without forking the console.
It does so with a **build-time UI module**: a small tree of React components
that the frontend image compiles in, alongside the shared application rather
than instead of it.

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

## What a module can and cannot change

| The module owns | The shared application keeps |
|---|---|
| the home page, if it exports one | dashboard, chat, admin, team and authorize pages, and the `/agents` proxy integration |
| the frame around the account pages, and where each field's parts go | the account forms, their fields, validation and submit calls |
| the legal text and the sign-up confirmations, if it ships its own | the consent step, the one `accepted_tos` flag sign-up records, and the terms anchors the account pages link to |
| its own wording for the account pages | the schemas, error codes, session handling and redirect rules |
| the public pages' titles, and the language of the pages it renders | every console page's title and language |
| its own stylesheet and design assets | the runtime branding document and `/site-config` |

A module cannot change who gets in: the account pages' business logic, the
registration gate, the email-verification requirement and the permission rules
are all in the shared application and are not part of this interface.

The shared application also keeps the session store, proxy rules, and
`AuthField` / `AuthNotice` / `AuthLoading` accessibility behavior. A module
cannot register a route or take over an unlisted page. A new path such as
`/pricing`, a console redesign or a new authentication method needs an
application change beyond this interface.

The host facade exposes public site configuration, resolved branding, session
state and the supported presentation types/helpers. Session state lets a page
choose a sign-in or console link; the facade does not expose login/logout,
tokens or authentication operations. The normative TypeScript interfaces live
in `apps/frontend/src/site-ui/contract.ts`, and the supported imports in
`apps/frontend/src/site-ui/host.ts`.

## What a module may import

A module may import `@site-ui/host`, `react`, `react/*`, `react-dom`, `next`,
`next/*` and its own files. Relative imports must stay inside the module.
Anything else — in particular `@/...` — fails the build.

The build holds a module to that list where webpack resolves each of its
requests, in every compilation Next.js runs, and judges a request by the real
path it resolves to rather than by how it is spelled. These fail as well:

- a relative path or a symlink that leads out of the module;
- a framework subpath that walks back out, such as `next/../../src/site-ui/routes`;
- a stylesheet `@import` or `url()` that leaves the module;
- a computed import — `require.context`, `import.meta.webpackContext` or an
  `import()` of a template literal — over a directory outside the module;
- a `data:` or `file:` URI, or a Node.js built-in;
- an inline loader other than Next.js's own, such as `!!raw-loader!./notes.txt`;
- anything in a `node_modules` directory inside the module. A module's
  dependencies come from the application's lockfile, and staging does not copy
  that directory.

The build also admits the helpers Next.js's compiler adds to a module's code,
`@swc/helpers` and `styled-jsx`, resolved from Next.js itself. A module does not
import them by name.

A source check reports what it can see earlier: when a module is staged, and
before every build, type check and test run. It parses scripts with the
TypeScript parser, reads stylesheets, and lists every problem at once. Files
under `test/`, `tests/`, `__tests__/` or `__mocks__/`, and `vitest`, `jest` or
`playwright` configuration files, are exempt unless production code imports
them; a test helper that `client.tsx` imports is checked like any other file.
Every symlink the staging step would copy must resolve inside the module.

## A module is a directory

```
my-ui/
  manifest.json     identity: id, site_ui_api, locale
  client.tsx        the components (required)
  server.ts         required entry; optional locale and metaMessages
  styles.css        the module's stylesheet (required, may be empty)
  public/           assets, under public/site-assets/<id>/
  ...               the module's own components, however it likes
```

The host loads `styles.css`: the root layout imports it through the generated
bridge on every page, after the application's own stylesheet, so a module rule
wins over an application rule of the same specificity. A module does not import
it.

`server.ts` must exist, and may contain only `export {};`. Its two exports,
`locale` and `metaMessages`, are optional; see
[Document language and page titles](#document-language-and-page-titles).

`client.tsx` exports:

| Export | Required | What it means |
|---|---|---|
| `descriptor` | yes | A named literal `{ siteUiApi, id, locale }`; all three values must match `site_ui_api`, `id` and `locale` in the manifest. |
| `Landing` | no | Rendered at `/`. Supplying one replaces the console's home page. |
| `AuthFrame` | no | The frame around `/login`, `/signup`, `/forgot-password`, `/reset-password`, `/verify-email`. |
| `fieldLayout` | no | Where each account field's label, control, hint, error and action go. |
| `TermsFrame` | all three or none | The page around the module's legal text at `/terms`. |
| `TermsContent` | all three or none | The module's legal text, at `/terms` and in the sign-up consent step. |
| `consentItems` | all three or none | What the sign-up consent step asks a visitor to confirm: a non-empty list of `{ id, label, description? }`. |
| `authAppearance` | no | Deprecated class-map compatibility for the shared forms, supported throughout API v1. |
| `authMessages` | no | The deployment's wording for the account pages. |

The host reads named exports only. A client entry with a default export fails
the build, and so does a second file for the same entry — `client.tsx` beside
`client.js`, say — because the bundler, the type checker and the test runner
complete the name in different orders. The server entry may keep a default
export; the host does not read it.

`npm run type-check` and `next build` check the exports against the contract in
`contract.ts`: `client.tsx` against `SiteUiClientModule`, `server.ts` against
`SiteUiServerModule`. An export of the wrong type fails, naming the export, and
so do a partial legal set and an empty `consentItems`. As the module loads, the
host checks again for what a cast or an `any` hides from the compiler: a
component export that is not a component, a partial legal set, and
`consentItems` that is empty, repeats an id, or has an item without an id or a
label. Any of them stops the module from loading.

**`next build` does not load the module.** Every page renders on demand, so the
build evaluates no client module. A mistake only the load-time check can see
builds cleanly and fails on the first request; from then on every page answers
HTTP 500. Smoke-test each image before releasing it: start it with its gateway
and request `/`, the five account pages and `/terms`.

**Leaving out an optional export is a choice, not a gap.** `Landing: null`
means "use the console's home page here"; omitting `AuthFrame` means "put the
account pages inside the console's container". A module that exports neither
replaces nothing, which is fine while you are still writing it.

**A frame draws the whole page.** `AuthFrame` and `TermsFrame` render their own
header, `<main>` and footer. A frame that draws only a card leaves the five
account pages without a header or footer, and the host cannot add them back,
so get this right first.

`AuthFrame` must render the supplied shared form `children` and preserve any
provided `topbar` and `legal` nodes. Its headings and page identifier let one
frame serve the five account routes. It supplies the complete outer page, so
the host does not add another header or footer around it.

**The legal set is one decision.** A module that publishes its own terms exports
`TermsFrame`, `TermsContent` and `consentItems` together; one that exports none
of them keeps the console's terms and its four confirmations. The host renders
`TermsContent` in both places a visitor meets the terms: at `/terms`, as the
`children` of `TermsFrame`, and in the sign-up consent step, where the module's
`consentItems` replace the console's confirmations. The text a visitor accepts
is the text the site publishes.

`TermsContent` renders the legal body only — its sections and any preamble such
as a date; the page heading is the frame's. It receives `headingLevel` 2 and
`compact` false at `/terms`, and `headingLevel` 3 and `compact` true in the
consent step's scrolling box. `TermsFrame` draws the page around it and must
render its `children`. At `/terms`, give each section its `terms-s` anchor: the
account pages link privacy to `/terms#terms-s5`. Setting manifest file paths
does not substitute a legal document here.

Declare `consentItems` with the `ConsentItems` type, and give each item a
unique `id`. A plain `ConsentItem[]` fails the type check, because it does not
promise at least one item. Every item unlocks once the visitor has read the
terms to the end, and Continue waits for all of them. The backend records a
single `accepted_tos` flag, which the sign-up request sends only after every
item has been checked.

`authMessages` changes wording, not validation or the required field set. The
host keeps only the keys `AUTH_MESSAGE_KEYS` declares, with string values, and
drops everything else before a page reads it, so wording for a console page —
`auth.authorize.*` for `/authorize`, `chat.*` for `/chat` — has no effect.
Missing keys retain the shared defaults. Preserve each message's interpolation
variables. A module may localize its own pages and supported keys, but this
does not translate every console page.

## Document language and page titles

The server entry's optional `locale` sets `<html lang>` on the routes the module
renders: `/` when it exports `Landing`, the five account pages when it exports
`AuthFrame`, and `/terms` when it exports the legal set. Every other route — the
console, and a public route whose export the module leaves out — is the
console's, in `en`. An empty or omitted `locale` keeps `en` everywhere. The
attribute follows client-side navigation as well as the first response.

The optional `metaMessages` words the titles and descriptions of the seven
public routes. Its keys are `meta.<page>.title` and `meta.<page>.description`,
where `<page>` is `home`, `login`, `signup`, `forgot`, `reset`, `verify` or
`terms`; `META_MESSAGE_KEYS` in `contract.ts` lists them. The host filters it
the way it filters `authMessages`, dropping undeclared keys and values that are
not strings. `meta.home.title` is the whole document title; every other title
is shown as `<title> | <site name>`, as on the console's own pages. Each value
may use `{app_name}`.

A page the module does not word keeps its default: the site's title and
description, or "Terms of Service" for `/terms`. Titles do not depend on which
routes the module renders. Console pages keep their English titles, and icons
come from runtime branding.

## Building an image with a module

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

The reverse fails too. `SITE_UI_API`, or a `SITE_UI_SUBDIR` other than `.`,
given without `--build-context site-ui=...` stops the build with a message that
names the missing context, rather than building the neutral UI in the module's
place.

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

Staging empties `--into` before it copies the module in, so `--into` must be a
directory of its own: it may not overlap `--context`, and may not be or contain
`--app`.

`SITE_UI_DIR` and `SITE_UI_API` name the module directly if you would rather not
stage a copy:

```bash
SITE_UI_DIR=/path/to/my-ui SITE_UI_API=1 \
  SITE_ASSETS_DIR=/path/to/my-ui/public/site-assets npm run dev
```

A relative `SITE_UI_DIR` resolves against `apps/frontend`, whichever directory
the command runs from. Tailwind scans the selected module's directory as well
as `src/`, so a utility class that only a module outside `src/` uses is still
generated.

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

`npm run type-check` and `next build` both type-check against
`tsconfig.generated.json`. It reaches a module's production code through the
entries' imports and leaves the module's directory out of its file globs, and
the application does not lint a module at all. A module's tests and tooling are
for the distribution's own repository to check. A declaration file that nothing
imports, such as one holding an ambient `declare module`, is outside that import
graph too: reference it from an entry with `/// <reference path="./types.d.ts" />`.

## Assets

A module's images live in `public/site-assets/<module-id>/`. The image build
packages them in `site-assets/<module-id>/` beside the standalone server rather
than in `public/`: Next.js serves `public/` before any route, and a file there
would bypass the `/site-assets` route. Three rules, all enforced:

- **Under the module's own id.** An asset elsewhere fails the build. This is what
  makes "two modules cannot write the same path, and neither can shadow the
  application's" true.
- **Never a collision.** A module asset at a path the application already ships
  fails the build rather than overwriting it.
- **The route is what serves them.** `/site-assets/*` serves image files only —
  AVIF, GIF, ICO, JPEG, PNG, SVG and WebP, up to 20 MB — with a five-minute
  revalidating cache policy and `nosniff`, and sandboxes an SVG with a
  `Content-Security-Policy` header. A bundle with anything under
  `public/site-assets/<module-id>/` fails the build. Test the URL, not the file.

At runtime, `SITE_ASSETS_DIR` is read first and the image's own copy second. A
deployment that mounts its branding directory keeps overriding the module's
files, and one that mounts nothing still serves a complete site.

The override works file by file, and `SITE_ASSETS_DIR` must be an absolute path.
The build copies a relative symlink as the link it is, with its target
unchanged, so a link that stays inside the module keeps working in the image.
The route does not serve a link whose target is outside its directory.

## Styling the shared forms

The shared account forms render their own markup and default styling. Stable
semantic hooks provide styles and state; the `fieldLayout` export provides
structural variation. The deprecated class map remains supported throughout
API v1.

**`data-auth` attributes for styling and state.** Shared form elements expose:

| Attribute | On |
|---|---|
| `data-auth="form"` | the body of an account page: its `<form>`, or the box a result or the sign-up consent step sits in |
| `data-auth="field"` | each field's wrapper |
| `data-auth="label"` / `"control"` | the label, and each input or textarea |
| `data-auth="hint"` / `"error"` | the hint and the error paragraph |
| `data-auth="notice"` | an info, error or success notice; a button inside one is `[data-auth="notice"] button` |
| `data-auth="submit"` | the page's primary button, or the link that stands in for it |
| `data-auth="field-action"` | the wrapper around a field's own action, such as the forgot-password link |
| `data-auth="secondary-actions"` | the row of alternative links on `/verify-email` |
| `data-auth="consent"` | each sign-up confirmation: the label holding its checkbox and sentence |
| `data-auth="consent-section"` | each section of the sign-up consent step |
| `data-auth="loading"` | the resolving-session region |

State is carried by the attributes the application already sets — `aria-invalid`,
`disabled`, `aria-busy`, `data-auth-error`, `data-auth-tone` — so a stylesheet
never has to infer "this field is in error" from a class name. The set is part
of the contract: an attribute is added, never repurposed.

The host describes each control itself. Before a `fieldLayout` sees the control,
`AuthField` gives it `aria-describedby` naming the field's hint (`<id>-hint`) and
error (`<id>-error`), kept alongside any value the page set, and
`aria-invalid="true"` while the error shows.

**Field layout.** The optional `fieldLayout` export receives the shared control,
hint, error and action nodes, and the label's text and `htmlFor` id. It must
render every supplied node, associate the label with that id, and preserve the
`data-auth="field"` and `data-auth="label"` hooks. The control arrives already
described, so the relationship survives any arrangement that renders every node.
The neutral layout places the field action after the control and validation; a
module may place it beside its label. `fieldLayout` is a supported export in its
own right, not part of the deprecated class map.

**Deprecated `authAppearance` class map.** The class-map export stays supported
for as long as API v1 does; it holds class names only. New styling should use
scoped `data-auth` and state selectors instead. The class map will only be
removed in a later API revision, with a migration guide, so existing v1 modules
keep working.

All customization is subject to the same rule: **the module's CSS may only affect
the pages it owns.** A stylesheet that reaches `body`, `:root` or a bare element
selector will restyle the console too, and no check will catch it for you —
scope everything under a root class the module sets on its own shell.

## When a module component fails

A module component that throws — `Landing`, `AuthFrame`, `fieldLayout`,
`TermsFrame` or `TermsContent` — takes down its own page and nothing else. Each
renders inside its page, and the application's route error page
(`app/error.tsx`) replaces that page with a neutral message and a Try again
button. On a route whose chrome the module draws, the message comes with the
console's header and footer. It never falls back to the console's terms or
confirmations when the module's fail to render. A console page's own render
error is shown the same way, inside the console chrome.

React renders no error boundary on the server, so the first response for a
failing page is still an error: HTTP 500, or HTTP 200 with a loading state on
the account pages that render inside `<Suspense>` (`/login`, `/reset-password`
and `/verify-email`). The browser then renders the page again and shows the
message. A status code alone does not show that a page works.

A module that fails its load-time check is different: the root layout loads the
module, so every page fails with it.

## Compatibility

- **Adding a module does not change the console.** The five account pages sit in
  the console container when no `AuthFrame` is exported, and the shared pages,
  schemas and session handling are untouched either way.
- **Removing an optional export is safe.** A module that stops exporting
  `Landing` gets the console's home page back.
- **The legal set is removed whole.** Dropping all three exports brings back the
  console's terms and confirmations; dropping one of them fails the type check.
- **Renaming or removing an export, changing a prop's meaning, or repurposing a
  `data-auth` value is a breaking change** and belongs behind an API revision
  bump. So does renaming a key in `authMessages` or `metaMessages`, or changing
  its interpolation variables.
- **Adding an optional key or attribute is compatible.**

`SITE_UI_API` selects the Site UI interface revision. The resolver refuses a
module that declares a revision this checkout does not implement, rather than building
something neither side was written for.

The module API has no shared public-layout export; a module organizes its own
internal layouts freely.

## Testing a module

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

Then smoke-test the built image: start it with a gateway, since every page reads
`/site-config`, and request `/`, the five account pages and `/terms`.
`next build` does not load the module, so this is the first point at which its
load-time check runs.

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
`id: "example"`. These image builds need Docker and network access for
dependencies. Serving the full application also needs the gateway's runtime
`/site-config` endpoint.

This repository's CI runs the same two builds, and checks how the example image
serves its assets, on every change to the console or to the example module.
That covers the example only: build and test your distribution's own image and
assets as well.
