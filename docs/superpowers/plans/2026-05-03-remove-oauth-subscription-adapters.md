# Remove OAuth Subscription Adapters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete all in-process OAuth subscription support (`codex_sub` + `claude_sub`) — adapters, shared account pool, scripts, config, frontend surfaces, and documentation — without touching the live Claude path that runs through the external `cli-proxy-api` service.

**Architecture:** Pure deletion. No new abstractions. The active Claude models in `config/models.yaml` use `provider: anthropic` with routes pointed at `${CLI_PROXY_BASE_URL}`; that path has zero imports from `claude_sub` / `claude_pool` / `claude_token`, so removing the OAuth stack cannot regress live traffic. The plan removes the orphaned code in two atomic adapter-removal tasks (codex first, then claude_sub), then mops up settings, scripts, config, frontend, harness, and docs.

**Tech Stack:** Python 3 (FastAPI gateway), TypeScript / Next.js (frontend), pytest, ruff, uv.

**Spec:** `docs/superpowers/specs/2026-05-03-remove-oauth-subscription-adapters-design.md` (commit it as the first commit on the implementation branch).

---

## Workflow conventions (from CLAUDE.md)

- Issue first, then PR.
- Pull `origin/dev` before starting.
- Work in worktree `.worktrees/remove-oauth-subs` on branch `jason/claude/remove-oauth-subs`.
- Subagent executes the work.
- `ruff format` before commit.
- PR to `dev`; monitor comments + CI every 2 min until green.
- After merge: delete branch and worktree.

## Verification cadence

After each task, run the relevant subset of:

- **Imports:** `cd .worktrees/remove-oauth-subs && uv run python -c "import serving.adapters; import serving.servers.bootstrap; import serving.servers.registry; print('OK')"`
- **Static grep:** `git grep -nI 'codex_sub\|codex_translator\|codex_token\|claude_sub\|claude_token\|claude_pool\|ClaudeSubscriptionAdapter\|CodexSubscriptionAdapter\|import_codex_auth\|import_claude_auth\|inspect_claude_accounts\|CODEX_BASE_URL' -- ':!docs/superpowers/' ':!test/unit/routing/test_routewise_router.py'`
- **Tests:** `uv run pytest test/unit -x --ff -q`
- **Frontend build:** `cd frontend && npm run build` (skip if no .ts/.tsx changed)

Final verification (Task 11) is more thorough — see that task.

---

## Task 0: Setup — issue, branch, worktree, spec commit

**Files:**
- Modify: `.worktrees/remove-oauth-subs` (new worktree)
- Move into branch: `docs/superpowers/specs/2026-05-03-remove-oauth-subscription-adapters-design.md`

- [ ] **Step 1: Pull dev**

```bash
cd /home/juncheng/hybridInference
git fetch origin
git checkout dev
git pull origin dev
```

Expected: working tree clean on `dev` at `origin/dev`.

- [ ] **Step 2: Verify the spec file exists in the working tree**

```bash
ls -la docs/superpowers/specs/2026-05-03-remove-oauth-subscription-adapters-design.md
```

Expected: file present (uncommitted from brainstorming session).

- [ ] **Step 3: Create the GitHub issue**

