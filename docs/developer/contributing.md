# Contributing

HybridInference is MIT-licensed (see `LICENSE`) and contributions are welcome.
This page covers what the repository contains, the checks a change has to pass,
and how a change gets proposed.

## Getting set up

```bash
git clone <your-fork-url> hybridinference
cd hybridinference
git remote add upstream https://github.com/HarvardMadSys/hybridInference.git
git fetch upstream dev
git switch -c yourname/tests/routing-config-defaults upstream/dev
make setup-dev
```

Replace `yourname` in the branch name with your GitHub username. To keep your
main checkout free for other work, create the branch in a separate
[worktree](https://git-scm.com/docs/git-worktree) instead, for example
`git worktree add -b <branch> ../hybridinference-<feature> upstream/dev`. If you
cloned the upstream repository rather than a fork, start from `origin/dev` and
push to your fork remote.

`make setup-dev` creates `.venv` on Python 3.12, installs the project editable,
syncs the `dev` dependency group and installs the pre-commit hooks. Full
prerequisites and the ways to run the gateway are in
[Installation](installation.md).

Every `make` target that touches Python runs through `uv run`. Override that if
your environment needs it:

```bash
make lint UV_RUN="uv run --active"
```

## Your first contribution

Start by running the [Quickstart](router-tutorial.md) in your worktree, or use
the [source development setup](installation.md#development-checkout-no-docker).
Seeing the gateway answer one request makes it easier to tell whether a later
failure comes from your change or from setup. A local example deployment is
enough; contributing does not require access to a maintainer's server.

Choose a small reproducible bug, an uncovered configuration case, or a confusing
instruction you can verify yourself. Search the issue tracker and existing
tests first. For a larger feature, open a feature request to agree on the
behavior before writing it; the repository provides bug and feature forms.

For a concrete first exercise, cover a routing YAML endpoint that uses an
environment-variable default. Read `_expand_env_value` and
`load_routing_config` in `apps/backend/routing/config.py`, then the neighboring
tests in `tests/unit/routing/test_config.py`. An unset variable should use the
default URL; a set variable should override it. The test below exercises both
through the public loader and restores the environment after each case:

```python
@pytest.mark.parametrize("endpoint", [None, "https://override.example"])
def test_endpoint_env_default(tmp_path, monkeypatch, endpoint):
    monkeypatch.delenv("TEST_ROUTING_ENDPOINT", raising=False)
    if endpoint is not None:
        monkeypatch.setenv("TEST_ROUTING_ENDPOINT", endpoint)
    path = tmp_path / "routing.yaml"
    path.write_text(
        "remote_deployment:\n"
        "  - endpoint: ${TEST_ROUTING_ENDPOINT:-https://default.example}\n"
        "    models: [example-chat]\n"
    )

    config = load_routing_config(path)

    assert config.remote_deployment[0].endpoint == (endpoint or "https://default.example")
    assert config.remote_deployment[0].models == ["example-chat"]
```

Add it to that test file only if the case is still uncovered. This is a
test-only contribution when the loader already behaves correctly. For a bug
fix, first make your regression test fail on the original code, then change
the smallest responsible function until it passes.

Run the focused tests while editing:

```bash
uv run pytest -q tests/unit/routing/test_config.py
```

For a runtime change, repeat the affected request against your local gateway.
With the [Quickstart](router-tutorial.md)'s example running,
`make build s=backend DISTRIBUTION=example` rebuilds
changed backend code; `make smoke DISTRIBUTION=example` checks health, the
model list and a routed completion. Exercise the changed behavior as well:
a generic smoke passing does not establish that a particular bug is fixed.
The [Quickstart](router-tutorial.md) explains the separate full-console check
when your change involves accounts or the frontend.

Before opening the PR, run `make format`, `make lint` and `make test`, plus the
[frontend](#frontend) or [documentation](#documentation) checks when relevant.
Review `git diff` so the patch contains only your intended files. Push your
branch to your fork and open a PR targeting upstream `dev`, using a title such
as `test(routing): cover endpoint environment defaults`. Fill in the existing
PR template with the behavior covered and the exact checks you ran; include
the reproduction and before/after result for a bug fix.

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
                  example the Quickstart uses
deploy/
  docker/         Dockerfiles and docker-compose.yml
  systemd/        unit files for host-level deployments
docs/
  developer/      this guide (MyST Markdown, built with Sphinx)
  agents/         design specs and plans
ops/              maintenance and CI helper scripts (admin, ci, db)
tests/            see the test tiers below
benchmark/        benchmarking scripts
```

Two things about the backend are easy to get wrong:

- `apps/backend/routing/executor.py` is a backward-compatibility shim that
  re-exports `FixedRouter` as `RouteExecutor`. Edit
  `apps/backend/routing/routers.py` instead.
- The backend packages are `serving` and `routing`, rooted at `apps/backend`.
  `make setup-dev` installs them into `.venv`, so `uv run` or an activated
  `.venv` imports them from any directory; outside that environment, set
  `PYTHONPATH=apps/backend`.

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
| Hits live external servers | `tests/external/` | `external` | no |

`testpaths` is `["tests", "distributions"]`, so overlay-owned tests run in the
default suite alongside `tests/`.

```bash
make test                              # the default suite
make test-verbose                      # same selection, serial, -vv
make test-cov                          # same selection, with coverage
make test-db                           # only -m dbtest
make test-all                          # everything except -m external
make test-external                     # only -m external

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
`docs/developer/index.rst` as the root toctree. Build them, and run the checks
CI runs, from the repository root:

```bash
make docs-verify
```

That builds the English and Chinese sites with warnings treated as errors,
checks the local links in every Markdown file, and checks that no translated
paragraph has quietly fallen back to English. A broken cross-reference or a page
missing from the toctree fails it, so run it before pushing. Sphinx,
`myst-parser` and `sphinx-rtd-theme` come from the `dev` dependency group, so
`make setup-dev` is enough to build. `make docs` builds the site alone.

Add a new page to `docs/developer/index.rst` as well as writing it; an orphan
file is a warning, and warnings are errors in CI.

### Writing a page

These pages ship with the source, and most readers arrive from a search or the
sidebar with one task in mind. Write for that reader:

- **Say who the page is for and what they can do after reading it**, in the
  first paragraph.
- **Lead with the action or the answer.** Put exceptions and edge cases after
  it, in a note if they run long, and do not open a section with what the
  software lacks.
- **Name a file only when the reader will open or edit it.** Do not cite
  functions or line numbers to prove a statement; tests and review do that.
- **Describe the software as it is now.** What changed, how it used to work and
  how you checked belong in the pull request, the release notes or
  `docs/reviews/`.
- **Leave out the details of any one installation** — its hosts, model ids,
  accounts and measurements. They belong in that distribution's own
  documentation.
- **Use the words in the [Glossary](glossary.md)**, and add a term there before
  relying on a new one.
- **Keep a paragraph to one idea.** When it piles up conditions, make it a list
  or a table.
- **When a setting does not do what its name suggests, say so in one
  sentence**, add it to
  [Settings that currently have no effect](configuration.md#settings-that-currently-have-no-effect),
  and open an issue, rather than explaining the internals at length.

### Translations

The site is published in English and Chinese, and an English edit changes the
paragraph a translation is attached to. Update the Chinese in the same pull
request; [Translating the Docs](translations.md) explains how.

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
- Call out changes to API behavior, configuration, defaults or database schema
  in the PR, with migration steps and rollback limits. See
  [Releases and upgrades](releases.md#compatibility-and-breaking-changes).
- Do not commit to `main` or `dev` directly.

### What CI will run

The `CI` workflow (`.github/workflows/ci.yml`) classifies which paths a pull
request touched and then runs only the relevant jobs:

| Job | What it does |
|---|---|
| Backend Quality | `ruff format --check`, `ruff check --no-fix`, `pydocstyle` |
| Frontend Quality | prettier, eslint, `tsc --noEmit`, vitest, `npm audit` |
| Site UI Containers | builds the frontend image with no module and with the example's Site UI module, requires a module build that lacks its context to fail, and checks how the running example serves its assets |
| Docs Build | `make docs-verify`: both languages built with warnings as errors, local links checked, and every translation checked against the English |
| `test` | pytest across four shards with a PostgreSQL service, `-m "not external"` — so the `dbtest` tier does run in CI even though it is excluded from `make test` |
| Security Scan | `gitleaks detect` over the tree, then `pip-audit` against the exported production requirements |
| `docker-build` | builds the affected images, then starts the runnable example and smokes it |
| Tutorial E2E | runs the Quickstart's `make up` → `make smoke` → `make demo` → `make demo-smoke` sequence |
| CI Gate | aggregates the results of the jobs above into one check |

Because CI includes `dbtest`, a change to storage or auth can be green locally
and red in CI. Run `make test-db` against a local Postgres when you touch
`apps/backend/serving/storage/` or the auth surface.

If a pull request shows no CI run at all, check whether it has a merge conflict
first — a conflicted pull request does not trigger the `pull_request` workflow.
