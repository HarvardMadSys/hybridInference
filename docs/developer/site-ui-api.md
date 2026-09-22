# Site UI API v1 — the seam that lets a distribution own its public pages

## The problem this replaces

Each distribution should own its public-page design, copy and assets. Keeping
those pages in `apps/frontend` would make the shared repository responsible for
every distribution's design.

Site UI modules let distributions supply those pages at build time through a
common interface. The shared application selects components by their declared
capabilities, without branching on deployment names or design presets.

## The seam

One Next.js application, one console, one session. A distribution ships a small
UI module; the build compiles it in.

```text
hybridinference @ C
  ├── neutral backend image
  └── generic Next.js frontend
        + distribution UI module @ U   →  the distribution's own frontend image
```

| The host keeps | The module owns |
|---|---|
| the gateway, the console, `/dashboard`, `/chat`, `/team`, `/authorize`, `/agents` | `/` and the six account/legal routes |
| the auth controllers: schemas, submit calls, error codes, `next` handling | the frame, the field classes and the wording those pages display |
| the session store, the signup policy, the field set, the proxy rules | its own stylesheet, assets and model-family presentation |
| `AuthField` / `AuthNotice` / `AuthLoading` and their ARIA contract | where the heading and a field action sit |

A module may import `@site-ui/host`, `react`, `next/*` and its own files.
Anything else — in particular `@/...` — fails the build.

`site_ui_api` is a revision number. A module written for one revision is not
compiled against another; the build stops instead.

## Resolution: one answer, three toolchains

Webpack, `tsc` and Vitest must agree on which module was compiled in, or a
distribution build's test run asserts against the neutral UI while the build
ships the distribution's. `src/site-ui/resolve.js` is the single decision, and
it writes three artefacts the three tools follow:

| Artefact | For | Ignored by git |
|---|---|---|
| `src/site-ui/active/{client,server}.ts` | Webpack, through the `@site-ui/*` alias | yes |
| `src/generated/distribution-ui/manifest.json` | build provenance and tests | yes |
| `tsconfig.generated.json` | `npm run type-check` | yes |

An **absent** extension compiles the neutral UI. A **present but unusable** one
fails the build — a missing entry, a missing export, a wrong API revision, a
missing `SITE_UI_API`, or an import outside the promised surface. Silently
publishing another design's home page is worse than a failed build.

### Two findings that cost real debugging time

**Next 15.5 and `typescript.tsconfigPath`.** Setting it to any file other than
`tsconfig.json` makes Next read `paths` from that file and then *skip installing
its own tsconfig-paths plugin*, so every `@/*` import in the application stops
resolving. Only the Webpack alias is set in `next.config.js`; `tsconfig.json`
carries the `@site-ui/*` mappings the bundler never uses.

**The import verifier was doing nothing, silently.** It has to tell a real
import from a code sample — the quickstart pages ship `import OpenAI from
"openai";` as text — and it got that wrong three ways, including a doc comment
containing a backtick that opened a template span and hid every import after it.
Fixed, with the four silent-failure modes pinned in
`src/site-ui/verify-imports.test.ts`.

## Chrome ownership

Getting this wrong produces two headers or none. Exactly one layer draws
`<header>` / `<main>` / `<footer>`:

| Route | Who draws it |
|---|---|
| `/` | the module's `Landing` if provided, otherwise the console page and chrome |
| the account routes | the module's `AuthFrame` if provided, otherwise the console chrome and neutral card |
| `/terms` | the module's `TermsFrame` if it exports one, else the console chrome |
| everything else | the console chrome, always |

`PublicRouteBoundary` decides by asking what the compiled-in module *provides*,
never what the deployment is *called*. `Landing === null` is a real answer: it
means "the console's own page is the right page", which is what the neutral
module declares.

## Where the pieces are

| Path | What |
|---|---|
| `contract.ts` | the normative types |
| `routes.ts` | the closed route table; a module cannot register a route |
| `host.ts` | the only module path a distribution may import |
| `resolve.js` | the one resolution decision |
| `verify-imports.js` | keeps a module on the promised import surface |
| `neutral/` | the default UI, itself written against the contract |
| `SiteUiBoundary.tsx` | installs the module and renders the route it owns |
| `PublicRouteBoundary.tsx` | replaces `LayoutChrome` |
| `terms-sections.tsx` | the legal text, shared by both frames |
| `tests/fixtures/site-ui-demo/` | a second, deliberately tiny module |

`tests/fixtures/site-ui-demo/` is not a template. It exists so the interface can
be tested against something the host has never seen — if it ever needs a host
change to keep working, the interface has grown a dependency on one design, and
that is the bug.

## Testing the seam

The shared repository owns both validation and the image recipe. No distribution
checkout or distribution-owned composer is required for these checks.

```bash
cd apps/frontend
npm run lint
npm run type-check
npm test
```

`resolve.test.ts` covers module selection and invalid declarations;
`SiteUiBoundary.test.tsx` and `chrome.test.tsx` cover shared primitives and chrome
ownership; `verify-imports.test.ts` checks the import boundary. The tests in
`scripts/site-ui/prepare-module.test.ts` exercise staging, asset merging and
bundle validation.

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

For local module staging, development-server selection and returning to the
neutral UI, follow [Local development](site-ui.md#local-development). Those
commands use the same resolver as the image build and type checker.
