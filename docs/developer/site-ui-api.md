# Site UI API v1 — the seam that lets a distribution own its public pages

## The problem this replaces

`apps/frontend` used to contain the SSV landing page, the SSV auth shell and
their stylesheets, selected at runtime by:

```ts
if (branding.presentation.preset === 'inference') { … }
```

A deployment's *name* was the branch condition. The shared repository had to
know which designs existed, and a second distribution would have needed a second
branch — in the same file the first one lived in. The neutral public pages sat
behind a shared `LayoutChrome` that also read the preset.

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
| `/` | the landing page, in either implementation |
| the account routes | the module's `AuthFrame`, which *is* the page |
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

```bash
cd apps/frontend
npm run lint && npm run type-check && npx vitest run        # neutral
SITE_UI_DIR=tests/fixtures/site-ui-demo SITE_UI_API=1 npm run build
SITE_UI_BUILD_TEST=1 npx vitest run src/site-ui/distribution-build.test.ts
```

`resolve.test.ts` covers resolution and every fail-loud path;
`SiteUiBoundary.test.tsx` covers route matching and the shared primitives;
`verify-imports.test.ts` covers the import checker. The third command compiles
the fixture in for real and asserts the produced bundle.

The last command is the end-to-end one, and it is opt-in because it runs a
distribution's composer — `frontend/build/build_frontend.py` — which installs,
type-checks, tests and builds the whole application twice (once with the fixture
compiled in, once neutral), so it takes a couple of minutes. `SITE_UI_DIR` and
`SITE_UI_API` must be *unset* in the shell that starts Vitest; the test sets them
for the child build.

It asserts, against the bundle the composer produced rather than against this
repository's own idea of it:

- `provenance.json` names the commit that was built (`core_commit` equals this
  checkout's `HEAD`; `SITE_UI_BUILD_COMMIT` pins a different revision) and
  carries a 40-hex `ui_commit` and `inputs_tree`;
- `standalone/site-ui-manifest.json` says `kind: 'distribution'` with the
  fixture's id, so the extension really is compiled in;
- the started bundle serves the fixture's landing page at `/` **without** the
  console's `<header>` — the assertion that exactly one layer owns the chrome;
- an unknown path is a 404 that is not the landing page, and `/dashboard` is the
  console's page with its header and no trace of the fixture;
- a second, neutral build from a cleared generated state resolves the neutral
  UI, so a distribution build cannot leak into the next default build.

It is skipped when the distribution checkout is not next to this one, since the
composer lives there. When it runs, the checkout it builds in is left untouched:
the build happens in a detached worktree of this repository at exactly the commit
under test, which is also what lets it run from a worktree with uncommitted
changes. It needs no database, no provider credential and no network — only Node,
npm and Python — and it stops its own server in `afterAll` even when an assertion
fails.
