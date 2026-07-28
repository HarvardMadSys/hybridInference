# HybridInference User README

This guide is for people running a HybridInference gateway, or calling one
that someone else runs.

Either way the API is the same: OpenAI-compatible, so existing SDKs and
coding agents work by changing a base URL.

## Run Your Own

The fastest path is the [Quickstart](README.md#quickstart) in the main README —
one credential, no database, real models in about a minute.

For a persistent deployment with accounts, API keys and quotas, use Docker
Compose:

```bash
git clone https://github.com/HarvardMadSys/hybridInference.git
cd hybridInference

cp .env.example .env
# Edit .env with database credentials, auth secrets, and provider keys.

make up
make ps
```

This brings up a gateway that names no deployment but yours. To give it an
identity — its own name, links, support address and console branding — see
`distributions/` and the overlay pattern described in
[README.developer.md](README.developer.md).

For local development without the full production stack, see
[README.developer.md](README.developer.md) and
[docs/developer/installation.md](docs/developer/installation.md).

## Call One Someone Else Runs

A HybridInference deployment exposes an OpenAI-compatible base URL. Get an API
key from its dashboard, then:

```bash
curl https://<gateway>/v1/chat/completions \
  -H "Authorization: Bearer $HYBRIDINFERENCE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<model-id>",
    "messages": [
      {"role": "user", "content": "Say hello."}
    ]
  }'
```

```python
from openai import OpenAI

client = OpenAI(
    api_key="your-api-key-here",
    base_url="https://<gateway>/v1",
)

response = client.chat.completions.create(
    model="<model-id>",
    messages=[{"role": "user", "content": "Write a one-line haiku about GPUs."}],
)

print(response.choices[0].message.content)
```

`GET /v1/models` lists what a given gateway serves.

### FreeInference

[FreeInference](https://freeinference.org/) is a public HybridInference
gateway run at Harvard SEAS, free for research use. Create an account, create
a key, and use `https://freeinference.org/v1` as the base URL. Its
[documentation](https://doc.freeinference.org/) covers the available models
and setup guides for Kilo Code, Cursor, Claude Code, Roo Code, Cline,
Continue, Aider and other OpenAI-compatible clients.

## Configuration Overview

The gateway is configured primarily through environment variables and YAML files.

- `.env`: secrets, database settings, provider API keys, auth settings, and runtime options.
- `config/models.yaml`: model registry and endpoint definitions.
- `config/routing.yaml`: local/remote split, routing strategy, and health-check settings.
- `config/alerts.yaml`: alert rules.
- `config/examples/`: reference registries you can run as-is.

YAML configuration supports environment variable interpolation with `${VAR}` and `${VAR:-default}` syntax.

## Local And Remote Providers

HybridInference can route to local inference backends and remote providers.

- Local backends include vLLM, SGLang, and Ollama.
- Remote providers can be connected through OpenAI-compatible adapters or provider-specific adapters.
- Routing can be fixed, weighted, health-aware, and configured per model.

For model setup, see [docs/developer/adding-models.md](docs/developer/adding-models.md) and [docs/developer/add-local-model.md](docs/developer/add-local-model.md).

## Troubleshooting

- **API key rejected:** confirm the key is active in the gateway's dashboard and sent as `Authorization: Bearer <key>`.
- **Model not found:** call `GET /v1/models` on the gateway, or check your own `config/models.yaml`.
- **Database errors on startup:** verify the `.env` database values and start the database service before the backend. To run without a database at all, set `DB_ENABLED=false` — you lose accounts, keys and history.
- **Local GPU endpoint unreachable:** verify the model server is running and that Docker networking points to the correct host.

## More Help

- Deeper guides: `docs/developer/`
- Contributor guide: [README.developer.md](README.developer.md)
- Issues and discussion: [github.com/HarvardMadSys/hybridInference](https://github.com/HarvardMadSys/hybridInference)
