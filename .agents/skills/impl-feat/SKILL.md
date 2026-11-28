---
name: impl-feat
description: Implement features or enhancements in HybridInference, verify the behavior, and prepare a PR when requested. Use for new functionality rather than bug diagnosis or existing PR maintenance.
---

# Implement Feature

Read `AGENTS.md` and any applicable subsystem guide first; they own the branch,
worktree and quality-gate rules, and this skill adds the feature workflow.

## Prepare and implement

- Inspect the working tree and existing worktrees. Reuse a suitable task
  worktree, or fetch the upstream `dev` and create a feature branch in a
  worktree as `AGENTS.md` describes. Never pull `dev` into an unrelated current
  branch.
- Read the neighboring code and any design record that `AGENTS.md` or the
  subsystem guide names for the area. State the intended behavior, affected
  components and how you will verify them. Proceed within the user's authorized
  scope; ask only about open decisions that would materially change the result.
- Implement with the existing abstractions. Leave out unrelated refactors and
  new dependencies the task does not need. Delegate only when useful and
  permitted; parallel agents are not required.
- Add tests for meaningful behavior and failure cases. Exercise runtime changes
  against a running local, example or authorized staging deployment; a generic
  health check alone is not enough.

## Review and deliver

- Review the committed diff against the base branch and any uncommitted
  changes for correctness, compatibility, concurrency, security and the
  subsystem's invariants. Inspect `git diff` again after formatting to catch
  unrelated edits.
- Run the gates in `AGENTS.md` and `docs/developer/contributing.md`, including
  frontend or database checks when relevant. Report exact results and any
  verification you could not run.
- When asked to submit a PR, commit only the intended paths, push the task
  branch and target `dev`. Write the description of the final behavior and its
  validation to a file, then run
  `gh pr create --base dev --title "..." --body-file <file>`.
- For PR follow-up the user asks for, use the sibling `check-pr` skill, and
  report the PR URL and remaining checks. Creating a PR does not authorize a
  merge, a deployment, a recurring monitor or deleting the worktree.
