---
name: babysit-pr
description: Follow one HybridInference PR, address valid feedback and CI failures, and report readiness. Use for ongoing PR follow-up; merge only with explicit user authorization for that PR.
---

# Babysit PR

Follow `AGENTS.md`. Select the PR explicitly named by the user, or the single open
PR for the current task branch. For branch-based selection:

```bash
BRANCH=$(git branch --show-current)
gh pr list --head "$BRANCH" --base dev --state open \
  --json number,title,headRefName,isDraft,url
```

If detached, missing or ambiguous, report that state rather than guessing. If the
PR is closed or merged, report completion and end this run. Draft PRs may be
inspected and repaired when requested, but remain drafts unless the user asks
otherwise.

## One bounded follow-up

Use the sibling `check-pr` skill for evidence-based review and CI repair when
available. The same permissions and worktree rules apply: fix only valid issues,
verify changes, push only intended commits, and leave unsupported review claims
for the user. Read [PR signals](../check-pr/references/pr-signals.md) for commands
and interpretation, including paginated threads and current-head checks.

A manual invocation may follow checks for up to 30 minutes using short,
interruptible waits. Stop on an actionable blocker, repeated failure without
new evidence, or the time limit. For an already configured recurring run, make
one pass and let the next scheduled run continue; do not create nested loops or
new schedules. Create recurring monitoring only on the user's request, and keep
unchanged/non-actionable runs quiet unless periodic updates were requested.

## Merge only when authorized and ready

"Babysit", a skill invocation, or a schedule alone does not authorize merging.
Honor an explicit "do not merge" instruction. Without merge authorization for
this PR, report readiness and stop. With authorization, re-fetch the PR just
before merging and require all of the following:

- The PR is open, not a draft, and targets `dev` in the intended repository.
- Its head matches the revision reviewed and tested after the latest push.
- Required checks have completed successfully, required approvals are satisfied,
  and no unresolved review threads or requested changes remain.
- GitHub reports it mergeable and branch protection permits the operation.
  Unknown, empty or errored signals need investigation, not a merge attempt.

Use the repository's permitted merge method and match the verified head:

```bash
gh pr merge <N> --squash --match-head-commit <verified-head-sha>
```

The PR template specifies squash merging into `dev`. Choose `--merge` or
`--rebase` instead only when appropriate to repository policy
and the task. Never use `--admin`, bypass protection, or force-push. If GitHub
refuses the merge, report the reason. Branch/worktree deletion is separate
cleanup: preserve active work and remove it only within the authorized scope.

Report the PR URL, verified head, changes, check/review status and any action
needed. Claim a merge only after GitHub confirms it.
