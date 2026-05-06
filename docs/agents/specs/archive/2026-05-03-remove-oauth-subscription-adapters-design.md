# Remove in-process OAuth subscription adapters (codex_sub + claude_sub)

**Date:** 2026-05-03
**Status:** Design approved, ready for implementation plan
**Branch:** `jason/claude/remove-oauth-subs`

## Goal

Delete all in-process OAuth subscription support — both `codex_sub` (ChatGPT subscription via OpenAI Codex CLI OAuth) and `claude_sub` (Anthropic subscription via Claude Code CLI OAuth) — along with the entire shared OAuth account-pool stack, related scripts, configuration, frontend surfaces, and documentation.

## Background

The gateway originally supported routing requests through ChatGPT and Claude subscription accounts using OAuth credentials harvested from the official Codex and Claude Code CLIs. Two adapters (`codex_sub`, `claude_sub`) shared a generic multi-account `AccountPool` with health-aware rotation, per-account cooldown, and consecutive-failure tracking.

The Codex subscription path was never activated in production: the `gpt-5.x` model entries in `config/models.yaml` are commented out and require an operator-provisioned `var/data/codex_accounts.json` file that does not exist on staging or prod hosts.

The Claude subscription path was migrated away from in-process OAuth on 2026-05-02 ("anthropic compatibility" plan): all active Claude models (`claude-sonnet-4.6`, `claude-opus-4.6`, `claude-opus-4.7`) now route through `provider: anthropic` (the direct `AnthropicAdapter`) with `base_url: ${CLI_PROXY_BASE_URL}`. An external `cli-proxy-api` service handles OAuth on its own behalf and exposes a standard Anthropic-API surface to the gateway. The new `anthropic_messages.py` router replaced the older `anthropic_proxy.py` (which was claude_sub-only).

As a result, `claude_sub` and its dependencies are orphaned dead code: no model in `config/models.yaml` declares `provider: claude_sub` or a route with `kind: claude_sub`; the live `anthropic_messages.py` router does not import `claude_pool`, `claude_token`, or `claude_sub`.

This spec removes the orphaned OAuth subscription stack in full.

## Non-goals

- Removing the `cli-proxy` upstream itself or the `${CLI_PROXY_BASE_URL}`/`${CLI_PROXY_API_KEY}` environment variables. These power the live Claude path and stay.
- Removing the end-user `ops/setup/setup_claude_code.sh` (configures the local Claude Code CLI to point at `freeinference.org/anthropic`) or any user-facing documentation that advertises freeinference as a Claude Code endpoint.
- Removing the Vertex AI `ClaudeAdapter` (`serving/adapters/claude.py`), `claude_format.py`, `anthropic.py`, `anthropic_translator.py`, `anthropic_aliases.py`, or `anthropic_messages.py`. These are on the live path.
- Cleaning up `var/data/codex_accounts.json` and `var/data/claude_accounts.json` files on staging/prod hosts. Operational follow-up flagged in the PR description.

## Architecture & boundary

| Deleted (whole files) | Edited (scrubbed) | Untouched (live path) |
|---|---|---|
| `serving/adapters/codex_sub.py` | `serving/adapters/__init__.py` (drop 2 exports) | `serving/adapters/anthropic.py` |
| `serving/adapters/codex_translator.py` | `serving/adapters/claude.py` (drop stale docstring line) | `serving/adapters/anthropic_translator.py` |
| `serving/adapters/codex_token.py` | `serving/config/settings.py` (drop 10 settings) | `serving/adapters/anthropic_aliases.py` |
| `serving/adapters/claude_sub.py` | `serving/servers/registry.py` (drop 2 branches) | `serving/adapters/claude.py` (Vertex) |
| `serving/adapters/claude_token.py` | `serving/servers/bootstrap.py` (drop both checks) | `serving/adapters/claude_format.py` |
| `serving/adapters/claude_pool.py` | `serving/schemas.py` (drop comment) | `serving/servers/routers/anthropic_messages.py` |
| `scripts/import_codex_auth.py` | `routing/routers.py` (rewrite stale comment) | `ops/setup/setup_claude_code.sh` |
| `scripts/import_claude_auth.py` | `config/models.yaml` (drop commented codex block) | All active models in `config/models.yaml` |
| `scripts/inspect_claude_accounts.py` | `.env.example` (drop `CODEX_BASE_URL`) | All `claude_format` / Vertex tests |
| 5 codex/claude_sub test files | `frontend/.../ModelsSection.tsx` (drop filter) | `${CLI_PROXY_BASE_URL}` / `${CLI_PROXY_API_KEY}` |
| `freeinference-harness/configs/fixtures/tools-codex-3.yaml` | `frontend/.../playground/page.tsx` (drop reasoning UI) | `docs/agents/plans/*` (history) |
| | 9 docs files (codex/claude_sub/Codex-CLI sections) | |
| | 2 harness configs (codex models/scenarios) | |

