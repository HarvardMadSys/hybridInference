# IDE & Coding Agent Integrations

Learn how to configure FreeInference with popular coding agents and IDEs.

All agents use the same FreeInference API key. If you don't have one yet, see the [Quick Start](quickstart.md).

If you are choosing one default setup path, use Kilo Code. It works directly with FreeInference's OpenAI-compatible endpoint and has the most detailed setup guide below.

## Kilo Code

[Kilo Code](https://kilocode.ai) is an AI coding assistant that works well with FreeInference through the standard OpenAI-compatible endpoint.

### Configuration Steps

1. Install the **Kilo Code** extension or plugin in your IDE.

2. Open the Kilo Code panel.

3. Open Kilo Code settings.

4. In **API Provider**, select **OpenAI Compatible**.

5. Configure the connection exactly as follows:

   ```
   Base URL: https://freeinference.org/v1
   API Key: your-api-key-here
   ```

6. Choose a model based on the workflow you want:

   | Use Case | Recommended Model |
   |----------|-------------------|
   | Default coding assistant | `glm-5.1` |
   | Faster edit loops | `glm-5-turbo` |
   | Long context or image input | `minimax-m2.5` |
   | Strong bilingual coding | `glm-4.7` |

7. Save the settings.

8. Start a new Kilo Code session and send a simple prompt such as `Summarize this repository` to confirm the connection works.

### Notes

- Use the exact base URL `https://freeinference.org/v1` with no extra path segments.
- If the model picker is empty, reopen the Kilo panel or paste the model ID manually.
- If you want repository indexing, see the `Codebase Indexing` section below for the Kilo-specific embedding setup.

---

## Cursor

[Cursor](https://cursor.sh/) is an AI-powered code editor built on VS Code.

### Configuration Steps

1. Open Cursor Settings
   - **macOS**: `Cmd + ,`
   - **Windows/Linux**: `Ctrl + ,`

2. Navigate to **Models** section in the settings

3. Find the **OpenAI API Key** field and enter your FreeInference API key

4. Click on **Override OpenAI Base URL**

5. Enter the base URL:
   ```
   https://freeinference.org/v1
   ```

6. Enable the **OpenAI API Key** toggle button

7. (Optional) Select your preferred model from the available models

8. Save and start using FreeInference models in Cursor!

---

## Claude Code

[Claude Code](https://docs.anthropic.com/en/docs/claude-code) is Anthropic's official CLI coding agent. FreeInference provides an Anthropic-compatible endpoint so Claude Code works without an Anthropic API key.

For the full walkthrough — model selection, verification, and troubleshooting — see the dedicated [Claude Code guide](claude-code.md).

### Quick Setup (macOS / Linux)

```bash
FREEINFERENCE_API_KEY="your-key-here" bash setup_claude_code.sh
```

> **Security note:** Always review remote shell scripts before executing them. You can also clone this repository and run `ops/setup/setup_claude_code.sh` from your local checkout instead of fetching it over the network.

### Manual Setup

Edit `~/.claude/settings.json` (on Windows: `%USERPROFILE%\.claude\settings.json`):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://freeinference.org/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "<your-freeinference-api-key>",
    "API_TIMEOUT_MS": "600000"
  }
}
```

---

## Cline

[Cline](https://cline.bot/) is an autonomous coding agent for VS Code.

### Configuration Steps

1. Install the **Cline** extension from the VS Code Marketplace

2. Open the Cline panel in the sidebar

3. Click the settings icon (gear) to open configuration

4. Set **API Provider** to **OpenAI Compatible**

5. Configure the connection:
   ```
   Base URL: https://freeinference.org/v1
   API Key:  your-api-key-here
   Model:    glm-5.1
   ```

6. Start chatting with Cline using FreeInference models!

---

## Continue

[Continue](https://continue.dev/) is an open-source AI code assistant for VS Code and JetBrains.

### Configuration Steps

1. Install the **Continue** extension from the VS Code Marketplace or JetBrains Marketplace

2. Open Continue settings — click the gear icon or run `Cmd + Shift + P` (macOS) / `Ctrl + Shift + P` (Windows/Linux) → "Continue: Open Config"

3. Edit `config.json` (or `config.yaml`). Add or modify the `models` section:

   ```json
   {
     "models": [
       {
         "title": "FreeInference GLM-5.1",
         "provider": "openai",
         "model": "glm-5.1",
         "apiBase": "https://freeinference.org/v1",
         "apiKey": "your-api-key-here"
       }
     ],
     "tabAutocompleteModel": {
       "title": "FreeInference Autocomplete",
       "provider": "openai",
       "model": "glm-5-turbo",
       "apiBase": "https://freeinference.org/v1",
       "apiKey": "your-api-key-here"
      },
      "embeddingsProvider": {
        "provider": "openai",
        "model": "your-embedding-model-id",
        "apiBase": "https://freeinference.org/v1",
        "apiKey": "your-api-key-here"
      }
   }
   ```

4. Save the config. FreeInference models will appear in the model dropdown.



---

## Roo Code

[Roo Code](https://roocode.com) is an AI coding assistant for VS Code and JetBrains with a similar OpenAI-compatible setup.

### Configuration Steps

1. Install the **Roo Code** extension or plugin in your IDE.

2. Open the Roo Code settings.

3. In **API Provider**, select **OpenAI Compatible**.

4. Configure the connection:
   ```
   Base URL: https://freeinference.org/v1
   API Key: your-api-key-here
   ```

5. Select your preferred model such as `glm-5.1`, `glm-5-turbo`, `glm-4.7`, or `minimax-m2.5`.

6. Save settings and start using FreeInference.

---

## Codeium / Windsurf

[Windsurf](https://codeium.com/windsurf) (by Codeium) is an AI-powered IDE. It supports OpenAI-compatible model providers via its **Cascade** feature.

### Configuration Steps

1. Open Windsurf Settings

2. Navigate to **Cascade** → **Model Provider**

3. Add a custom **OpenAI Compatible** provider

4. Configure:
   ```
   Base URL: https://freeinference.org/v1
   API Key:  your-api-key-here
   Model:    glm-5.1
   ```

5. Save and select the custom provider in Cascade.

> **Note:** Windsurf's free built-in features use Codeium's own models. To use FreeInference, you need to configure a custom provider as described above.

---

## JetBrains AI Assistant

[JetBrains AI](https://www.jetbrains.com/ai/) does not natively support custom OpenAI-compatible endpoints. To use FreeInference with JetBrains IDEs, use one of the agent extensions that support JetBrains:

- **Roo Code** — available as a JetBrains plugin
- **Continue** — available as a JetBrains plugin
- **CodeGPT** — available as a JetBrains plugin

Follow the instructions in the respective sections above for each extension.

---

## Generic OpenAI-Compatible Clients

Any client that supports the OpenAI API format can connect to FreeInference.

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://freeinference.org/v1",
    api_key="your-api-key-here",
)

response = client.chat.completions.create(
    model="glm-5.1",
    messages=[{"role": "user", "content": "Hello"}],
)

print(response.choices[0].message.content)
```

### curl

```bash
curl -X POST https://freeinference.org/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key-here" \
  -d '{"model": "glm-5.1", "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 50}'
```

### Node.js (OpenAI SDK)

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "https://freeinference.org/v1",
  apiKey: "your-api-key-here",
});

