# FreeInference

Free LLM inference for coding agents and AI-powered IDEs.

## Overview

FreeInference provides free access to state-of-the-art language models for coding agents and AI-powered IDEs, with a particularly smooth setup path for Kilo Code.

## Documentation

- Production: https://doc.freeinference.org/
- Staging (tracks `dev` branch): https://doc.staging.freeinference.org/

## Supported IDEs & Coding Agents

- **[Kilo Code](https://kilocode.ai)** - AI coding assistant with the recommended FreeInference setup path
- **[Cursor](https://cursor.sh/)** - AI-powered code editor
- **[Roo Code](https://roocode.com)** - VS Code & JetBrains extension
- And any tool that supports OpenAI-compatible APIs

## Quick Start

### Kilo Code Setup (Recommended)

1. Install the Kilo Code extension or plugin in your IDE
2. Open the Kilo Code panel
3. Open Kilo Code settings
4. Set **API Provider** to **OpenAI Compatible**
5. Configure:
   - Base URL: `https://freeinference.org/v1`
   - API Key: `your-api-key-here`
6. Pick a model:
   - `glm-5.1` for general coding work
   - `qwen3.6-35b` for faster iterations
   - `minimax-m3` for long-context or multimodal work
   - `deepseek-v4-flash` for complex agentic coding
7. Save settings and start a new Kilo Code session

### Cursor Setup

1. Open Settings (`Cmd + ,` or `Ctrl + ,`)
2. Go to **Models** section
3. Enter your FreeInference API key
4. Click **Override OpenAI Base URL**
5. Enter: `https://freeinference.org/v1`
6. Enable the toggle and start coding!

### Roo Code Setup

1. Install the extension in your IDE
2. Open settings
3. Select **OpenAI Compatible** as provider
4. Configure:
   - Base URL: `https://freeinference.org/v1`
   - API Key: `your-api-key-here`
5. Select your preferred model

## Available Models

- **GLM-5.1** - 200K context, general coding and bilingual work
- **DeepSeek V4 Flash** - 1M context, agentic coding and reasoning
- **Qwen3.6 35B** - 262K context, fast non-thinking model
- **MiniMax M3** - 1M context, multimodal input
- **MiniMax M2.5** - 205K context, thinking mode
- **DiffusionGemma** - 262K context, fast local text model
- **BGE-M3** - 8K-context embedding model for codebase indexing

Pro-enabled keys also expose **GLM-5.2** and **Kimi K2.7 Code**. Query
`GET https://freeinference.org/v1/models` with your API key for the current
catalog available to your account.

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
