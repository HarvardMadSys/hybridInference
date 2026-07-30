# Claude Code

[Claude Code](https://code.claude.com/docs/en/overview) is Anthropic's CLI
coding agent. FreeInference exposes an Anthropic-compatible Messages endpoint,
so you can run Claude Code through FreeInference with a FreeInference API key.

If you do not have a key yet, register at
[https://freeinference.org](https://freeinference.org) and create one from the
dashboard. See the [Quick Start](quickstart.md) for details.

## How it works

Claude Code sends Anthropic Messages API requests to the configured gateway.
For FreeInference, use this base URL:

```
https://freeinference.org/anthropic
```

Claude Code appends `/v1/messages`; FreeInference translates and routes those
requests to public models, then returns Anthropic-format responses. Authenticate
with your FreeInference key (`hyi-...`) through `ANTHROPIC_AUTH_TOKEN`; you do
not need an Anthropic API key.

The current Claude-family aliases resolve as follows:

| Claude Code selection | FreeInference model |
|-----------------------|---------------------|
| Opus / `claude-opus-4-8` | `deepseek-v4-flash` |
| Sonnet / `claude-sonnet-5` | `deepseek-v4-flash` |
| Haiku / background tasks | `qwen3.6-35b` |

Pinning the models in your settings, as shown below, keeps this mapping
predictable when Claude Code changes its built-in defaults.

## One-click setup (macOS / Linux)

Download the setup script so you can inspect it before running it:

```bash
curl -fsSL -o setup_claude_code.sh \
  https://doc.freeinference.org/setup_claude_code.sh
less setup_claude_code.sh
bash setup_claude_code.sh
```

The script prompts for your API key, merges the FreeInference settings into
`~/.claude/settings.json`, and checks connectivity. For non-interactive setup:

```bash
FREEINFERENCE_API_KEY="hyi-your-api-key" bash setup_claude_code.sh
```

It pins `minimax-m3` for main work and `qwen3.6-35b` for Haiku/background
calls. Set `FREEINFERENCE_MODEL` or `FREEINFERENCE_HAIKU_MODEL` before running
the script to choose different accessible models.

> **Security note:** Review remote shell scripts before running them. If you
> have a repository checkout, you can run `ops/setup/setup_claude_code.sh`
> directly instead.

## Manual setup

Edit `~/.claude/settings.json` (Windows:
`%USERPROFILE%\.claude\settings.json`). Merge these keys with any existing
settings instead of replacing the whole file:

```json
{
  "model": "minimax-m3",
  "env": {
    "ANTHROPIC_BASE_URL": "https://freeinference.org/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "hyi-your-api-key",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "minimax-m3",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "minimax-m3",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "qwen3.6-35b"
  }
}
```

| Setting | Purpose |
|---------|---------|
| `model` | Main model used when a new session starts. |
| `ANTHROPIC_BASE_URL` | Routes Claude Code to the FreeInference Anthropic endpoint. |
| `ANTHROPIC_AUTH_TOKEN` | Sends your FreeInference key as the bearer credential. |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | Maps the Opus selection to the main model. |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | Maps the Sonnet selection to the main model. |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | Maps Haiku and lightweight background work to the smaller model. |

Claude Code supports environment variables in the `env` block, so no shell
profile export is required. `ANTHROPIC_SMALL_FAST_MODEL` is deprecated; use
`ANTHROPIC_DEFAULT_HAIKU_MODEL` instead. Claude Code already provides a default
API timeout, so a custom `API_TIMEOUT_MS` is not needed for normal setup.

The current setup script also removes the exact marked `ANTHROPIC_BASE_URL`
block written to `.zshrc` or `.bashrc` by older FreeInference script versions.
It leaves all unrelated shell-profile content unchanged.

Restart any running Claude Code session after changing the settings.

## Choosing and checking a model

The recommended defaults are:

| Model | Best for |
|-------|----------|
| `minimax-m3` | Setup default; long-context or image-aware work |
| `deepseek-v4-flash` | Complex coding and agentic work; long context and tool use |
| `qwen3.6-35b` | Fast background and lightweight tasks |
| `glm-5.1` | General-purpose coding alternative |

Inside Claude Code, run `/model` to view or change the current session model.
Run `/status` to confirm the active model and authentication/provider status.
`/model` changes the current session; edit the top-level `model` setting to
change the default for future sessions.

You can fetch the catalog available to your key at any time:

```bash
curl https://freeinference.org/v1/models \
  -H "Authorization: Bearer hyi-your-api-key"
```

See [Available Models](models.md) for model descriptions and access levels.

## Verify the endpoint

Send a one-shot request directly to the Anthropic-compatible endpoint:

```bash
curl -X POST https://freeinference.org/anthropic/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer hyi-your-api-key" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "minimax-m3",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Say hello in one word."}]
  }'
```

A `200` response with a `message` payload confirms the endpoint and key work.
A `429` means the endpoint recognized the request but is currently rate
limited. Then start Claude Code in a project directory:

```bash
claude
```

## Gateway limitations

The FreeInference setup supports normal terminal coding workflows through the
Anthropic Messages API, including streaming and tool use supported by the
selected model. It does not provide an Anthropic subscription or reproduce
every Anthropic-hosted service.

Features that require a Claude.ai login or Anthropic cloud infrastructure may
be unavailable with a FreeInference API key. This includes Remote Control
(which does not support API-key authentication), Claude Code on the web/mobile,
and account-dependent features such as voice. Other new Claude Code features
may depend on Anthropic-specific beta headers or model behavior and should be
tested before relying on them through the gateway.

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| 401 authentication error | Missing or invalid FreeInference key | Check `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` in `~/.claude/settings.json`. |
| "There's an issue with the selected model" | The model ID is unavailable to your key or temporarily offline | Run `/model`, or choose an ID returned by `/v1/models`; update the top-level `model` setting for future sessions. |
| 404 model not found | A Claude alias or explicit model is not mapped to an available public model | Pin `model` and the three `ANTHROPIC_DEFAULT_*_MODEL` variables as shown in [Manual setup](#manual-setup). |
| 200 but empty `content`, `stop_reason: "max_tokens"` | A reasoning model used the small output budget for thinking | Increase `max_tokens` (for example, to 1024 or more). |
| 429 rate limited | Too many concurrent or total requests | Wait briefly and retry. |
| 503 accounts unavailable | The upstream pool is temporarily exhausted | Wait briefly and retry. |
| Connection timeout | Network or gateway connectivity issue | Confirm access to `freeinference.org` and retry. |

## See also

- [Quick Start](quickstart.md) — create an API key
- [IDE & Coding Agent Integrations](integrations.md) — configure other agents
- [Available Models](models.md) — browse the model catalog
- [API Headers Reference](api_headers.md) — authentication and Anthropic headers
- [Claude Code model configuration](https://code.claude.com/docs/en/model-config)
- [Claude Code LLM gateway configuration](https://code.claude.com/docs/en/llm-gateway-connect)
