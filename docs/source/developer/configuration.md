
# Configuration Guide

The system uses two configuration files for runtime setup:

- `config/models.yaml` (required): Registers available models and their backend adapters
- `config/routing.yaml` (optional): Configures traffic distribution between local and remote deployments

These files are independent: `models.yaml` provides the candidate set of adapters, while `routing.yaml` adjusts weights on top of registered adapters. Without `routing.yaml`, the system uses default weights from `models.yaml` or environment variables (typically 1.0).

## 1. Environment Variables and Priority

### Variable Substitution
The system supports environment variable placeholders in YAML:
- `${VAR}`: Reads `VAR` from environment
- `${VAR:-default}`: Reads `VAR` from environment, uses `default` if not found

### Configuration Priority
Environment variables (`.env` or system) > `routing.yaml`/`models.yaml` > code defaults

## 2. models.yaml (Required)

Registers models and adapters at startup. Each entry describes a model identifier and its backend configuration (base_url, api_key, capabilities, aliases, etc.).

Example:

```yaml
# config/models.yaml
models:
  - id: my-local-model
    name: My Local Model
    provider: ollama
    provider_model_id: "my-local-model"
    context_length: 65536
    max_output_length: 8192
    supports_tools: true
    supports_structured_output: true
    supported_params: [temperature, top_p, max_tokens, stop]
    pricing:
      prompt: "0"
      completion: "0"
      image: "0"
      request: "0"
      input_cache_reads: "0"
      input_cache_writes: "0"
    route:
      - kind: ollama
        weight: 1.0
        base_url: "http://localhost:11434/v1"
        provider_model_id: "my-local-model"

  - id: my-remote-model
    name: My Remote Model
    provider: openai_compat
    provider_model_id: "actual-model-id"
    context_length: 131072
    max_output_length: 8192
    supports_tools: true
    supports_structured_output: true
    supported_params: [temperature, top_p, max_tokens, stop]
    pricing:
      prompt: "1.0"
      completion: "3.0"
      image: "0"
      request: "0"
      input_cache_reads: "0"
      input_cache_writes: "0"
    route:
      - kind: openai_compat
        weight: 1.0
        base_url: ${PROVIDER_BASE_URL}
        api_key: ${PROVIDER_API_KEY}
        provider_model_id: "actual-model-id"
```