```bash
gh issue create \
  --title "Remove in-process OAuth subscription adapters (codex_sub + claude_sub)" \
  --body "$(cat <<'EOF'
Remove all in-process OAuth subscription support — `codex_sub` (ChatGPT) and `claude_sub` (Anthropic) — along with the shared OAuth account pool, scripts, config, frontend, and docs. The active Claude path through `cli-proxy-api` is untouched.

See spec: `docs/superpowers/specs/2026-05-03-remove-oauth-subscription-adapters-design.md` (will land in the implementation PR).

Scope: 15 file deletes + 22 file edits.

Out of scope:
- `cli-proxy` upstream and `${CLI_PROXY_*}` env vars (live Claude path)
- `scripts/setup_claude_code.sh` (end-user setup)
- Vertex AI `ClaudeAdapter` and `claude_format.py`
- Cleanup of `var/data/*_accounts.json` on hosts (operational follow-up)
EOF
)"
```

Capture the issue number from output (e.g., `#345`). Save for Task 10.

- [ ] **Step 4: Create the worktree and branch**

```bash
git worktree add .worktrees/remove-oauth-subs -b jason/claude/remove-oauth-subs
cd .worktrees/remove-oauth-subs
git status
```

Expected: working tree clean on `jason/claude/remove-oauth-subs`.

- [ ] **Step 5: Move the uncommitted spec and plan files into the worktree**

Both the design spec and this implementation plan were created in the main worktree's working tree. Copy them into the new worktree:

```bash
cp /home/juncheng/hybridInference/docs/superpowers/specs/2026-05-03-remove-oauth-subscription-adapters-design.md \
   /home/juncheng/hybridInference/.worktrees/remove-oauth-subs/docs/superpowers/specs/
cp /home/juncheng/hybridInference/docs/superpowers/plans/2026-05-03-remove-oauth-subscription-adapters.md \
   /home/juncheng/hybridInference/.worktrees/remove-oauth-subs/docs/superpowers/plans/
ls -la /home/juncheng/hybridInference/.worktrees/remove-oauth-subs/docs/superpowers/specs/2026-05-03-remove-oauth-subscription-adapters-design.md \
       /home/juncheng/hybridInference/.worktrees/remove-oauth-subs/docs/superpowers/plans/2026-05-03-remove-oauth-subscription-adapters.md
```

Expected: both files present in the worktree.

- [ ] **Step 6: Commit the spec and plan as the first commit on the branch**

```bash
cd /home/juncheng/hybridInference/.worktrees/remove-oauth-subs
git add docs/superpowers/specs/2026-05-03-remove-oauth-subscription-adapters-design.md \
        docs/superpowers/plans/2026-05-03-remove-oauth-subscription-adapters.md
git commit -m "$(cat <<'EOF'
docs(spec+plan): design and implementation plan for OAuth subscription adapter removal

Brainstormed design and step-by-step plan to remove codex_sub and
claude_sub adapters, the shared OAuth account pool, related scripts,
settings, frontend surfaces, and docs. The active Claude path via
cli-proxy is untouched.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 7: Clean up the spec and plan files from the main worktree**

Both files are now committed on the feature branch. Remove the stray copies from the main worktree:

```bash
cd /home/juncheng/hybridInference
rm docs/superpowers/specs/2026-05-03-remove-oauth-subscription-adapters-design.md
rm docs/superpowers/plans/2026-05-03-remove-oauth-subscription-adapters.md
git status
```

Expected: clean working tree on `dev`.

---

## Task 1: Delete codex_sub adapter and tests

**Files:**
- Delete: `serving/adapters/codex_sub.py`
- Delete: `serving/adapters/codex_translator.py`
- Delete: `serving/adapters/codex_token.py`
- Delete: `test/unit/adapters/test_codex_sub.py`
- Delete: `test/unit/adapters/test_codex_sub_fallback.py`
- Delete: `test/unit/adapters/test_codex_translator.py`
- Delete: `test/unit/adapters/test_codex_token.py`
- Modify: `serving/adapters/__init__.py`
- Modify: `serving/servers/registry.py`
- Modify: `serving/servers/bootstrap.py`

This task is atomic: deleting `codex_sub.py` without simultaneously updating `__init__.py`, `registry.py`, and `bootstrap.py` will produce import errors. All edits must land in one commit.

- [ ] **Step 1: Delete the adapter modules and their tests**

```bash
cd /home/juncheng/hybridInference/.worktrees/remove-oauth-subs
git rm serving/adapters/codex_sub.py
git rm serving/adapters/codex_translator.py
git rm serving/adapters/codex_token.py
git rm test/unit/adapters/test_codex_sub.py
git rm test/unit/adapters/test_codex_sub_fallback.py
git rm test/unit/adapters/test_codex_translator.py
git rm test/unit/adapters/test_codex_token.py
```

Note: `codex_token.py` was the home of the shared `AccountPool` used by `claude_sub`. After this step, `claude_sub.py` will fail to import. That import is fixed in Task 2 (which deletes `claude_sub` itself). The intermediate state is broken — that's why Task 1 and Task 2 must each be committed as atomic units, but the test gate runs after Task 2.

- [ ] **Step 2: Update `serving/adapters/__init__.py` — drop CodexSubscriptionAdapter export**

Edit `serving/adapters/__init__.py`. Remove the line:

```python
from .codex_sub import CodexSubscriptionAdapter
```

And remove `"CodexSubscriptionAdapter",` from the `__all__` list.

The post-edit file should be:

```python
from .anthropic import AnthropicAdapter
from .base import BaseAdapter, ModelConfig, UsageInfo
from .claude import ClaudeAdapter
from .claude_sub import ClaudeSubscriptionAdapter
from .gemini import GeminiAdapter
from .openai_compat import OpenAICompatAdapter
from .openrouter import OpenRouterAdapter

__all__ = [
    "AnthropicAdapter",
    "BaseAdapter",
    "ClaudeAdapter",
    "ClaudeSubscriptionAdapter",
    "GeminiAdapter",
    "ModelConfig",
    "OpenAICompatAdapter",
    "OpenRouterAdapter",
    "UsageInfo",
]
```

(Task 2 will delete the `ClaudeSubscriptionAdapter` line.)

- [ ] **Step 3: Update `serving/servers/registry.py` — drop CodexSubscriptionAdapter import and dispatch branch**

In `serving/servers/registry.py`:

(a) In the imports near line 20, remove `CodexSubscriptionAdapter,`. The import block should become:

```python
from serving.adapters import (
    AnthropicAdapter,
    ClaudeAdapter,
    ClaudeSubscriptionAdapter,
    GeminiAdapter,
    ModelConfig,
    OpenAICompatAdapter,
    OpenRouterAdapter,
)
```

(b) Around line 189, delete the two-line dispatch branch:

```python
    if kind == "codex_sub":
        return CodexSubscriptionAdapter(model_cfg)
```

The surrounding context after the edit should read:

```python
    if kind == "claude":
        return ClaudeAdapter(model_cfg)
    if kind == "gemini":
        return GeminiAdapter(model_cfg)
    if kind == "claude_sub":
        return ClaudeSubscriptionAdapter(model_cfg)
    if kind == "anthropic":
        return AnthropicAdapter(model_cfg)
```

(Task 2 will delete the `claude_sub` branch.)

- [ ] **Step 4: Update `serving/servers/bootstrap.py` — drop CodexSubscriptionAdapter import and codex branch**

In `serving/servers/bootstrap.py`:

(a) Edit the import at line 20 from:

```python
from serving.adapters import ClaudeSubscriptionAdapter, CodexSubscriptionAdapter
```

to:

```python
from serving.adapters import ClaudeSubscriptionAdapter
```

(b) In `_warn_missing_subscription_files` around lines 85–111, drop the codex flag, the codex `isinstance` arm, and the codex warning. The function should become:

```python
def _warn_missing_subscription_files(router: RouteExecutor) -> None:
    """Warn at startup if subscription adapters are configured but accounts files are missing."""
    needs_claude = False

    for route in router.routes.values():
        for adapter, _ in route.adapters:
            if isinstance(adapter, ClaudeSubscriptionAdapter):
                needs_claude = True

    if not needs_claude:
        return

    settings = get_settings()

    if needs_claude and not Path(settings.claude_sub_accounts_file).exists():
        logger.warning(
            f"claude_sub route configured but accounts file not found: "
            f"{settings.claude_sub_accounts_file} — requests will fail until credentials are imported"
        )
```

(Task 2 will gut the rest of this function.)

- [ ] **Step 5: Stage edits (do not commit until Task 2 lands the matching claude_sub edits)**

```bash
git add serving/adapters/__init__.py serving/servers/registry.py serving/servers/bootstrap.py
git status
```

Expected: 7 deleted files + 3 modified files, all staged.

> **Important:** Do **not** commit between Task 1 and Task 2. Together they form one atomic "remove subscription adapters" change. Committing between them would push a broken intermediate state to the branch.

---

## Task 2: Delete claude_sub adapter and tests

**Files:**
- Delete: `serving/adapters/claude_sub.py`
- Delete: `serving/adapters/claude_pool.py`
- Delete: `serving/adapters/claude_token.py`
- Delete: `test/unit/adapters/test_claude_sub.py`
- Modify: `serving/adapters/__init__.py`
- Modify: `serving/servers/registry.py`
- Modify: `serving/servers/bootstrap.py`

- [ ] **Step 1: Delete the adapter modules and the test**

```bash
cd /home/juncheng/hybridInference/.worktrees/remove-oauth-subs
git rm serving/adapters/claude_sub.py
git rm serving/adapters/claude_pool.py
git rm serving/adapters/claude_token.py
git rm test/unit/adapters/test_claude_sub.py
```

- [ ] **Step 2: Update `serving/adapters/__init__.py` — drop ClaudeSubscriptionAdapter export**

In `serving/adapters/__init__.py`, remove the line:

```python
from .claude_sub import ClaudeSubscriptionAdapter
```

And remove `"ClaudeSubscriptionAdapter",` from `__all__`. The final file should be:

```python
from .anthropic import AnthropicAdapter
from .base import BaseAdapter, ModelConfig, UsageInfo
from .claude import ClaudeAdapter
from .gemini import GeminiAdapter
from .openai_compat import OpenAICompatAdapter
from .openrouter import OpenRouterAdapter

__all__ = [
    "AnthropicAdapter",
    "BaseAdapter",
    "ClaudeAdapter",
    "GeminiAdapter",
    "ModelConfig",
    "OpenAICompatAdapter",
    "OpenRouterAdapter",
    "UsageInfo",
]
```

- [ ] **Step 3: Update `serving/servers/registry.py` — drop ClaudeSubscriptionAdapter import and dispatch branch**

In `serving/servers/registry.py`:

(a) Remove `ClaudeSubscriptionAdapter,` from the import block. After Task 1 + Task 2 edits, the import block should be:

```python
from serving.adapters import (
    AnthropicAdapter,
    ClaudeAdapter,
    GeminiAdapter,
    ModelConfig,
    OpenAICompatAdapter,
    OpenRouterAdapter,
)
```

(b) Delete the dispatch branch:

```python
    if kind == "claude_sub":
        return ClaudeSubscriptionAdapter(model_cfg)
```

After Task 1 + Task 2 edits, the surrounding dispatch block should be:

```python
    if kind == "claude":
        return ClaudeAdapter(model_cfg)
    if kind == "gemini":
        return GeminiAdapter(model_cfg)
    if kind == "anthropic":
        return AnthropicAdapter(model_cfg)
```

- [ ] **Step 4: Update `serving/servers/bootstrap.py` — delete the now-empty subscription warner**

In `serving/servers/bootstrap.py`:

(a) Delete the `from serving.adapters import ClaudeSubscriptionAdapter` line at line 20.

(b) Delete the entire `_warn_missing_subscription_files` function (which after Task 1 only checks `claude_sub` and is now also dead).

(c) Delete the call to `_warn_missing_subscription_files(router)` at the end of `_init_router_and_models` (it's the second-to-last line of the function, just before `return embedding_adapters, model_infos`).

(d) Verify no other reference. Run:

```bash
git grep -n '_warn_missing_subscription_files\|ClaudeSubscriptionAdapter\|CodexSubscriptionAdapter' serving/
```

Expected: empty output.

- [ ] **Step 5: Stage and commit Task 1 + Task 2 together**

```bash
git add serving/adapters/__init__.py serving/servers/registry.py serving/servers/bootstrap.py
git status
```

Expected: 11 deleted files + 3 modified files staged.

```bash
git commit -m "$(cat <<'EOF'
refactor: remove in-process OAuth subscription adapters

Delete codex_sub and claude_sub adapters, the shared OAuth account pool
(codex_token), the Claude credential provider (claude_token), the
process-wide pool singleton (claude_pool), and all tests for these
modules. Update the adapter package exports, registry dispatch, and
bootstrap startup-warning to drop the now-dead branches.

The live Claude path through cli-proxy via provider: anthropic is
unaffected (no model in config/models.yaml uses kind: codex_sub or
kind: claude_sub).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6: Verify imports and tests still pass**

```bash
uv run python -c "import serving.adapters; import serving.servers.bootstrap; import serving.servers.registry; print('OK')"
```

Expected: `OK`.

```bash
uv run pytest test/unit -x --ff -q
```

Expected: all green. Note: 11 fewer tests should run vs before (the deleted test files).

If the import smoke fails, the most likely cause is a stray `claude_sub_*` setting referenced by another module. Run:

```bash
git grep -n 'claude_sub_\|codex_token\|codex_sub\|claude_pool' -- ':!docs/' ':!.git/'
```

and resolve before proceeding. (Settings cleanup is Task 3 — if `bootstrap.py:claude_sub_accounts_file` reference still exists, it should be present until Task 3.)

---

## Task 3: Settings, schemas, and stale comment cleanup

**Files:**
- Modify: `serving/config/settings.py` (drop 10 fields)
- Modify: `serving/schemas.py` (line 65)
- Modify: `routing/routers.py` (line 679)
- Modify: `serving/adapters/claude.py` (line 14)

- [ ] **Step 1: Drop subscription settings from `serving/config/settings.py`**

In `serving/config/settings.py`, delete lines 100–112 (the entire codex + claude_sub blocks). Replace this region:

```python
    # Codex subscription
    codex_accounts_file: str = "var/data/codex_accounts.json"
    codex_fallback_api_key: str = ""
    codex_token_refresh_margin: int = 30
    codex_account_cooldown: int = 60
    codex_failure_threshold: int = 3

    # Claude subscription
    claude_sub_accounts_file: str = "var/data/claude_accounts.json"
    claude_sub_fallback_api_key: str = ""
    claude_sub_token_refresh_margin: int = 300  # 5 min (tokens last ~1 hour)
    claude_sub_account_cooldown: int = 60
    claude_sub_failure_threshold: int = 3

    # Provider quota cookies (admin dashboard "Providers" tab)
```

with just the trailing comment + following content:

```python
    # Provider quota cookies (admin dashboard "Providers" tab)
```

(i.e., delete the 13 lines from `# Codex subscription` through the blank line after `claude_sub_failure_threshold: int = 3`.)

- [ ] **Step 2: Edit `serving/schemas.py:65` — generic reasoning_effort comment**

Change line 65 from:

```python
    # Reasoning effort (for Codex models: "low", "medium", "high")
```

to:

```python
    # Reasoning effort: "low", "medium", "high"
```

The field itself stays (used by other adapters that accept reasoning_effort).

- [ ] **Step 3: Edit `routing/routers.py:679` — generic comment**

Change line 679 from:

```python
            # Preserve adapter-set _routing (e.g. codex_sub fallback overrides);
            # only set default routing if the adapter didn't provide one.
```

to:

```python
            # Preserve adapter-set _routing if present;
            # only set default routing if the adapter didn't provide one.
```

- [ ] **Step 4: Edit `serving/adapters/claude.py:13-14` — drop stale ClaudeSubscriptionAdapter docstring reference**

Change lines 13–14 of `serving/adapters/claude.py` from:

```python
Format translation (OpenAI ↔ Claude Messages API) is shared with
ClaudeSubscriptionAdapter via the ``claude_format`` module.
```

to:

```python
Format translation (OpenAI ↔ Claude Messages API) is shared with the
direct ``AnthropicAdapter`` via the ``claude_format`` module.
```

- [ ] **Step 5: Verify, format, and commit**

```bash
git grep -n 'codex_accounts_file\|codex_fallback_api_key\|codex_token_refresh_margin\|codex_account_cooldown\|codex_failure_threshold\|claude_sub_accounts_file\|claude_sub_fallback_api_key\|claude_sub_token_refresh_margin\|claude_sub_account_cooldown\|claude_sub_failure_threshold' -- ':!docs/superpowers/'
```

Expected: empty.

```bash
uv run ruff format serving/config/settings.py serving/schemas.py routing/routers.py serving/adapters/claude.py
uv run python -c "from serving.config.settings import get_settings; s = get_settings(); print('OK')"
uv run pytest test/unit -x --ff -q
```

Expected: each green.

```bash
git add serving/config/settings.py serving/schemas.py routing/routers.py serving/adapters/claude.py
git commit -m "$(cat <<'EOF'
chore: scrub subscription-adapter references from settings and comments

Remove 10 codex_*/claude_sub_* settings (no consumers after the adapters
were deleted), generalize the reasoning_effort comment in schemas.py and
the routing comment in routers.py, and update the Vertex ClaudeAdapter
docstring to point at AnthropicAdapter (the surviving claude_format
sibling).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Delete subscription import/inspection scripts

