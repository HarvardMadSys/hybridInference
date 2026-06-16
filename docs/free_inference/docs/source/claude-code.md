# Claude Code

[Claude Code](https://docs.anthropic.com/en/docs/claude-code) is Anthropic's
official CLI coding agent. FreeInference exposes an **Anthropic-compatible
Messages endpoint**, so Claude Code works against FreeInference without an
Anthropic subscription — you just point it at the FreeInference base URL and
authenticate with your FreeInference API key.

If you don't have a key yet, register at
[https://freeinference.org](https://freeinference.org) and create one from the
dashboard. See the [Quick Start](quickstart.md) for details.

## How it works

Claude Code speaks the Anthropic Messages API. FreeInference accepts that
format at:

```
https://freeinference.org/anthropic/v1/messages
```

so the base URL Claude Code should use is:

```
https://freeinference.org/anthropic
```

Requests in Anthropic format are translated and routed to FreeInference's
backends. You authenticate with your FreeInference API key (`hyi-...`) — no
`ANTHROPIC_API_KEY` from Anthropic is needed.

## One-click setup (macOS / Linux)

The repository ships a setup script that configures
`~/.claude/settings.json` and runs a connectivity check.

```bash
# Download first so you can inspect it, then run it
curl -fsSL -o setup_claude_code.sh \
  https://raw.githubusercontent.com/HarvardMadSys/hybridInference/main/ops/setup/setup_claude_code.sh
bash setup_claude_code.sh
```

The script prompts for your API key. To run it non-interactively, supply the
key through the `FREEINFERENCE_API_KEY` environment variable:

```bash
FREEINFERENCE_API_KEY="hyi-your-api-key" bash setup_claude_code.sh
```

> **Security note:** Always review remote shell scripts before executing them.
> You can also clone the repository and run
> `ops/setup/setup_claude_code.sh` from your local checkout.

## Manual setup

Edit `~/.claude/settings.json` (on Windows:
`%USERPROFILE%\.claude\settings.json`) and add the `env` block below. If the
file already has settings, merge the keys into the existing `env` object
rather than overwriting it.

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://freeinference.org/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "hyi-your-api-key",
    "API_TIMEOUT_MS": "600000"
  }
}
```

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_BASE_URL` | Points Claude Code at the FreeInference Anthropic endpoint. |
| `ANTHROPIC_AUTH_TOKEN` | Your FreeInference API key (`hyi-...`). Sent as the auth credential. |
| `API_TIMEOUT_MS` | Request timeout. `600000` (10 min) is recommended for long agentic turns. |

Restart any running `claude` session after editing the file, then run
`claude` in a project directory to start.

## Choosing a model

Claude Code's built-in defaults target Anthropic model IDs (e.g.
`claude-sonnet-4-6`, `claude-opus-4-6`). FreeInference recognizes these IDs —
including dated and `-latest` aliases — and resolves them to its registered
models, so the defaults work out of the box:

| Model | Notes |
|-------|-------|
| `claude-sonnet-4.6` | Balanced default for everyday coding. |
| `claude-opus-4.6` | Highest capability for harder tasks. |

You don't normally need to override the model. To pin a specific one, set it
explicitly in your `env` block:

```json
{
  "env": {
    "ANTHROPIC_MODEL": "claude-sonnet-4.6"
  }
}
```

You can also target any model from the live FreeInference catalog. Fetch the
current list at any time:

```bash
curl https://freeinference.org/v1/models \
  -H "Authorization: Bearer hyi-your-api-key"
```

See the [Available Models](models.md) page for the full catalog and the
[API Headers Reference](api_headers.md) for supported headers such as
`anthropic-version` and `anthropic-beta`.

## Verify it works

Send a one-shot request straight to the endpoint. Claude Code clients send the
key in the `x-api-key` header; `Authorization: Bearer` is also accepted.

```bash
curl -X POST https://freeinference.org/anthropic/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: hyi-your-api-key" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-sonnet-4.6",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "Say hello in one word."}]
  }'
```

A `200` with a `message` payload means you're set. A `429` means the key works
but you're momentarily rate limited — your configuration is still correct.

Then just run:

```bash
claude
```

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| 401 Authentication error | Bad or missing API key | Check `ANTHROPIC_AUTH_TOKEN` in `~/.claude/settings.json` |
| 404 Model not found | Unknown model ID | Use a model from `https://freeinference.org/v1/models`; don't override `ANTHROPIC_MODEL` with an unsupported ID |
| 429 Rate limited | Too many concurrent/total requests | Wait a moment and retry |
| 503 Accounts unavailable | Upstream pool exhausted | Wait a moment and retry |
| Connection timeout | Network issue | Confirm connectivity to `freeinference.org`; raise `API_TIMEOUT_MS` |

## See also

- [Quick Start](quickstart.md) — get an API key in a few minutes
- [IDE & Coding Agent Integrations](integrations.md) — other agents and IDEs
- [Available Models](models.md) — full model catalog
- [API Headers Reference](api_headers.md) — authentication and custom headers
