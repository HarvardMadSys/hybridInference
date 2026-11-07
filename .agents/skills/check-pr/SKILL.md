---
name: check-pr
description: Review and repair specified HybridInference PRs targeting dev, validate feedback, fix CI failures, and follow checks to a bounded result. Use for existing PR maintenance; this skill does not merge PRs.
---

# Check PR

Follow `AGENTS.md` for worktree setup and quality gates. A review-only request
stays read-only; apply and push fixes only within the requested scope. Never
merge through this skill.

## Select and inspect

Use the PRs the user names, including drafts or older PRs. If the request is
about the user's open PRs without specifying which, list them in this repository:

```bash
gh pr list --author @me --base dev --state open \
  --json number,title,headRefName,headRepositoryOwner,isDraft,url
```

Do not silently exclude PRs older than seven days. Inspect drafts when relevant;
keep their draft status unless the user requests a change. Confirm the target
repository, base, head revision and branch ownership before edits.

Read [PR signals](references/pr-signals.md) for supported CLI queries, paginated
review threads and check-state interpretation. Read the diff and enough owning
code/tests to validate each claim. Prioritize actionable bugs, security issues,
compatibility problems, demonstrated performance regressions and configured
project conventions. Evaluate human and bot feedback by the same evidence.

## Repair and verify

- For each failed check, read the actual job logs using its real Actions run/job
  ID. External checks may have only a provider URL; do not invent Actions IDs.
- Apply minimal, justified fixes in an isolated worktree for that PR head. Reuse
  a suitable clean task worktree; do not switch branches in another task's
  checkout. Preserve unrelated edits and never push a branch without authority.
- Run the relevant reproduction and the gates in `AGENTS.md`. Commit intended
  paths and push without force. If an infrastructure failure is demonstrably
  transient, rerun the failed job at most once before reporting a persistent issue.
- Validate review feedback against the current code before changing it. Explain
  unsupported claims or decisions requiring user input; leave them unresolved.
  Resolve a review thread only after its issue is actually addressed and only
  when review-thread changes are within the requested scope.
- Publish comments/replies only when the user requested that or an explicitly
  invoked workflow authorizes it. Otherwise report findings in the chat. Do not
  post routine "looks good" comments on every run.

## Follow checks and report

For requested CI follow-up, re-query structured state about every two minutes,
using short interruptible waits rather than a blocking shell loop. Bound the
invocation to 30 minutes unless the user specifies otherwise. Investigate a new
failure before trying another fix; stop repeated attempts without new evidence.

Never treat an empty result, API error, skipped/cancelled check or unknown state
as proof of success. After pushing, wait for checks on the new head revision.
If the time limit is reached, report pending/failing checks and the PR URL;
recurring monitoring is created only when requested.

Report each PR's findings, changes, actual validation and remaining actions.
