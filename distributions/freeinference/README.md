# FreeInference distribution overlay

This directory is the future single home for everything that is specific to
the freeinference.org deployment — the FreeInference *distribution* in the
sense of the
[neutral-upstream split design](../../docs/agents/specs/2026-07-16-hybridinference-neutral-upstream-multi-distribution-design.zh.md)
([#946](https://github.com/HarvardMadSys/hybridInference/pull/946)).
Phase 2's detailed closed-root and independent-frontend target is defined in
the
[self-contained distribution design](../../docs/agents/specs/2026-07-22-phase2-self-contained-distribution-design.zh.md).
What belongs here vs. upstream is ruled per directory by the
[ownership-classification PR #953](https://github.com/HarvardMadSys/hybridInference/pull/953).
The implementation and that classification land together; the design is not
published as a separate Phase 2 implementation unit.

## Current state

Skeleton only. `distribution.yaml` is the real Phase 1 manifest, while
`distribution.v2.yaml` is a strict, closed-root Phase 2 candidate with all
selectors defaulting to `legacy` unless supplied by the deployment.
`bundle.yaml`, `bundle.lock.json`, and `config/environment-contract.yaml`
exercise the new source-inventory boundary without switching production
truth. The Phase 1 manifest
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

It validates only the *inherited* environment (it never supplies its own
defaults) and exits non-zero if the overlay is unconfigured, the path points
elsewhere, the mode is not explicitly `dark`, the manifest is not visible
from the container, or any comparison is not `identical`.

Note on CI: both local pytest discovery and the PR CI partitioner scan
`tests/` and `distributions/`. The neutral manifest gate lives at
`tests/unit/config/test_distribution_manifests_discovery.py`; it also carries
the temporary Phase 1 assertion that this overlay aliases the legacy truth.
Phase 2 adds the stricter runtime/bundle and detached-copy gates described in
the detailed design.

The checked-in candidate and source lock can be validated without loading any
secret values:

```bash
uv run hybridinference-distribution runtime-validate \
  distribution.v2.yaml --root distributions/freeinference --strict
uv run hybridinference-distribution bundle-validate \
  bundle.yaml --root distributions/freeinference --strict
```

Both commands accept a detached-copy directory through `--root` and keep every
path inside that root. Stable failure codes are 10 (schema), 11 (path), 12
(semantic parser), 13 (environment contract), and 14 (bundle lock).

RAG has a production parser for its JSON vector index but not yet for
distribution YAML settings or generated metadata. Strict validation therefore
uses `VectorStore.load` for the index plus a deliberately minimal, closed local
schema for settings/metadata and cross-checks model and embedding dimension
between all three artifacts.

## Target layout (grows in Phase 2, one category per PR)

```text
distributions/freeinference/
  distribution.yaml   # backend runtime manifest
  bundle.yaml         # distribution-owned source inventory
  bundle.lock.json    # deterministic declared-resource digests
  frontend/            # complete FreeInference Next.js product frontend
  config/             # real models/routing/alerts + environment contract
  branding/           # site/status identity and assets
  content/            # terms, privacy, email templates
  docs/               # user docs + RAG corpus
  rag/                # site RAG settings and generated index
  deploy/             # site compose/systemd overlays (workflows stay put)
  ops/                # machine-specific scripts (spark/h200/backup)
  monitoring/         # status/alert site configuration
  targets/            # harness/e2e site targets
```

Two temporary states to be aware of:

- The `../../config/*.yaml` cross-root aliases are a **Phase 1 expedient
  only**: once Phase 2 moves the real config in here, `paths:` must point
  inside the overlay — the directory is the future visibility boundary and
  must not reach outside itself.
- `site:` / `features:` are exposed read-only via `GET /site-config`; the
  frontend consumes the safe identity fields and public signup/RAG flags only
  when the selected manifest is active. Runtime v1 dark mode returns the
  neutral fallback; runtime v2 uses its independent resource selectors and
  does not accept the legacy global mode. Local content paths remain
  server-only and are not exposed.

## Rules

- Upstream code must never import from this directory (design principle 2).
- The distribution frontend consumes upstream through HTTP/OpenAPI/SSE/Auth
  contracts; it must not import upstream React/Next source.
- Every manifest/build/data/deploy path must remain within this directory;
  upstream is injected only as an explicit image/package reference. Phase 2's
  detached-copy gate is the final proof of that boundary.
- No secrets in any file here — credentials stay in env / Secret Manager.
- Content moves in are Tier B (revert = rollback); switching a config file's
  production truth into `config/` here is Tier A and follows the design
  doc's dual-read → dark → canary ladder.
- This directory is the future repo-split and visibility boundary: assume
  everything in it stays private to the FreeInference operation.
