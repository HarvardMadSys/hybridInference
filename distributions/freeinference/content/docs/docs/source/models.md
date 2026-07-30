# Available Models

FreeInference exposes an OpenAI-compatible model catalog for coding agents and
IDEs. The catalog changes as providers and local deployments change, so the
authenticated API is the source of truth for your account:

```bash
curl -H "Authorization: Bearer $FREEINFERENCE_API_KEY" \
  https://freeinference.org/v1/models
```

The response is filtered by access level. A model that is not returned for
your key is not available to that account.

## Model Overview

| Model ID | Access | Context | Max Output | Input | Highlights |
|----------|--------|---------|------------|-------|------------|
| `glm-5.1` | Free | 200K | 128K | Text | Tools, structured output, thinking, tool streaming |
| `minimax-m2.5` | Free | 205K | 131K | Text | Tools, structured output, thinking |
| `minimax-m3` | Free | 1M | 131K | Text, image, video | Long context, tools, structured output, thinking |
| `qwen3.6-35b` | Free | 262K | 8K | Text, image, video | Fast non-thinking model, tools, structured output |
| `diffusiongemma` | Free | 262K | 8K | Text | Fast local model, tools, structured output, thinking |
| `deepseek-v4-flash` | Free | 1M | 393K | Text | Agentic coding, tools, structured output, reasoning controls |
| `glm-5.2` | Pro | 1M | 131K | Text | Tools, structured output, thinking, tool streaming |
| `kimi-k2.7-code` | Pro | 262K | 131K | Text, image, video | Coding agents, tools, structured output, thinking |

All listed models produce text. Context and output limits are deployment
limits; an upstream provider may enforce a smaller limit for an individual
request.

## Choosing a Model

- Start with `glm-5.1` for general coding and bilingual work.
- Use `qwen3.6-35b` for quick edits and background-agent calls that do not need
  extended reasoning.
- Use `minimax-m3` for long-context or multimodal work.
- Use `deepseek-v4-flash` for complex agentic coding and long reasoning chains.
- Pro users can choose `glm-5.2` for its larger context or
  `kimi-k2.7-code` for coding-agent workflows.

## Access and Retired IDs

Free accounts can use models marked **Free**. Models marked **Pro** require a
Pro-enabled key. Operational models restricted to administrators or staff are
intentionally omitted from the public recommendation list.

Old IDs such as `glm-4.7` and `minimax-m2.7` are no longer in the production
catalog. If a saved IDE configuration uses a retired ID, select one returned
by `GET /v1/models`; otherwise the gateway returns `404 Model not found`.

## Switching Models

Use the exact model ID from `GET /v1/models` in your client's model selector or
configuration. In Claude Code, use `/model`; for provider-specific setup, see
the [IDE and coding-agent integrations](integrations.md).
