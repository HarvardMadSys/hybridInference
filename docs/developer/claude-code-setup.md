# Claude Code Setup

Point Claude Code at a HybridInference gateway instead of Anthropic, so it
uses the models and API key issued by that deployment.

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

`/anthropic` is the gateway's Anthropic-compatible surface. Claude Code sends
Messages API requests to `/anthropic/v1/messages`, and token counting to
`/anthropic/v1/messages/count_tokens`; both are registered in
`apps/backend/serving/servers/routers/anthropic_messages.py`, which also serves
the same handlers at the bare `/v1/messages` paths for clients configured
without the `/anthropic` prefix.

The top-level `model` setting is optional. It selects the initial model and can
also be changed with `/model`.

Everything on the client side of this — which variables a given Claude Code
version reads, its request timeout, whether `settings.json` alone is enough —
belongs to Claude Code, not to the gateway. See Anthropic's current
[LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect),
[model configuration](https://code.claude.com/docs/en/model-config), and
[installation](https://code.claude.com/docs/en/installation) documentation.

## Model Families and Aliases

Claude Code sends Anthropic family IDs such as `claude-sonnet-4-6` or
`claude-opus-4-7` rather than a gateway model id. The gateway resolves those
through the `aliases:` list on a model entry in the registry it loads —
whatever `MODELS_CONFIG_PATH` names, else the `paths.models` entry of an active
distribution manifest, else the shipped `config/examples/models.openrouter.yaml`.
Nothing guarantees a given family ID is registered on a given deployment, so
query `GET /v1/models` first.

Claude Code also appends a bracketed context-window marker to the model id when
the user opts into a long-context variant (`claude-sonnet-4-6[1m]`). The
gateway strips that marker before alias and registry lookup, so you do not need
to register the bracketed form.

To map Claude Code's family selectors explicitly, add any of these supported
variables to the same `env` block:

```json
{
  "ANTHROPIC_DEFAULT_OPUS_MODEL": "<gateway-model-id>",
  "ANTHROPIC_DEFAULT_SONNET_MODEL": "<gateway-model-id>",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL": "<fast-gateway-model-id>"
}
```

Use the Haiku mapping for Claude Code's smaller background calls. The gateway
does not read any of these variables — Claude Code resolves them locally and
sends the resulting id — so Anthropic's model-configuration documentation
linked above is the authority on which ones your version supports.

Legacy and dated Anthropic IDs (`claude-3-5-sonnet-latest`,
`claude-sonnet-4-5`, `claude-3-opus-20240229`, …) first pass through a fixed
compatibility table in
`apps/backend/serving/adapters/anthropic_aliases.py`, which rewrites them to
this project's canonical ids (`claude-sonnet-4.6`, `claude-opus-4.6`,
`claude-opus-4.7`). Unknown ids pass through untouched. Either way the result
is then looked up in the registry, so what a deployment must register is the
**rewritten** id — as a model id or in that model's `aliases:` list. A `404`
means the deployment has no route for the final id.

## Usage and Verification

```bash
cd your-project
claude
```

Use `/status` to confirm the active model and gateway configuration, and
`/model` to change the model. Local agent features such as tool use, file
editing, and search continue to work when the selected gateway model supports
the required tool calls.

These settings cover model inference only, and only for this machine's Claude
Code. Anthropic-hosted product surfaces are not part of the Messages API the
gateway implements, and Claude sessions elsewhere — the web app, other clients
— read none of this local configuration.

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| 401 Authentication error | Bad API key | Check `ANTHROPIC_AUTH_TOKEN` in `~/.claude/settings.json` |
| 404 Model not found | The gateway registers no route for the final model ID, or your key's role may not see it | Query `GET /v1/models` (it lists what your key can reach), then update `model` or the matching `ANTHROPIC_DEFAULT_*_MODEL` |
| 429 Rate limited | Too many requests | Wait and retry |
| 503 No provider available | The model resolved, but none of its routes can currently serve | Retry, or choose another model |
| 504/timeout | A gateway or upstream request exceeded its deadline | Check gateway health, then adjust timeouts only if needed |

## Uninstall

Remove the gateway-specific `model` value and the `ANTHROPIC_BASE_URL`,
`ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_DEFAULT_*_MODEL` keys from
`~/.claude/settings.json`. Preserve any unrelated Claude Code settings and
environment variables in the file. Claude Code then goes back to talking to
Anthropic directly.
