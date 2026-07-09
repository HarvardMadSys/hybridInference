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
> `minimax-m3`.

## One-click setup (macOS / Linux)

FreeInference ships a setup script that configures
`~/.claude/settings.json` and runs a connectivity check.

```bash
# Download first so you can inspect it, then run it
curl -fsSL -o setup_claude_code.sh \
  https://doc.freeinference.org/setup_claude_code.sh
bash setup_claude_code.sh
```

The script prompts for your API key. To run it non-interactively, supply the
key through the `FREEINFERENCE_API_KEY` environment variable:

```bash
FREEINFERENCE_API_KEY="hyi-your-api-key" bash setup_claude_code.sh
```

The script configures the base URL, auth token, and public default models
(`minimax-m3` for main work, `qwen3.6-35b` for background tasks), then runs a
connectivity check. To use a different model, override `FREEINFERENCE_MODEL`
(and `FREEINFERENCE_SMALL_FAST_MODEL`) in the environment before running it, or
edit `ANTHROPIC_MODEL` afterwards as shown in [Manual setup](#manual-setup).

> **Security note:** Always review remote shell scripts before executing them.
> If you have repository access, you can instead run
> `ops/setup/setup_claude_code.sh` from a local checkout.

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
    "ANTHROPIC_MODEL": "minimax-m3",
    "ANTHROPIC_SMALL_FAST_MODEL": "qwen3.6-35b",
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

> **Claude Code ≥ 2.1.198 also needs a shell export.** Recent Claude Code
> versions no longer apply `ANTHROPIC_BASE_URL` from the settings-file `env`
> block — requests silently go to `api.anthropic.com`, which rejects
> FreeInference keys with `Failed to authenticate. API Error: 401 Invalid
> bearer token`. Add the base URL to your shell profile as well
> (`~/.bashrc`, `~/.zshrc`, or equivalent):
>
> ```bash
> export ANTHROPIC_BASE_URL="https://freeinference.org/anthropic"
> ```
>
> The other variables (auth token, models) still work from
> `settings.json`, which keeps your API key out of shell config files. The
> [one-click script](#one-click-setup-macos--linux) does both steps for you.

Restart any running `claude` session (and open a new terminal so the export
takes effect), then run `claude` in a project directory to start.

## Choosing a model

FreeInference serves its public catalog under its own model IDs (GLM, Qwen,
MiniMax). The Anthropic endpoint accepts these IDs directly, so point Claude
Code at one with `ANTHROPIC_MODEL`:

| Model | Best for |
|-------|----------|
| `minimax-m3` | Default main model — long context and image input |
| `qwen3.6-35b` | Default background model — fast, non-reasoning |
| `glm-5.1` | Balanced alternative for everyday coding |
| `glm-5-turbo` | Faster edit loops |

Claude Code also runs a lightweight model for background tasks — set
`ANTHROPIC_SMALL_FAST_MODEL` so that one resolves to a public model too (e.g.
`qwen3.6-35b`). Both variables are in the [Manual setup](#manual-setup) block
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
    "model": "minimax-m3",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Say hello in one word."}]
  }'
```

A `200` with a `message` payload means you're set. A `429` means the key works
but you're momentarily rate limited — your configuration is still correct.

> **Keep `max_tokens` generous.** MiniMax and GLM models are reasoning
> models: they spend hidden thinking tokens before emitting visible text.
> With a very small
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
| "Failed to authenticate. API Error: 401 Invalid bearer token" | Claude Code ≥ 2.1.198 ignored `ANTHROPIC_BASE_URL` from `settings.json` and sent your FreeInference key to `api.anthropic.com` | Export `ANTHROPIC_BASE_URL` in your shell profile (see [Manual setup](#manual-setup)), open a new terminal, retry |
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