**Total: 15 file deletes + 22 file edits.** Breakdown — deletes: 6 backend modules + 3 scripts + 5 tests + 1 harness fixture. Edits: 7 backend files + 2 frontend files + 2 config files + 2 harness configs + 9 docs files.

No file rename is required. The earlier idea of renaming `codex_token.py` → `account_pool.py` is moot — with both subscription adapters going, nothing imports the pool.

### Why this is safe

- `grep -n "kind: claude_sub\|provider: claude_sub" config/models.yaml` returns nothing. No active model wires up either subscription adapter.
- `serving/servers/routers/anthropic_messages.py`, `serving/adapters/anthropic.py`, and `serving/adapters/claude_format.py` (the live Claude path) have zero imports from `claude_sub`, `claude_pool`, `claude_token`, `codex_sub`, `codex_translator`, or `codex_token`.
- Deleting the OAuth stack cannot break a live request path because no live request path uses it.

## File-by-file change list

### Backend code — full deletes

| File | Action |
|---|---|
| `serving/adapters/codex_sub.py` | Delete |
| `serving/adapters/codex_translator.py` | Delete |
| `serving/adapters/codex_token.py` | Delete |
| `serving/adapters/claude_sub.py` | Delete |
| `serving/adapters/claude_token.py` | Delete |
| `serving/adapters/claude_pool.py` | Delete |

### Backend code — edits

| File | Specific change |
|---|---|
| `serving/adapters/__init__.py` | Drop `from .claude_sub import …` and `from .codex_sub import …`; drop both names from `__all__`. |
| `serving/adapters/claude.py` line 14 | Edit docstring "shared with `ClaudeSubscriptionAdapter`" → "shared with the direct `AnthropicAdapter`". |
| `serving/config/settings.py` lines 100–112 | Delete 10 fields: `codex_accounts_file`, `codex_fallback_api_key`, `codex_token_refresh_margin`, `codex_account_cooldown`, `codex_failure_threshold`, `claude_sub_accounts_file`, `claude_sub_fallback_api_key`, `claude_sub_token_refresh_margin`, `claude_sub_account_cooldown`, `claude_sub_failure_threshold`, plus the two section-comment lines. |
| `serving/servers/registry.py` lines 23–24, 189–192 | Drop `ClaudeSubscriptionAdapter` / `CodexSubscriptionAdapter` from imports; delete both `if kind == "codex_sub"` / `"claude_sub"` branches. |
| `serving/servers/bootstrap.py` lines 20, 87–110 | Drop both adapter imports; delete `needs_codex` / `needs_claude` flags, the `isinstance` checks, the early return, and both accounts-file existence warnings. If the function has nothing meaningful left after the edit, delete the function and its single caller (verify during implementation). |
| `serving/schemas.py` line 65 | Drop the trailing "(for Codex models: …)" portion of the `reasoning_effort` comment. The field stays — used by other backends. |
| `routing/routers.py` line 679 | Rewrite the comment from "Preserve adapter-set _routing (e.g. codex_sub fallback overrides);" to "Preserve adapter-set _routing if present;". The behavior is generic — only the example is gone. |

### Scripts — full deletes

| File | Action |
|---|---|
| `scripts/import_codex_auth.py` | Delete |
| `scripts/import_claude_auth.py` | Delete |
| `scripts/inspect_claude_accounts.py` | Delete |

### Tests — full deletes

| File | Action |
|---|---|
| `test/unit/adapters/test_codex_token.py` | Delete |
| `test/unit/adapters/test_codex_sub.py` | Delete |
| `test/unit/adapters/test_codex_sub_fallback.py` | Delete |
| `test/unit/adapters/test_codex_translator.py` | Delete |
| `test/unit/adapters/test_claude_sub.py` | Delete |

### Frontend — edits

