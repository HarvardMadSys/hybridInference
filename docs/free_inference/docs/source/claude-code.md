# Claude Code

[Claude Code](https://docs.anthropic.com/en/docs/claude-code) is Anthropic's
official CLI coding agent. FreeInference exposes an **Anthropic-compatible
Messages endpoint**, so Claude Code works against FreeInference without an
Anthropic subscription — you just point it at the FreeInference base URL,
authenticate with your FreeInference API key, and select one of FreeInference's
public models.

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
public models, then translated back into Anthropic format for Claude Code. You
authenticate with your FreeInference API key (`hyi-...`) — no `ANTHROPIC_API_KEY`
from Anthropic is needed.

> **Defaults work, but pinning is better.** Claude Code's built-in default
> model IDs (`claude-opus-4-8`, `claude-sonnet-5`, `claude-haiku-4-5`) are
> recognized and served by budget models from the public catalog
> (see [Choosing a model](#choosing-a-model)). For predictable quality and
> availability, set `ANTHROPIC_MODEL` explicitly to a public model such as
> `glm-5.1`.

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

The script configures the base URL, auth token, and a public default model
(`glm-5.1`, with `glm-5-turbo` for background tasks), then runs a connectivity
check. To use a different model, override `FREEINFERENCE_MODEL` (and
`FREEINFERENCE_SMALL_FAST_MODEL`) in the environment before running it, or edit
`ANTHROPIC_MODEL` afterwards as shown in [Manual setup](#manual-setup).

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
    "ANTHROPIC_MODEL": "glm-5.1",
    "ANTHROPIC_SMALL_FAST_MODEL": "glm-5-turbo",
    "API_TIMEOUT_MS": "600000"
  }
}
```

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_BASE_URL` | Points Claude Code at the FreeInference Anthropic endpoint. |
| `ANTHROPIC_AUTH_TOKEN` | Your FreeInference API key (`hyi-...`). Sent as the auth credential. |
| `ANTHROPIC_MODEL` | The FreeInference model Claude Code uses for its main work. |
| `ANTHROPIC_SMALL_FAST_MODEL` | The model Claude Code uses for lightweight background tasks. |
| `API_TIMEOUT_MS` | Request timeout. `600000` (10 min) is recommended for long agentic turns. |

Restart any running `claude` session after editing the file, then run
`claude` in a project directory to start.

## Choosing a model

FreeInference serves its public catalog under its own model IDs (GLM, Qwen,
MiniMax). The Anthropic endpoint accepts these IDs directly, so point Claude
Code at one with `ANTHROPIC_MODEL`:

| Model | Best for |
|-------|----------|
| `glm-5.1` | Balanced default for everyday coding |
| `glm-5-turbo` | Faster edit loops and background tasks |
| `minimax-m2.5` | Ultra-long context and image input |

Claude Code also runs a lightweight model for background tasks — set
`ANTHROPIC_SMALL_FAST_MODEL` so that one resolves to a public model too (e.g.
`glm-5-turbo`). Both variables are in the [Manual setup](#manual-setup) block
above.

Fetch the live catalog at any time:

```bash
curl https://freeinference.org/v1/models \
  -H "Authorization: Bearer hyi-your-api-key"
```

> **Note on Claude model IDs.** Claude Code's current default IDs are served
> by public catalog models: `claude-opus-4-8` (and the `[1m]` long-context
> variant) resolves to `minimax-m3`, while `claude-sonnet-5` and
> `claude-haiku-4-5` resolve to `qwen3.6-35b`. So Claude Code works without
> setting any model — just be aware the defaults are budget models, and
> `qwen3.6-35b` is hosted on FreeInference's own GPUs, which can be briefly
> unavailable during maintenance. Legacy IDs like `claude-3-5-sonnet-latest`
> resolve to internal Claude models and require elevated access; on a public
> key they return a `404`.

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
    "model": "glm-5.1",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Say hello in one word."}]
  }'
```

A `200` with a `message` payload means you're set. A `429` means the key works
but you're momentarily rate limited — your configuration is still correct.

> **Keep `max_tokens` generous.** GLM models are reasoning models: they spend
> hidden thinking tokens before emitting visible text. With a very small
> `max_tokens` (say 64) the whole budget can go to reasoning and the response
> comes back `200` with **empty** `content` and
> `stop_reason: "max_tokens"` — that's a budget problem, not a setup problem.

Then just run:

```bash
claude
```

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| "There's an issue with the selected model (…). It may not exist or you may not have access to it." | The configured model is not currently in the catalog — either a typo'd ID, or a FreeInference-hosted model (e.g. `qwen3.6-35b`) that is temporarily offline for GPU maintenance | List the live catalog (`https://freeinference.org/v1/models`) and switch to an available model — set `ANTHROPIC_MODEL` (e.g. `glm-5.1`) or use `/model` inside Claude Code, then restart |
| 401 Authentication error | Bad or missing API key | Check `ANTHROPIC_AUTH_TOKEN` in `~/.claude/settings.json` |
| 404 Model not found | Model ID not in the public catalog | Set `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` to a model from `https://freeinference.org/v1/models` (e.g. `glm-5.1`) |
| 200 but empty `content`, `stop_reason: "max_tokens"` | Reasoning model spent the whole tiny `max_tokens` budget on hidden thinking | Raise `max_tokens` (1024+), or use a non-reasoning model for small calls |
| 429 Rate limited | Too many concurrent/total requests | Wait a moment and retry |
| 503 Accounts unavailable | Upstream pool exhausted | Wait a moment and retry |
| Connection timeout | Network issue | Confirm connectivity to `freeinference.org`; raise `API_TIMEOUT_MS` |

## See also

- [Quick Start](quickstart.md) — get an API key in a few minutes
- [IDE & Coding Agent Integrations](integrations.md) — other agents and IDEs
- [Available Models](models.md) — full model catalog
- [API Headers Reference](api_headers.md) — authentication and custom headers