**Files:**
- Delete: `scripts/import_codex_auth.py`
- Delete: `scripts/import_claude_auth.py`
- Delete: `scripts/inspect_claude_accounts.py`

- [ ] **Step 1: Delete the scripts**

```bash
cd /home/juncheng/hybridInference/.worktrees/remove-oauth-subs
git rm scripts/import_codex_auth.py
git rm scripts/import_claude_auth.py
git rm scripts/inspect_claude_accounts.py
```

- [ ] **Step 2: Verify nothing references them**

```bash
git grep -n 'import_codex_auth\|import_claude_auth\|inspect_claude_accounts' -- ':!docs/'
```

Expected: empty (docs references are removed in Task 8).

- [ ] **Step 3: Commit**

```bash
git status
git commit -m "$(cat <<'EOF'
chore: delete OAuth subscription import/inspection scripts

import_codex_auth.py, import_claude_auth.py, and inspect_claude_accounts.py
all targeted the deleted in-process subscription adapters and the now-orphaned
var/data/*_accounts.json files. The end-user setup_claude_code.sh stays —
it configures the Claude Code CLI to point at freeinference.org/anthropic
and is unrelated to the OAuth pool.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Config — models.yaml + .env.example

**Files:**
- Modify: `config/models.yaml` (lines 482–515)
- Modify: `.env.example` (line 37)

- [ ] **Step 1: Strip the Codex Subscription Models block from `config/models.yaml`**

In `config/models.yaml`, delete lines 482–515 (everything from `# --- Codex Subscription Models …` through the blank line before `# ── Anthropic direct …`). The exact block to remove is:

