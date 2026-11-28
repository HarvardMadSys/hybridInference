# Site UI Modules

This page is for frontend developers building a distribution's own public
pages. If configuration and branding are enough, see
[Distribution customization](distribution-customization.md) instead.

A Site UI module replaces the public pages a visitor sees (the landing page, the
frame around the account pages, the legal page) without forking the console. It
is a small tree of React components that the frontend image compiles in next to
the shared application, which still provides the one console and session.

```text
hybridinference @ C
  ├── neutral backend image
  └── generic Next.js frontend
        + distribution UI module @ U   →  the distribution's own frontend image
```

`C` is the upstream source commit and `U` the distribution's UI source commit;
pin both when you build a release. The module is never fetched or swapped at
runtime, so a change to its components, stylesheet or wording needs a new
frontend image. Runtime branding that the module reads still changes at
runtime.

## What a module can and cannot change

| The module owns | The shared application keeps |
|---|---|
| the home page, if it exports one | dashboard, chat, admin, team and authorize pages, and the `/agents` proxy integration |
| the frame around the account pages, and where each field's parts go | the account forms, their fields, validation and submit calls |
| the legal text and the sign-up confirmations, if it ships its own | the consent step, the one `accepted_tos` flag sign-up records, and the terms anchors the account pages link to |
| its own wording for the account pages | the schemas, error codes, session handling and redirect rules |
| the public pages' titles, and the language of the pages it renders | every console page's title and language |
| its own stylesheet and design assets | the runtime branding document and `/site-config` |

A module cannot change who gets in. Sign-in logic, the registration gate, email
verification and permissions stay in the shared application, together with the
session store, proxy rules and the accessibility behavior of `AuthField`,
`AuthNotice` and `AuthLoading`. A module cannot register a route or take over an
unlisted page; a new path such as `/pricing`, a console redesign or a new
authentication method is an application change.

The host facade gives a module the public site configuration, resolved
branding, session state (enough to choose between a sign-in and a console link)
and the supported presentation types and helpers. It offers no login, logout,
token or other authentication operation. The TypeScript interfaces are in
`apps/frontend/src/site-ui/contract.ts`, and the supported imports in
`apps/frontend/src/site-ui/host.ts`.

## What a module may import

A module may import `@site-ui/host`, `react`, `react/*`, `react-dom`, `next`,
`next/*` and its own files. Anything else, `@/...` in particular, fails the
build. The build checks each request by the real path webpack resolves it to,
in every compilation, so these fail too:

- a relative path or a symlink that leads out of the module;
- a framework subpath that walks back out, such as `next/../../src/site-ui/routes`;
- a stylesheet `@import` or `url()` that leaves the module;
- a computed import (`require.context`, `import.meta.webpackContext` or an
  `import()` of a template literal) over a directory outside the module;
- a `data:` or `file:` URI, or a Node.js built-in;
- an inline loader other than Next.js's own, such as `!!raw-loader!./notes.txt`;
- anything in a `node_modules` directory inside the module. Dependencies come
  from the application's lockfile, and staging does not copy that directory.

An earlier source check lists every problem it can see at once. It runs when a
module is staged and before every build, type check and test run. Files under
`test/`, `tests/`, `__tests__/` or `__mocks__/`, and `vitest`, `jest` or
`playwright` configuration, are exempt unless production code imports them.
Every symlink in the module must resolve inside it.

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

The root layout loads `styles.css` on every page, after the application's own
stylesheet, so a module rule wins over an application rule of the same
specificity. Do not import it yourself. `server.ts` must exist, but may contain
only `export {};`; its optional exports are described under
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

The host reads named exports only. A default export from `client.tsx` fails the
build, and so does a second file for the same entry, such as `client.js` next to
`client.tsx`. `npm run type-check` and `next build` check `client.tsx` against
`SiteUiClientModule` and `server.ts` against `SiteUiServerModule`, so a wrong
export type, a partial legal set or an empty `consentItems` fails with the
export's name. As the module loads, the host checks again for what a cast or
`any` hides: a component export that is not a component, a partial legal set,
and `consentItems` that is empty, repeats an id, or lacks an id or a label. Any
of these stops the module from loading.

**`next build` does not load the module.** Every page renders on demand, so a
mistake only the load-time check can see builds cleanly, then fails the first
request and answers HTTP 500 on every page from then on. Smoke-test each image
before releasing it: start it with its gateway and request `/`, the five
account pages and `/terms`.

Optional exports can be left out. `Landing: null` keeps the console's home page,
and without `AuthFrame` the account pages sit in the console's container.

