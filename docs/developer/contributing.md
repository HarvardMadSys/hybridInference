# Contributing

HybridInference is MIT-licensed (see `LICENSE`) and contributions are welcome.
This page covers the checks a change must pass and how to propose it.

## Getting set up

```bash
git clone <your-fork-url> hybridinference
cd hybridinference
git remote add upstream https://github.com/HarvardMadSys/hybridInference.git
git fetch upstream dev
git switch -c yourname/tests/routing-config-defaults upstream/dev
make setup-dev
```

Replace `yourname` with your GitHub username. To keep your main checkout free,
create the branch in a [worktree](https://git-scm.com/docs/git-worktree)
instead: `git worktree add -b <branch> ../hybridinference-<feature> upstream/dev`.
If you cloned upstream rather than a fork, start from `origin/dev` and push to
your fork remote.

`make setup-dev` creates `.venv` on Python 3.12, installs the project editable
with the `dev` dependency group, and installs the pre-commit hooks;
[Installation](installation.md) has the full prerequisites. Every `make` target
that runs Python goes through `uv run`; override it if you need to, for example
`make lint UV_RUN="uv run --active"`.

## Your first contribution

Run the [Quickstart](router-tutorial.md) or the
[source development setup](installation.md#development-checkout-no-docker)
first, so you can tell a failure your change causes from a setup problem. A
local example deployment is enough. Pick a small reproducible bug, an
uncovered configuration case or a confusing instruction, and search the issues
and tests first. For a larger feature, open a feature request to agree on the
behavior before writing it.

For a first exercise, test a routing-policy endpoint that uses an
environment-variable default, such as
`${TEST_ROUTING_ENDPOINT:-https://default.example}`. Read `_expand_env_value`
and `load_routing_config` in `apps/backend/routing/config.py`, and the tests in
`tests/unit/routing/test_config.py`. The gateway keeps its routing policy in the
database as JSON; `load_routing_config` parses the same structure from a file,
which keeps the test self-contained. An unset variable should give the default
URL, and a set one should override it:

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

Add it only if the case is still uncovered. For a bug fix, make the regression
test fail on the original code first, then change the smallest responsible
function until it passes. Run the focused tests while editing:

```bash
uv run pytest -q tests/unit/routing/test_config.py
```

For a runtime change, repeat the affected request against your local gateway.
With the Quickstart's example running, `make build s=backend DISTRIBUTION=example`
rebuilds the backend, and `make smoke DISTRIBUTION=example` checks health, the
model list and a routed completion. A passing smoke does not show that your bug
is fixed, so exercise the changed behavior too.

Before opening the pull request, run `make format`, `make lint` and `make test`,
plus the [frontend](#frontend) or [documentation](#documentation) checks when
relevant, and review `git diff`. Push to your fork and open a pull request
against upstream `dev`, with a title such as
`test(routing): cover endpoint environment defaults`. In the template, list the
behavior covered and the checks you ran; for a bug fix, add the reproduction
and the before and after result.

## Repository layout

```text
apps/
  backend/
    serving/      FastAPI gateway: HTTP surface, SSE streaming, provider
                  adapters, auth, storage, observability, admin API
    routing/      routing engine: routers, strategies, endpoint health,
                  circuit breaker
    migrations/   versioned schema migrations (`python -m migrations`)
  frontend/       Next.js web and admin console
config/
  examples/       example JSON documents (model catalog, routing policy, site
                  configuration, branding) to copy or import; never loaded
                  automatically
distributions/
  example/        the runnable example overlay the Quickstart uses; real
                  deployment overlays live in their own repositories
deploy/
  docker/         Dockerfiles and docker-compose.yml
  kubernetes/     example RBAC for discovering a proxy Pod's address
  systemd/        unit files for host-level deployments
  public-paths.json  the public path table
docs/
  developer/      this guide (MyST Markdown, built with Sphinx)
  agents/         design specs and plans
ops/              maintenance and CI helper scripts (admin, ci, db, dev), and
                  the OpenInfra reporter that GPU hosts run (openinfra)
tests/            see the test tiers below
```

The backend packages, `serving` and `routing`, are rooted at `apps/backend`.
`make setup-dev` installs them into `.venv`; outside it, set
`PYTHONPATH=apps/backend`.

## Quality gates

| Command | What it runs |
|---|---|
| `make format` | `ruff format .`, then `ruff check --fix --unsafe-fixes .` |
| `make lint` | `ruff format --check .`, `ruff check --no-fix .`, `pydocstyle` |
| `make test` | `pytest -m "not external and not dbtest" -n auto --dist loadfile` |
| `make check` | `lint` + `test` |
| `make all` | `format` + `check` |

Nothing type-checks the backend. Tooling enforces style: ruff (line length
100, target `py311`, rules `E`, `W`, `F`, `I`, `UP`, `B`, `C4`, `SIM`, `TCH`
and `RUF`) and pydocstyle (Google convention), which skips `tests`, `.venv`,
`node_modules`, `apps/frontend`, `ops` and `docs` and so enforces docstrings in
`apps/backend`.

The pre-commit hooks (`pre-commit run --all-files` runs them on the whole tree)
cover ruff at the version CI uses, pydocstyle, `gitleaks` secret scanning, the
standard whitespace, YAML, JSON and TOML checks, and eslint and prettier for
`apps/frontend`.

Before opening a pull request, run `make format` and make sure `make test`
passes.

## Tests

A test's marker decides its tier; the directories are a convention. The markers
declared in `pyproject.toml` are `unit`, `integration`, `external` and
`dbtest`, and `--strict-markers` makes any other marker an error.

| Tier | Lives in | Marker | In `make test`? |
|---|---|---|---|
| Unit | `tests/unit/` | — | yes |
| API surface | `tests/api/` | — | yes |
| Server / observability | `tests/servers/`, `tests/observability/` | — | yes |
| Needs a live Postgres | mostly `tests/integration/` | `dbtest` | no |
| Hits live external servers | `tests/external/` | `external` | no |

`testpaths` is `["tests", "distributions"]`, so tests an overlay ships run in
the default suite too.

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

The `dbtest` tier needs PostgreSQL. With Docker, `make test-db-local` needs no
setup: it starts one container for the whole machine
(`hybridinference-test-postgres` on `127.0.0.1:54329`, data in memory), gives
each checkout its own database, prepares it, and runs the tier with one worker
per CPU, up to eight. The container and databases are kept, so a re-run takes
about 15 seconds.

```bash
make test-db-local                                     # the dbtest tier
make test-db-local ARGS="tests/integration/test_x.py"  # narrower
eval "$(ops/dev/test-db-local.sh env)"                 # TEST_DB_* for plain pytest
make test-db-local-reset                               # start this worktree's databases afresh
make test-db-local-clean                               # drop removed worktrees' databases
make test-db-local-down                                # remove the container: every checkout's databases
```

Otherwise, every test finds the database through `tests/fixtures/postgres.py`,
from `TEST_DB_HOST`, `TEST_DB_PORT`, `TEST_DB_USER`, `TEST_DB_PASSWORD` and
`TEST_DB_NAME` (defaults `localhost:5432`, `postgres`/`postgres`,
`hybridinference_test_db`). Starting the database container is enough:

```bash
docker compose -f deploy/docker/docker-compose.yml --env-file .env up -d postgres
```

Each test process creates and migrates its own database on first use;
`uv run python -m tests.fixtures.postgres prepare --workers N` does it up front
before a run with `-n N`. `make test-db` sets `TEST_DB_REQUIRED=1`, as CI does:
a database test that cannot connect then fails instead of skipping, and
`TEST_DB_HOST` and `TEST_DB_PORT` must be set.

`make test` runs files in parallel. Run serially to debug one failure, but
check a suspected environment leak under `-n auto` as well: leaks between files
show up only in the parallel run.

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

CI also runs `npm run format:check` (prettier) and
`npm audit --omit=dev --audit-level=high` in `apps/frontend`.

## Documentation

These pages are MyST Markdown built by Sphinx from `docs/developer/`, with
`docs/developer/index.rst` as the root toctree. Before pushing, run the checks
CI runs:

```bash
make docs-verify
```

It builds the English and Chinese sites with warnings as errors, checks local
links, and checks that no translation has fallen back to English. `make docs`
builds the site alone. Add a new page to `docs/developer/index.rst`; an orphan
page is a warning, and warnings fail the build.

### Writing a page

Most readers arrive from a search or the sidebar with one task in mind:

- **Say who the page is for and what they can do after reading it**, in the
  first paragraph.
- **Lead with the action or the answer.** Put exceptions and edge cases after
  it, and do not open a section with what the software lacks.
- **Name a file only when the reader will open or edit it.** Do not cite
  functions or line numbers to prove a statement; tests and review do that.
- **Describe the software as it is now.** What changed and how you checked
  belong in the pull request or the release notes.
- **Leave out the details of any one installation**: its hosts, model ids,
  accounts and measurements belong in that distribution's own documentation.
- **Use the words in the [Glossary](glossary.md)**, and add a new term there
  first.
- **Keep a paragraph to one idea.** When it piles up conditions, make it a list
  or a table.
- **When a setting does not do what its name suggests, say so in one
  sentence**, add it to
  [Settings that currently have no effect](configuration.md#settings-that-currently-have-no-effect),
  and open an issue.

### Translations

The site is published in English and Chinese, and a translation is attached to
each English paragraph. Update the Chinese in the same pull request;
[Translating the Docs and the Console](translations.md) explains how.

## Proposing a change

- **Branch off `dev`, not `main`.** Pull requests target `dev`; CI runs on pull
  requests against `main` and `dev`.
- **Name branches `<user>/<scope>/<feature-name>`**, e.g.
  `jane/routing/weighted-fallback`.
- **Write commit and pull-request titles as conventional commits**:
  `type(scope): summary`, e.g. `fix(routing): keep the fallback route on 429`.
  Each pull request is squashed into one commit on `dev`, so its title becomes
  the commit subject.
- Keep a pull request to one feature or fix, update the docs in the same change,
  and add tests for new behavior.
- Call out changes to API behavior, configuration, defaults or database schema,
  with migration steps and rollback limits. See
  [Releases and upgrades](releases.md#compatibility-and-breaking-changes).
- Do not commit to `main` or `dev` directly.

### What CI will run

The `CI` workflow (`.github/workflows/ci.yml`) runs the jobs relevant to the
paths a pull request touches:

| Job | What it does |
|---|---|
| Backend Quality | `ruff format --check`, `ruff check --no-fix`, `pydocstyle` |
| Frontend Quality | prettier, eslint, `tsc --noEmit`, vitest, `npm audit` |
| Site UI Containers | builds the frontend image with no module and with the example's Site UI module, requires a module build that lacks its context to fail, and checks how the running example serves its assets |
| Docs Build | `make docs-verify` |
| `test` | pytest in four shards for the default selection, plus a `db` shard that runs the `dbtest` tier against PostgreSQL |
| Security Scan | `gitleaks detect` over the tree, then `pip-audit` against the exported production requirements |
| `docker-build` | builds the affected images, then starts the runnable example and smokes it |
| Tutorial E2E | runs the Quickstart's `make up` → `make smoke` → `make demo` → `make demo-smoke` sequence |
| CI Gate | aggregates the results of the jobs above into one check |

Because CI runs `dbtest`, a change can pass `make test` and fail in CI. When you
touch storage (`apps/backend/serving/storage/`), SQL, the schema, auth or a
database test, run `make test-db-local`, or `make test-db` against your own
PostgreSQL, before you push. If a pull request shows no CI run at all, check it
for a merge conflict: a conflicted pull request does not trigger the
`pull_request` workflow.
