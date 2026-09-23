# Site UI implementation notes

- Date: 2026-09-22
- Status: current implementation record for maintainers of Site UI API v1
- Operator and module-author documentation:
  [Distribution customization](../../developer/distribution-customization.md)

This record describes the shared application's implementation and verification
points. The customization guide owns the supported exports, public-route
boundaries, module-author rules, build commands and upgrade instructions. The
normative TypeScript definitions remain in
[`contract.ts`](../../../apps/frontend/src/site-ui/contract.ts). This record does
not create additional public interfaces.

## Resolution across toolchains

A distribution module is compiled into the existing Next.js application. It is
not a runtime plugin or a second frontend. Webpack, TypeScript and Vitest must
select the same module: otherwise tests or type checking could cover the neutral
UI while the image contains a distribution's implementation.

[`resolve.js`](../../../apps/frontend/src/site-ui/resolve.js) makes that selection
and generates the entries used by the toolchains. It is CommonJS so plain Node
can load it while Next.js and the test runner read their configuration.

1. `readSiteUiRequest()` interprets the module selection. An absent, empty or
   whitespace-only `SITE_UI_DIR` selects `neutral/`; an external directory
   requires an explicit supported `SITE_UI_API`. A relative `SITE_UI_DIR`
   resolves against `apps/frontend`, not the working directory. An entry name
   that more than one file completes, such as `client.tsx` beside `client.js`,
   is refused: each toolchain tries extensions in a different order.
2. `resolveSiteUi()` determines concrete client, server and stylesheet paths.
   The client path is chosen once and handed to every reader of it.
3. `generateSiteUi()` writes the bridge entries, module manifest and TypeScript
   project. Repeating the same selection produces byte-identical output, and a
   file whose content is unchanged is not rewritten, so a concurrent reader
   never observes it truncated.
4. `prepareSiteUi()` checks the required files and exports, refuses a default
   export from the client entry, compares the module manifest with its literal
   client descriptor, and runs the early import check.

Paths in the following table are relative to `apps/frontend/`. All listed
outputs are generated and gitignored; changes belong in the resolver or selected
module, not in these files.

| Output | Purpose |
|---|---|
| `src/site-ui/active/client.ts` | `export *` of the selected client module for bundler/test aliases; named exports only |
| `src/site-ui/active/server.ts` | Server bridge: checks the server entry against `SiteUiServerModule` and exposes `locale` (defaulting to an empty string) and `metaMessages` |
| `src/site-ui/active/styles.css` | Stylesheet bridge: `@import`s the selected module's `styles.css` by its full file name |
| `src/generated/distribution-ui/manifest.json` | Records `site_ui_api`, `kind`, `id` and module directory basename, without build-host filesystem paths |
| `tsconfig.generated.json` | Extends the base project, maps `@site-ui/*` to the selected module, retains the application's path mappings and include set, and keeps the module's directory out of its globs |

[`next.config.js`](../../../apps/frontend/next.config.js) calls `prepareSiteUi()`,
installs the generated client/server aliases and the import containment guard
in every webpack compilation, and sets `typescript.tsconfigPath` to the
generated project, so `next build` type-checks the module it compiles. The
TypeScript mapping points the client alias directly at the selected module and
the server alias at its generated bridge, so an omitted `locale` has the same
default in both tools.
[`vitest.config.ts`](../../../apps/frontend/vitest.config.ts) calls the same
resolver and uses `vitestAlias()`, which shares the Webpack alias mapping.
[`tailwind.config.ts`](../../../apps/frontend/tailwind.config.ts) calls
`resolveSiteUi()` with the same environment and scans the selected module's
directory beside `src/`.

[`prepare.js`](../../../apps/frontend/src/site-ui/prepare.js) is the standalone
entry point. The npm dev, build, type-check and test scripts invoke it; in
particular, `npm run type-check` prepares the selection before running `tsc -p
tsconfig.generated.json`. A bare type check against the base project is not a
substitute for validating the selected module. Switching back to neutral must
rewrite the generated selection rather than reuse a previous distribution's
entries.

