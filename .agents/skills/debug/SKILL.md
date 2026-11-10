---
name: debug
description: Diagnose and fix HybridInference runtime bugs or failing tests by reproducing the failure, investigating its cause, and verifying a focused fix. Use for code defects rather than infrastructure-only outages or new features.
---

# Debug

Follow the repository's `AGENTS.md` and applicable subsystem guides for worktree
setup, ownership boundaries and quality gates. Reuse a suitable task worktree or
create one from `dev`; do not edit the main checkout or an unrelated branch.

## Establish the failure

- Capture the actual error, input, configuration and relevant revision. Run the
  failing test or affected request before proposing a fix.
- For a test, use `uv run pytest -vv <test-path>` with the appropriate markers.
  Database tests use `make test-db-local ARGS="-vv <test-path>"`; follow the
  shared-container restrictions in `AGENTS.md`.
- For CI, read the failed job logs. Distinguish code failures from unavailable
  services or runner problems rather than patching code to hide an outage.
- For deployment issues, consult that deployment's
  `distributions/<name>/AGENTS.md`. The model registry, routing and alerts YAML
  belong to `distributions/<name>/config/`; `config/examples/` contains reference
  defaults. Other settings resolve through the database-backed app settings.
- If the original failure cannot be reproduced, continue useful read-only
  investigation (logs, call paths, recent changes). State the evidence gap;
  avoid speculative patches and request missing inputs only when needed.

## Investigate and fix

Trace the failing value from the caller through the owning component:

| Symptom | Starting point |
|---|---|
| Provider response, timeout or protocol error | `apps/backend/serving/adapters/` |
| Selection, retry, health or admission | `apps/backend/routing/` and its `AGENTS.md` |
| HTTP, auth, middleware or SSE | `apps/backend/serving/servers/` and the code it calls |
| Configuration resolution or reload | `apps/backend/serving/config/` and the active overlay |
| SQL, schema or persistence | `apps/backend/serving/storage/` |
| Console behavior | `apps/frontend/src/` plus browser console/network evidence |

Check relevant callers, recent history and tests. Form an evidence-backed cause,
then make the smallest fix that preserves unrelated behavior. Add a regression
test for a code defect when it can meaningfully demonstrate the failure. Do not
weaken assertions or suppress errors to make a test pass.

## Verify and report

Re-run the reproduction and affected tests, then the required repository gates.
For runtime changes, verify the changed request against a running deployment
within the task's permissions. Distinguish isolated tests from integration and
live acceptance.

Report the cause, fix, validation and remaining uncertainty. Commit, push or
open a PR when included in the user's request; diagnosis alone does not require
publishing changes. Note unrelated defects without silently expanding the fix.
