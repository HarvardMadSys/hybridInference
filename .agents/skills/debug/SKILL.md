---
name: debug
description: Diagnose and fix HybridInference runtime bugs or failing tests by reproducing the failure, investigating its cause, and verifying a focused fix. Use for code defects rather than infrastructure-only outages or new features.
---

# Debug

Follow `AGENTS.md` and any applicable subsystem guide for worktree setup,
ownership boundaries and quality gates. Reuse a suitable task worktree or
create one from `dev`; never edit the main checkout or an unrelated branch.

## Establish the failure

- Capture the actual error, input, configuration and revision, and run the
  failing test or request before proposing a fix.
- Run a test with `uv run pytest -vv <test-path>` and the appropriate markers,
  a database test with `make test-db-local ARGS="-vv <test-path>"` (follow the
  shared-container rules in `AGENTS.md`).
- For CI, read the failed job logs. Tell code failures apart from unavailable
  services or runner problems; never patch code to hide an outage.
- For a deployment issue, read that deployment's `distributions/<name>/AGENTS.md`.
  Configuration lives in PostgreSQL, edited through Admin: the model catalog,
  routing policy and alert rules are `app_config` documents, and other
  settings resolve through the database-backed settings (`AGENTS.md` §6.3).
  `config/examples/` holds the JSON examples the tutorials seed.
- If you cannot reproduce the failure, keep investigating read-only (logs, call
  paths, recent changes) and state the evidence gap. Avoid speculative patches,
  and ask for missing inputs only when needed.

## Investigate and fix

Trace the failing value from the caller through the component that owns it:

| Symptom | Starting point |
|---|---|
| Provider response, timeout or protocol error | `apps/backend/serving/adapters/` |
| Selection, retry, health or admission | `apps/backend/routing/` and its `AGENTS.md` |
| HTTP, auth, middleware or SSE | `apps/backend/serving/servers/` and the code it calls |
| Configuration resolution or reload | `apps/backend/serving/config/` and the stored settings and documents |
| SQL, schema or persistence | `apps/backend/serving/storage/` |
| Console behavior | `apps/frontend/src/` plus browser console and network evidence |

Check callers, recent history and tests, form a cause backed by evidence, and
make the smallest fix that preserves unrelated behavior. Add a regression test
for a code defect when a test can meaningfully show the failure. Never weaken
assertions or suppress errors to make a test pass.

## Verify and report

Re-run the reproduction and affected tests, then the required gates. For
runtime changes, verify the changed request against a running deployment
within the task's permissions, and keep isolated tests, integration and live
acceptance distinct in your report.

Report the cause, fix, validation and remaining uncertainty. Commit, push or
open a PR only when the user's request includes it; a diagnosis alone does not
require publishing changes. Note unrelated defects instead of silently widening
the fix.
