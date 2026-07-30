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
    "ANTHROPIC_BASE_URL": "https://<your-gateway>/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "<your-api-key>"
  }
}
```

`/anthropic` is the gateway's Anthropic-compatible surface; Claude Code sends
Messages API requests to `/anthropic/v1/messages`. Current Claude Code
versions read both gateway variables from the `settings.json` `env` block, so
no shell-profile export is required.

The top-level `model` setting is optional. It selects the initial model and can
also be changed with `/model`. Claude Code's default request timeout is already
600 seconds; only add `API_TIMEOUT_MS` if a deployment needs a different value.

For the client-side behavior, see Anthropic's current
[LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect),
[model configuration](https://code.claude.com/docs/en/model-config), and
[installation](https://code.claude.com/docs/en/installation) documentation.

## Model Families and Aliases

Claude Code can send Anthropic family IDs such as `claude-opus-4-8`,
`claude-sonnet-5`, and `claude-haiku-4-5`. The gateway resolves those IDs
through aliases in its deployed `models.yaml`. Ask the deployment for
`GET /v1/models` before assuming a family ID is registered.

To map Claude Code's family selectors explicitly, add any of these supported
variables to the same `env` block:

```json
{
  "ANTHROPIC_DEFAULT_OPUS_MODEL": "<gateway-model-id>",
  "ANTHROPIC_DEFAULT_SONNET_MODEL": "<gateway-model-id>",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL": "<fast-gateway-model-id>"
}
```

Use the Haiku mapping for Claude Code's smaller background calls.
`ANTHROPIC_SMALL_FAST_MODEL` is deprecated in current Claude Code; use
`ANTHROPIC_DEFAULT_HAIKU_MODEL` instead.

Legacy dated Anthropic IDs (`claude-3-5-sonnet-latest`,
`claude-sonnet-4-5`) first pass through the gateway's fixed compatibility
table in `serving/adapters/anthropic_aliases.py`. Register the rewritten model
ID as an alias, not only the legacy source ID. A `404` means the deployment has
no route for the final ID.

The FreeInference deployment publishes its current mapping at
[doc.freeinference.org](https://doc.freeinference.org/claude-code.html#choosing-a-model).

## Usage and Verification

```bash
cd your-project
claude
```

Use `/status` to confirm the active model and gateway configuration, and
`/model` to change the model. Local agent features such as tool use, file
editing, and search continue to work when the selected gateway model supports
the required tool calls.

Gateway credentials do not enable every Anthropic-hosted surface. For example,
Remote Control and voice mode are unavailable through an LLM gateway, and
Claude web or Slack sessions do not inherit this local gateway configuration.

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| 401 Authentication error | Bad API key | Check `ANTHROPIC_AUTH_TOKEN` in `~/.claude/settings.json` |
| 404 Model not found | The gateway registers no route for the final model ID | Query `GET /v1/models`, then update `model` or the matching `ANTHROPIC_DEFAULT_*_MODEL` |
| 429 Rate limited | Too many requests | Wait and retry |
| 502/503 Upstream unavailable | The selected route or provider is unavailable | Retry or choose another model |
| 504/timeout | A gateway or upstream request exceeded its deadline | Check gateway health, then adjust timeouts only if needed |

## Uninstall

Remove the FreeInference or gateway-specific `model` value and the
`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, and
`ANTHROPIC_DEFAULT_*_MODEL` keys from `~/.claude/settings.json`. Preserve any
unrelated Claude Code settings and environment variables in the file.
