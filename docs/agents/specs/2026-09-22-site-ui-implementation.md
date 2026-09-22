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
   requires an explicit supported `SITE_UI_API`.
2. `resolveSiteUi()` determines concrete client, server and stylesheet paths.
3. `generateSiteUi()` writes the bridge entries, module manifest and TypeScript
   project. Repeating the same selection produces byte-identical output.
4. `prepareSiteUi()` checks the required files and exports, compares the module
   manifest with its literal client descriptor, and runs import validation.

Paths in the following table are relative to `apps/frontend/`. All listed
outputs are generated and gitignored; changes belong in the resolver or selected
module, not in these files.

| Output | Purpose |
|---|---|
| `src/site-ui/active/client.ts` | Static re-exports of the selected client module for bundler/test aliases |
| `src/site-ui/active/server.ts` | Server facade exposing only the optional `locale`, defaulting to an empty string |
| `src/site-ui/active/styles.css` | Generated stylesheet bridge; its existence alone does not establish that a stylesheet is loaded |
| `src/generated/distribution-ui/manifest.json` | Records `site_ui_api`, `kind`, `id` and module directory basename, without build-host filesystem paths |
| `tsconfig.generated.json` | Extends the base project while selecting the same module and retaining the application's existing path mappings and include set |

[`next.config.js`](../../../apps/frontend/next.config.js) calls `prepareSiteUi()`
and installs the generated client/server aliases. The TypeScript mapping points
the client alias directly at the selected module and the server alias at its
generated facade, so an omitted `locale` has the same default in both tools.
[`vitest.config.ts`](../../../apps/frontend/vitest.config.ts) calls the same
resolver and uses `vitestAlias()`, which shares the Webpack alias mapping.

[`prepare.js`](../../../apps/frontend/src/site-ui/prepare.js) is the standalone
entry point. The npm dev, build, type-check and test scripts invoke it; in
particular, `npm run type-check` prepares the selection before running `tsc -p
tsconfig.generated.json`. A bare type check against the base project is not a
substitute for validating the selected module. Switching back to neutral must
rewrite the generated selection rather than reuse a previous distribution's
entries.

The resolver currently emits a CSS bridge, but the application does not import
that generated bridge. A module with authored CSS must bring it into its loaded
client import graph. Verify rendered styling when changing this wiring; a
required stylesheet file and a successful generated-file test do not prove that
the browser received those rules.

## Validation boundaries

The resolver checks client/server/stylesheet presence, the required descriptor
export, supported revision, and agreement between the external module's
`manifest.json` and literal descriptor identity, revision and locale. It reads
source declarations without executing TSX in the configuration process. Optional
page exports are optional: omission is not an invalid module.

[`verify-imports.js`](../../../apps/frontend/src/site-ui/verify-imports.js) checks
the supported host/framework imports and module-local paths. Relative imports
are checked for directory and symlink escapes. The scan distinguishes executable
imports from comments and example strings; module test files and test-runner
configuration are excluded from the shipped-module import rules.

These checks enforce specific interface boundaries. They are not a sandbox or
a complete validation of every optional export's runtime behavior, CSS scope,
accessibility or authentication presentation. Compilation, focused tests and
browser verification cover different failure modes.

## Rendering ownership

[`module.tsx`](../../../apps/frontend/src/site-ui/module.tsx) normalizes the
selected exports. `SiteUiCore.tsx` installs the appearance and wording providers;
it deliberately does not choose routes, keeping the shared form primitives from
depending on the boundary that renders them.

`SiteUiBoundary.tsx` renders a supplied landing page or the shared route content.
`PublicRouteBoundary.tsx` decides whether to draw the shared header, main
container and footer by checking whether the module supplies the component for
the current public route. This decision does not branch on a distribution ID.
Missing components retain the shared presentation.

Account controllers keep their forms and pass presentation nodes to
`AuthPageFrame`. A supplied legal `TermsFrame` instead renders its complete legal
page and receives `null` children. `terms-sections.tsx` supplies the default legal
body; it is not automatically inserted into a distribution's `TermsFrame`.