```yaml
  # --- Codex Subscription Models (uncomment when accounts are configured) ---
  # Requires: var/data/codex_accounts.json with valid OAuth credentials.
  # See docs/codex-subscription-design.md for setup instructions.
  #
  # Reasoning effort is controlled via the `reasoning_effort` request param
  # ("low", "medium", "high") — no separate model entries needed per effort level.
  #
  - id: gpt-5.4
    name: GPT-5.4
    provider: codex_sub
    required_role: internal
    provider_model_id: "gpt-5.4"
    base_url: ${CODEX_BASE_URL}
    quantization: "none"
    input_modalities: ["text", "image"]
    output_modalities: ["text"]
    context_length: 1050000
    max_output_length: 128000
    supports_tools: true
    supports_structured_output: true
    supported_params: [temperature, max_tokens, stop, tools, tool_choice, reasoning_effort]
    # OpenAI API https://openai.com/api/pricing/ (GPT-5.4)
    pricing:
      prompt: "2.50"
      completion: "15.00"
      image: "0"
      request: "0"
      input_cache_reads: "0.25"
      input_cache_writes: "0"
    route:
      - kind: codex_sub
        weight: 1.0
        base_url: ${CODEX_BASE_URL}

```

After the deletion, the line `  # ── Anthropic direct (kind: anthropic) ─────────────────────` should immediately follow line 481 (`        provider_model_id: "MiniMaxAI/MiniMax-M2.5"`) with one blank line between them.

- [ ] **Step 2: Strip CODEX_BASE_URL from `.env.example`**

In `.env.example`, delete line 37:

```
CODEX_BASE_URL=https://chatgpt.com/backend-api/codex
```

Also delete the blank line that immediately follows (line 38), so the file flows from `MINIMAX_GROUP_ID=` directly into `# Local self-hosted endpoints …`.

- [ ] **Step 3: Verify**

```bash
git grep -n 'codex_sub\|CODEX_BASE_URL\|gpt-5\.4\|gpt-5\.3-codex\|gpt-5\.2-codex\|gpt-5\.1-codex' config/models.yaml .env.example
```

Expected: empty.

```bash
uv run python -c "import yaml; yaml.safe_load(open('config/models.yaml')); print('models.yaml parses OK')"
```

Expected: `models.yaml parses OK`.

- [ ] **Step 4: Commit**

```bash
git add config/models.yaml .env.example
git commit -m "$(cat <<'EOF'
chore(config): drop codex subscription model and CODEX_BASE_URL env

Remove the gpt-5.4 entry and its codex_sub route from models.yaml (the
adapter is gone) and remove the CODEX_BASE_URL env from .env.example.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Frontend — drop codex/claude_sub references and dead reasoningEffort state

**Files:**
- Modify: `frontend/src/components/features/dashboard/ModelsSection.tsx` (line 31)
- Modify: `frontend/src/app/dashboard/playground/page.tsx` (lines 50, 63, 116, 279, 372, 524–525, 571, 583, 586–588, 591–619, 783–784)

The codex playground UI was the only setter for `reasoningEffort`. With it gone, the entire `reasoningEffort` state becomes dead. Per the spec, drop it cleanly.

- [ ] **Step 1: Edit `ModelsSection.tsx` — remove subscription filter**

In `frontend/src/components/features/dashboard/ModelsSection.tsx`, delete line 31:

```tsx
  if (p === 'codex_sub' || p === 'claude_sub') return false;
```

The function should now be:

```tsx
export function isDashboardModelVisible(
  model: ModelCatalogItem,
  showInternalModels: boolean,
): boolean {
  const p = model.owned_by.toLowerCase();
  if (!showInternalModels) {
    if (p.includes('openai')) return false;
    if (p.includes('anthropic')) return false;
  }
  return true;
}
```

- [ ] **Step 2: Edit `playground/page.tsx` — remove `reasoningEffort` from the session type**

Delete line 50:

```tsx
  reasoningEffort: ReasoningEffort;
```

Then check whether the `ReasoningEffort` type (likely defined just above) has any other consumers in this file:

```bash
grep -n 'ReasoningEffort' frontend/src/app/dashboard/playground/page.tsx
```

If the type appears only in its own definition + the deleted line 50, also delete the type definition. (If used elsewhere, leave it.)

- [ ] **Step 3: Edit `playground/page.tsx` — drop the default value**

Delete the `reasoningEffort: null,` line (currently line 63 in the session-default block).

- [ ] **Step 4: Edit `playground/page.tsx` — drop the read accessor**

Delete line 116:

```tsx
  const reasoningEffort = session?.reasoningEffort ?? null;
```

- [ ] **Step 5: Edit `playground/page.tsx` — drop `isCodexModel` derived flag**

Delete line 119:

```tsx
  const isCodexModel = model?.provider === 'codex_sub';
```

- [ ] **Step 6: Edit `playground/page.tsx` — drop `reasoning_effort` from request body**

Delete line 279:

```tsx
          ...(reasoningEffort ? { reasoning_effort: reasoningEffort } : {}),