**A frame draws the whole page.** `AuthFrame` and `TermsFrame` render their own
header, `<main>` and footer; the host adds none around them. A frame that draws
only a card leaves the account pages without a header or footer. `AuthFrame`
must render the shared form it receives as `children` and keep any `topbar` and
`legal` nodes it is given; its headings and page identifier let one frame serve
all five account routes.

**Export the legal set together or not at all.** A module that publishes its own
terms exports `TermsFrame`, `TermsContent` and `consentItems`; one that exports
none of them keeps the console's terms and its four confirmations. The host
renders `TermsContent` wherever a visitor meets the terms: at `/terms`, as the
`children` of `TermsFrame` (`headingLevel` 2, `compact` false), and in the
sign-up consent step's scrolling box (`headingLevel` 3, `compact` true), where
the module's `consentItems` replace the console's confirmations. The text a
visitor accepts is the text the site publishes.

`TermsContent` renders only the legal body: its sections and any preamble, such
as a date. The page heading belongs to the frame. At `/terms`, give each
section its `terms-s` anchor; the account pages link privacy to
`/terms#terms-s5`. Declare `consentItems` with the `ConsentItems` type (a plain
`ConsentItem[]` fails the type check, because it does not promise one item) and
give each item a unique `id`. Every item unlocks once the visitor has read the
terms to the end, and Continue waits for all of them. Sign-up records a single
`accepted_tos` flag, sent only after every item is checked.

`authMessages` changes wording, not validation or the required fields. The host
keeps only the keys `AUTH_MESSAGE_KEYS` declares, with string values, so wording
for a console page (`auth.authorize.*`, `chat.*`) has no effect. Missing keys
keep the shared defaults; keep each message's interpolation variables. The
login method selector uses `auth.login.method_label`,
`auth.login.password_method` and `auth.login.sms_method`; sign-up's email and
phone choice uses `auth.phone.method_label`, `auth.phone.email_method` and
`auth.phone.phone_method`.

