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
| `glm-5.3` | Pro | 1M | 131K | Text | Tools, structured output, always-on thinking with `reasoning_effort`, tool streaming |
| `kimi-k2.7-code` | Pro | 262K | 131K | Text, image, video | Coding agents, tools, structured output, thinking |

All listed models produce text. Context and output limits are deployment
limits; an upstream provider may enforce a smaller limit for an individual
request.

## DeepSeek Upstream Reference Pricing

The following prices are DeepSeek's upstream API reference rates in U.S.
dollars per 1 million tokens. The gateway uses them for cost accounting and
route selection; they are not fees charged by FreeInference to users. See the
[official DeepSeek pricing page](https://api-docs.deepseek.com/quick_start/pricing/)
for the latest rates.

Before **2026-08-16 16:00 UTC**, the reference rates are:

| Model | Input (cache hit) | Input (cache miss) | Output |
|-------|------------------:|-------------------:|-------:|
| V4 Flash (`deepseek-v4-flash`) | $0.0028 | $0.14 | $0.28 |
| V4 Pro (`deepseek-v4-pro`) | $0.003625 | $0.435 | $0.87 |

From **2026-08-16 16:00 UTC**, DeepSeek uses peak and off-peak rates. Peak
hours are **01:00–04:00 UTC** and **06:00–10:00 UTC** each day; all other
hours are off-peak.

| Model | Period | Input (cache hit) | Input (cache miss) | Output |
|-------|--------|------------------:|-------------------:|-------:|
| V4 Flash (`deepseek-v4-flash`) | Off-peak | $0.007 | $0.22 | $0.66 |
| V4 Flash (`deepseek-v4-flash`) | Peak | $0.014 | $0.44 | $1.32 |
| V4 Pro (`deepseek-v4-pro`) | Off-peak | $0.022 | $0.66 | $1.98 |
| V4 Pro (`deepseek-v4-pro`) | Peak | $0.044 | $1.32 | $3.96 |

The V4 Pro rows document the upstream rate for completeness; they do not add
`deepseek-v4-pro` to the generally available FreeInference catalog.
Availability remains determined by the authenticated `GET /v1/models`
response, and administrator-only models remain omitted from the overview.

## Embedding Model

| Model ID | Access | Context | Input | Output |
|----------|--------|---------|-------|--------|
| `bge-m3` | Free | 8K | Text | Embedding vector |

Use `bge-m3` with the OpenAI-compatible `/v1/embeddings` endpoint for codebase
indexing. It is not a chat or completion model.

## Choosing a Model

- Start with `glm-5.1` for general coding and bilingual work.
- Use `qwen3.6-35b` for quick edits and background-agent calls that do not need
  extended reasoning.
- Use `minimax-m3` for long-context or multimodal work.
- Use `deepseek-v4-flash` for complex agentic coding and long reasoning chains.
- Pro users can choose `glm-5.3` for the strongest coding results, `glm-5.2`
  when thinking has to be switchable, or `kimi-k2.7-code` for coding-agent
  workflows. `glm-5.3` and `glm-5.2` share the same 1M context.
- `glm-5.3` always reasons: it rejects `thinking: {"type": "disabled"}`. Send
  `reasoning_effort` (`low`, `high`, `max`) to control how much it thinks, and
  stay on `glm-5.2` if your client needs thinking fully off.

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