| File | Specific change |
|---|---|
| `frontend/src/components/features/dashboard/ModelsSection.tsx` line 28 | Delete the entire `if (p === 'codex_sub' \|\| p === 'claude_sub') return false;` line. Neither provider exists; the filter is moot. |
| `frontend/src/app/dashboard/playground/page.tsx` lines 119, 525, 591 | Delete `const isCodexModel = …` (line 119); drop the `newModel?.provider === 'codex_sub' ? s.reasoningEffort : null` ternary (line 525) — replace with `null`; delete the `{isCodexModel && (…)}` reasoning-effort UI block (line 591). If `reasoningEffort` becomes dead state after this, drop it from the state shape too. |

### Config — edits

| File | Specific change |
|---|---|
| `config/models.yaml` lines 482–516 | Delete the entire `# --- Codex Subscription Models ---` block: the comment header, the `gpt-5.4` model entry, its commented `route:` with `kind: codex_sub`. Verify nothing else (commented or active) references `codex_sub` or `gpt-5.x` after the cut. |
| `.env.example` line 37 | Delete `CODEX_BASE_URL=…` line. |

### Harness — full delete + edits

| File | Action |
|---|---|
| `freeinference-harness/configs/fixtures/tools-codex-3.yaml` | Delete |
| `freeinference-harness/configs/targets/freeinference.yaml` lines ~115–167 | Delete the `gpt-5.x-codex-admin` model definitions and their `tags: [codex, admin]` entries. Verify no other harness target references them. |
| `freeinference-harness/configs/scenarios/tool-call-core.yaml` | Delete the four scenarios `forced_tool_codex_stream`, `forced_tool_codex_nonstream`, `auto_tool_codex_stream`, `multi_turn_codex_stream` (each pulls `tools_fixture: tools-codex-3.yaml`). |

### Docs — edits

| File | Specific change |
|---|---|
| `docs/user/README.md` | Drop the Codex bullet (line 17) and the `~/.codex/config.toml` setup step (line 35 onward — likely a multi-line block). |
| `docs/user/docs/developer/integrations.md` | Strip the entire Codex section (lines 9–18 intro + config example, plus troubleshooting bullets at 230–231). If Codex was the only integration in the file, leave a stub with surviving integrations or delete the file (verify when editing). |
| `docs/user/docs/developer/quickstart.md` line 24 | Drop the Codex `config.toml` setup step. |
| `docs/user/docs/developer/models.md` | Delete the `gpt-5.4` model entry (line 161 onward) and the "Codex: Edit `~/.codex/config.toml`" client-config block (line 202 onward). |
| `docs/developer/developer/architecture.md` | Drop the diagram row mentioning `codex_token.py` (line 101); drop the `CodexSubscriptionAdapter` table row (line 120); rewrite the AccountPool paragraph (line 122) — there is no shared pool anymore. Drop the `ClaudeSubscriptionAdapter` row if present (verify when editing). |
| `docs/developer/developer/configuration.md` | Drop `codex_sub` and `claude_sub` from the supported `kind` list (line 80); delete the Codex-subscription paragraph (lines 300–310) and the `CODEX_ACCOUNTS_FILE` env mention. Drop equivalent Claude-subscription content if present. |
| `docs/developer/developer/adding-models.md` | Drop `codex_sub` and `claude_sub` from the supported-providers list (line 295) and the kind reference table (line 435). |
| `docs/developer/developer/openrouter.md` line 18 | Drop the `codex_sub.py` reference from the code-tree comment block. |
| `docs/developer/developer/deployment.md` | Drop the host-script paragraph mentioning `~/.codex/`, `~/.claude/`, `var/data/` accounts files (line 107) and the `codex --login` / `import_codex_auth.py` instructions (lines 119–120). Verify any `import_claude_auth.py` mention is removed too. |

### Untouched (verified, intentional)

- `docs/agents/plans/2026-05-02-*.md`, `docs/agents/specs/2026-05-02-*.md` — historical artifacts, frozen in time.
- `test/unit/routing/test_routewise_router.py` line 1261 — "codex review fix" comment refers to a code review, not the codex adapter.
- `serving/adapters/anthropic_translator.py` line 3 mention of `claude_format.py` — references file, not OAuth.

## Workflow

1. Open GitHub issue titled "Remove in-process OAuth subscription adapters (codex_sub + claude_sub)" with a short summary and the file list.
2. Pull `dev`: `git fetch origin && git checkout dev && git pull origin dev`.
3. Create worktree at `.worktrees/remove-oauth-subs` on branch `jason/claude/remove-oauth-subs`.
4. Subagent executes the deletes + edits per the file-by-file list.
5. Run all verification gates below.
6. `ruff format` before commit.
7. Open PR to `dev`; PR description references the issue.
8. Watch comments + CI every 2 minutes until clean.
9. After merge: delete branch and worktree.