```

- [ ] **Step 7: Edit `playground/page.tsx` — drop the export/restore reference**

Around line 372, locate the line `    reasoningEffort,` (likely inside an object literal that bundles the session for save/restore). Delete it.

- [ ] **Step 8: Edit `playground/page.tsx` — simplify the model-switch handler**

Around lines 520–526, the patch closure includes:

```tsx
                      patch((s) => ({
                        ...s,
                        selectedModelId: newId,
                        selectedProvider: null,
                        reasoningEffort:
                          newModel?.provider === 'codex_sub' ? s.reasoningEffort : null,
                      }));
```

Drop the `reasoningEffort:` property entirely. The result:

```tsx
                      patch((s) => ({
                        ...s,
                        selectedModelId: newId,
                        selectedProvider: null,
                      }));
```

The unused `newModel` lookup can also be removed if it's no longer referenced after this edit (it was only used by the codex branch); check the line directly before the `patch(...)` call.

- [ ] **Step 9: Edit `playground/page.tsx` — simplify temperature-display block**

Around lines 568–588, the temperature slider has reasoningEffort-coupled state. Replace the block:

```tsx
                      {reasoningEffort ? '--' : temp.toFixed(1)}
```

with:

```tsx
                      {temp.toFixed(1)}
```

Replace:

```tsx
                    disabled={streaming || !!reasoningEffort}
```

with:

```tsx
                    disabled={streaming}
```

Delete the trailing reasoning-active note:

```tsx
                  {reasoningEffort && (
                    <p className="mt-1 text-xs text-gray-600">Disabled while reasoning is active</p>
                  )}
