# HybridInference User README

This guide is for people who want to use FreeInference as a hosted OpenAI-compatible API or run HybridInference as their own inference gateway.

## Choose Your Path

- **Use hosted FreeInference** if you want an API key, a stable base URL, hosted model access, and integrations with coding agents or OpenAI-compatible SDKs.
- **Self-host HybridInference** if you want to route your own traffic across local GPUs, internal models, or provider accounts while keeping an OpenAI-compatible interface.

## Hosted API Quick Start

1. Create an account at [freeinference.org](https://freeinference.org/).
2. Create an API key from the dashboard.
3. Use the OpenAI-compatible base URL: `https://freeinference.org/v1`.
4. Send a test request:

```bash
curl https://freeinference.org/v1/chat/completions \
  -H "Authorization: Bearer $FREEINFERENCE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.1",
    "messages": [
      {"role": "user", "content": "Say hello from FreeInference."}
    ]
  }'
```

## Python SDK Example

```python
from openai import OpenAI

client = OpenAI(
    api_key="your-api-key-here",
    base_url="https://freeinference.org/v1",
)

response = client.chat.completions.create(
    model="glm-5.1",
    messages=[{"role": "user", "content": "Write a one-line haiku about GPUs."}],
)

print(response.choices[0].message.content)
```

## Integrations

FreeInference works with clients that support OpenAI-compatible APIs. The public docs include setup guides for Kilo Code, Cursor, Claude Code, Roo Code, Cline, Continue, Aider, and other tools.

- Quick start: [distributions/freeinference/content/docs/docs/source/quickstart.md](distributions/freeinference/content/docs/docs/source/quickstart.md)
- Integration guides: [distributions/freeinference/content/docs/docs/source/integrations.md](distributions/freeinference/content/docs/docs/source/integrations.md)
- Available models: [distributions/freeinference/content/docs/docs/source/models.md](distributions/freeinference/content/docs/docs/source/models.md)
- API headers: [distributions/freeinference/content/docs/docs/source/api_headers.md](distributions/freeinference/content/docs/docs/source/api_headers.md)

## Self-Hosting Quick Start

The production-oriented path uses Docker and Docker Compose.

```bash
git clone https://github.com/HarvardMadSys/hybridInference.git
cd hybridInference

cp .env.example .env
# Edit .env with database credentials, auth secrets, and provider keys.

make up
make ps
```

For local development without the full production stack, see [README.developer.md](README.developer.md) and [docs/developer/installation.md](docs/developer/installation.md).

## Configuration Overview

The gateway is configured primarily through environment variables and YAML files.

- `.env`: secrets, database settings, provider API keys, auth settings, and runtime options.
- `config/models.yaml`: model registry and endpoint definitions.
- `distributions/<name>/config/routing.yaml`: local/remote split, routing strategy, health checks. Upstream ships none; the gateway starts without one.
- `distributions/<name>/config/alerts.yaml`: alert rules. Upstream ships none; without one the built-in thresholds apply.

YAML configuration supports environment variable interpolation with `${VAR}` and `${VAR:-default}` syntax.

## Local And Remote Providers

HybridInference can route to local inference backends and remote providers.

- Local backends include vLLM, SGLang, and Ollama.
- Remote providers can be connected through OpenAI-compatible adapters or provider-specific adapters.
- Routing can be fixed, weighted, health-aware, and configured per model.

For model setup, see [docs/developer/adding-models.md](docs/developer/adding-models.md) and [docs/developer/add-local-model.md](docs/developer/add-local-model.md).

## Troubleshooting

- **Hosted API key rejected:** confirm the key is active in the dashboard and sent as `Authorization: Bearer <key>`.
- **Model not found:** check the model list in the public docs or your self-hosted `config/models.yaml`.
- **Self-hosted database errors:** verify `.env` database values and start the database service before the backend.
- **Local GPU endpoint unreachable:** verify the model server is running and that Docker networking points to the correct host.

## More Help

- Hosted docs: [doc.freeinference.org](https://doc.freeinference.org/)
- Developer docs: [internaldoc.freeinference.org](https://internaldoc.freeinference.org/)
- Contributor guide: [README.developer.md](README.developer.md)
