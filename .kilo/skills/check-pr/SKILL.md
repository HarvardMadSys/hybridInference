---
name: check-pr
description: Check the user's open PRs against the dev branch, validate review comments before acting, make only minimal changes to fix CI failures and address valid feedback, and poll until all checks pass. Use when the user wants to review and fix their PRs after creation.
---

# Check PR

Review the user's open PRs targeting `dev`, validate review comments before acting on them, apply only minimal fixes for CI failures and valid feedback, and poll every 2 minutes until all checks pass.

## Scope

- **Repo:** the current working directory's repo only.
- **Author:** PRs where the user is the author (`--author=@me`).
- **State:** open, not draft.
- **Base branch:** `dev`.
- **Recency:** created within the last 7 days.

## Step 1 — list PRs

```bash
SINCE=$(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ)
gh pr list \
  --search "author:@me created:>=$SINCE base:dev" \
  --state=open --draft=false \
  --json number,title,headRefName,isDraft,mergeable,mergeStateStatus,createdAt
```

(On macOS, replace `date -u -d '7 days ago'` with `date -u -v-7d`.)

If empty, report "no open PRs targeting dev" and stop.

## Step 2 — per PR, gather signals

For each PR number, fetch in parallel:

```bash
gh pr view <N> --json number,title,headRefName,baseRefName,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,reviewThreads,comments,latestReviews
gh pr checks <N>
gh pr diff <N>
```

Identify:
- **CI status** — any failed or pending required checks?
- **Review threads** — any unresolved threads with code-change requests or questions?
- **Approval** — `reviewDecision == "APPROVED"`?
- **Mergeability** — `mergeable == "MERGEABLE"` and no conflicts?

## Step 3 — code review

Review the diff of each PR and add review comments for issues found. Focus on:

1. **Bugs** — logic errors, missing edge cases, unhandled errors, race conditions.
2. **Security** — exposed secrets, SQL injection, XSS, missing auth checks.
3. **Performance** — N+1 queries, unnecessary loops, missing indexes.
4. **Correctness** — wrong types, missing null checks, off-by-one errors.
5. **Code style** — violations of project conventions (check ruff/lint config).
6. **Best practices** — missing tests for new code, hardcoded values that should be config.

Add comments using:

```bash
gh api repos/{owner}/{repo}/pulls/<N>/comments \
  --method POST \
  -f body="<comment body>" \
  -f commit_id="<sha>" \
  -f path="<file>" \
  -f line=<line> \
  -f side="RIGHT"
```

For multi-line comments, use `start_line` and `start_side` as well.

If no issues are found, add a summary comment: `gh pr comment <N> --body "Code review: looks good."`

## Step 4 — fix CI failures (if any)

For each failed check:
1. Fetch logs: `gh run view <run-id> --log-failed` (find the run id from `gh pr checks` or `gh api repos/{owner}/{repo}/commits/<sha>/check-runs`).
2. Diagnose root cause from the logs. Don't guess — read the actual error.
3. Check out the PR branch locally: `gh pr checkout <N>`.
4. **Apply the minimum fix.** Change only what is necessary to resolve the CI failure. Do not refactor surrounding code, reformat unrelated lines, or "improve" nearby code.
5. Run `ruff format` before committing.
6. Run the failing check locally if possible to verify.
7. Commit with a descriptive message (no Claude attribution unless user asks).
8. Push: `git push`.

If the failure is environmental (flaky test, infra blip), re-run instead: `gh run rerun <run-id> --failed`. Do this only once per failure — if it fails again, treat it as a real bug.

If you can't confidently fix it, leave a PR comment explaining what's failing and stop work on this PR.

## Step 5 — validate and address review comments from others

Before acting on any review comment, **validate** it. Many comments are suggestions, preferences, or outright wrong. Only act on comments that are genuinely valid.

### 5a — Validate each comment

For each unresolved review thread from other reviewers, evaluate whether the comment is **valid** before classifying it:

