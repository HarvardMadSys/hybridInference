# Contributing

HybridInference is MIT-licensed (see `LICENSE`) and contributions are welcome.
This page covers what the repository contains, the checks a change has to pass,
and how a change gets proposed.

## Getting set up

```bash
git clone <your-fork-url> hybridinference
cd hybridinference
make setup-dev
```

`make setup-dev` creates `.venv` on Python 3.12, installs the project editable,
syncs the `dev` dependency group and installs the pre-commit hooks. Full
prerequisites and the ways to run the gateway are in
[Installation](installation.md).

Every `make` target that touches Python runs through `uv run`. Override that if
your environment needs it:

```bash
make lint UV_RUN="uv run --active"
```

## Repository layout

```text
apps/
  backend/
    serving/      FastAPI gateway: HTTP surface, SSE streaming, provider
                  adapters, auth, storage, observability, admin API
    routing/      routing engine: routers, strategies, endpoint health,
                  circuit breaker
  frontend/       Next.js web and admin console
config/
  examples/       reference model registry and routing config; also the
                  built-in fallback a checkout with no overlay resolves to
distributions/    deployment overlays, one directory each: manifest, config,
                  branding, deploy env files. `example/` is the runnable
                  teaching overlay used by the Router Tutorial
deploy/
  docker/         Dockerfiles and docker-compose.yml
  systemd/        unit files for host-level deployments
docs/
  developer/      this guide (MyST Markdown, built with Sphinx)
  agents/         design specs and plans
ops/              maintenance and CI helper scripts (admin, ci, db)
services/         standalone protocol-conformance harnesses and testkits
tests/            see the test tiers below
benchmark/        benchmarking scripts
```

Two things about the backend are easy to get wrong:

- `apps/backend/routing/executor.py` is a backward-compatibility shim that
  re-exports `FixedRouter` as `RouteExecutor`. Edit
  `apps/backend/routing/routers.py` instead.
- The backend packages are `serving` and `routing`, rooted at `apps/backend`.
  Anything you run by hand needs `PYTHONPATH=apps/backend`; pytest gets it from
  `pythonpath` in `pyproject.toml`.

## Quality gates

| Command | What it runs |
|---|---|
| `make format` | `ruff format .`, then `ruff check --fix --unsafe-fixes .` |
| `make lint` | `ruff format --check .`, `ruff check --no-fix .`, `pydocstyle` |
| `make test` | `pytest -m "not external and not dbtest" -n auto --dist loadfile` |
| `make check` | `lint` + `test` |
| `make all` | `format` + `check` |

`make all` does **not** type-check. `mypy` is in the `dev` dependency group and
you may run it by hand, but `pyproject.toml` has no `[tool.mypy]` section and no
target invokes it, so nothing enforces it.

Style is enforced by tooling, not by review:

- **ruff**, line length 100, target `py310`, with `E`/`W`/`F`/`I`/`UP`/`B`/`C4`/
  `SIM`/`TCH`/`RUF` enabled (`[tool.ruff]` in `pyproject.toml`).
- **pydocstyle**, Google convention. It skips `tests`, `.venv`,
  `node_modules`, `apps/frontend`, `ops` and `docs`, so docstrings are required
  in `apps/backend` and enforced there.

Pre-commit hooks run on commit and are installed by `make setup-dev`:

```bash
pre-commit run --all-files     # run them over the whole tree
```

They cover ruff (pinned to the version CI uses), pydocstyle, `gitleaks` secret
scanning, the standard whitespace/YAML/JSON/TOML checks, and eslint + prettier
for `apps/frontend`.

Before opening a pull request, run `make format` and make sure `make test`
passes.

## Tests

Markers are the contract; directories are a convention. The markers declared in
`pyproject.toml` are `unit`, `integration`, `slow`, `perf`, `external` and
`dbtest`, and `--strict-markers` means an undeclared marker is an error.

| Tier | Lives in | Marker | In `make test`? |
|---|---|---|---|
| Unit | `tests/unit/` | — | yes |
| API surface | `tests/api/` | — | yes |
| Server / observability | `tests/servers/`, `tests/observability/` | — | yes |
| Needs a live Postgres | mostly `tests/integration/` | `dbtest` | no |
| Hits live external servers | `tests/external/`, `tests/e2e/` | `external` | no |

`testpaths` is `["tests", "distributions"]`, so overlay-owned tests run in the
default suite alongside `tests/`.

