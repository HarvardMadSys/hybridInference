# Claude Code Setup

Point Claude Code at a HybridInference gateway instead of Anthropic, so it uses
whatever models that gateway serves and whatever key it issues you.

## Setup

Edit `~/.claude/settings.json` (`%USERPROFILE%\.claude\settings.json` on
Windows):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://<your-gateway>/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "<your-api-key>",
    "API_TIMEOUT_MS": "600000"
  }
}
```

`/anthropic` is the gateway's Anthropic-compatible surface — the same one
`/v1/messages` is served from. `API_TIMEOUT_MS` is raised because a long
agentic turn can exceed the client default.

Some deployments ship a script that writes this file for you; check the
gateway's own documentation. The three variables above are the whole of it.

## Which models you get

Claude Code sends its default model IDs (`claude-opus-4-8`, `claude-sonnet-5`,
`claude-haiku-4-5`). A gateway resolves those through the aliases in its
`config/models.yaml`, so what you actually reach depends on that deployment's
catalogue — ask it for `/v1/models`, or read its user documentation.

Legacy dated Anthropic IDs (`claude-3-5-sonnet-latest`, `claude-sonnet-4-5`)
resolve the same way. A deployment that registers no alias for the ID your
client sends returns `404`; that is the gateway saying it serves no such model,
not that the model does not exist.

The FreeInference deployment publishes its own mapping at
[doc.freeinference.org](https://doc.freeinference.org/claude-code.html#choosing-a-model),
as a worked example of what a deployment's documentation covers here.

## Usage

```bash
cd your-project
claude
```

All Claude Code features (tool use, file editing, search) work normally.

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| 401 Authentication error | Bad API key | Check `ANTHROPIC_AUTH_TOKEN` in `~/.claude/settings.json` |
| 404 Model not found | The gateway registers no alias for the ID your client sent | Ask it for `/v1/models`; do not override `ANTHROPIC_DEFAULT_*_MODEL` unless you know what it serves |
| 429 Rate limited | Too many requests | Wait a minute and retry |
| 503 Accounts unavailable | The upstream pool that model routes to is exhausted | Wait a minute and retry |
| Connection timeout | Network issue, or the gateway is down | Check connectivity to your `ANTHROPIC_BASE_URL` |

## Uninstall

Remove the three env vars from `~/.claude/settings.json`, or delete the file:

```bash
rm ~/.claude/settings.json
```