const response = await client.chat.completions.create({
  model: "glm-5.1",
  messages: [{ role: "user", content: "Hello" }],
});

console.log(response.choices[0].message.content);
```

---

## Codebase Indexing

FreeInference exposes an embedding endpoint at `/v1/embeddings` and a Qdrant proxy at `/v1/qdrant` for codebase indexing in supported IDEs.

> **Note:** Embedding model availability changes over time. Check `https://freeinference.org/v1/models` for the currently registered embedding model ID and substitute it in the examples below.

### Roo Code

Roo Code natively supports OpenAI-compatible embedding providers.

1. Open the Roo Code plugin panel in your IDE.
2. Click the **Index** button in the bottom-right corner to open **Codebase Indexing**.
3. Configure:

| Setting | Value |
|---------|-------|
| **Embedder Provider** | OpenAI Compatible |
| **Base URL** | `https://freeinference.org/v1` |
| **API Key** | Your FreeInference API key |
| **Model** | `your-embedding-model-id` |
| **Model Dimension** | Matching model dimension |
| **Qdrant URL** | `https://freeinference.org/v1/qdrant` |
| **Qdrant API Key** | Your FreeInference API key |

4. Click **Start Indexing** — Roo Code will scan your codebase, generate embeddings via FreeInference, and store vectors in the shared Qdrant instance.

Your collections are automatically isolated per user — other users cannot see or access your indexed data.

### Kilo Code

Kilo Code supports OpenAI-compatible embedding configuration. To use FreeInference, select **OpenAI Compatible**:

1. Open the Kilo Code plugin panel in your IDE.
2. Click the **Index** button in the bottom-right corner to open **Codebase Indexing**.
3. Configure:

| Setting | Value |
|---------|-------|
| **Embedder Provider** | OpenAI Compatible |
| **Base URL** | `https://freeinference.org/v1` |
| **API Key** | Your FreeInference API key |
| **Model** | `your-embedding-model-id` |
| **Model Dimension** | Matching model dimension |
| **Qdrant URL** | `https://freeinference.org/v1/qdrant` |
| **Qdrant API Key** | Your FreeInference API key |