A module's `authMessages` are written in the module's language (its descriptor
`locale`, or English when that is empty) and apply only to a page in that
language. A reader who chose another console language gets the console's own
translation of those pages; see
[Translating the console](translations.md#translating-the-console).

## Document language and page titles

The server entry's optional `locale` sets `<html lang>` on the routes the module
renders: `/` when it exports `Landing`, the five account pages when it exports
`AuthFrame`, and `/terms` when it exports the legal set. Every other route uses
the reader's console language, English unless the reader or their browser asks
for another language the console ships. An empty or omitted `locale` gives the
module's routes the reader's language too. The attribute follows client-side
navigation.

The optional `metaMessages` words the titles and descriptions of the seven
public routes, with the keys `meta.<page>.title` and `meta.<page>.description`,
where `<page>` is `home`, `login`, `signup`, `forgot`, `reset`, `verify` or
`terms` (`META_MESSAGE_KEYS` in `contract.ts`). It is filtered like
`authMessages`. `meta.home.title` is the whole document title; any other title
is shown as `<title> | <site name>`. Each value may use `{app_name}`. A page
the module does not word keeps its default, the site's title and description or
"Terms of Service" for `/terms`, whichever routes the module renders. Console
pages keep their English titles, and icons come from runtime branding.

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

A distribution may pass its whole `frontend/` tree as the context and select the
module with `SITE_UI_SUBDIR=site-ui`, keeping the module and its build inputs
under one pinned tree. The build never falls back to the neutral UI: an
external context without a module fails, and so does `SITE_UI_API`, or a
`SITE_UI_SUBDIR` other than `.`, without `--build-context site-ui=...`. The
resolver also refuses a module that declares a `SITE_UI_API` revision this
checkout does not implement.

### Local development

The same staging step runs on a workstation, without Docker. Start a gateway
first, because every page reads its `/site-config`, and set
`BACKEND_INTERNAL_URL` to its address if it is not `http://backend:8080`:

```bash
cd apps/frontend
node scripts/site-ui/prepare-module.mjs prepare \
  --app . --context ../../distributions/example/frontend/site-ui \
  --subdir . --into src/site-ui/external --api 1
SITE_UI_DIR="$PWD/src/site-ui/external" SITE_UI_API=1 \
  SITE_ASSETS_DIR="$PWD/src/site-ui/external/public/site-assets" npm run dev
# For build or type-check, pass the same module selection variables.
```

Staging empties `--into` before copying, so `--into` must be a directory of its
own: it may not overlap `--context`, and may not be or contain `--app`. Staging
does not select the module for later commands; pass the variables to each one.
To skip staging, name the module directly:

```bash
SITE_UI_DIR=/path/to/my-ui SITE_UI_API=1 \
  SITE_ASSETS_DIR=/path/to/my-ui/public/site-assets npm run dev
```

A relative `SITE_UI_DIR` resolves against `apps/frontend`. Tailwind scans the
selected module's directory as well as `src/`. In local development
`SITE_ASSETS_DIR` serves the module's assets; the image build packages them.
To return to the neutral UI, stop the server and run:

```bash
env -u SITE_UI_DIR -u SITE_UI_API -u SITE_ASSETS_DIR npm run dev
```

The resolver writes `src/site-ui/active/` and `tsconfig.generated.json`, so the
bundler, the type checker and the test runner read the same selection. Do not
edit those generated files. Type checking reaches a module's production code
through its entries' imports, and the application lints no module code, so a
module's tests and tooling are for the distribution's repository to check. A
declaration file that nothing imports, such as one holding an ambient
`declare module`, is outside that graph: reference it from an entry with
`/// <reference path="./types.d.ts" />`.

## Assets

A module's images live in `public/site-assets/<module-id>/`. The image build
packages them in `site-assets/<module-id>/` next to the standalone server, not
in `public/`, because Next.js serves `public/` before any route and a file
there would bypass the `/site-assets` route. The build fails on an asset
outside the module's own id, on an asset at a path the application already
ships, and on a bundle that still has files under `public/site-assets/<module-id>/`.

`/site-assets/*` serves image files only (AVIF, GIF, ICO, JPEG, PNG, SVG and
WebP, up to 20 MB) with a five-minute revalidating cache policy and `nosniff`,
and sandboxes an SVG with a `Content-Security-Policy` header. Test the URL, not
the file. At runtime the route reads `SITE_ASSETS_DIR` first and the image's
own copy second, file by file, so a deployment that mounts its branding
directory overrides the module's files and one that mounts nothing still serves
a complete site.

`SITE_ASSETS_DIR` must be an absolute path. The console runs as uid 1000, so
the mounted directory and files must be readable by that user (`0755`
directories and `0644` files are enough); a file it cannot read is answered as
missing. The build copies a relative symlink as a link, so a link that stays
inside the module keeps working; the route does not serve a link whose target
is outside its directory.

Every page is served with a `Content-Security-Policy`
(`apps/frontend/src/lib/security/contentSecurityPolicy.ts`). A module's markup
runs no inline `<script>` and no script from another host, its stylesheets and
fonts load from the console's origin, and its images load from `/site-assets`
or over HTTPS. Anything more needs a change to the policy.

## Styling the shared forms

The shared account forms render their own markup and default styling. Style them
through `data-auth` attributes, and rearrange them with the `fieldLayout`
export.

| Attribute | On |
|---|---|
| `data-auth="form"` | the body of an account page: its `<form>`, or the box a result or the sign-up consent step sits in |
| `data-auth="field"` | each field's wrapper |
| `data-auth="label"` / `"control"` | the label, and each input or textarea |
| `data-auth="hint"` / `"error"` | the hint and the error paragraph |
| `data-auth="notice"` | an info, error or success notice; a button inside one is `[data-auth="notice"] button` |
| `data-auth="submit"` | the page's primary button, or the link that stands in for it |
| `data-auth="field-action"` | the wrapper around a field's own action, such as the forgot-password link |
| `data-auth="secondary-actions"` | alternative actions, including the method selector and links on `/verify-email` |
| `data-auth="consent"` | each sign-up confirmation: the label holding its checkbox and sentence |
| `data-auth="consent-section"` | each section of the sign-up consent step |
| `data-auth="loading"` | the resolving-session region |

State is in attributes the application already sets: `aria-invalid`,
`disabled`, `aria-busy`, `data-auth-error` and `data-auth-tone`. The login and
sign-up method group also carries `data-auth-method-selector`, and its buttons
use `aria-pressed` for the selected method. The attribute set is part of the
contract: attributes are added, never repurposed.

`fieldLayout` receives the shared control, hint, error and action nodes, and
the label's text and `htmlFor` id. It must render every node, associate the
label with that id, and keep the `data-auth="field"` and `data-auth="label"`
hooks. The control arrives with `aria-describedby` already naming the hint
(`<id>-hint`) and error (`<id>-error`), and with `aria-invalid="true"` while
the error shows, so any arrangement that renders every node stays accessible.
The neutral layout puts the field action after the control; a module may put it
beside the label.

The deprecated `authAppearance` class map holds class names only and stays
supported for as long as API v1 is. It will be removed only in a later API
revision, with a migration guide. Use `data-auth` and state selectors for new
styling.

**A module's CSS may affect only the pages it owns.** A rule that reaches
`body`, `:root` or a bare element selector restyles the console too, and no
check catches it. Scope every rule under a root class that the module sets on
its own shell.

## When a module component fails

A module component that throws (`Landing`, `AuthFrame`, `fieldLayout`,
`TermsFrame` or `TermsContent`) takes down its own page and nothing else: the
route error page (`app/error.tsx`) replaces that page with a neutral message and
a **Try again** button, inside the console's header and footer. It never falls
back to the console's terms or confirmations.

React renders no error boundary on the server, so the first response for a
failing page is still an error: HTTP 500, or HTTP 200 with a loading state on
`/login`, `/reset-password` and `/verify-email`. The browser then shows the
message. A status code alone does not show that a page works. A module that
fails its load-time check takes down every page, because the root layout loads
it.

## Compatibility

- Adding a module does not change the console's pages, schemas or session
  handling.
- Removing an optional export is safe: a module that stops exporting `Landing`
  gets the console's home page back. The legal set is removed whole; dropping
  only one of its exports fails the type check.
- Renaming or removing an export, changing a prop's meaning, repurposing a
  `data-auth` value, or renaming a key in `authMessages` or `metaMessages` (or
  changing its interpolation variables) is a breaking change and needs a new
  API revision. Adding an optional key or attribute is compatible.

## Testing a module

These checks use the built-in neutral module and the public example; pass the
same module selection variables as for local development to check your own:

```bash
cd apps/frontend
npm run lint
npm run type-check
npm test
```

Exercise the normal, invalid, loading and keyboard states of the shared forms,
every public page your module supplies, and the console, to catch style
leakage. Then smoke-test the built image with a gateway behind it: request `/`,
the five account pages and `/terms`. This is the first point at which the
load-time check runs.

From the repository root, build the neutral image and the public example with
the same recipe (this needs Docker and network access):

```bash
docker buildx build -f deploy/docker/Dockerfile.frontend \
  --load -t local/frontend:neutral .
docker buildx build -f deploy/docker/Dockerfile.frontend \
  --build-context site-ui=./distributions/example/frontend/site-ui \
  --build-arg SITE_UI_API=1 --load -t local/frontend:example .
docker run --rm --entrypoint cat local/frontend:neutral /app/site-ui-manifest.json
docker run --rm --entrypoint cat local/frontend:example /app/site-ui-manifest.json
```

The first manifest must report `kind: "neutral"` and the second
`id: "example"`. This repository's CI runs both builds, and checks how the
example image serves its assets, on every change to the console or the example
module. Build and test your own image and assets as well.

## Maintaining the host

This section is for contributors who change the Site UI support in the shared
application. Paths are under `apps/frontend/src/site-ui/` unless noted.

- `resolve.js` selects the module and writes the generated files; `prepare.js`
  runs it before the npm dev, build, type-check and test scripts. Webpack,
  TypeScript, Vitest and Tailwind all read that one selection, so tests cover
  the module the image compiles. A bare `tsc` against the base project does not
  see the selected module; use `npm run type-check`.
- `containment.js` enforces the import rule in every webpack compilation, and
  `verify-imports.js` is the earlier source check. The source check may miss
  what the webpack guard catches, but must never reject a module the guard
  accepts.
- `module.tsx` checks the client module as it loads, normalizes its optional
  exports and filters `authMessages`; `meta.ts` filters `metaMessages`.
- `moduleRendersRoute()` in `SiteUiBoundary.tsx` decides which routes the
  module renders, from its exports and never from a distribution id. The page
  chrome (`PublicRouteBoundary.tsx`), the document language (`SiteDocument.tsx`)
  and the error page all read it. `routes.ts` holds the closed list of public
  routes.
- `apps/frontend/scripts/site-ui/prepare-module.mjs` stages a module from a
  build context and, after `next build`, assembles the standalone bundle and
  its assets.
- `apps/frontend/tests/fixtures/site-ui-demo/` is a small independent module
  that exercises the interface. Do not add a distribution-specific branch to
  shared code to make it pass; the fixture is there to expose such coupling.

These checks hold a module to the interface; they are not a sandbox. A module is
compiled into the application and could reach around the host facade, and the
checks do not prove its runtime behavior, CSS scope or accessibility. When you
change the wiring, check rendered pages in a browser: a test of the generated
files does not show that the browser received the styles.
