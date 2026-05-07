# Quick Start

Get started with FreeInference in 5 minutes.

## Step 1: Get Your API Key

1. Visit [https://freeinference.org](https://freeinference.org)
2. Register for a free account
3. Log in and create your API key from the dashboard

## Step 2: Configure Your Agent

Choose your coding agent below and follow the steps. If you want the fastest agent-first setup, start with Kilo Code. For detailed setup and troubleshooting, see the [Integration Guides](integrations.md).

### Kilo Code (Recommended)

1. Install the Kilo Code extension or plugin in your IDE
2. Open the Kilo Code side panel
3. Open Kilo Code settings
4. Set **API Provider** to **OpenAI Compatible**
5. Enter the exact connection values:
   - Base URL: `https://freeinference.org/v1`
   - API Key: `your-api-key-here`
6. Select a model:
   - `glm-5.1` for general coding tasks
   - `glm-5-turbo` for faster edits and shorter loops
   - `minimax-m2.5` for long-context or image-aware work
7. Save the settings
8. Start a new Kilo Code session and send a short prompt to verify the connection

> **Tip:** If the model list does not refresh immediately, reopen the Kilo panel or paste the model ID manually.

### Cursor

1. Open Settings (`Cmd + ,` or `Ctrl + ,`)
2. Go to **Models** → Enter your API key
3. Click **Override OpenAI Base URL** → Enter: `https://freeinference.org/v1`
4. Enable the toggle and start coding

### Claude Code

```bash
curl -fsSL -o setup_claude_code.sh https://raw.githubusercontent.com/HarvardMadSys/hybridInference/main/ops/setup/setup_claude_code.sh
# Inspect the script before running (recommended):
less setup_claude_code.sh
bash setup_claude_code.sh
```

> **Security note:** Always review remote shell scripts before executing them. You can also clone the repo and run `ops/setup/setup_claude_code.sh` from your local checkout.

### Roo Code / Cline

1. Install extension in your IDE
2. Settings → **OpenAI Compatible**
3. Base URL: `https://freeinference.org/v1`
4. API Key: `your-api-key-here`

### Continue

Add a new model entry to the `models` array in `~/.continue/config.json` (merge with your existing config — don't overwrite it):

```json
{
  "title": "FreeInference",
  "provider": "openai",
  "model": "glm-5.1",
  "apiBase": "https://freeinference.org/v1",
  "apiKey": "your-api-key-here"
}
```

> **Note:** If `~/.continue/config.json` doesn't exist yet, wrap the entry above as `{"models": [ <entry> ]}`. Otherwise, append it to your existing `models` array so prior models and settings are preserved.

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
