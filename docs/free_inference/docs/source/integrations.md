# IDE & Coding Agent Integrations

Learn how to configure FreeInference with popular coding agents and IDEs.

---

## Codex

[Codex](https://codex.so/) is a powerful AI coding assistant.

### Configuration Steps

1. Create or edit the Codex configuration file at `~/.codex/config.toml`

2. Add the following configuration:

```toml
# ~/.codex/config.toml

model = "glm-4.7"

model_provider = "free_inference"
model_reasoning_effort = "high"

[model_providers.free_inference]
name = "FreeInference"
base_url = "https://freeinference.org/v1"
wire_api = "chat"
env_http_headers = { "X-Session-ID" = "CODEX_SESSION_ID", "Authorization" = "FREEINFERENCE_API_KEY" }
request_max_retries = 5
```

3. Set up environment variables in your shell configuration file (`~/.zshrc` or `~/.bashrc`):

```bash
# Add these lines to ~/.zshrc or ~/.bashrc

# Generate unique session ID for each shell session
export CODEX_SESSION_ID="$(date +%Y%m%d-%H%M%S)-$(uuidgen)"

# Your FreeInference API key (note: include "Bearer " prefix)
export FREEINFERENCE_API_KEY="Bearer your-api-key-here"
```

4. Reload your shell configuration:

```bash
# For zsh
source ~/.zshrc

# For bash
source ~/.bashrc
```

5. Start using Codex with FreeInference!

---

## Cursor

[Cursor](https://cursor.sh/) is an AI-powered code editor built on VS Code.

### Configuration Steps

1. Open Cursor Settings
   - **macOS**: `Cmd + ,`
   - **Windows/Linux**: `Ctrl + ,`

2. Navigate to **API Keys** section in the settings

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

## Roo Code & Kilo Code

[Roo Code](https://roo.dev/) and [Kilo Code](https://kilo.dev/) are AI coding assistants with similar configuration.

### Configuration Steps

1. Install the Roo Code or Kilo Code extension/plugin in your IDE (VS Code or JetBrains)

2. Open the settings (click the settings icon in the extension panel)

3. In **API Provider**, select **OpenAI Compatible**

4. Configure the connection:
   ```
   Base URL: https://freeinference.org/v1
   API Key: your-api-key-here
   ```

5. Select your preferred model (e.g., `glm-4.7`, `glm-4.7-flash`, `llama-3.3-70b-instruct`, etc.)

6. Save settings and start using with FreeInference!

---

## Codebase Indexing

FreeInference provides a free embedding endpoint (`/v1/embeddings`) powered by **BGE-M3** (1024 dimensions) and a **shared Qdrant vector database** via the proxy at `/v1/qdrant`. This enables codebase indexing for semantic code search in supported IDEs — no local Docker setup needed.

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
| **Model** | `bge-m3` |
| **Model Dimension** | `1024` |
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
| **Model** | `bge-m3` |
| **Model Dimension** | `1024` |
| **Qdrant URL** | `https://freeinference.org/v1/qdrant` |
| **Qdrant API Key** | Your FreeInference API key |

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
    model="bge-m3",
    input=["def hello():", "function greet() {"],
)

for item in response.data:
    print(f"Index {item.index}: {len(item.embedding)} dimensions")
```

```bash
curl -X POST https://freeinference.org/v1/embeddings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key-here" \
  -d '{"model": "bge-m3", "input": "hello world"}'
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
- Try switching to a different model like `glm-4.7` or `glm-4.7-flash`

### Codex-Specific Issues

**Environment variables not loaded:**
- Make sure you've reloaded your shell configuration after editing `~/.zshrc` or `~/.bashrc`
- Verify variables are set: `echo $FREEINFERENCE_API_KEY`
- Open a new terminal window to ensure variables are loaded

**Session ID issues:**
- The session ID is auto-generated each time you start a new shell
- If needed, you can manually set it: `export CODEX_SESSION_ID="custom-session-id"`

**Config file not found:**
- Ensure the directory exists: `mkdir -p ~/.codex`
- Check file permissions: `ls -la ~/.codex/config.toml`

### Cursor-Specific Issues

**API key not working:**
- Make sure you've enabled the **OpenAI API Key** toggle
- Try removing and re-entering the API key
- Restart Cursor after configuration

**Base URL not applied:**
- Ensure there are no trailing slashes in the URL
- The URL should be exactly: `https://freeinference.org/v1`

### Roo Code / Kilo Code Issues

**Provider not connecting:**
- Verify **OpenAI Compatible** is selected as the provider
- Check that the base URL and API key are correct
- Try reloading the extension

---

## Need Help?

- [Available Models](models.md) - Complete model specifications
- [Quick Start Guide](quickstart.md) - Get started in 5 minutes
- Report issues on GitHub
