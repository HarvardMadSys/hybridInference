# FreeInference distribution overlay

This directory is the future single home for everything that is specific to
the freeinference.org deployment — the FreeInference *distribution* in the
sense of the
[neutral-upstream split design](../../docs/agents/specs/2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md).
What belongs here vs. upstream is ruled per directory by the
[ownership classification](../../docs/agents/specs/2026-07-17-repo-ownership-classification.zh.md).

## Current state

Skeleton only. `distribution.yaml` is a real, loadable manifest, but its
`paths:` deliberately point back at the legacy `config/*.yaml` locations —
**the legacy paths remain production truth** (Phase 1). Try it on staging
with:

```bash
DISTRIBUTION_CONFIG_PATH=distributions/freeinference/distribution.yaml
DISTRIBUTION_CONFIG_MODE=dark   # loads + validates + logs; changes nothing
```

Dark mode must log every path comparison as `identical` while this state
holds.

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

## Rules

- Upstream code must never import from this directory (design principle 2).
- No secrets in any file here — credentials stay in env / Secret Manager.
- Content moves in are Tier B (revert = rollback); switching a config file's
  production truth into `config/` here is Tier A and follows the design
  doc's dual-read → dark → canary ladder.
- This directory is the future repo-split and visibility boundary: assume
  everything in it stays private to the FreeInference operation.