### Key Points:
- `id`: Public model ID exposed by the API (what clients use to call the model)
- `provider_model_id`: The actual model name sent to the backend provider (e.g., vLLM/freeinference's `/models/...`). If omitted, uses `id`
- `aliases`: Additional public aliases that are registered alongside `id` to point to the same adapter
- `provider`: Determines adapter type. Supported kinds: `openai_compat`, `vllm`, `sglang`, `ollama`, `deepseek`, `openai`, `zhipu`, `chutes`, `featherless`, `gemini`, `claude`, `claude_sub`, `codex_sub`. See [adding-models.md](adding-models.md) for the full reference table.
- `/v1/models` endpoint dynamically generates its response from registered adapters

## 3. routing.yaml (Optional)

Controls traffic distribution between local and remote deployments with optional health monitoring.

### Fixed-Ratio Strategy
- Set `routing_strategy: fixed`
- Control local traffic percentage via `routing_parameter.local_fraction` (0.0–1.0)
- Weights are distributed equally within local and remote groups

### Health Checking (Optional)
- `health_check: N`: Sends GET request to `/health` every N seconds
- Unhealthy endpoints temporarily get weight 0
- Endpoints recover automatically when health checks succeed
- Set to 0 or omit to disable health checking

### Example: Hybrid Deployment (60% local / 40% remote)

```yaml
# config/routing.yaml
routing_strategy: fixed
routing_parameter:
  local_fraction: 0.6
timeout: 2
health_check: 30
logging:
  output: output.log
local_deployment:
  - endpoint: ${LOCAL_BASE_URL:-http://localhost:8000}
    models:
remote_deployment:
    models:
```

### How It Works:
- At startup, `RoutingManager` applies 60/40 weights to registered adapters
- If local endpoint becomes unhealthy, weights adjust automatically (0% local, 100% remote)
- System falls back gracefully to maintain service availability

### Local-Only Deployment

Simply omit `routing.yaml` to use default weights from `models.yaml` (typically 1.0 for all adapters).

## 4. Running the System

### Set Environment Variables:
  ```bash
  export LOCAL_BASE_URL=http://localhost:8000
  ```

### Start the Server:
```bash
python -m serving.servers.app
# Or use uvicorn/pm2/supervisor for production
# uvicorn serving.servers.app:app --host 0.0.0.0 --port 8080
```

### Verify Operation:
- `GET /v1/models` - Returns available models
- `POST /v1/chat/completions` - Routes requests based on configured ratios
- `GET /routing` - Shows current routing configuration

(subscription-adapters)=
## 5. Subscription Adapters (Claude / Codex)

Subscription adapters use **OAuth account pools** instead of static API keys. This requires a separate credentials file and import workflow.

### 5.1 Claude Subscription Setup

**Step 1: Import credentials from Claude Code CLI**

```bash
# Run from the project root on the host machine.
# Auto-detect credentials from ~/.claude/
python scripts/import_claude_auth.py

# Or specify a custom path
python scripts/import_claude_auth.py --claude-auth /path/to/credentials.json

# Override the account label
python scripts/import_claude_auth.py --label "murphy-max"
```

This writes to `var/data/claude_accounts.json` (v2 format, `chmod 0600`).
If you use Docker, run the import script on the host so it can read `~/.claude/` and write into the bind-mounted project workspace.

**Re-importing** is safe: the script matches accounts in this order:
- explicit `--account-id`
- matching `refresh_token`
- matching `organization_id` plus compatible email

If an account was previously `revoked` or `disabled`, re-importing resets it to `active`.

**Step 2: Verify account status**

```bash
python scripts/inspect_claude_accounts.py
```

Output shows per-account state, token expiry, consecutive failures, and a summary:

```
[OK] acct_01 (murphy-max)
    state: active  |  plan: max
    email: user@example.com
    token expires: 2026-03-17 15:30:00 UTC (2h 15m)

--- Summary ---
  active: 1
```

**Step 3: Add model entries to `config/models.yaml`**

Claude subscription models use `provider: claude_sub`:

```yaml
- id: claude-sonnet-4.6
  name: Claude Sonnet 4.6
  provider: claude_sub
  provider_model_id: "claude-sonnet-4-6"
  base_url: https://api.anthropic.com
  context_length: 200000
  max_output_length: 64000
  supports_tools: true
  route:
    - kind: claude_sub
      weight: 1.0
```

**Step 4: (Optional) Configure paid API fallback**

Set `CLAUDE_SUB_FALLBACK_API_KEY` in `.env` to fall back to direct Anthropic API when all subscription accounts are unhealthy:

```bash
CLAUDE_SUB_FALLBACK_API_KEY=sk-ant-api03-...
```

**Step 5: Tune runtime settings (optional)**

These settings are read from `.env` via `serving/config/settings.py`:

```bash
CLAUDE_SUB_ACCOUNTS_FILE=var/data/claude_accounts.json
CLAUDE_SUB_TOKEN_REFRESH_MARGIN=300
CLAUDE_SUB_ACCOUNT_COOLDOWN=60
CLAUDE_SUB_FAILURE_THRESHOLD=3
```

These control where credentials are loaded from, how early tokens are refreshed, how long transient failures cool down an account, and how many consecutive failures mark an account unhealthy in the pool.

**Step 6: Anthropic-compatible northbound surface (optional)**

Claude subscription models can also be reached through `POST /anthropic/v1/messages`, which is intended for Anthropic-native clients such as Claude Code CLI.

Important behavior:
- the request `model` must resolve to a registered model whose provider is `claude_sub`
- the same shared Claude account pool is used as `/v1/chat/completions`
- DB logging still applies

Example client environment:

```bash
ANTHROPIC_BASE_URL=https://freeinference.org/anthropic
ANTHROPIC_AUTH_TOKEN=hyi-your-api-key
```

### 5.2 Account Lifecycle

Accounts have four states:

| State | Meaning | Pool membership |
|---|---|---|
| `active` | Normal operation | In pool |
| `cooldown` | Transient failure (e.g., 429 rate limit) | In pool (skipped by health check, auto-recovers) |
| `revoked` | Permanent failure (`invalid_grant` on OAuth refresh) | Removed from pool |
| `disabled` | Manually disabled | Removed from pool |

**Automatic transitions:**
- `active → cooldown`: on rate limit (429) or repeated failures
- `active → revoked`: when token refresh returns `invalid_grant`
- `cooldown → active`: automatic after cooldown period expires

**Manual recovery:**
- `revoked → active`: re-import fresh credentials via `import_claude_auth.py`
- `disabled → active`: re-import or future admin endpoint

Account state persists across restarts in `var/data/claude_accounts.json`.

### 5.3 Credential File Format (v2)

```json
{
  "version": 2,
  "accounts": [
    {
      "id": "acct_01",
      "label": "murphy-max",
      "type": "oauth",
      "access_token": "sk-ant-oat-...",
      "refresh_token": "...",
      "expires_at": 1773702740076,
      "organization_id": "org-uuid-...",
      "email": "user@example.com",
      "plan": "max",
      "state": "active",
      "state_changed_at": 1710700000000,
      "revoke_reason": "",
      "consecutive_failures": 0
    }
  ]
}
```

Files without a `version` field are treated as v1 (uses `enabled` boolean) and auto-migrated to v2 on first load. A `.bak` backup is created before each write.

Persisted `cooldown` entries are promoted back to `active` on startup because cooldown is treated as transient runtime state rather than a durable operator action.

### 5.4 Codex Subscription

Codex subscription follows the same pattern with `provider: codex_sub` and `var/data/codex_accounts.json`. Import via:

```bash
# Run from the project root on the host machine.
python scripts/import_codex_auth.py
```

Optional runtime settings:

```bash
CODEX_ACCOUNTS_FILE=var/data/codex_accounts.json
CODEX_FALLBACK_API_KEY=
CODEX_TOKEN_REFRESH_MARGIN=30
CODEX_ACCOUNT_COOLDOWN=60
CODEX_FAILURE_THRESHOLD=3
```

Codex currently exposes only the OpenAI-compatible northbound surface (`POST /v1/chat/completions`); there is no separate Codex-native public route yet.

For Codex-specific details, see the repository design doc `docs/codex-subscription-design.md`.

## 6. FAQ

**Q: How do I recover a revoked Claude account?**
A: Run `python scripts/import_claude_auth.py` after re-authenticating with `claude login`. The import script detects the revoked account (by `refresh_token` or `organization_id` match) and resets it to `active`.

**Q: Which models can use `/anthropic/v1/messages`?**
A: Only models registered in `config/models.yaml` whose effective route includes `provider: claude_sub`. The route resolves the public model ID through the normal model registry before forwarding upstream.

**Q: What if routing.yaml conflicts with models.yaml?**
A: `routing.yaml` only adjusts weights; it doesn't add/remove adapters. The candidate set comes from `models.yaml` and environment variables.

**Q: How to disable health checks?**
A: Set `health_check: 0` or omit the field entirely.

**Q: Can I use other routing strategies?**
A: Currently only `fixed` is built-in. You can add new strategies in `routing/strategies.py` and configure them in `routing.yaml`.

**Q: What happens during failover?**
A: The system automatically tries alternative adapters when the primary fails, ensuring continuous service availability.
