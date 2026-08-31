---
name: babysit-pr
description: Babysit the PR for the current branch — address review comments, fix CI failures, and auto-merge when ready. Use when the user wants to babysit/triage the current PR, or when scheduled via /loop or /schedule.
---

# Babysit PR

Operate on the single PR associated with the current branch. Address what you safely can, then either auto-merge it or surface it for the user.

## Scope

- **Repo:** the current working directory's repo only.
- **PR:** the open PR whose head ref is the current branch.
- **State:** open, not draft.

## Step 1 — identify the PR

```bash
BRANCH=$(git branch --show-current)
gh pr list --head "$BRANCH" --state=open --draft=false \
  --json number,title,headRefName,isDraft,mergeable,mergeStateStatus
```

If the result is empty, report "no open PR for branch `$BRANCH`" and stop. If more than one is returned, report the ambiguity and stop — don't guess.

Take the single PR number as `N` for the rest of this run.

## Step 2 — gather signals

Fetch in parallel:

```bash
gh pr view <N> --json number,title,headRefName,baseRefName,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,reviewThreads,comments,latestReviews
gh pr checks <N>
```

Identify:
- **CI status** — any failed required checks?
- **Review threads** — any unresolved threads with code-change requests or questions?
- **Approval** — `reviewDecision == "APPROVED"`?
- **Mergeability** — `mergeable == "MERGEABLE"` and no conflicts?

## Step 3 — fix CI failures (if any)

For each failed check:
1. Fetch logs: `gh run view <run-id> --log-failed` (find the run id from `gh pr checks`).
2. Diagnose root cause from the logs. Don't guess — read the actual error.
3. You're already on the branch (it's the current one). If not, `gh pr checkout <N>`.
4. Apply the minimum fix. Don't refactor surrounding code.
5. Run the failing check locally if possible to verify.
6. Commit with a message describing the fix (no Claude attribution unless user asks).
7. Push: `git push`.

If the failure is environmental (flaky test, infra blip), re-run instead: `gh run rerun <run-id> --failed`. Do this only once per failure — if it fails again, treat it as a real bug.

If you can't confidently fix it, leave a PR comment explaining what's failing and stop work.

## Step 4 — address review comments

For each unresolved review thread, classify:

- **Code-change request** ("please rename X", "this should handle null", "extract this into a helper") → apply the change if reasonable. "Reasonable" means: scoped to the PR's purpose, doesn't require new architectural decisions, doesn't conflict with another reviewer's request. If unreasonable or ambiguous, reply explaining and leave for the user.
- **Question** ("why did you do it this way?", "does this handle case X?") → reply with an answer. Be specific — reference the code, not vague reassurances. If you don't know, say so and leave for the user.
- **Nit / style** → apply if trivial; otherwise skip and note it.
- **Approval / praise / "lgtm"** → ignore.
- **Note**: not all comments are correct, so use your judgment. If a comment is factually wrong or based on a misunderstanding, reply with the correct info and skip the change.
- **Other** → use your judgment.

Apply code changes in one commit (not per comment). Push.

After replying or pushing fixes, mark threads resolved only if you actually addressed them: `gh api -X POST repos/{owner}/{repo}/pulls/{n}/threads/{id}/resolve` (or use the GraphQL `resolveReviewThread` mutation).

## Step 5 — merge if ready

The PR is mergeable iff ALL of these hold:
- CI: all required checks green
- Threads: zero unresolved review threads
- Mergeable: `mergeable == "MERGEABLE"` (no conflicts)
- No new commits pushed in this run that haven't yet been checked by CI (wait for them)

If any condition fails, do NOT merge. Please go back to step 2 and keep babysitting.

If all hold:

```bash
gh pr merge <N> --squash --delete-branch
```

(Use `--squash` unless the repo's convention is different — check recent merges with `gh pr list --state=merged --limit=5 --json mergeCommit,title` to infer.)

## Step 6 — report

End with a concise status line:

```
PR #<N> "title" — <merged | pushed CI fix, waiting for re-run | replied to N comments, M threads need your call | failing CI I can't diagnose: <link>>
```

## Guardrails

- **Never force-push.** If the branch needs a rebase, leave a comment and stop.
- **Never merge into main**. You should only merge into dev.
- **Never merge to a protected branch by bypassing checks.** If `gh pr merge` fails because checks aren't green, that's the system working — don't pass `--admin`.
- **Don't switch branches.** This skill operates on the current branch's PR. If the working tree is dirty or you're mid-rebase, stop and report.
- **One pass per invocation.** Don't loop internally — if scheduled, the next run picks up where this left off.
- **Keep running the loop until the PR is merged or closed.** If scheduled, keep invoking this skill on a timer (e.g. every 30 minutes) until the PR is no longer open. This way it can babysit over time, even if the user isn't actively monitoring.