### Continue

Continue supports embeddings for codebase indexing. Add an `embeddingsProvider` to your `config.json`:

```json
{
  "embeddingsProvider": {
    "provider": "openai",
    "model": "your-embedding-model-id",
    "apiBase": "https://freeinference.org/v1",
    "apiKey": "your-api-key-here"
  }
}
```

### Alternative: Local Qdrant

If you prefer to host your own Qdrant instance instead of using the shared service, run it locally with Docker:

```bash
docker run -d --name qdrant --restart unless-stopped \
  -p 6333:6333 -v qdrant_data:/qdrant/storage qdrant/qdrant
```

Then use `http://localhost:6333` as the **Qdrant URL** in the tables above instead of the shared URL.

### Using the Embedding API Directly

You can also call the embedding endpoint directly via the OpenAI SDK or curl:

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://freeinference.org/v1",
    api_key="your-api-key-here",
)

response = client.embeddings.create(
    model="your-embedding-model-id",
    input=["def hello():", "function greet() {"],
)

for item in response.data:
    print(f"Index {item.index}: {len(item.embedding)} dimensions")
```

```bash
curl -X POST https://freeinference.org/v1/embeddings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key-here" \
  -d '{"model": "your-embedding-model-id", "input": "hello world"}'
```

---

## Troubleshooting

### Connection Issues

If you encounter connection errors:

1. Verify your API key is correct
2. Check the base URL is exactly: `https://freeinference.org/v1`
3. Ensure your firewall allows HTTPS connections
4. Restart your IDE after configuration changes

### Model Not Found

If you get "model not found" errors:

- Check the [available models](models.md) list
- Ensure the model name is exactly as listed (case-sensitive)
- Try switching to a different model like `glm-5.1` or `glm-4.7`

### Cursor-Specific Issues

**API key not working:**
- Make sure you've enabled the **OpenAI API Key** toggle
- Try removing and re-entering the API key
- Restart Cursor after configuration

**Base URL not applied:**
- Ensure there are no trailing slashes in the URL
- The URL should be exactly: `https://freeinference.org/v1`

### Claude Code Issues

| Error | Cause | Fix |
|-------|-------|-----|
| 401 Authentication error | Bad API key | Check `ANTHROPIC_AUTH_TOKEN` in `~/.claude/settings.json` |
| 404 Model not found | Wrong model ID | Don't override `ANTHROPIC_DEFAULT_*_MODEL` — Claude Code uses correct IDs by default |
| 429 Rate limited | Too many requests | Wait a minute and retry |
| 503 Accounts unavailable | Subscription pool exhausted | Wait a minute and retry |
| Connection timeout | Network issue | Check connectivity to `freeinference.org` |

### Kilo Code / Roo Code Issues

**Provider not connecting:**
- Verify **OpenAI Compatible** is selected as the provider
- Check that the base URL and API key are correct
- Try reloading the extension

**Model list empty or stale:**
- Reopen the Kilo Code or Roo Code panel
- Paste a known model ID such as `glm-5.1` manually
- Confirm the base URL is exactly `https://freeinference.org/v1`

---

## Quick Reference

| Agent | API Format | Base URL | Config Location |
|-------|-----------|----------|-----------------|
| Cursor | OpenAI | `https://freeinference.org/v1` | Settings → Models |
| Claude Code | Anthropic | `https://freeinference.org/anthropic` | `~/.claude/settings.json` |
| Cline | OpenAI | `https://freeinference.org/v1` | Extension settings |
| Continue | OpenAI | `https://freeinference.org/v1` | `~/.continue/config.json` |
| Aider | OpenAI | `https://freeinference.org/v1` | Environment variables |
| Twinny | OpenAI | `https://freeinference.org/v1` | Extension settings |
| CodeGPT | OpenAI | `https://freeinference.org/v1` | Extension settings |
| Kilo Code | OpenAI | `https://freeinference.org/v1` | Extension settings |
| Roo Code | OpenAI | `https://freeinference.org/v1` | Extension settings |
| Windsurf | OpenAI | `https://freeinference.org/v1` | Cascade → Model Provider |
| JetBrains AI | via plugin | `https://freeinference.org/v1` | Use Roo Code / Continue / CodeGPT plugin |
| Any OpenAI client | OpenAI | `https://freeinference.org/v1` | Client config |

---

## Need Help?

- [Available Models](models.md) - Complete model specifications
- [Quick Start Guide](quickstart.md) - Get started in 5 minutes
- [API Headers Reference](api_headers.md) - Authentication and custom headers
- Report issues on GitHub