The root layout imports the generated stylesheet bridge after `globals.css`, so
the selected module's `styles.css` reaches every page, console pages included,
and a module rule wins over an application rule of equal specificity. Modules
do not import their own stylesheet. The bridge names the file in full because no
CSS resolver completes an extensionless name. Verify rendered styling when
changing this wiring; a generated-file test does not prove that the browser
received the rules.

## Validation boundaries

The resolver checks client/server/stylesheet presence, one file per entry, the
required descriptor export, the absence of a client default export, the
supported revision, and agreement between the external module's `manifest.json`
and literal descriptor identity, revision and locale. It reads source
declarations without executing TSX in the configuration process. Optional page
exports are optional: omission is not an invalid module.

[`containment.js`](../../../apps/frontend/src/site-ui/containment.js) enforces the
import rule. `next.config.js` installs it into every compilation Next runs —
client, server and edge — with the module directory and facade the resolution
already chose. It judges each request made from the module by the real path it
resolves to, not by its spelling:

- an enhanced-resolve plugin on the normal, context and loader resolvers, tapped
  ahead of webpack's resolver cache, covers imports, requires, css-loader's
  `@import` and `url()`, computed imports and inline loaders;
- an audit of the finished module graph covers what webpack records without
  resolving: externals (Node built-ins in the server compilation) and `data:`
  or `file:` URIs.

A request must land in the module itself, outside any `node_modules` below it;
in the `@site-ui/host` facade; in the application's `react`, `react-dom` or
`next`; or in `@swc/helpers` or `styled-jsx` resolved from Next, which Next's
compiler injects into module code. A computed import must range over a module
directory, and an inline loader must be one of Next's. The neutral UI is part
of the application and is not contained.

[`verify-imports.js`](../../../apps/frontend/src/site-ui/verify-imports.js) is
the early half of the same rule. It runs from `prepareSiteUi()` and from
staging, parses scripts with the TypeScript parser and stylesheets for
`@import`, `url()`, `composes` and `@value`, and lists every problem at once. It
follows what the entries reach through the module's own files, so test
directories and test-runner configuration are exempt only while production
code does not reach them. It checks every symlink the staging step would copy,
and refuses a framework specifier that walks back out with `..`. It may miss
what the guard catches, but must never reject what the guard accepts.

Type checking holds the exports to the contract. `module.tsx` assigns the module
namespace to a `SiteUiClientModule` annotation, and the generated server bridge
assigns the server entry to `SiteUiServerModule` with an index signature, so
`npm run type-check` and `next build` fail on an incompatible export. The
generated project reaches module production code through the entries' imports;
ESLint and Prettier ignore the staging directory. A module's tests and tooling
are the distribution's to check.

`module.tsx` guards what the compiler cannot see, as the module loads: a
component export that is neither null nor a function, memo, forwardRef or lazy
component; a partial legal set; and `consentItems` that is not a non-empty
array, repeats an id, or lacks an id or label. It keeps a frozen copy of the
items with only the declared fields, and filters `authMessages` to
`AUTH_MESSAGE_KEYS` with string values; `meta.ts` does the same for
`metaMessages` and `META_MESSAGE_KEYS`. The root layout is `force-dynamic`, so
`next build` renders no page and evaluates no client module. The load-time
guard first runs on the first request, and a failure there fails every page,
because the root layout imports the module.

These checks enforce specific interface boundaries. They are not a sandbox or
a complete validation of every optional export's runtime behavior, CSS scope,
accessibility or authentication presentation. Compilation, focused tests and
browser verification cover different failure modes.

## Rendering ownership

