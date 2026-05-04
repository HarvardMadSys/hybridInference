---
name: impl-feat
description: Implement new features or enhancements to the codebase, ensuring they are well-tested and follow project conventions. Use when the user wants to add new functionality or improve existing features.
---

# Implement Feature

Implement new features or enhancements to the codebase with a structured workflow: plan → implement → review → PR → babysit until merged.

## Scope

- **Base branch:** always branch off `dev`.
- **Branch naming:** `<username>/claude/<feature-name>` (kebab-case, descriptive).
- **Worktree:** always create a git worktree for the new branch — never work in the main worktree.

## Step 1 — Prepare

```bash
git fetch origin && git pull origin dev
```

Create a worktree and branch:

```bash
git worktree add ../hybridInference-<feature-name> -b <username>/claude/<feature-name> origin/dev
```

All subsequent work happens inside the new worktree.

## Step 2 — Plan (before coding)

Analyze the codebase to understand:
- Existing patterns and conventions (read neighboring files, check configs).
- Where the change should live (which module, file, layer).
- Dependencies and potential side effects.

Present the user with a plan including:
1. **Description** — what the feature/enhancement does and why.
2. **Files to change** — list of files that will be created or modified.
3. **Implementation steps** — ordered breakdown of the work.
4. **Testing strategy** — how the change will be verified (unit tests, manual test on staging, etc.).

Wait for user approval before proceeding.

## Step 3 — Implement

Use a subagent to execute the plan. Guidelines for the implementation:

- **Read before writing.** Understand the existing code context before making changes.
- **Follow conventions.** Match the code style, naming, and patterns used in the codebase.
- **No unnecessary comments.** Don't add comments unless the user asks or the code is genuinely non-obvious.
- **No new dependencies.** Don't introduce libraries not already used in the project.
- **Security first.** Never expose secrets, hardcode credentials, or introduce injection vulnerabilities.
- **Minimal scope.** Only change what is necessary for the feature. Don't refactor or "improve" unrelated code.

## Step 4 — Self-review

After implementation, review the diff:

```bash
git diff origin/dev...HEAD
```

Check for:
- **Bugs** — logic errors, missing edge cases, unhandled errors, race conditions.
- **Security** — exposed secrets, injection vectors, missing auth checks.
- **Performance** — N+1 queries, unnecessary loops, missing indexes.
- **Correctness** — wrong types, missing null checks, off-by-one errors.
- **Code style** — violations of project conventions (check ruff/lint config).
- **Tests** — new code should have tests; existing tests should still pass.

Fix any issues found.

## Step 5 — Format, lint, and test

```bash
make format
make check
```

If the feature includes frontend changes, also run:

```bash
make check-all
```

All tests must pass before proceeding. If any test fails, fix the code — never skip or mark tests as expected failures to work around issues.

## Step 7 — Create a GitHub issue

Create an issue first (required before PR):

```bash
gh issue create \
  --title "<feature title>" \
  --body "$(cat <<'EOF'
## Summary
<brief description>

## Implementation
<plan summary>
EOF
)"
```

Note the issue number — reference it in the PR.

## Step 8 — Commit and push

```bash
git add -A && git commit -m "<descriptive message>" && git push -u origin <username>/claude/<feature-name>
```

Use descriptive commit messages. Don't attribute to Claude unless asked.

## Step 9 — Create PR

```bash
gh pr create \
  --base dev \
  --title "<feature title>" \
  --body "$(cat <<'EOF'
## Summary
<bullet points describing the change>

Closes #<issue-number>
EOF
)"
```

Provide the PR link to the user.

## Step 10 — Babysit PR

After creating the PR, poll every 2 minutes to:
1. **Fix CI failures** — fetch logs, diagnose, apply minimal fix, push.
2. **Address review comments** — validate each comment (see check-pr skill Step 5), apply only valid feedback with minimal changes.
3. **Re-poll** until all checks pass and comments are resolved.

Use the `check-pr` skill for detailed validation criteria and CI fix procedures.

Maximum babysit time: 30 minutes. If issues persist, report status and stop.

## Step 11 — Merge and cleanup (after user approval)

Only merge after the user approves:

```bash
gh pr merge <N> --merge --delete-branch
git worktree remove ../hybridInference-<feature-name>
```

## Guardrails

- **Never commit to `main` or `dev`.** Always use a feature branch.
- **Never force-push.** If rebase is needed, leave a comment and stop.
- **Always run `make format` and `make check`** before every commit/push.
- **Minimal changes only.** Don't refactor or touch unrelated code.
- **Always create an issue first.** Reference it in the PR body.
- **Work in a worktree.** Never modify the main working directory's branch.
- **Validate before acting.** Don't blindly accept review comments — verify against the code.
- **Test against staging.** Verify the feature on https://staging.freeinference.org if applicable (use the staging test account — check the project's internal docs for credentials).
