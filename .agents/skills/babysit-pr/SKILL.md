---
name: babysit-pr
description: Follow one HybridInference PR, address valid feedback and CI failures, and report readiness. Use for ongoing PR follow-up; merge only with explicit user authorization for that PR.
---

# Babysit PR

Follow `AGENTS.md`. Work on the PR the user names, or else the single open PR
for the current task branch:

```bash
BRANCH=$(git branch --show-current)
gh pr list --head "$BRANCH" --base dev --state open \
  --json number,title,headRefName,isDraft,url
```

If the branch is detached or there is no single open PR, report that instead of
guessing. If the PR is closed or merged, report it complete and end the run.
Inspect and repair a draft only when asked, and leave it a draft unless the
user says otherwise.

## One bounded follow-up

Use the sibling `check-pr` skill for review and CI repair, under the same
permissions and worktree rules: fix only valid issues, verify, push only
intended commits, and leave unsupported review claims for the user.
[PR signals](../check-pr/references/pr-signals.md) has the commands and how to
read them.

A manual invocation may follow checks for up to 30 minutes with short,
interruptible waits. Stop at an actionable blocker, a repeated failure with no
new evidence, or the time limit. In an already configured recurring run, make
one pass and leave the rest to the next run; never create nested loops or new
schedules. Create recurring monitoring only when the user asks, and keep runs
with nothing new or actionable quiet unless the user asked for periodic updates.

## Merge only when authorized and ready

"Babysit", a skill invocation or a schedule does not authorize a merge. Honor
an explicit "do not merge". Without merge authorization for this PR, report
readiness and stop. With it, re-fetch the PR just before merging and require
all of the following:

- It is open, not a draft, and targets `dev` in the intended repository.
- Its head is the revision you reviewed and tested after the latest push.
- Required checks completed successfully, required approvals are in place, and
  no unresolved review threads or requested changes remain.
- GitHub reports it mergeable and branch protection permits the merge.
  Unknown, empty or errored signals need investigation, not a merge attempt.

Merge with the repository's permitted method, pinned to the verified head. The
PR template says `dev` is squash-merged; use `--merge` or `--rebase` only when
repository policy and the task call for it.

```bash
gh pr merge <N> --squash --match-head-commit <verified-head-sha>
```

Never use `--admin`, bypass branch protection or force-push. If GitHub refuses
the merge, report why. Deleting the branch or worktree is separate cleanup:
preserve active work and delete only within the authorized scope.

Report the PR URL, verified head, changes, check and review status, and any
action needed. Claim a merge only after GitHub confirms it.
