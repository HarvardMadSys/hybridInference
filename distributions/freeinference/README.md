# FreeInference distribution overlay

This directory is the future single home for everything that is specific to
the freeinference.org deployment — the FreeInference *distribution* in the
sense of the
[neutral-upstream split design](../../docs/agents/specs/2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md)
([#946](https://github.com/HarvardMadSys/hybridInference/pull/946)).
What belongs here vs. upstream is ruled per directory by the
[ownership classification](../../docs/agents/specs/2026-07-17-repo-ownership-classification.zh.md)
([#953](https://github.com/HarvardMadSys/hybridInference/pull/953)).
Both relative links resolve once those PRs merge; until then use the PR links.

## `deploy/` — this site's public identity

`deploy/*.env` is live, not a skeleton. It holds the values that make the
stack *this* site: name, public URLs, support address, CORS origins, and the
console's build-time identity. Upstream's compose defaults name no deployment
(so `docker compose up` on a clone brings up an unbranded gateway), which
means these files are what production actually runs on —
`ops/deploy/deploy_{production,staging}.sh` feed every `deploy/*.env` to
`docker compose --env-file`, ahead of the server's `.env`.

Two consequences worth remembering:

- **Deleting a key here changes production**, silently and immediately on the
  next deploy — it falls back to the neutral upstream default rather than
  erroring. `tests/unit/deploy/test_compose_identity.py` pins the ones that
  matter, and also fails if compose stops reading these files.
- **These files are checked in, so no secrets.** Keys, passwords and tokens
  stay in the server's `.env`, which is passed last and still overrides
  anything here, so per-host tweaks keep working.

Adding a value is two steps: reference it in `deploy/docker/docker-compose.yml`
with a neutral default (`${VAR-}` is enough when the code already has one),
then set it in the matching `deploy/*.env`.

## Current state

`config/` holds this deployment's real `models.yaml`, `routing.yaml` and
`alerts.yaml` — Phase 2 moved all three in, and `config/` at the repository
root keeps only `examples/`. Production reads them through
`MODELS_CONFIG_PATH` / `ROUTING_CONFIG_PATH` / `ALERTS_CONFIG_PATH`, set in
`deploy/backend.env` above; the paths there are container paths, because
`distributions/` is bind-mounted under `/app`.

Losing any of those three lines fails differently and none of them errors:
alerts falls back to built-in thresholds, routing loses the endpoint map and
starts anyway, models leaves the catalogue empty. `tests/unit/deploy/test_compose_identity.py`
pins all three for that reason.

The dark-mode procedure below predates that move and its precondition no
longer holds: `distribution.yaml` resolves all three paths inside this overlay
now, so there is no legacy copy to compare against. Rewrite it before running
it again.

`distribution.yaml` is a real, loadable manifest, and its `paths:` now resolve
inside this directory. It is still not what production reads from: the env
vars above are. Applying the manifest needs
`DISTRIBUTION_CONFIG_MODE=active`, and the dark-mode rehearsal below has to be
rewritten first, since it compares against legacy copies that no longer exist.
For reference, that rehearsal was configured from the repo-root `.env`
(Compose passes it via `env_file`; the backend bind-mounts `distributions/`
read-only, so the overlay is never baked into the neutral image):

```bash
DISTRIBUTION_CONFIG_PATH=distributions/freeinference/distribution.yaml
DISTRIBUTION_CONFIG_MODE=dark   # loads + validates + logs; changes nothing
```

While the legacy copies existed, dark mode had to log every path comparison as
`identical`. That check is what the move retired. Because the loader fails open
(a missing mount starts the service without any comparison), the smoke script
was the way to verify rather than trusting a clean boot:

```bash
docker compose -f deploy/docker/docker-compose.yml exec backend \
    python distributions/freeinference/smoke_dark_load.py
```

It validates only the *inherited* environment (it never supplies its own
defaults) and exits non-zero if the overlay is unconfigured, the path points
elsewhere, the mode is not explicitly `dark`, the manifest is not visible
from the container, or any comparison is not `identical`.

Note on CI: local `make test` collects `distributions/*/tests/` via pytest
`testpaths`, but the PR CI sharder currently only walks `tests/` — the
neutral manifest gate that runs in CI lives at
`tests/unit/config/test_distribution_manifests_discovery.py`; it asserts that
all three paths resolve inside this overlay and that no copy was left behind at
the legacy location.
The tests here remain local/deploy verification until the future CI/CD stage
wires the distribution suite in.

## What `config/models.yaml` here does not say

The admin console can change routing weights, disable a provider and gate a
model by role at runtime. Those changes live only in Postgres, so the registry
in this directory is not by itself a description of what production serves.
`ops/db/export_runtime_overrides.py` produces the inventory; run it on the host.

The C0 export on 2026-07-28 found 75 overrides. Role gating has been folded in
— `required_role` here now equals what production serves, for all fourteen
models — and the database still wins at runtime, so nothing about production
changed. What is left is deliberate, and it is worth knowing before deploying
this registry anywhere else.

**Four providers are disabled in production and still declared here.**
`chutes`, `featherless`, `ollama` and `openrouter` were turned off between
2026-07-06 and 2026-07-21. The disable is a database row; the routes stay in
this file because the intent is temporary. So a deployment that starts from
this registry with an empty database gets routes production has switched off:

```
minimax-fast        production: no usable route   fresh: chutes, featherless, openrouter
deepseek-v4-flash   production: sglang, staging   fresh: + deepseek, ollama
deepseek-v4-pro     production: deepseek, staging fresh: + ollama
glm-5.1 / glm-5.2   production: zai, staging      fresh: + ollama, chutes
glm-5-turbo         production: zai, staging      fresh: + chutes
minimax-m2.5        production: minimax, staging  fresh: + ollama, chutes
minimax-m3          production: minimax, staging  fresh: + ollama
kimi-k2.7-code      production: kimi_coding, ...  fresh: + ollama, chutes
```

`minimax-fast` is the one to look at first: every route it declares belongs to
a disabled provider, so production serves it from nowhere and does not list it
in `/v1/models`. It is in the registry and it is not a model this deployment
offers.

**Eight database rows point at models this registry no longer declares** —
`glm-4.7`, `glm-5`, `gpt-5.3-spark`, `gpt-oss-20b` and `minimax-m2.7` still
carry weight or visibility rows. They are inert while the models are absent.
Deleting them is a write against the production database and has not been done.

## Target layout (Phase 2 in progress, one category per PR)

```text
distributions/freeinference/
  distribution.yaml   # this manifest
  config/             # real models/routing/alerts yaml — MOVED IN
  branding/           # logos, colors, site metadata
  content/            # terms, privacy, email templates
  docs/               # user docs + RAG corpus
  deploy/             # site compose/systemd overlays (workflows stay put)
  ops/                # machine-specific scripts (spark/h200/backup)
  targets/            # harness/e2e site targets
```

Two temporary states to be aware of:

- The `../../config/*.yaml` cross-root aliases are gone: `paths:` points inside
  this directory, which is what the visibility boundary requires. The rule that
  produced them still stands — nothing here may reach outside itself.
- `site:` / `features:` are exposed read-only via `GET /site-config`; the
  frontend consumes the safe identity fields and public signup/RAG flags only
  when `DISTRIBUTION_CONFIG_MODE=active`. Dark mode returns the neutral
  fallback and does not alter the public UI. Local content paths remain
  server-only and are not exposed.

## What still has to move, and why it has not

`config/` is in. `ops/`, `services/` and `deploy/systemd/` are not, and the
reason is not that they are large — it is that they fail differently.

Each config file had exactly one indirection point: an env var. Moving one was
a change in a single place, pinned by a test, with the old path proven empty.
These have none. They are named directly, and measuring the coupling gives:

```
ops/db                    38 files   29 external references
ops/setup                  4         16
ops/ci                    19         10
ops/deploy                 3         10
ops/local_deployment_proxy 11         11
ops/h200_idle_proxy        6         11
ops/spark_idle_proxy       6          4
ops/admin                  4          3
```

Some of those references live outside the repository, in systemd units
templated with the real checkout path at install time and **installed on the
machines**. Moving the source leaves them pointing at nothing until someone
re-runs the installer, so the merge has to happen in a window that includes the
machine half.

That much was always true. What was not was the size of it — this paragraph
used to name the H200 and DGX Spark boxes and quote
`WorkingDirectory=/srv/hybridInference/ops/h200_idle_proxy`. Surveying the
hosts on 2026-07-29 found something narrower:

| host | unit | source | state |
|---|---|---|---|
| rtx6000b | `local_deployment_proxy` | a colleague's checkout, not `/srv` | running since 2026-07-25 |
| rtx6000a | `local_deployment_proxy` | `/srv/hybridInference/ops/…` | running |
| spark1, spark2, production | — | — | nothing installed |

So `ops/h200_idle_proxy` and `ops/spark_idle_proxy` are installed on no
machine at all, and the outage this paragraph promised does not exist for
them. **One directory is coupled to running services: `ops/local_deployment_proxy`,
on two hosts.** One of those two runs out of `/madsys/juncheng/…` rather than
`/srv`, so re-running its installer is a conversation with its owner rather
than a command on a box we administer — which is the real reason to schedule
this rather than the number of files.

The survey also turned up a `sglang_idle_proxy.service` on rtx6000b, enabled,
`status=200/CHDIR`, `NRestarts=23906`. #727 renamed that directory to
`local_deployment_proxy` on 2026-06-18; the new unit was installed and the old
one never removed, so it has been failing to chdir every few seconds since.
Nothing in this repository refers to the old name, so there is nothing to fix
here — it wants `systemctl disable --now` and its unit file deleted.

That is the whole blocker. Everything else is prepared:

- `tests/unit/repo/test_no_dangling_repo_paths.py` resolves every repo path
  written into anything that executes — workflows, shell scripts, the Makefile,
  the systemd units and the Dockerfiles — including the `${REPO_DIR}/…` and
  `__REPO_ROOT__/…` forms these use. A reference missed during the move fails
  on the pull request instead of on the server.
- `ops/` is not uniformly this deployment's. `ops/admin`, `ops/ci`, `ops/db`
  (minus `analysis/`) and the GeoIP updater are generic operator tooling and
  belong upstream — the export manifest already keeps them, after excluding
  them wholesale once turned out to take `create_admin.py` with it.

So the move is: relocate the site-specific directories, fix `REPO_DIR` in each
installer (it resolves `../..` from its own location, which changes), let the
guard find what was missed, then on each machine:

```bash
cd /srv/hybridInference && git pull
sudo ./distributions/freeinference/ops/<proxy>/install_service.sh
```

The installers are idempotent and support `--uninstall`, so the machine half is
one command each. It has to happen in the same maintenance window as the merge,
which is why this is a decision about timing rather than a task waiting to be
picked up.

## Rules

- Upstream code must never import from this directory (design principle 2).
- No secrets in any file here — credentials stay in env / Secret Manager.
- Content moves in are Tier B (revert = rollback); switching a config file's
  production truth into `config/` here is Tier A and follows the design
  doc's dual-read → dark → canary ladder.
- This directory is the future repo-split and visibility boundary: assume
  everything in it stays private to the FreeInference operation.