The closed mapping in `routes.ts` prevents a module from registering additional
paths or claiming console routes. The chrome rule describes React pages passing
through these boundaries; it does not turn route handlers such as the standalone
agent proxy at `/agents` into shared-layout pages.

## Implementation map

Unless noted otherwise, these paths are under `apps/frontend/src/site-ui/`.

| Path | Responsibility |
|---|---|
| `contract.ts` | Public types, route identifiers, semantic hooks and message-key contract |
| `host.ts` | Supported facade for distribution imports; exposes configuration and session state, not authentication operations |
| `routes.ts` | Exact public-route lookup and account-route classification |
| `resolve.js`, `prepare.js` | Module selection, generated entries and validation entry point |
| `verify-imports.js` | Import-surface and local-path validation |
| `module.tsx` | Normalizes the compiled module's optional exports |
| `SiteUiCore.tsx`, `appearance.tsx` | Shared form appearance and wording context |
| `SiteUiBoundary.tsx` | Module provider, landing replacement and account/legal presentation boundaries |
| `PublicRouteBoundary.tsx` | Shared page chrome ownership |
| `neutral/` | Default descriptor, optional-export choices and account-card presentation |
| `terms-sections.tsx` | Default legal content |
| `apps/frontend/scripts/site-ui/prepare-module.mjs` | Docker-context staging and standalone bundle assembly |
| `apps/frontend/tests/fixtures/site-ui-demo/` | Small independent module used to exercise the interface; not an operator template |
| `distributions/example/frontend/site-ui/` | Public homepage-only example used by the container checks |

The independent fixture should expose accidental coupling to a particular
design. Adding a distribution-specific branch in shared code to make the fixture
work defeats its purpose.

## Image assembly

[`Dockerfile.frontend`](../../../deploy/docker/Dockerfile.frontend) uses a
Buildx named context. Its default context contains the built-in marker; an
external context is staged inside the frontend before compilation. The staging
step writes `.site-ui-build-env`, which the Dockerfile sources for the build.
An npm command does not automatically read that staging environment file.

After `next build`, `prepare-module.mjs` performs three steps:

1. `merge` assembles public assets, using the module ID from the generated build
   manifest. Module assets must remain under their own `site-assets/<id>/`
   namespace and cannot overwrite host assets.
2. `manifest` copies the module record and compiled static chunks into the
   standalone tree. The runtime record is `/app/site-ui-manifest.json`.
3. `check` verifies the standalone server, static output, public directory and
   manifest before the runtime stage copies that assembled tree.

The asset route at `/site-assets/*` shadows ordinary Next.js static serving for
that prefix. A file present in `public/` is therefore insufficient evidence of a
working URL. The route checks the configured runtime asset directory before
the image's bundled assets; both packaging and HTTP behavior need coverage.

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
| `src/site-ui/resolve.test.ts` | Selection, required declarations, manifest agreement, optional exports, consistent aliases, generated provenance, neutral reset and idempotence |
| `src/site-ui/verify-imports.test.ts` | Supported imports, forbidden application internals, local-path boundaries and lexical scanning cases |
| `src/site-ui/SiteUiBoundary.test.tsx`, `src/site-ui/chrome.test.tsx` | Shared primitives and which layer owns the public-page chrome |
| `scripts/site-ui/prepare-module.test.ts` | Built-in marker, staging, selection environment, context containment, asset namespaces/collisions and standalone-bundle validation |

The **Site UI Containers** job in
[`ci.yml`](../../../.github/workflows/ci.yml) builds both the unchanged neutral
invocation and the public example through the same Dockerfile. It asserts
`kind: "neutral"` in the first image's manifest and `id: "example"` in the
second, then starts the example image and requests its bundled mark over HTTP.
This detects a named context being ignored or assets being packaged without a
serving path.

The container job's asset check does not establish that all pages, form states
or login flows work. Serving and verifying full pages requires a working gateway
`/site-config` endpoint and the relevant backend services. Distribution staging
verification remains necessary after changes to shared forms, chrome, styles,
runtime configuration or module compilation.
