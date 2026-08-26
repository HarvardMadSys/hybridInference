# Direct publication readiness for HybridInference

**Decision date:** 2026-08-26

**Status:** Adopted; readiness work remains in progress

**Target:** Make the existing `HarvardMadSys/hybridInference` repository public

## Decision

HybridInference will be published by changing the visibility of the existing
repository. We will not create a second public source repository, maintain a
filtered mirror, or materialize a replacement tree with shadow files.

That choice makes all of these part of the publication surface:

- the complete tracked tree that remains at the visibility change; and
- every Git object reachable through a retained or GitHub-managed ref,
  including pull-request refs rather than only branches and tags;
- issues, pull requests, reviews, comments, discussions, releases, wiki pages,
  attachments, and other repository-native records; and
- GitHub Actions history and logs, which GitHub makes visible when a private
  repository becomes public. See GitHub's
  [repository visibility documentation](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility).

The retired filtered-export tool is not a security boundary. Private
FreeInference deployment content must move to the private `freeInference`
repository or be removed; content that cannot be published must not remain in
the HybridInference tree or retained history.

## Why the runnable distribution exists

The runnable example is executable documentation for the neutral upstream. A
fresh clone must be able to start the router, call a deterministic fake
OpenAI-compatible provider, exercise streaming and failover, and shut down
without FreeInference configuration, internal infrastructure, a GPU, or a paid
provider key.

Its target is `distributions/example/` because it is a distribution assembled
through the same public contract that real downstream deployments use. It is
not a deployment mirror and has no role in selecting files for publication.

## Hard gates before changing visibility

1. **Finish the deployment migration.** FreeInference configuration, content,
   operational scripts, deployment workflows, topology, and private docs must
   be served from the private repository. HybridInference must not retain a
   production deployment path merely because a filter used to hide it.
2. **Prove the neutral checkout works.** The runnable example and the normal
   developer test suite must pass from a fresh clone with no private overlay.
3. **Audit the current tree.** Credential, personal-data, private-note, and
   brand-residue checks run against the repository itself. The strict brand
   and private-surface sweeps must have no pending migration buckets or
   unclaimed findings:

   ```bash
   uv run python ops/admin/brand_residue_sweep.py --strict
   uv run python ops/admin/private_surface_sweep.py --strict
   ```
4. **Audit all reachable Git history.** Enumerate the exact remote refs GitHub
   exposes, including pull-request refs, and scan their full history with
   gitleaks in Git mode,
   including explicit rules for the project-issued `hyi-`, `agr.` and `ajt.`
   token formats and the repository's private-surface patterns.
   Findings are resolved, not accepted through a baseline: revoke or rotate a
   real secret first, remove private data, replace false-positive fixtures with
   obviously invalid values, and use only narrow documented exceptions.
5. **Audit GitHub-native data.** Review issues, pull requests, reviews,
   comments, discussions, Projects, releases, wiki content, attachments, Actions run
   history, logs, and artifacts. Edit or remove private material through the
   owning GitHub surface; a Git history rewrite cannot clean these records.
6. **Make public CI safe for untrusted forks.** Pull-request jobs use
   GitHub-hosted runners or isolated ephemeral runners. The public repository
   has no access to persistent self-hosted runner groups. Product deployment
   workflows and secrets have moved to the private repository; retained
   release workflows contain only neutral upstream behavior.
7. **Harden the repository settings.** Branch rules, Actions permissions,
   environment protection, package permissions, webhooks, deploy keys, and
   collaborator access are reviewed for a public project.
8. **Rehearse the visibility change.** Run the gates from a fresh remote clone,
   record the exact refs scanned, verify public documentation and
   packages, and prepare the incident and communication steps before changing
   the repository setting. Switching the repository back to private is not a
   security rollback: public clones and forks may continue to exist.

## History cleanup sequence

If the history audit finds material that cannot become public, cleanup is a
coordinated migration rather than an incidental force-push:

1. freeze writes and enumerate retained branches, tags, pull-request refs, and
   other GitHub-managed refs;
2. revoke or rotate exposed credentials;
3. rewrite the retained history and delete refs that still reach the old
   objects;
4. push the rewritten refs, verify that GitHub-managed pull-request refs and
   cached commit views no longer expose the old objects, and scan the remote
   again; if GitHub still serves an object, resolve it with GitHub before the
   visibility change;
5. separately remove or sanitize private GitHub-native records and workflow
   runs, logs, and artifacts;
6. rebuild or republish artifacts whose revision labels include old commits;
7. land a complete new FreeInference bump so `upstream.lock` points at the new
   HybridInference commit.

This invalidates old HybridInference commit IDs, open branches, and worktrees,
so it happens only after the repository split is complete and normal writes are
temporarily frozen. Follow GitHub's
[sensitive-data removal guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
for pull-request refs, cached views, forks, and support escalation.

## Deliberate PR boundaries

- Retiring filtered export is one mechanical PR.
- The runnable distribution remains an independent product/documentation PR.
- Public-runner and workflow hardening is reviewed separately because it
  changes CI and release trust.
- Private deployment removal follows the migration observation and rollback
  gates; it is not bundled into publication tooling cleanup.

The repository stays private until every hard gate above is complete.
