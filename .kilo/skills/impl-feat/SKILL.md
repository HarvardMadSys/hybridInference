---
name: impl-feat
description: Implement new features or enhancements to the hybridInference codebase, ensuring they are well-tested and follow project conventions. Use when the user wants to add new functionality or improve existing features.
---

# Implement Feature

Implement new features or enhancements to hybridInference with a structured workflow: plan → implement → review → PR → babysit until merged.

## Project Overview

hybridInference is a hybrid LLM inference gateway. Key layout:

| Path | Purpose |
|---|---|
| `apps/backend/serving/` | FastAPI backend — adapters, auth, storage, admin, middleware, schemas |
| `apps/backend/routing/` | Request routing engine |
| `apps/backend/benchmark/` | Load-testing client |
| `apps/frontend/` | Next.js 15 + React 18 + TypeScript + Tailwind dashboard |
| `config/` | YAML config — `models.yaml`, `routing.yaml`, `alerts.yaml`, `routewise.yaml` |
| `deploy/docker/` | Docker Compose for production |
| `services/` | Supporting services (llm-prober, alert-logger, freeinference-harness) |
| `tests/` | Backend tests (pytest markers: `unit`, `integration`, `external`, `dbtest`, `d1`) |

**Tech stack:** Python 3.12 · FastAPI · Pydantic v2 · asyncpg · uv · ruff · pydocstyle (Google-style) · Next.js 15 · Vitest · ESLint

**Deployments:**
- Production: https://freeinference.org
- Staging: https://staging.freeinference.org (test account: `admin@admin.com` / `admin`)

## Scope

- **Base branch:** always branch off `dev`.
- **Branch naming:** `jason/claude/<feature-name>` (kebab-case, descriptive).
- **Worktree:** always create a git worktree for the new branch — never work in the main worktree.

## Step 1 — Prepare

```bash
git fetch origin && git pull origin dev
```

Create a worktree and branch:

```bash
git worktree add ../hybridInference-<feature-name> -b jason/claude/<feature-name> origin/dev
```

All subsequent work happens inside the new worktree.

## Step 2 — Plan (before coding)

Analyze the codebase to understand:
- Existing patterns and conventions in `apps/backend/serving/` (backend) or `apps/frontend/src/` (frontend).
- Which adapters, routers, schemas, or storage modules are relevant.
- Config entries in `config/models.yaml` or `config/routing.yaml` if the feature touches routing.
- Dependencies and potential side effects across the FastAPI app.

Present the user with a plan including:
1. **Description** — what the feature/enhancement does and why.
2. **Files to change** — list of files that will be created or modified.
3. **Implementation steps** — ordered breakdown of the work.
4. **Testing strategy** — which test markers to use (`unit`, `integration`, etc.), how to verify.

Wait for user approval before proceeding.

## Step 3 — Implement

Use a subagent to execute the plan. Guidelines for the implementation:

- **Read before writing.** Understand the existing code context before making changes.
- **Follow conventions.** Match the code style (line-length 100, double quotes, Google-style docstrings). Use `uv run` for Python commands. Frontend uses npm.
- **No unnecessary comments.** Don't add comments unless the user asks or the code is genuinely non-obvious.
- **No new dependencies.** Don't introduce libraries not already in `pyproject.toml` or `apps/frontend/package.json`.
- **Security first.** Never expose secrets, hardcode credentials, or introduce injection vulnerabilities. Use the existing JWT/auth patterns in `serving/auth/` and `serving/utils/jwt.py`.
- **Minimal scope.** Only change what is necessary for the feature. Don't refactor or "improve" unrelated code.
- **Backend patterns:** Use Pydantic v2 models from `serving/schemas.py`, FastAPI `Depends()` from `serving/servers/deps.py`, adapters from `serving/adapters/`, storage from `serving/storage/`.
- **Frontend patterns:** Use React Query (`@tanstack/react-query`), react-hot-toast for notifications, zod + react-hook-form for forms, clsx for classnames.

## Step 4 — Self-review

After implementation, review the diff:

```bash
git diff origin/dev...HEAD
```

Check for:
- **Bugs** — logic errors, missing edge cases, unhandled errors, race conditions (async code).
- **Security** — exposed secrets, injection vectors, missing auth checks.
- **Performance** — N+1 queries, unnecessary loops, missing indexes.
- **Correctness** — wrong types, missing null checks, off-by-one errors.
- **Code style** — line-length ≤ 100, Google-style docstrings, double quotes, proper isort.
- **Tests** — new code should have tests; existing tests should still pass.

Fix any issues found.

## Step 5 — Format, lint, and test

```
make lint
make format
make test
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
git add -A && git commit -m "<descriptive message>" && git push -u origin jason/claude/<feature-name>
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
- **Test against staging.** Verify the feature on https://staging.freeinference.org if applicable (use `admin@admin.com` / `admin`).
