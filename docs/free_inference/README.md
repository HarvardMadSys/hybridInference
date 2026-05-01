# FreeInference

Free LLM inference for coding agents and AI-powered IDEs.

## Overview

FreeInference provides free access to state-of-the-art language models specifically designed for coding agents like Cursor, Codex, Roo Code, and other AI-powered development tools.

## Documentation

- Production: https://doc.freeinference.org/
- Staging (tracks `dev` branch): https://doc.staging.freeinference.org/

## Supported IDEs & Coding Agents

- **[Cursor](https://cursor.sh/)** - AI-powered code editor
- **[Codex](https://codex.so/)** - Terminal-based coding assistant
- **[Roo Code](https://roo.dev/)** - VS Code & JetBrains extension
- **[Kilo Code](https://kilo.dev/)** - AI coding assistant
- And any tool that supports OpenAI-compatible APIs

## Quick Start

### Cursor Setup

1. Open Settings (`Cmd + ,` or `Ctrl + ,`)
2. Go to **API Keys** section
3. Enter your FreeInference API key
4. Click **Override OpenAI Base URL**
5. Enter: `https://freeinference.org/v1`
6. Enable the toggle and start coding!

### Codex Setup

1. Create `~/.codex/config.toml`:

```toml
model = "glm-4.7"
model_provider = "free_inference"

[model_providers.free_inference]
name = "FreeInference"
base_url = "https://freeinference.org/v1"
wire_api = "chat"
env_http_headers = { "X-Session-ID" = "CODEX_SESSION_ID", "Authorization" = "FREEINFERENCE_API_KEY" }
```

2. Add to `~/.zshrc` or `~/.bashrc`:

```bash
export CODEX_SESSION_ID="$(date +%Y%m%d-%H%M%S)-$(uuidgen)"
export FREEINFERENCE_API_KEY="Bearer your-api-key-here"
```

3. Reload: `source ~/.zshrc`

### Roo Code / Kilo Code Setup

1. Install the extension in your IDE
2. Open settings
3. Select **OpenAI Compatible** as provider
4. Configure:
   - Base URL: `https://freeinference.org/v1`
   - API Key: `your-api-key-here`
5. Select your preferred model

## Available Models

- **GLM-4.7** - 200K context, bilingual coding assistant
- **GLM-5** - 200K context, latest generation
- **GLM-5.1** - 200K context, enhanced version
- **GLM-5 Turbo** - 200K context, performance variant
- **Qwen3.6 27B** - 65K context, self-hosted
- **Qwen3.6 35B** - 65K context, self-hosted
- **MiniMax M2.7** - 196K context
- **MiniMax M2.5** - 1M context, multimodal (text + image)

See the [Models documentation](https://doc.freeinference.org/models.html) for the complete list.

## Get API Key

1. Visit [https://freeinference.org](https://freeinference.org)
2. Register for a free account
3. Log in and create your API key
4. Start using FreeInference with your favorite IDE!

## Documentation Links

- [Quick Start Guide](https://doc.freeinference.org/quickstart.html)
- [IDE Integration Guides](https://doc.freeinference.org/integrations.html)
- [Available Models](https://doc.freeinference.org/models.html)

## Support

- Documentation: https://doc.freeinference.org/
- Issues: GitHub Issues
- Questions: Contact the team