[`module.tsx`](../../../apps/frontend/src/site-ui/module.tsx) normalizes the
selected exports into `SITE_UI_CLIENT`, with the legal set as one `legalText`
field: all three exports, or `null`. `SiteUiCore.tsx` installs the appearance
and field-layout contexts. Wording needs no provider, because `useT` reads the
filtered dictionary from `SITE_UI_CLIENT`. `SiteUiCore.tsx` deliberately does
not choose routes, keeping the shared form primitives from depending on the
boundary that renders them.

`moduleRendersRoute()` in `SiteUiBoundary.tsx` is the single ownership decision.
The module renders `/` when it exports `Landing`, the account routes when it
exports `AuthFrame`, and `/terms` when it publishes the legal set; the decision
reads the exports, never a distribution ID. Three consumers share it:

- `PublicRouteBoundary.tsx` draws the console header, main container and footer
  only around routes the module does not render;
- `SiteDocument.tsx` renders `<html>` with the module's server `locale` on those
  routes and `en` everywhere else. It is a client component because the server
  sees the module's client exports only as references. It still renders on the
  server with the request's pathname, and again on each client navigation;
- `app/error.tsx` draws the console chrome around its message on a route the
  module renders.

`SiteUiBoundary` renders no module component. Each page renders its own:
`HomeContent` renders `Landing`; the account pages render `AuthPageFrame` (the
module's `AuthFrame`, or the neutral card) and `AuthField` (the module's
`fieldLayout`); `/terms` renders `TermsPageContent`; and `SignupConsentStep`
renders the legal text. A module component that throws is therefore caught by
[`app/error.tsx`](../../../apps/frontend/src/app/error.tsx), the error page for
every route below the root layout, which shows a neutral message and never the
console's terms or confirmations in place of the module's. A component rendered
by the root layout would sit above every route's boundary. React renders no
error boundary on the server, so the response for a failing page is still 500,
or 200 with a Suspense fallback, and the boundary takes over in the browser.

Account controllers keep their forms and pass presentation nodes to
`AuthPageFrame`. `AuthField` sets `aria-describedby` and `aria-invalid` on the
control before the field layout receives it. For the legal set, the host renders
`TermsContent` at heading level 2 and passes it to `TermsFrame` as `children`;
the consent step renders the same component at level 3, compact, with the
module's `consentItems` in place of the console's four confirmations. Every
confirmation folds into the single `accepted_tos` flag. `terms-sections.tsx`
supplies the console's legal body; it is never handed to a module's frame or
shown beside a module's confirmations.

[`meta.ts`](../../../apps/frontend/src/site-ui/meta.ts) supplies
`publicPageMetadata()`, the `generateMetadata` of `/`, `/terms` and the five
account routes. The account routes carry pass-through layouts for it, and `/`
is a server page around `HomeContent`. It reads `metaMessages` through the
server bridge, whichever routes the module renders.

The closed mapping in `routes.ts` prevents a module from registering additional
paths or claiming console routes. The chrome rule describes React pages passing
through these boundaries; it does not turn route handlers such as the standalone
agent proxy at `/agents` into shared-layout pages.

## Implementation map

Unless noted otherwise, these paths are under `apps/frontend/src/site-ui/`.

