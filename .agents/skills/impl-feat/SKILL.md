---
name: impl-feat
description: Implement features or enhancements in HybridInference, verify the behavior, and prepare a PR when requested. Use for new functionality rather than bug diagnosis or existing PR maintenance.
---

# Implement Feature

Read the repository's `AGENTS.md` and applicable subsystem guides first. They
own branch/worktree conventions and quality gates; this skill adds the feature
workflow.

## Prepare and implement

- Inspect the working tree and existing worktrees. Reuse a suitable task worktree;
  otherwise fetch the upstream `dev` and create a feature branch in a worktree
  following `AGENTS.md`. Do not pull `dev` into an unrelated current branch.
- Read neighboring code and the newest applicable specs, plans and reviews.
  Explain the intended behavior, affected components and verification approach.
  Proceed within the user's authorized scope; clarify only unresolved decisions
  that materially change the result.
- Implement the requested behavior using existing abstractions. Keep unrelated
  refactors and new dependencies out unless the task needs them. Delegate only
  when useful and permitted; parallel agents are not a prerequisite.
- Add tests for meaningful behavior and failure cases. For runtime changes,
  exercise the affected behavior against a running local/example or authorized
  staging deployment; a generic health check alone is insufficient.

## Review and deliver

- Review both the committed diff against the base branch and uncommitted changes.
  Check correctness, compatibility, concurrency, security and relevant subsystem
  invariants. Inspect `git diff` again after formatting to exclude unrelated edits.
- Run the gates in `AGENTS.md` and
  `docs/developer/contributing.md`, including frontend or database checks when
  relevant. Report exact results and any verification that remains unavailable.
- When asked to submit a PR, commit only intended paths, push the task branch and
  target `dev`. Write a description of the final behavior and validation to a
  file, then use `gh pr create --base dev --title "..." --body-file <file>`.
- If PR follow-up is requested, use the sibling `check-pr` skill when available.
  Report the PR URL and remaining checks. Creating a PR does not authorize a
  merge, deployment, recurring monitor or worktree deletion.
