---
name: check-pr
description: Review and repair specified HybridInference PRs targeting dev, validate feedback, fix CI failures, and follow checks to a bounded result. Use for existing PR maintenance; this skill does not merge PRs.
---

# Check PR

Follow `AGENTS.md` for worktree setup and quality gates. A review-only request
stays read-only; apply and push fixes only within the requested scope. Never
merge through this skill.

## Select and inspect

Work on the PRs the user names, drafts and older PRs included. If the user asks
about their open PRs without naming them, list them in this repository:

```bash
gh pr list --author @me --base dev --state open \
  --json number,title,headRefName,headRepositoryOwner,isDraft,url
```

Do not silently drop PRs older than seven days. Keep drafts as drafts unless
the user asks otherwise. Before editing, confirm the target repository, base,
head revision and branch ownership.

[PR signals](references/pr-signals.md) has the supported queries, paginated
review threads and how to read check states. Read the diff and enough of the
owning code and tests to validate each claim. Prioritize actionable bugs,
security issues, compatibility problems, demonstrated performance regressions
and configured project conventions, and judge human and bot feedback by the
same evidence.

## Repair and verify

- For each failed check, read the real job logs through its actual Actions run
  or job ID. An external check may have only a provider URL; never invent
  Actions IDs.
- Make minimal, justified fixes in an isolated worktree for the PR head. Reuse
  a suitable clean task worktree; never switch branches in another task's
  checkout. Preserve unrelated edits, and never push a branch you have no
  authority to push.
- Run the relevant reproduction and the gates in `AGENTS.md`. Commit only the
  intended paths and push without force. Rerun a demonstrably transient
  infrastructure failure at most once before reporting it as persistent.
- Validate review feedback against the current code before changing anything.
  Explain unsupported claims and decisions that need the user, and leave those
  threads unresolved. Resolve a thread only when its issue is actually fixed and
  changing threads is within the requested scope.
- Post comments or replies only when the user asked or an explicitly invoked
  workflow authorizes it; otherwise report in the chat. No routine "looks good"
  comments.

## Follow checks and report

When asked to follow CI, re-query the structured state about every two minutes
with short, interruptible waits, not a blocking shell loop, for at most 30
minutes unless the user sets another limit. Investigate each new failure before
another fix, and stop repeating attempts that bring no new evidence.

An empty result, an API error, a skipped or cancelled check, or an unknown
state never proves success. After a push, wait for checks on the new head. At
the time limit, report the pending or failing checks and the PR URL; create
recurring monitoring only when the user asks.

Report each PR's findings, changes, the validation you actually ran, and the
remaining actions.