`tests/e2e/` is not a separate marker tier: its files carry
`pytestmark = pytest.mark.external` like everything in `tests/external/`, so
`make test-e2e` (`pytest -m external`) selects both. What distinguishes it is
its own `tests/e2e/Makefile`, which stands up the stack the phase tests expect
before running them.

```bash
make test                              # the default suite
make test-verbose                      # same selection, serial, -vv
make test-cov                          # same selection, with coverage
make test-db                           # only -m dbtest
make test-all                          # everything except -m external
make test-e2e                          # only -m external

uv run pytest tests/unit/routing/test_manager.py           # one file
uv run pytest -m dbtest tests/integration/                 # one tier
```

The `dbtest` tier needs a reachable PostgreSQL. Tests read `TEST_DB_HOST`,
`TEST_DB_PORT`, `TEST_DB_USER`, `TEST_DB_PASSWORD` (defaulting to `localhost:5432`
and `postgres`/`postgres`), and some read a full `TEST_PG_DSN`. Starting just
the database container is enough:

```bash
docker compose -f deploy/docker/docker-compose.yml --env-file .env up -d postgres
```

`make test` parallelises with `pytest-xdist` at file granularity. Run serially
when you are debugging a specific failure, but check a suspected env-leak
failure under `-n auto` too — cross-file environment leakage only shows up
under the parallel run.

### Frontend

The console's gates are separate from the Python ones:

```bash
make frontend-install       # npm ci
make frontend-lint          # eslint, --max-warnings 0
make frontend-type-check    # tsc --noEmit
make frontend-test          # vitest run
make frontend-check         # all three

make check-all              # backend lint + test, then frontend-check
```

CI additionally runs `npm run format:check` (prettier) and
`npm audit --omit=dev --audit-level=high` in `apps/frontend`.

## Documentation

These pages are MyST Markdown compiled by Sphinx from `docs/developer/`, with
`docs/developer/index.rst` as the root toctree. Build them from the repository
root:

```bash
make docs
```

CI's **Docs Build** job builds the same tree with warnings promoted to errors:

```bash
uv run sphinx-build -b html docs/developer docs/build/html -W --keep-going
```

That job is what gates documentation changes in this repository — a broken
cross-reference or a page missing from the toctree fails the build, so run it
locally before pushing. Sphinx, `myst-parser` and `sphinx-rtd-theme` come from
the `dev` dependency group, so `make setup-dev` is enough to build.

Add a new page to `docs/developer/index.rst` as well as writing it; an orphan
file is a warning, and warnings are errors in CI.

### Translations

The pages are written in English and translated with Sphinx's gettext
workflow, so a translation is attached to *each paragraph of source text*
rather than to a whole file. That is what makes a partial translation safe:
any string without one falls back to English, and the site still builds
complete.

```bash
make docs-gettext                      # extract one catalog template per page
make docs-translate DOCS_LANG=zh_CN    # create or update that language's catalogs
# edit docs/developer/locale/zh_CN/LC_MESSAGES/*.po -- fill in msgstr
make docs-lang DOCS_LANG=zh_CN         # build it and read the result
```

A catalog entry pairs the English source with its translation:

```po
#: ../index.rst:4
msgid "HybridInference is an open-source LLM inference gateway."
msgstr "HybridInference 是一个开源的 LLM 推理网关。"
```

Because the English text *is* the lookup key, editing a paragraph invalidates
its translation automatically: the next `make docs-translate` marks that entry
`#, fuzzy`, the build stops using it, and the page falls back to English rather
than serving a translation that no longer matches what the code does. A
translator only has to revisit the entries that are marked. **This is the
reason to use catalogs instead of parallel `.zh.md` files**, which drift
silently and give a reader no signal that what they are reading is out of date.

Commit the `.po` files. `docs/gettext/` is generated and ignored.

Translating a page does not require translating all of it, and there is no
obligation to keep a language complete — an untranslated paragraph is a
fallback, not a bug.

#### Translating into Chinese, Japanese or Korean

Four traps, all of them silent — the build stays green and the page is wrong.
Each was hit while translating this doc set into Chinese.

**A heading that starts with a number is discarded.** Sphinx re-parses a
translated title, and MyST reads `1. ` as an enumerated-list marker rather than
text. The structure no longer matches the source, the translation is dropped,
the English heading is emitted, and nothing warns — not even under `-W`. Escape
the period:

```po
msgstr "1\. 申请一个节点"
```

