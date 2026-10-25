# PR #1631 route mutation regression validation

The follow-up fixes were developed with Claude Code (`claude-opus-5-5`, high effort) and independently reviewed and executed by Codex. The starting PR head was `f3683327e4c82ce9d131e0b8f1929bd9c761a1b6`; integration includes `dev` at `a88458c4a945f3852d5723e800ea7b028f2d3944`.

## Regression cases

1. Deleting an earlier config route during Verify must not make an edit overwrite a different route. Installation resolves the stable route ID again.
2. Restoring a config route must repopulate the real RouteWise candidate table, including when the next state reload is unchanged.
3. Preparing a saved override during Restore must leave the route excluded until its final adapter and state can be published together.
4. Enable from a stale worker must not clear another worker's removal mark. State writes compare persisted flags atomically; conflicting writes return 409.
5. An edit awaiting Verify must not overwrite a newer quota or concurrency limit changed on the same adapter.
6. Config edits recheck endpoint reservations under the transition lock after Verify and before persistence.
7. Moving a runtime route with weight 0 must keep the new endpoint out of real RouteWise candidates, including after an unchanged reload.

Integration preserves the shared admin confirmation flow introduced on `dev`: config and runtime actions, restoration, and runtime edits all remain reviewable before writing. Gateway errors remain visible in the confirmation dialog.

## Validation

All automated execution occurred in an isolated Linux test checkout and disposable test databases. No production changes or PR merge were performed.

- The original seven deterministic reproducers all pass (253 tests together with the original focused suites). The restore observer treats an absent route as weight 0; its exclusion assertion is unchanged.
- Permanent backend regression suite: 259 passed, including 13 added interleaving/rollback cases. PostgreSQL suite: 519 passed, including two new tests for conditional writes and racing writers.
- Frontend: 1,859 tests passed, plus type checking and ESLint. The provider routes suite includes 88 tests.
- Full `make test`: 9,010 passed, 5 intentionally skipped (performance tests, a scanner run separately in CI, and an unavailable pool candidate).
- `make format` and `make lint`: passed. Formatting was executed only in the isolated environment and copied back.
- A running uvicorn acceptance app used actual HTTP requests and real PostgreSQL state. Disable → Delete → Restore kept the route excluded; Enable restored selection. The real RouteWise candidate table and a fresh state resolver agreed. Upstream adapter responses were synthetic; this does not claim live provider or production acceptance.
- Harness prerequisites were repaired without changing product code: the frontend container needed the full repository and Git; Docker Compose was supplied only in this run's test directory.

The requested scope remains seven route-mutation issues. Config-route endpoint moves retain their pre-existing weight-override behavior; this change fixes weight migration for runtime-route moves.