```

- [ ] **Step 10: Edit `playground/page.tsx` — delete the codex reasoning-effort UI block**

Delete the entire block at lines 591–619:

```tsx
                {isCodexModel && (
                  <div>
                    <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-gray-500">
                      Reasoning effort
                    </label>
                    <div className="flex rounded-lg border border-gray-700 bg-gray-800">
                      {([null, 'low', 'medium', 'high'] as const).map((level) => {
                        const label =
                          level === null ? 'None' : level.charAt(0).toUpperCase() + level.slice(1);
                        const active = reasoningEffort === level;
                        return (
                          <button
                            key={label}
                            type="button"
                            onClick={() => patch((s) => ({ ...s, reasoningEffort: level }))}
                            disabled={streaming}
                            className={`flex-1 px-2 py-1.5 text-xs font-medium transition first:rounded-l-md last:rounded-r-md disabled:opacity-50 ${
                              active
                                ? 'bg-indigo-600 text-white'
                                : 'text-gray-400 hover:bg-gray-700 hover:text-gray-200'
                            }`}
                          >
                            {label}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}
```

- [ ] **Step 11: Edit `playground/page.tsx` — drop the "reasoning: …" toolbar parts**

Around lines 783–784, delete:

```tsx
                        if (reasoningEffort) {
                          parts.push(`reasoning: ${reasoningEffort}`);
                        }
```

- [ ] **Step 12: Final sweep for any remaining `reasoningEffort` reference in the file**

```bash
grep -n 'reasoningEffort\|isCodexModel\|reasoning_effort' frontend/src/app/dashboard/playground/page.tsx
```

Expected: empty. If anything remains, it's likely a missed inline reference — remove it.

- [ ] **Step 13: Type-check + build the frontend**

```bash
cd frontend
npm run build
```

Expected: build succeeds, no TypeScript errors. (If the `ReasoningEffort` type is referenced by other files, the build will surface it; resolve.)

- [ ] **Step 14: Commit**

```bash
cd /home/juncheng/hybridInference/.worktrees/remove-oauth-subs
git add frontend/src/components/features/dashboard/ModelsSection.tsx frontend/src/app/dashboard/playground/page.tsx
git commit -m "$(cat <<'EOF'
feat(frontend): drop codex/claude_sub references from dashboard and playground

Remove the codex_sub/claude_sub filter rule in ModelsSection (neither
provider exists anymore) and strip the entire reasoningEffort state
surface from the playground (the codex reasoning-effort UI block was
the only setter; with codex_sub gone, the state is dead).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Harness configs — drop codex models, scenarios, and fixture

**Files:**
- Delete: `freeinference-harness/configs/fixtures/tools-codex-3.yaml`
- Modify: `freeinference-harness/configs/targets/freeinference.yaml`
- Modify: `freeinference-harness/configs/scenarios/tool-call-core.yaml`

- [ ] **Step 1: Delete the codex fixture**

```bash
git rm freeinference-harness/configs/fixtures/tools-codex-3.yaml
```

- [ ] **Step 2: Strip codex/gpt-5.x model entries from `freeinference.yaml`**

In `freeinference-harness/configs/targets/freeinference.yaml`, delete the model entries between lines ~104 and ~167. Specifically, delete the five entries with names `gpt-5.4-admin`, `gpt-5.3-codex-admin`, `gpt-5.2-codex-admin`, `gpt-5.2-admin`, `gpt-5.1-codex-admin`. Each is a 13-line YAML block; the deleted region is roughly:

```yaml
  - name: gpt-5.4-admin
    model: gpt-5.4
    suite_type: gateway-public
    capabilities:
      chat: true
      streaming: true
      tools: true
      structured_output: true
      embeddings: false
      anthropic_messages: false
      admin_required: true
    tags: [codex, admin]

  - name: gpt-5.3-codex-admin
    model: gpt-5.3-codex
    ... (same structure)
    tags: [codex, admin]

  - name: gpt-5.2-codex-admin
    model: gpt-5.2-codex
    ... (same structure)
    tags: [codex, admin]

  - name: gpt-5.2-admin
    model: gpt-5.2
    ... (same structure)
    tags: [codex, admin]

  - name: gpt-5.1-codex-admin
    model: gpt-5.1-codex
    ... (same structure)
    tags: [codex, admin]
```

After deletion, the section should flow directly from `tags: [minimax, public]` (line 101) to `  - name: claude-sonnet-4.6-admin` (was line 169).

Also remove the `# Admin-only chat models` comment (line 103) only if **all** admin-only entries beneath it were codex-related — they aren't; `claude-*-admin` entries follow, so keep the comment.

- [ ] **Step 3: Strip codex scenarios from `tool-call-core.yaml`**

In `freeinference-harness/configs/scenarios/tool-call-core.yaml`, delete the four scenarios that use `tools_fixture: tools-codex-3.yaml`:

(a) `forced_tool_codex_stream` (lines ~14–21)
(b) `forced_tool_codex_nonstream` (lines ~51–58)
(c) `auto_tool_codex_stream` (lines ~71–77)
(d) `multi_turn_codex_stream` (lines ~89–96)

After deletion, the surviving scenarios should still parse as a valid YAML list. Verify with:

```bash
uv run python -c "import yaml; data = yaml.safe_load(open('freeinference-harness/configs/scenarios/tool-call-core.yaml')); print(f\"{len(data['scenarios'])} scenarios remain\")"
```

Expected: `7 scenarios remain` (was 11; minus 4 = 7).

- [ ] **Step 4: Verify nothing else references the removed harness items**

```bash
git grep -n 'gpt-5\.4\|gpt-5\.3-codex\|gpt-5\.2-codex\|gpt-5\.2\b\|gpt-5\.1-codex\|tools-codex-3' freeinference-harness/
```

Expected: empty.

- [ ] **Step 5: Commit**

```bash
git add freeinference-harness/configs/targets/freeinference.yaml freeinference-harness/configs/scenarios/tool-call-core.yaml
git commit -m "$(cat <<'EOF'
chore(harness): drop codex targets, scenarios, and fixture

Remove gpt-5.x-codex* admin-only model entries from the freeinference
target file, the four codex tool-call scenarios, and the
tools-codex-3.yaml fixture. None route to a live model after the
codex_sub adapter removal.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Docs — strip codex/claude_sub/Codex-CLI references

**Files (9):**
- Modify: `docs/free_inference/README.md`
- Modify: `docs/free_inference/docs/source/integrations.md`
- Modify: `docs/free_inference/docs/source/quickstart.md`
- Modify: `docs/free_inference/docs/source/models.md`
- Modify: `docs/source/developer/architecture.md`
- Modify: `docs/source/developer/configuration.md`
- Modify: `docs/source/developer/adding-models.md`
- Modify: `docs/source/developer/openrouter.md`
- Modify: `docs/source/developer/deployment.md`

- [ ] **Step 1: `docs/free_inference/README.md` — drop Codex bullet and Codex Setup section**

In `docs/free_inference/README.md`:

(a) Delete line 17:

```markdown
- **[Codex](https://github.com/openai/codex)** - Terminal-based coding assistant
```

(b) Delete the entire "Codex Setup" section (lines ~33–55), starting at:

```markdown
### Codex Setup

1. Create `~/.codex/config.toml`:
```

through (and including) the line `3. Reload: `source ~/.zshrc`` and the blank line that follows. The next section "### Roo Code / Kilo Code Setup" should immediately follow "### Cursor Setup" (with one blank line between).

Also fix the overview paragraph: line 7 currently mentions Codex among supported clients. Change:

```markdown
FreeInference provides free access to state-of-the-art language models specifically designed for coding agents like Cursor, Codex, Roo Code, and other AI-powered development tools.
```

to:

```markdown
FreeInference provides free access to state-of-the-art language models specifically designed for coding agents like Cursor, Roo Code, and other AI-powered development tools.
```

- [ ] **Step 2: `docs/free_inference/docs/source/integrations.md` — strip the entire Codex section**

In `docs/free_inference/docs/source/integrations.md`:

(a) Delete the top-level Codex section (lines ~7–57): the `## Codex` heading, its description, the configuration steps, and the closing `---` separator. The file should flow from `# IDE & Coding Agent Integrations` directly into `## Cursor`.

(b) Delete the Codex-Specific Issues subsection (lines ~218–231):

```markdown
### Codex-Specific Issues

**Environment variables not loaded:**
- Make sure you've reloaded your shell configuration after editing `~/.zshrc` or `~/.bashrc`
- Verify variables are set: `echo $FREEINFERENCE_API_KEY`
- Open a new terminal window to ensure variables are loaded

**Session ID issues:**
- The session ID is auto-generated each time you start a new shell
- If needed, you can manually set it: `export CODEX_SESSION_ID="custom-session-id"`

**Config file not found:**
- Ensure the directory exists: `mkdir -p ~/.codex`
- Check file permissions: `ls -la ~/.codex/config.toml`
```

The "### Cursor-Specific Issues" subsection should follow "### Model Not Found" with no Codex content between them.

- [ ] **Step 3: `docs/free_inference/docs/source/quickstart.md` — drop Codex Setup step**

In `docs/free_inference/docs/source/quickstart.md`, delete the "### Codex" subsection (lines ~22–44, from `### Codex` through `[Detailed setup →](integrations.md)` and the blank line after). The "### Roo Code / Kilo Code" section should follow "### Cursor" directly.

- [ ] **Step 4: `docs/free_inference/docs/source/models.md` — drop GPT-5.4 entry and Codex switcher block**

In `docs/free_inference/docs/source/models.md`:

(a) Delete the GPT-5.4 entry (lines ~159–170):

```markdown
### GPT-5.4

**Model ID:** `gpt-5.4`

- Context length: 1,050,000 tokens
- Max output: 128,000 tokens
- Input modalities: text, image
- Output modalities: text
- Function calling: Yes
- Structured output: Yes

---
```

The "### Claude Sonnet 4.6" entry should follow the "## Internal Models" intro line directly.

(b) Delete the Codex switcher entry from "## Switching Models" (lines ~202–205):

```markdown
**Codex:** Edit `~/.codex/config.toml`:
```toml
model = "glm-5"  # Change to any model ID
```
```

(Including the closing triple-backtick.)

- [ ] **Step 5: `docs/source/developer/architecture.md` — drop Subscription Adapters section and stale adapter mentions**

In `docs/source/developer/architecture.md`:

(a) Delete the entire `### Subscription Adapters` section (lines ~92–129) — from the `### Subscription Adapters` heading through the line before `### Northbound API Surfaces`.

(b) Update line ~32 (the Adapters key-component bullet) from:

```markdown
- **Adapters (`serving.adapters.*`)**: Translate requests to providers — `OpenAICompatAdapter` (vLLM, SGLang, Ollama, DeepSeek, Zhipu, MiniMax, Chutes, Featherless), `OpenRouterAdapter`, `GeminiAdapter`, `AnthropicAdapter`, `ClaudeAdapter`, plus subscription pools (`ClaudeSubscriptionAdapter`, `CodexSubscriptionAdapter`).
```

to:

```markdown
- **Adapters (`serving.adapters.*`)**: Translate requests to providers — `OpenAICompatAdapter` (vLLM, SGLang, Ollama, DeepSeek, Zhipu, MiniMax, Chutes, Featherless), `OpenRouterAdapter`, `GeminiAdapter`, `AnthropicAdapter`, `ClaudeAdapter` (Vertex).
```

- [ ] **Step 6: `docs/source/developer/configuration.md` — drop subscription kinds and §5.4**

In `docs/source/developer/configuration.md`:

(a) Edit line 80, removing `, claude_sub, codex_sub` from the supported-kinds list. The line should become:

```markdown
- `provider`: Determines adapter type. Supported kinds (dispatched in `serving/servers/registry.py:_make_adapter`): `openai_compat`, `vllm`, `sglang`, `ollama`, `chutes`, `featherless`, `deepseek`, `zhipu`, `minimax`, `openrouter` (also `openrouter[<slug>]` to pin a sub-provider), `gemini`, `claude`, `anthropic`. See [adding-models.md](adding-models.md) for the full reference table.
```

(b) Delete the entire `### 5.4 Codex Subscription` subsection (lines ~298–317), from the `### 5.4 Codex Subscription` heading through the line `Codex currently exposes only the OpenAI-compatible northbound surface (\`POST /v1/chat/completions\`); there is no separate Codex-native public route yet.` and the blank line after. The "## 6. FAQ" section should follow.

(c) Check the surrounding context — if there's a `### 5.x Claude Subscription` subsection, delete that too. (Run `grep -n "Claude Subscription\|claude_sub" docs/source/developer/configuration.md` to identify any remaining content, then remove.)

- [ ] **Step 7: `docs/source/developer/adding-models.md` — drop subscription rows**

In `docs/source/developer/adding-models.md`:

(a) Edit line 288 — strip "Codex/Claude OAuth subscriptions" from the list of "non-OpenAI wire format" examples. Change:

```markdown
**B) Genuinely custom protocols.** If the provider speaks a non-OpenAI wire format (e.g., Gemini's `generateContent`, the Anthropic Messages API, Codex/Claude OAuth subscriptions, OpenRouter's provider-pinning header), add a dedicated adapter class and a dispatch branch:
```

to:

```markdown
**B) Genuinely custom protocols.** If the provider speaks a non-OpenAI wire format (e.g., Gemini's `generateContent`, the Anthropic Messages API, OpenRouter's provider-pinning header), add a dedicated adapter class and a dispatch branch:
```

(b) Edit line 295 — drop `claude_sub, codex_sub, ` from the example pattern list. Change:

```markdown
`gemini`, `claude`, `anthropic`, `claude_sub`, `codex_sub`, and `openrouter` all follow this pattern.
```

to:

```markdown
`gemini`, `claude`, `anthropic`, and `openrouter` all follow this pattern.
```

(c) Delete lines 434–435 from the kind reference table:

```markdown
| `claude_sub` | Subscription | Claude via OAuth account pool — see §5 of configuration.md |
| `codex_sub` | Subscription | Codex CLI via OAuth account pool — see §5 of configuration.md |
```

The table should end at the `anthropic` row.

- [ ] **Step 8: `docs/source/developer/openrouter.md` — drop codex_sub.py from the code-tree comment**

In `docs/source/developer/openrouter.md`, edit line 18 from:

```markdown
│   │                           #   claude_sub.py, codex_sub.py, plus shared profiles.py
```

to:

```markdown
│   │                           #   plus shared profiles.py
```

If the surrounding lines also reference `claude_sub.py`, scrub them too. Run `grep -n 'claude_sub\|codex_sub' docs/source/developer/openrouter.md` to confirm clean.

- [ ] **Step 9: `docs/source/developer/deployment.md` — strip subscription account management section**

In `docs/source/developer/deployment.md`, delete the entire `## Subscription Account Management` section. It runs from line ~103 (`## Subscription Account Management`) through the end of the "Verify Anthropic-native surface" subsection (which depends on subscription routing). Specifically, delete:

```markdown
## Subscription Account Management

If you use Claude or Codex subscription adapters (OAuth-based), accounts must be imported separately from the standard `.env` API keys.

Run these scripts from the project root on the **host machine**, not inside the backend container. They read credentials from the host user's home directory (`~/.claude/`, `~/.codex/`) and write into the project workspace under `var/data/`.

### Import Claude credentials
... (etc, through "### Verify Anthropic-native surface" if it depends on subscription routing)
```

To find the exact end of the section, look for the next top-level `##` heading that isn't subscription-related. Run:

```bash
grep -n '^## ' docs/source/developer/deployment.md
```

Delete from `## Subscription Account Management` up to (but not including) the next `## ` heading. If "Verify Anthropic-native surface" is a subsection (`###`), it's part of the deletion. If it's a top-level (`##`) section that documents the live surface (not subscription-routing), keep it but remove any reference to subscription routing.

(Inspect the file when editing to make the right cut.)

- [ ] **Step 10: Verify all docs**

```bash
git grep -n 'codex_sub\|codex_translator\|codex_token\|claude_sub\|claude_token\|claude_pool\|ClaudeSubscriptionAdapter\|CodexSubscriptionAdapter\|import_codex_auth\|import_claude_auth\|inspect_claude_accounts\|CODEX_BASE_URL\|gpt-5\.4\|~/\.codex\|codex --login' docs/ \
  | grep -v 'docs/superpowers/'
```

Expected: empty.

- [ ] **Step 11: Commit**

```bash
git add docs/free_inference/ docs/source/developer/
git commit -m "$(cat <<'EOF'
docs: remove codex and claude_sub references throughout

Strip the Codex CLI integration sections from user-facing free_inference
docs (README, integrations, quickstart, models) and the in-process
subscription adapter sections from developer docs (architecture,
configuration, adding-models, openrouter code-tree, deployment).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Final repo-wide grep — catch any stragglers

- [ ] **Step 1: Strict grep across the whole repo**

```bash
cd /home/juncheng/hybridInference/.worktrees/remove-oauth-subs
git grep -nI \
  -e 'codex_sub' \
  -e 'codex_translator' \
  -e 'codex_token' \
  -e 'claude_sub' \
  -e 'claude_token' \
  -e 'claude_pool' \
  -e 'ClaudeSubscriptionAdapter' \
  -e 'CodexSubscriptionAdapter' \
  -e 'import_codex_auth' \
  -e 'import_claude_auth' \
  -e 'inspect_claude_accounts' \
  -e 'CODEX_BASE_URL' \
  | grep -v '^docs/superpowers/' \
  | grep -v '^test/unit/routing/test_routewise_router.py:1261'
```

Expected: empty.

If anything appears, investigate and fix. The two excluded patterns are the documented exceptions:
- `docs/superpowers/` is historical — never edit.
- `test_routewise_router.py:1261` says "codex review fix" — refers to a code review iteration, not the codex adapter; coincidental string match.

- [ ] **Step 2: Static check for `gpt-5.4` orphans**

```bash
git grep -nI 'gpt-5\.4\|gpt-5\.3-codex\|gpt-5\.2-codex\|gpt-5\.1-codex\|GPT-5\.4' | grep -v '^docs/superpowers/'
```

Expected: empty.

If anything turns up in this task, do not commit a "miscellaneous fix" commit — instead amend the relevant earlier task's commit if the omission is small and isolated, or create a focused fix commit ("chore: remove stray codex_sub mention in <file>") if it spans multiple categories.

---

## Task 10: Local verification gates (per spec)

- [ ] **Step 1: Imports resolve**

```bash
cd /home/juncheng/hybridInference/.worktrees/remove-oauth-subs
uv run python -c "from serving.servers.bootstrap import initialize" && \
uv run python -c "from serving.servers.registry import register_from_models_yaml" && \
uv run python -c "import serving.adapters" && \
echo "imports OK"
```

Expected: `imports OK`.

- [ ] **Step 2: Unit tests**

```bash
uv run pytest test/unit -x --ff -q
```

Expected: all green. The five deleted test files (4 codex + 1 claude_sub) should no longer appear in the test count vs the pre-change baseline.

- [ ] **Step 3: Backend boots cold**

```bash
uv run uvicorn serving.servers.app:app --host 127.0.0.1 --port 8765 --no-access-log &
SERVER_PID=$!
sleep 3
curl -fsS http://127.0.0.1:8765/healthz
echo
curl -fsS http://127.0.0.1:8765/v1/models | python -c "import json, sys; d = json.load(sys.stdin); ids = [m['id'] for m in d['data']]; print(f'{len(ids)} models'); assert 'gpt-5.4' not in ids, 'gpt-5.4 leaked'; print('no codex/sub leakage')"
kill $SERVER_PID
wait $SERVER_PID 2>/dev/null
```

Expected: `/healthz` returns 200; `/v1/models` returns the active list; the assert passes.

- [ ] **Step 4: Live Claude path against staging**

```bash
# Use a staging admin API key (not committed to repo). Set ADMIN_KEY in your shell.
curl -fsS https://staging.freeinference.org/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ADMIN_KEY:?set ADMIN_KEY env first}" \
  -d '{"model":"claude-opus-4.7","messages":[{"role":"user","content":"reply with the single word OK"}],"max_tokens":16}' \
  | python -c "import json, sys; d = json.load(sys.stdin); print(d['choices'][0]['message']['content'])"
```

Expected: a non-empty assistant reply (proves cli-proxy → Anthropic still works; this PR cannot have broken it because no live code path was touched, but verify anyway).

- [ ] **Step 5: ruff format + final commit if needed**

```bash
uv run ruff format .
git status
```

If `ruff format` produces changes, stage and commit them:

```bash
git add -A
git commit -m "style: ruff format after subscription adapter removal"
```

Otherwise skip the commit.

---

## Task 11: Open PR, monitor, fix until green

- [ ] **Step 1: Push the branch**

```bash
cd /home/juncheng/hybridInference/.worktrees/remove-oauth-subs
git push -u origin jason/claude/remove-oauth-subs
```

- [ ] **Step 2: Open the PR**

Use the issue number from Task 0 Step 3 (replace `<ISSUE>` below):

```bash
gh pr create --base dev --title "Remove in-process OAuth subscription adapters (codex_sub + claude_sub)" --body "$(cat <<'EOF'
## Summary

- Delete `codex_sub` and `claude_sub` adapters, the shared OAuth account pool (`codex_token`/`claude_token`/`claude_pool`), and all related tests, scripts, settings, frontend surfaces, and docs.
- Active Claude path through `cli-proxy-api` (via `provider: anthropic`) is untouched. No model in `config/models.yaml` declared `kind: codex_sub` or `kind: claude_sub` before this change.
- 15 files deleted, 22 files edited.

## Why now

- `codex_sub` was never enabled in production (`gpt-5.4` was commented out and required a `var/data/codex_accounts.json` that doesn't exist on staging/prod).
- `claude_sub` was migrated away from on 2026-05-02 — Claude models now route via the direct `AnthropicAdapter` to an external `cli-proxy-api` that owns OAuth.
- The in-process OAuth stack has been orphaned dead code since that migration.

## Out of scope

- The `cli-proxy` upstream and `${CLI_PROXY_*}` env vars stay (live Claude path).
- `scripts/setup_claude_code.sh` stays (configures end-user Claude Code CLI to point at `freeinference.org/anthropic`; unrelated to OAuth pool).
- Vertex `ClaudeAdapter` and `claude_format.py` stay.

## Operational follow-ups (post-merge)

- Remove `var/data/codex_accounts.json` and `var/data/claude_accounts.json` from staging and prod hosts.
- Remove any stale `CODEX_*` / `CLAUDE_SUB_*` env vars from host `.env` files (they become silent no-ops post-merge).

## Test plan

- [x] `uv run pytest test/unit -x --ff -q` green
- [x] Backend cold boot (`uvicorn serving.servers.app:app`) — `/healthz` 200, `/v1/models` excludes `gpt-5.4`
- [x] Live Claude path: `curl https://staging.freeinference.org/v1/chat/completions` with `claude-opus-4.7` returns assistant reply
- [ ] Frontend smoke against staging (browser): Models section excludes gpt-5.4; Playground with claude-opus-4.7 has no reasoning-effort UI; chat works; no console errors

Closes #<ISSUE>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Capture the PR URL from the output and report it back.

- [ ] **Step 3: Frontend smoke test against staging (browser)**

Per CLAUDE.md and the spec's mandatory frontend gate:

1. Open `https://staging.freeinference.org` in a browser (the dev branch is auto-deployed; **wait for the merge** before this step in production reality, but the equivalent local-build check from Task 6 Step 13 already passed).
2. Log in as `admin@admin.com` / `admin`.
3. Open the Models section: confirm `gpt-5.4` is gone; the visible model list is otherwise unchanged.
4. Open Playground: pick `claude-opus-4.7`; confirm no reasoning-effort UI block appears; send a short message; confirm a response.
5. Browser DevTools console: confirm no errors.

Note: staging tracks `dev`, so the in-browser check happens **after** merge in this case. Tick the PR's Test Plan checkbox for "Frontend smoke against staging" only after this verification passes post-merge.

- [ ] **Step 4: Watch CI + comments until green**

Per CLAUDE.md, check every 2 minutes after PR creation until all comments are resolved and CI is passing:

```bash
gh pr view --json statusCheckRollup,reviewDecision,comments | python -c "import json, sys; d = json.load(sys.stdin); print('checks:', d.get('statusCheckRollup')); print('review:', d.get('reviewDecision')); print('comments:', len(d.get('comments', [])))"
gh pr checks
gh api repos/:owner/:repo/pulls/$(gh pr view --json number -q .number)/comments
```

For each failing check or unresolved comment:
- Investigate locally inside the worktree.
- Apply the fix as a new commit on the same branch (do NOT amend or force-push).
- Push: `git push`.
- Wait for the next CI cycle.
- Repeat until checks are all green and there are no open review comments.

- [ ] **Step 5: Hand off for merge**

When CI is green and review comments are resolved, report the PR URL to the user. Do not self-merge — wait for explicit approval.

---

## Task 12: Post-merge cleanup

Run these only after the PR is merged into `dev`.

- [ ] **Step 1: Delete the remote branch**

```bash
cd /home/juncheng/hybridInference
git fetch origin
git checkout dev
git pull origin dev
git push origin --delete jason/claude/remove-oauth-subs
```

- [ ] **Step 2: Remove the worktree and local branch**

```bash
git worktree remove .worktrees/remove-oauth-subs
git branch -D jason/claude/remove-oauth-subs
git worktree list
```

Expected: only the main `/home/juncheng/hybridInference` worktree remains.

- [ ] **Step 3: Operational follow-up reminder**

Tell the user to remove the orphaned account files from staging/prod hosts:

```
ssh <staging-host> 'rm -f /path/to/hybridInference/var/data/codex_accounts.json /path/to/hybridInference/var/data/claude_accounts.json'
```

(Do not perform this from the agent — it's a host operation requiring user authorization.)