A comment is **valid** if at least one of the following is true:
- It identifies a real bug or logic error in the current code.
- It points out a correctness issue (wrong type, missing null check, off-by-one).
- It identifies a security vulnerability.
- It flags code that violates an explicitly configured project convention (e.g. ruff rules, ESLint config, style guide).
- It points out missing error handling that could cause runtime failures.
- It identifies a performance regression with measurable impact.

A comment is **NOT valid** (skip or politely push back) if:
- It is a stylistic preference not backed by a project convention or linter rule.
- It suggests an alternative approach that works equally well — the current code is fine.
- It asks for a refactor or architectural change that goes beyond the PR's scope.
- It is factually incorrect about how the code behaves (verify by reading the code carefully).
- It is based on a misunderstanding of the requirements or intent.
- It suggests adding code that is not needed (YAGNI).

When in doubt, err on the side of **not changing** the code. Reply politely explaining your reasoning, and leave the thread for the user to decide.

### 5b — Classify and act on valid comments only

For each **valid** comment, classify:

- **Code-change request** ("please rename X", "this should handle null") → apply the change only if it is reasonable and scoped to the PR's purpose. "Reasonable" means: doesn't require new architectural decisions, doesn't conflict with another reviewer's request. If unreasonable or ambiguous, reply explaining and leave for the user.
- **Question** ("why did you do it this way?") → reply with an answer referencing the code. If you don't know, say so and leave for the user.
- **Nit / style** → apply only if it aligns with an existing project convention; otherwise skip and note it.
- **Approval / praise / "lgtm"** → ignore.

### 5c — Apply minimal changes

- Apply code changes in **one commit per PR** (not per comment).
- **Only change lines that are necessary** to address the specific valid feedback. Do not reformat, refactor, or touch unrelated code.
- Run `ruff format` before pushing.
- After replying or pushing fixes, mark threads resolved only if you actually addressed them.
- For **invalid** comments, reply politely explaining why you believe the current code is correct, and leave the thread unresolved for the user to review.

## Step 6 — poll until green

After pushing any fixes, poll every 2 minutes until CI passes:

```bash
# Wait loop
while true; do
  STATUS=$(gh pr checks <N> 2>&1)
  echo "$STATUS"
  if echo "$STATUS" | grep -q "fail"; then
    echo "CI still failing, checking again in 2 minutes..."
    sleep 120
  elif echo "$STATUS" | grep -q "pending"; then
    echo "CI still running, checking again in 2 minutes..."
    sleep 120
  else
    echo "All checks passed!"
    break
  fi
done
```

If CI fails again after a fix, go back to Step 4.

Maximum poll time: 30 minutes. If CI still hasn't passed after 30 minutes, leave a comment and stop.

## Step 7 — report

End with a concise per-PR status table:

```
PR #123 "title" — all checks green, reviewed
PR #124 "title" — pushed CI fix, waiting for re-run
PR #125 "title" — replied to 2 questions, 1 thread needs your call: <link>
PR #126 "title" — failing CI I can't diagnose: <link>
```

## Guardrails

- **Never force-push.** If the branch needs a rebase, leave a comment and stop.
- **Never merge.** Only fix and review — the user merges manually or via the babysit-pr skill.
- **Never push to a branch you don't own.** Only act on PRs where you can push.
- **Don't address comments from bots** unless they block merge.
- **Stop on conflict.** If `gh pr checkout` reports merge conflicts, leave them for the user.
- **Always run `ruff format`** before pushing any commit.
- **Minimal changes only.** Never refactor, reformat, or "improve" code beyond what is strictly necessary to resolve the issue at hand. If a fix requires touching multiple lines, explain why each change is needed in the commit message.
- **Validate before acting.** Never blindly accept a review comment as correct. Read the code, verify the claim, and only act if the comment identifies a genuine problem.
- **One pass per invocation.** Don't loop internally beyond the CI polling in Step 6 — if scheduled, the next run picks up where this left off.