**In `index.rst`, inline markup that touches a CJK character does not parse.**
reStructuredText requires whitespace or specific punctuation before an opening
`*`, and a Chinese character is neither, so `请求的*模型 id*` renders literal
asterisks. Separate them with an escaped space — written `\\ ` in the catalog,
which is a backslash-space in the string:

```po
msgstr "客户端请求的\\ *模型 id*\\ ，与真正服务它的\\ *端点*\\ 是解耦的。"
```

This applies to `index.rst` only. Markdown pages need no escaping: CommonMark
treats a CJK character as neither whitespace nor punctuation, so `**模型 id**`
between Chinese characters is a valid emphasis run.

**One Markdown case does break, though**: a closing `**` preceded by a CJK full
stop and followed by a CJK character is not right-flanking, so
`**术语。**后文` leaves literal asterisks. Put the period outside the bold —
`**术语**。后文` — which is better typography anyway, since bolding punctuation
is wrong.

**A stale `.mo` masks your edits.** Sphinx skips recompilation when the `.mo` is
newer than its `.po`, so you can verify a build that never read your changes.
Before any verification build:

```bash
find docs/developer/locale -name '*.mo' -delete
```

The check that catches all four at once is a structural diff against the English
build: for each page, compare the counts of `<code>`, `<strong>`, `<em>` and
`<a>`, and the multiset of inline-code literals and link targets. A dropped
marker or a translated link target shows up there and in no other check.

One more, which at least fails loudly: `make docs-translate` can append
`python-format` to an entry you had annotated `no-python-format` — a source
string containing something like `≥ 5%` looks like a format string to gettext —
and `msgfmt -c` then rejects the contradictory pair. Delete the added
`python-format` and keep `no-python-format`.

#### Publishing more than one language

A single-language build is the default and nothing above changes it. To
publish several, set `DOCS_LANGUAGES` to `code:endonym` pairs and build the
whole set:

```bash
make docs-site DOCS_LANGUAGES="en:English,zh_CN:简体中文"
```

That writes `docs/build/site/<code>/` — one directory per language, siblings
under a common root — and renders a language switcher in the sidebar that
links to the same page in each other language. The switcher assumes exactly
that layout. It renders nothing when `DOCS_LANGUAGES` names fewer than two
languages, so a single-language site never shows a dead control.

Serving that layout is a publication-side decision: it changes the site's URLs
(`/en/…` rather than `/…`), so whoever owns the deploy has to move to it
deliberately.

## Proposing a change

- **Branch off `dev`, not `main`.** Pull requests target `dev`; CI runs on pull
  requests against `main` and `dev`.
- **Name branches `<user>/<scope>/<feature-name>`**, e.g.
  `jane/routing/weighted-fallback`.
- **Write commit and pull-request titles as conventional commits** —
  `type(scope): summary`, e.g. `fix(routing): keep the fallback route on 429`.
  History on `dev` is squashed one commit per pull request, so the title you
  write becomes the commit subject — `git log --oneline` shows the shape to
  match.
- Keep a pull request to one feature or fix, update the docs in the same change,
  and add tests for new behaviour.
- Do not commit to `main` or `dev` directly.

### What CI will run

The `CI` workflow (`.github/workflows/ci.yml`) classifies which paths a pull
request touched and then runs only the relevant jobs:

| Job | What it does |
|---|---|
| Backend Quality | `ruff format --check`, `ruff check --no-fix`, `pydocstyle` |
| Frontend Quality | prettier, eslint, `tsc --noEmit`, vitest, `npm audit` |
| Docs Build | `sphinx-build -W --keep-going` |
| `test` | pytest across four shards with a PostgreSQL service, `-m "not external"` — so the `dbtest` tier does run in CI even though it is excluded from `make test` |
| Security Scan | `gitleaks detect` over the tree, then `pip-audit` against the exported production requirements |
| `docker-build` | builds the affected images, then starts the runnable example and smokes it |
| Tutorial E2E | runs the Router Tutorial's `make up` → `make smoke` → `make demo` → `make demo-smoke` transition |
| CI Gate | aggregates the results of the jobs above into one check |

Because CI includes `dbtest`, a change to storage or auth can be green locally
and red in CI. Run `make test-db` against a local Postgres when you touch
`apps/backend/serving/storage/` or the auth surface.

If a pull request shows no CI run at all, check whether it has a merge conflict
first — a conflicted pull request does not trigger the `pull_request` workflow.
