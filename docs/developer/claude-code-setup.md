# Claude Code Setup

Use Claude Code with FreeInference — no Anthropic API key needed.

## Quick Setup (macOS / Linux)

```bash
# Download and run the setup script:
curl -fsSL -o setup_claude_code.sh https://raw.githubusercontent.com/HarvardMadSys/hybridInference/main/ops/setup/setup_claude_code.sh
bash setup_claude_code.sh
```

The script will ask for your FreeInference API key, configure `~/.claude/settings.json`, and test connectivity.

You can also skip the prompt by passing your key as an env var:

```bash
FREEINFERENCE_API_KEY="your-key-here" bash setup_claude_code.sh
```

## Manual Setup

If you prefer to configure manually, edit `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://freeinference.org/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "<your-freeinference-api-key>",
    "API_TIMEOUT_MS": "600000"
  }
}
```

On Windows the file is at `%USERPROFILE%\.claude\settings.json`.

## Available Models

> **Note:** These models require internal role access and are not available to general users.

| Model | Description |
|-------|-------------|
| `claude-sonnet-4.6` | Default model in Claude Code |
| `claude-opus-4.6` | Most capable |
| `claude-opus-4.7` | Latest Opus (1M context) |

## Usage

```bash
cd your-project
claude
```

That's it. All Claude Code features (tool use, file editing, search, etc.) work normally.

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| 401 Authentication error | Bad API key | Check `ANTHROPIC_AUTH_TOKEN` in `~/.claude/settings.json` |
| 404 Model not found | Wrong model ID | Claude Code uses the correct IDs by default — don't override `ANTHROPIC_DEFAULT_*_MODEL` |
| 429 Rate limited | Too many requests | Wait a minute and retry |
| 503 Accounts unavailable | Subscription pool exhausted | Wait a minute and retry |
| Connection timeout | Network issue or proxy down | Check connectivity to `freeinference.org` |

## Uninstall

Remove the three env vars from `~/.claude/settings.json`, or delete the file:

```bash
rm ~/.claude/settings.json
```
