# Quick Start

Get started with FreeInference in 5 minutes.

## Step 1: Get Your API Key

1. Visit [https://freeinference.org](https://freeinference.org)
2. Register for a free account
3. Log in and create your API key from the dashboard

## Step 2: Configure Your Agent

Choose your coding agent below and follow the steps. For detailed setup and troubleshooting, see the [Integration Guides](integrations.md).

### Cursor

1. Open Settings (`Cmd + ,` or `Ctrl + ,`)
2. Go to **Models** → Enter your API key
3. Click **Override OpenAI Base URL** → Enter: `https://freeinference.org/v1`
4. Enable the toggle and start coding

### Claude Code

```bash
curl -fsSL -o setup_claude_code.sh https://raw.githubusercontent.com/HarvardMadSys/hybridInference/main/scripts/setup_claude_code.sh
bash setup_claude_code.sh
```

### Roo Code / Kilo Code / Cline

1. Install extension in your IDE
2. Settings → **OpenAI Compatible**
3. Base URL: `https://freeinference.org/v1`
4. API Key: `your-api-key-here`

### Continue

Add to `~/.continue/config.json`:
```json
{
  "models": [{
    "title": "FreeInference",
    "provider": "openai",
    "model": "glm-5.1",
    "apiBase": "https://freeinference.org/v1",
    "apiKey": "your-api-key-here"
  }]
}
```

### Aider

```bash
export OPENAI_API_BASE=https://freeinference.org/v1
export OPENAI_API_KEY=your-api-key-here
aider --model openai/glm-5.1
```

### Other Agents (Windsurf, Twinny, CodeGPT, etc.)

See the full [Integration Guides](integrations.md) for all supported agents.

---

## Step 3: Choose a Model

See [available models](models.md) and select one that fits your needs.

---

## Next Steps

- [Integration Guides](integrations.md) - Detailed setup for all supported agents and troubleshooting
- [Available Models](models.md) - Model specifications and features
- [API Headers Reference](api_headers.md) - Authentication and custom headers

## Need Help?

Having issues? Check the [integration guide](integrations.md) for troubleshooting.
