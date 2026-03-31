# Claude Code Integration — Implementation Plan

## Goal

Let FreeInference users run [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (Anthropic's CLI coding agent) against our infrastructure instead of needing their own Anthropic API key. We provide a one-click shell script and a manual configuration guide that configure Claude Code to route through our existing `/anthropic/v1/messages` proxy.

## Prior Art: Zhipu (智谱)

Zhipu offers a similar setup at https://docs.z.ai/devpack/tool/claude. Their approach:

- Override `ANTHROPIC_BASE_URL` → point to their own API proxy (`https://api.z.ai/api/anthropic`)
- Override `ANTHROPIC_AUTH_TOKEN` → use their platform's API key instead of an Anthropic key
- Override `ANTHROPIC_DEFAULT_*_MODEL` → map sonnet/haiku/opus slots to their GLM models
- Provide: (1) `npx @z_ai/coding-helper` interactive CLI, (2) `curl | bash` script, (3) manual edit guide

We adopt the same mechanism but **only proxy real Claude models** through our subscription account pool — no model remapping needed.

## Architecture

```
Claude Code CLI
    │
    │  POST /v1/messages  (Anthropic Messages API format)
    │  Header: x-api-key: <FreeInference API key>
    │  Body: { "model": "claude-sonnet-4-6", "stream": true, ... }
    ▼
FreeInference Proxy   https://freeinference.org/anthropic/v1/messages
    │  (serving/servers/routers/anthropic_proxy.py)
    │
    │  Steps:
    │  1. verify_api_key  — authenticate the FreeInference user
    │  2. _resolve_model  — map public id (claude-sonnet-4.6) → provider_model_id (claude-sonnet-4-6),
    │                       verify provider == "claude_sub"
    │  3. Rate limiting via PersistentRateLimiter
    │  4. _acquire_with_retry — get subscription token from AccountPool
    │  5. _ensure_system_prefix — inject required Claude Code system prompt (idempotent, see below)
    │  6. _build_upstream_headers — Bearer token + Anthropic headers
    │  7. Forward request to api.anthropic.com/v1/messages
    │  8. Stream SSE response back to client, extract usage for DB logging
    ▼
Anthropic API   api.anthropic.com/v1/messages
    ▼
Claude Sonnet 4.6 / Opus 4.6
```

**Key point**: the proxy is an *identity surface* — both the client (Claude Code) and upstream (Anthropic) speak the same Anthropic Messages API. No format translation is needed. The proxy's job is credential injection and usage tracking.

## Model ID Mapping

Two ID formats exist; the distinction matters:

| What | Sonnet | Opus |
|---|---|---|
| **Public ID** (`id` in models.yaml, what users see) | `claude-sonnet-4.6` | `claude-opus-4.6` |
| **Provider model ID** (sent to Anthropic upstream) | `claude-sonnet-4-6` | `claude-opus-4-6` |

Claude Code sends the **provider model ID** format (e.g. `claude-sonnet-4-6`) in the request body. The proxy's `_resolve_model()` accepts the public ID and maps it, but Claude Code's IDs also match directly since `provider_model_id` is what gets forwarded upstream.

We do **not** need to set `ANTHROPIC_DEFAULT_SONNET_MODEL` / `ANTHROPIC_DEFAULT_OPUS_MODEL` because Claude Code already sends the correct model IDs natively.

## What Claude Code's Environment Variables Do

Claude Code reads these from [`~/.claude/settings.json`](https://docs.anthropic.com/en/docs/claude-code/settings) → `env` block:

| Variable | Purpose | Our value |
|---|---|---|
| `ANTHROPIC_BASE_URL` | Replaces `https://api.anthropic.com` as the API base | `https://freeinference.org/anthropic` |
| `ANTHROPIC_AUTH_TOKEN` | Sent as `x-api-key` header (replaces user's Anthropic key) | User's FreeInference API key |
| `API_TIMEOUT_MS` | Request timeout in ms | `600000` (10 min) |

Claude Code concatenates `ANTHROPIC_BASE_URL + "/v1/messages"` → hits our proxy at `https://freeinference.org/anthropic/v1/messages`.

**On `API_TIMEOUT_MS`**: Set to 600,000 ms (10 minutes). Claude Code's agentic loops make multiple sequential API calls, but each individual call should return within minutes. 10 minutes gives ample headroom for slow responses while still failing fast if the proxy is down. (Zhipu uses 3,000,000 ms / 50 min, but they have additional latency from model format translation that we don't.)

## Existing Backend Support

Everything needed on the server side **already exists**:

- **Proxy endpoint**: `serving/servers/routers/anthropic_proxy.py` — full implementation with auth, rate limiting, streaming, usage logging
- **Model config**: `config/models.yaml` — `claude-sonnet-4.6` and `claude-opus-4.6` registered with `provider: claude_sub`, `required_role: internal`
- **Account pool**: `serving/adapters/claude_pool.py` + `claude_sub.py` — OAuth subscription credential management
- **Auth**: `serving/servers/auth.py` — `verify_api_key` dependency

**No backend code changes are required.**

### `required_role: internal` — TODO: Decide Before Launch

The two Claude models currently require `internal` role. This is a **launch blocker** that needs a decision:

- **(a) Relax to `authenticated`** — any user with a valid API key can use Claude Code through FreeInference. Simplest, but opens up subscription quota to all authenticated users.
- **(b) Keep `internal`, issue internal-role keys** — hand out internal-role API keys specifically to approved Claude Code users. More controlled, but manual key management.

**Recommended**: Option (b) for initial launch — keep it controlled while we monitor subscription account pool usage. Revisit after we have usage data.

### System Prompt — No Duplication Risk

`_ensure_system_prefix()` checks `startswith(prefix)` before injecting the `"You are Claude Code..."` string. Since Claude Code natively sends a system prompt that begins with this exact prefix, the check passes and **no duplicate injection occurs**. This is verified in the code at `anthropic_proxy.py:284`.

## Planned Deliverables

### 1. `scripts/setup_claude_code.sh` — One-click Setup Script

A Bash script (macOS / Linux) that:

1. Checks `claude` CLI is installed, prints version; exits with install instructions if missing
2. Reads API key from `$FREEINFERENCE_API_KEY` env var, or prompts interactively
3. Safely merges env vars into `~/.claude/settings.json`:
   - `ANTHROPIC_BASE_URL` = `https://freeinference.org/anthropic`
   - `ANTHROPIC_AUTH_TOKEN` = user's key
   - `API_TIMEOUT_MS` = `600000`
4. Runs a `curl` connectivity test against the proxy (minimal `max_tokens:1` request)
5. Prints available models and success message

Design decisions:
- **Merge, not overwrite**: Users may have other settings in `settings.json`. The script reads existing JSON and only updates the `env` sub-object.
- **Multiple JSON tools**: Tries python3 first (most reliable, near-universal on macOS/Linux), then jq, then raw write with a warning.
- **Non-destructive test**: The connectivity test sends a minimal `max_tokens:1` request to avoid burning real quota.
- **Idempotent**: Running the script multiple times just overwrites the same three env vars.

Distribution — recommend download-then-execute over pipe-to-bash:
```bash
# Recommended: download first, inspect, then run
curl -fsSL -o setup_claude_code.sh https://raw.githubusercontent.com/HarvardSys/hybridInference/main/scripts/setup_claude_code.sh
bash setup_claude_code.sh

# Or from a cloned repo:
bash scripts/setup_claude_code.sh
```

**Windows**: Out of scope for v1. Windows users can follow the manual configuration guide (copy-paste JSON into `%USERPROFILE%\.claude\settings.json`). We can add a PowerShell script in a follow-up if there's demand.

### 2. User-Facing Documentation Page

A doc page (location TBD — user docs site or README section) covering:

- **Option 1: Shell script** — download-then-execute instructions
- **Option 2: Manual config** — copy-paste JSON block for `~/.claude/settings.json`, with Windows path equivalent
- **How it works** — simplified architecture diagram
- **Troubleshooting** — 401 (bad key) / 404 (model not found) / 503 (account pool exhausted) / timeout explanations
- **Uninstall** — how to remove the configuration

## Things to Watch

1. **`required_role: internal`** — Launch blocker, see decision above.
2. **Claude Code version compatibility** — The env vars we rely on (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`) are [documented stable interfaces](https://docs.anthropic.com/en/docs/claude-code/settings), but worth monitoring across Claude Code updates.
3. **Streaming correctness** — The proxy does raw byte forwarding for SSE, which is correct for identity proxying. Future SSE event types pass through transparently.
4. **Rate limiting** — Claude Code can be chatty (many tool calls per session). Monitor whether current rate limits and account pool size are adequate for concurrent Claude Code users.
5. **Subscription quota** — Each Claude Code session can burn significant tokens. Track per-user usage via the existing DB logging to prevent abuse.

## Files to Create

| File | Description |
|---|---|
| `scripts/setup_claude_code.sh` | One-click setup script for macOS/Linux |
| User docs page (location TBD) | Setup guide with manual config option + troubleshooting |
