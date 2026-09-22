---
orphan: true
---

# Sequential follow-up PR automation

`sequential-follow-up-prs.yml` polls the upstream repository for the merge
state of four published foundation PRs. It advances each independent thread by
at most one unpublished unit per invocation. It never merges the PR it creates.

The machine-readable source of truth is
`[.github/sequential-prs.yml](../../.github/sequential-prs.yml)`. It contains
exact fork branch names, immutable source ranges, titles, bodies, and focused
validation commands. There is no model-based sequence selection.

## Current sequence map

| Thread | Foundation PR | Successor order | Prepared fork branch |
| --- | ---: | --- | --- |
| prefill | #1419 | reviewable core → benchmarks | `prep/1419-reviewable-core` → `prep/1419-prefill-benchmarks` |
| trusted proxy | #1427 | consumer contract → key-pool lifecycle | `prep/1427-consumer-contract` → `prep/1427-keypool-lifecycle` |
| erasure | #1428 | claim state machine → identity protection | `prep/1428-claim-state-machine` → `prep/1428-identity-protection` |
| classifier | #1430 | admission recording → routing consumption | `prep/1430-admission-recording` → `prep/1430-routing-consumption` |

The `origin/prep/1427-provenance-foundation` ref is stale foundation-only
material: published #1427 is at the separate `fix/trusted-proxy-ip` head and
contains additional compatibility/documentation commits. The
`origin/prep/1428-fence-substrate` and `origin/prep/1430-evidence-model` refs
are foundation mirrors already represented by their open PRs. They are listed
as retired in the manifest and are never publication candidates.

The current #1419 head is `bamn/routewise/prefill-load-routing-v2` at
`4462e630`; it is not modified by this automation.

## State and duplicate protection

The sequencer queries the upstream REST API for each foundation PR and advances
only when `merged_at` is non-null. A closed PR without a merge halts only that
thread. Before any publication it checks all upstream PRs whose head is
`B-A-M-N:<branch>`; an existing open, merged, or closed PR is adopted or
reported instead of creating a duplicate. It repeats that check immediately
before push and after push to cover partial failures.

An eligible unit is reconstructed by applying the manifest's
`source_base..source_tip` patch with `git apply --3way --index` onto a fresh
`upstream/dev` worktree. `source_ref` may name a durable fork-tracking ref;
the manifest's immutable `source_tip` must still match that ref exactly. When
`source_ref` is omitted, the sequencer verifies the corresponding prepared fork
branch. A missing or moved source ref halts that thread. The resulting changed
paths must exactly equal the source unit's paths.
`git diff --check`, the focused tests, Ruff formatting, Ruff lint, the
sequencer's mypy check, and the unit's declared gates must pass before
publication. Repaired units use pinned clean commits rather than the obsolete
stacked prep ancestry.

Existing prepared branches are updated with
`--force-with-lease`, never an unguarded force push. A changed remote ref
causes the push to fail safely. The workflow-level concurrency group prevents
overlapping scheduler invocations.

## Credentials

Store these repository secrets for scheduled publication:

* The workflow's `GITHUB_TOKEN` needs **Contents: read/write** on
  `B-A-M-N/hybridInference`; checkout/git use it to update prepared branches
  in the fork.
* `HYBRIDINFERENCE_PR_TOKEN`: the existing **classic PAT** for `B-A-M-N`,
  stored only as a fork Actions secret. Its `repo` and `workflow` scopes are
  required by the current cross-repository workflow: a fork-only fine-grained
  token is not sufficient for creating the upstream PR. It is used by `gh` to
  read merge state, detect duplicate PRs, and create cross-repository PRs.

The deployed credential was verified as `B-A-M-N` with fork push access and
upstream PR-read access. GitHub does not expose a non-mutating check for the
final upstream PR-create permission, so the workflow does not create a test
PR; its real publication call remains the final permission gate.

No credential is committed. A manual dry-run can use the workflow token for
public reads and does not push or open PRs; scheduled publication fails closed
when `HYBRIDINFERENCE_PR_TOKEN` is absent.

Before the sequencer runs, the workflow preflight verifies the upstream and
fork remotes, both `dev` refs, authenticated reads of all four foundation PRs,
and fork write authorization with a non-mutating `git push --dry-run`. GitHub
does not expose a non-mutating test for creating a cross-repository PR, so the
token's upstream pull-request permission remains enforced by the real
publication call; no test PR is created.

## Operations

The schedule runs every ten minutes. `workflow_dispatch` defaults to
`dry_run=true`; use it to inspect the per-thread current PR, state, next unit,
and would-normalize/push/create decisions before enabling publication.
