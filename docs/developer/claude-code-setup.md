# Claude Code Setup

Point Claude Code at a HybridInference gateway, so it uses that deployment's
models and API key instead of Anthropic's.

## Setup

Edit `~/.claude/settings.json` (`%USERPROFILE%\.claude\settings.json` on
Windows):

```json
{
  "model": "<gateway-model-id>",
  "env": {
    "ANTHROPIC_BASE_URL": "https://your-gateway.example/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "<your-api-key>"
  }
}
```

`/anthropic` is the gateway's Anthropic-compatible surface: Claude Code sends
`/anthropic/v1/messages` and `/anthropic/v1/messages/count_tokens`. The same
endpoints also answer at `/v1/messages`. The `model` setting is optional; it
picks the starting model, and `/model` changes it.

Which variables your Claude Code version reads, its timeouts, and whether
`settings.json` alone is enough are Claude Code's behavior, not the gateway's.
See Anthropic's [LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect),
[model configuration](https://code.claude.com/docs/en/model-config) and
[installation](https://code.claude.com/docs/en/installation) documentation.

## Model Families and Aliases

Claude Code asks for models by Anthropic's names. The gateway translates the
names it knows before looking the model up:

| Claude Code sends | The gateway looks up |
|---|---|
| `claude-sonnet-4-6`, `claude-sonnet-4-5`, `claude-3-5-sonnet-latest`, `claude-3-5-sonnet-20241022`, `claude-3-5-sonnet-20240620` | `claude-sonnet-4.6` |
| `claude-opus-4-7`, `claude-3-opus-latest` | `claude-opus-4.7` |
| `claude-opus-4-6`, `claude-3-opus-20240229` | `claude-opus-4.6` |

A deployment registers the right-hand id (as a model id or an alias), not the
name Claude Code sends; registering `claude-sonnet-4-6` itself gives a 404. Any
other id is looked up as sent. A long-context marker such as
`claude-sonnet-4-6[1m]` is removed first, so never register the bracketed form.
Not every deployment serves these models: check `GET /v1/models`.

To choose the model behind each of Claude Code's families, add these to the
same `env` block. Claude Code resolves them itself and sends the resulting id:

```json
{
  "ANTHROPIC_DEFAULT_OPUS_MODEL": "<gateway-model-id>",
  "ANTHROPIC_DEFAULT_SONNET_MODEL": "<gateway-model-id>",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL": "<fast-gateway-model-id>"
}
```

The Haiku model handles Claude Code's smaller background calls.

## Usage and Verification

```bash
cd your-project
claude
```

`/status` shows the active model and gateway; `/model` switches models. Tool
use, file editing and search work when the gateway model supports tool calls.

This configures inference for Claude Code on this machine only. Anthropic's
server tools (web search, web fetch, code execution, MCP connectors and the
other tools Anthropic runs and bills per use) are not available through the
gateway, which cannot bill them: on a model served by Anthropic's API, a
request for one gets `400 invalid_request_error`. Tools that Claude Code runs
itself are unaffected.

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| 401 Authentication error | Bad API key | Check `ANTHROPIC_AUTH_TOKEN` in `~/.claude/settings.json` |
| 404 Model not found | The gateway has no route for the final model ID, or your key's role cannot see it | Query `GET /v1/models` (it lists what your key can reach), then update `model` or the matching `ANTHROPIC_DEFAULT_*_MODEL` |
| 429 Rate limited | Too many requests | Wait and retry |
| 503 No provider available | The model resolved, but none of its routes can serve right now | Retry, or choose another model |
| 504/timeout | A gateway or upstream deadline passed | Check gateway health before raising timeouts |

## Uninstall

Remove `model`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and any
`ANTHROPIC_DEFAULT_*_MODEL` keys from `~/.claude/settings.json`, keeping your
other settings. Claude Code then talks to Anthropic directly again.
