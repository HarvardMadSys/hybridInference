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

## Current state

Skeleton only. `distribution.yaml` is a real, loadable manifest, but its
`paths:` deliberately point back at the legacy `config/*.yaml` locations —
**the legacy paths remain production truth** (Phase 1). To try it on
staging, add to the repo-root `.env` (Compose passes it via `env_file`; the
backend service bind-mounts `distributions/` read-only, so the overlay is
never baked into the neutral image):

```bash
DISTRIBUTION_CONFIG_PATH=distributions/freeinference/distribution.yaml
DISTRIBUTION_CONFIG_MODE=dark   # loads + validates + logs; changes nothing
```

Dark mode must log every path comparison as `identical` while this state
holds. Because the loader fails open (a missing mount starts the service
without any comparison), **verify with the smoke script** instead of
trusting a clean boot:

```bash
docker compose -f deploy/docker/docker-compose.yml exec backend \
    python distributions/freeinference/smoke_dark_load.py
```

It exits non-zero if the manifest is not visible from the container or any
comparison is not `identical`.

## Target layout (grows in Phase 2, one category per PR)

```text
distributions/freeinference/
  distribution.yaml   # this manifest
  config/             # real models/routing/alerts yaml (Tier A move, last)
  branding/           # logos, colors, site metadata
  content/            # terms, privacy, email templates
  docs/               # user docs + RAG corpus
  deploy/             # site compose/systemd overlays (workflows stay put)
  ops/                # machine-specific scripts (spark/h200/backup)
  targets/            # harness/e2e site targets
```

Two temporary states to be aware of:

- The `../../config/*.yaml` cross-root aliases are a **Phase 1 expedient
  only**: once Phase 2 moves the real config in here, `paths:` must point
  inside the overlay — the directory is the future visibility boundary and
  must not reach outside itself.
- `site:` / `features:` in the manifest are declarations only for now:
  exposed read-only via `GET /site-config`, wired to no runtime behavior.

## Rules

- Upstream code must never import from this directory (design principle 2).
- No secrets in any file here — credentials stay in env / Secret Manager.
- Content moves in are Tier B (revert = rollback); switching a config file's
  production truth into `config/` here is Tier A and follows the design
  doc's dual-read → dark → canary ladder.
- This directory is the future repo-split and visibility boundary: assume
  everything in it stays private to the FreeInference operation.