## Verification gates (in order)

### 1. Static — nothing references the deleted modules

```
grep -rn "codex_sub\|codex_translator\|codex_token\|claude_sub\|claude_token\|claude_pool\|ClaudeSubscriptionAdapter\|CodexSubscriptionAdapter\|import_codex_auth\|import_claude_auth\|inspect_claude_accounts\|CODEX_BASE_URL" \
  --include="*.py" --include="*.ts" --include="*.tsx" --include="*.yaml" --include="*.yml" --include="*.toml" --include="*.md" --include="*.sh" \
  | grep -v docs/agents/ \
  | grep -v test_routewise_router.py:1261
```
Expected: empty (with the two documented exceptions excluded).

### 2. Imports resolve — fast smoke

```
python -c "from serving.servers.bootstrap import *"
python -c "from serving.servers.registry import _make_adapter"
python -c "import serving.adapters"
```
Each must exit 0.

### 3. Test suite

```
pytest test/unit -x --ff
```
Expected: green. Pay attention to:
- `test/unit/adapters/test_claude_vertex_stream.py`, `test_claude_message_merge.py`, `test_claude_images.py` — these stay; must still pass (exercise `claude.py` Vertex + `claude_format.py`).
- `test/unit/storage/test_cost_calculation.py` — uses `claude_format.parse_usage` / `build_final_usage`; must still pass.
- `test/unit/routing/test_routewise_router.py` — must still pass after the comment edit at `routing/routers.py:679`.

### 4. Backend boots cold

```
uv run uvicorn serving.servers.app:app --host 127.0.0.1 --port 8765 --no-access-log &
# wait for "Application startup complete"
curl -fsS http://127.0.0.1:8765/healthz
curl -fsS http://127.0.0.1:8765/v1/models | jq '.data | length'
```
Expected: `/healthz` 200; `/v1/models` returns the active model list with no `gpt-5.4`, no `codex_*`, no `claude_sub_*` entries.

### 5. Live Claude path still works against staging

```
curl -fsS https://staging.freeinference.org/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <admin-key>" \
  -d '{"model":"claude-opus-4.7","messages":[{"role":"user","content":"hi"}],"max_tokens":16}'
```
Expected: 200 with assistant message — proves the cli-proxy path the active models depend on is still wired end-to-end.

### 6. Frontend smoke (mandatory)

Per CLAUDE.md ("test against staging") and the system prompt's UI-testing rule (we removed user-visible code paths — `isCodexModel` UI branch and the `claude_sub` / `codex_sub` filter rule):

- Log in to `https://staging.freeinference.org` as `admin@admin.com`.
- Open Models section: confirm `gpt-5.4` is gone; the visible model list is otherwise unchanged.
- Open Playground: pick `claude-opus-4.7`; confirm no reasoning-effort UI block appears (it was the only consumer); send a message; confirm a response.
- Browser console: no errors.

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Bootstrap warnings about missing accounts files removed; existing prod env vars (`CODEX_ACCOUNTS_FILE`, `CLAUDE_SUB_*`) become silently no-op. | Low | PR description documents that these env vars are no-ops post-merge; users should remove from their `.env`. |
| Stale `var/data/codex_accounts.json` / `claude_accounts.json` on staging/prod hosts. | Operational, not blocking | PR description: "After merge, remove these files from the host." |
| `bootstrap.py` becomes empty after edits. | Low | If both adapter checks were the only logic, delete the function and its single caller; verify no other callers via grep. |
| `serving/schemas.py` `reasoning_effort` field still used by other backends. | Verified | Only the comment changes; field stays. |
| Old PR descriptions / closed issues reference `gpt-5.4`. | Cosmetic | Not a code concern. |

## Rollback

Single PR, single revert commit. No data migration, no env-var rename, no schema change — pure code deletion. Reverting the merge restores all functionality byte-for-byte.

## Out of scope (explicit non-goals)

- Removing the `cli-proxy` upstream itself or the `${CLI_PROXY_*}` env vars (live Claude path).
- Removing `setup_claude_code.sh` or any end-user Claude Code documentation that points at `freeinference.org/anthropic`.
- Removing the Vertex AI `ClaudeAdapter` (`claude.py`) or `claude_format.py`.
- Renaming or refactoring `anthropic_messages.py`.
- Cleanup of `var/data/*_accounts.json` files on hosts (operational follow-up, flagged in PR description).