| Path | Responsibility |
|---|---|
| `contract.ts` | Public types, route identifiers, the terms anchor prefix, and the declared message and metadata keys |
| `host.ts` | Supported facade for distribution imports; exposes configuration and session state, not authentication operations |
| `routes.ts` | Exact public-route lookup and account-route classification |
| `resolve.js`, `prepare.js` | Module selection, generated entries and validation entry point |
| `containment.js` | Import containment at webpack resolution, in every compilation |
| `verify-imports.js` | Early source check of the import rule and module-local paths |
| `module.tsx` | Checks the compiled module against the client contract, at type-check time and as it loads; normalizes its optional exports and filters its wording |
| `meta.ts` | Public-route titles and descriptions from the server entry's `metaMessages` |
| `SiteUiCore.tsx`, `appearance.tsx` | Shared form appearance and field-layout contexts |
| `SiteUiBoundary.tsx` | Route ownership (`moduleRendersRoute`), the styling provider, and the account and legal page frames |
| `PublicRouteBoundary.tsx` | Shared page chrome ownership |
| `SiteDocument.tsx` | `<html lang>` by the route's author |
| `neutral/` | Default descriptor, optional-export choices and account-card presentation |
| `terms-sections.tsx` | The console's legal content |
| `apps/frontend/src/app/error.tsx` | Error page for a failing route, module-owned or console |
| `apps/frontend/src/app/signup/SignupConsentStep.tsx` | Consent step: the console's or the module's legal text and confirmations |
| `apps/frontend/src/components/auth/AuthForm.tsx` | Shared account primitives, the `data-auth` hooks (`AUTH_DATA`) and each control's ARIA description |
| `apps/frontend/src/app/site-assets/[[...path]]/route.ts` | Serves runtime and packaged module assets |
| `apps/frontend/scripts/site-ui/prepare-module.mjs` | Docker-context staging and standalone bundle assembly |
| `apps/frontend/tests/fixtures/site-ui-demo/` | Small independent module used to exercise the interface; not an operator template |
| `distributions/example/frontend/site-ui/` | Public homepage-only example used by the container checks |

The independent fixture should expose accidental coupling to a particular
design. Adding a distribution-specific branch in shared code to make the fixture
work defeats its purpose.

## Image assembly

[`Dockerfile.frontend`](../../../deploy/docker/Dockerfile.frontend) uses a
Buildx named context. Its default context contains the built-in marker; an
external context is staged inside the frontend before compilation. With only
the marker in the context, `prepare-module.mjs prepare` refuses module settings
(`--api`, or a `--subdir` other than `.`), so an image build given
`SITE_UI_API` or `SITE_UI_SUBDIR` without `--build-context site-ui=...` fails
instead of producing the neutral UI. Staging empties `--into` first, so it
refuses an `--into` that overlaps `--context` or is or contains `--app`,
compared by real path. Every copy keeps a symlink as a link with its target
verbatim, and staging checks the staged copy with the rules `next build`
applies. The script compares real paths to decide whether it was invoked
directly, so it also runs through a symlinked path.

The staging step writes `.site-ui-build-env`, which the Dockerfile sources for
the build. An npm command does not automatically read that staging environment
file.

After `next build`, `prepare-module.mjs` performs three steps:

1. `merge` copies the application's `public/` into the standalone bundle and
   packages the module's assets in `site-assets/<id>/` beside the server,
   outside `public/`, using the module ID from the generated build manifest.
   Module assets must remain under their own `site-assets/<id>/` namespace and
   cannot collide with the application's public files.
2. `manifest` copies the module record and compiled static chunks into the
   standalone tree. The runtime record is `/app/site-ui-manifest.json`.
3. `check` verifies the standalone server, static output, public directory and
   manifest, and refuses a bundle with anything under `public/site-assets/<id>/`,
   before the runtime stage copies that assembled tree.

Next serves `public/` before any route, so a module asset under `public/` would
bypass the asset route: a runtime file could not override it, and it would be
sent with Next's static headers. The route at `/site-assets/*` reads
`SITE_ASSETS_DIR` (an absolute path) first and the packaged `site-assets/`
second, file by file. It serves image types only, follows a symlink only while
its real path stays inside that root, and sends a five-minute revalidating cache
policy, `nosniff`, and a CSP sandbox for SVG. Both packaging and HTTP behavior
need coverage.

## Verification

Run the frontend gates from `apps/frontend/`:

```bash
npm run lint
npm run type-check
npm test
```

Use the same module selection environment for compilation, type checking and
tests. The consolidated customization guide documents local preparation and
image-build commands; the implementation checks are mapped here for maintainers.

| Test | Coverage |
|---|---|
| `src/site-ui/resolve.test.ts` | Selection, relative `SITE_UI_DIR`, required declarations, manifest agreement, one file per entry, no client default export, consistent aliases, the server and stylesheet bridges, type-check and lint scope, generated provenance, neutral reset and idempotence |
| `src/site-ui/verify-imports.test.ts` | The source check: parsed requests, stylesheets, local-path and symlink boundaries, test exemptions and framework subpaths |
| `src/site-ui/containment.test.ts` | The webpack guard: requests judged by their resolved real path, symlinks, cached resolutions, externals, and a module that uses everything allowed still compiling |
| `src/site-ui/module.test.tsx` | The client type check, named exports only, load-time refusal of non-component exports and of partial or invalid legal sets, `fieldLayout`, and `authMessages` filtering |
| `src/site-ui/meta.test.ts` | Public-route titles and descriptions, key filtering, and the server bridge's exports and type check |
| `src/site-ui/legal-text.test.tsx` | The module's legal text at `/terms` and in the consent step, and sign-up gated on its confirmations |
| `src/site-ui/SiteDocument.test.tsx` | `<html lang>` for each route and module shape, and across client-side navigation |
| `src/site-ui/containment.test.tsx` | A throwing module component caught by the route error page; the root layout renders none |
| `src/site-ui/SiteUiBoundary.test.tsx`, `src/site-ui/chrome.test.tsx` | Shared primitives and which layer owns the public-page chrome |
| `src/components/auth/AuthForm.test.tsx`, `src/components/auth/account-pages.test.tsx` | The `data-auth` hooks in both directions, and each account control's description and `aria-invalid` under the default and a module field layout |
| `src/app/site-assets/[[...path]]/route.test.ts` | Runtime and packaged asset roots, same-path override, headers and the type allowlist |
| `tailwind.config.site-ui.test.ts` | Tailwind emitting a utility used only by a module outside `src/` |
| `scripts/site-ui/prepare-module.test.ts` | Built-in marker, module settings without a module, what staging may empty, staging and symlinks, selection environment, context containment, asset packaging and standalone-bundle validation |

The **Site UI Containers** job in
[`ci.yml`](../../../.github/workflows/ci.yml) builds both the unchanged neutral
invocation and the public example through the same Dockerfile, and requires a
build given `SITE_UI_API=1` and `SITE_UI_SUBDIR=site-ui` but no `site-ui`
context to fail with the staging refusal. It asserts `kind: "neutral"` in the
first image's manifest and `id: "example"` in the second. It then creates the
example container with a `SITE_ASSETS_DIR` holding a replacement for the
example's mark and requires the route to serve that runtime file. After the
file is removed, the packaged mark must come back through the route, with the
SVG `Content-Security-Policy` sandbox and the cache policy. The readiness probe
polls that asset rather than `/`, which is a 500 with no gateway behind the
container. Image tags and the container name belong to one run, the port is
chosen free for each run, and an `always()` step removes the container and
images.

The change classifier
([`classify_changes.py`](../../../ops/ci/classify_changes.py)) sets `site_ui`
for every frontend input — `apps/frontend/`, `deploy/docker/Dockerfile.frontend`
and its `.dockerignore`, the built-in marker under `deploy/docker/site-ui/`, and
the example's `frontend/` tree, which also selects Frontend Quality — and for
the root `.dockerignore` and any change under `distributions/example/`. On a
pull request the job runs when `site_ui` or `full` is set, and the gate
([`verify_ci_gate.py`](../../../ops/ci/verify_ci_gate.py)) then requires it to
succeed; other events always run it. The gate's classification check also
rejects a result that sets `frontend` without `site_ui`.

The container job's asset check does not establish that all pages, form states
or login flows work. Serving and verifying full pages requires a working gateway
`/site-config` endpoint and the relevant backend services. Nor does a
successful build show that a module loads: `next build` renders no page, so the
load-time guard first runs when the built image serves a request. Distribution
staging verification remains necessary after changes to shared forms, chrome,
styles, runtime configuration or module compilation.
