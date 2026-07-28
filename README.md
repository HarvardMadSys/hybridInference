# HybridInference

HybridInference is an open-source LLM inference gateway for routing requests across local inference servers and remote OpenAI-compatible providers. It powers FreeInference, but it can also be self-hosted as a standalone gateway for teams that need provider fallback, local/remote routing, observability, and a familiar API surface.

## Quickstart

Run the gateway against real models with one credential. This uses the bundled
reference registry (`config/examples/models.openrouter.yaml`): two
OpenRouter-served models plus a local-first hybrid entry.

```bash
uv sync
export OPENROUTER_API_KEY=sk-or-...

PYTHONPATH=apps/backend \
  MODELS_CONFIG_PATH=config/examples/models.openrouter.yaml \
  DB_ENABLED=false USER_AUTH_ENABLED=false \
  uv run uvicorn serving.servers.app:app --port 8080
```

```bash
curl localhost:8080/v1/models

curl localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "llama-3.1-8b", "messages": [{"role": "user", "content": "hi"}]}'
```

`DB_ENABLED=false USER_AUTH_ENABLED=false` is the evaluation shortcut: no
Postgres, and every request is treated as an anonymous admin. A real
deployment drops both and gets user accounts, API keys, quotas and request
history — see the [Developer README](README.developer.md).

To route to a local model instead, point the first route of
`llama-3.1-8b-hybrid` at your own OpenAI-compatible server (Ollama, vLLM or
SGLang). It is preferred over the OpenRouter route, which stays as automatic
fallback.

## Start Here

- **Use the API or self-host the gateway:** [User README](README.user.md)
- **Develop, operate, or contribute:** [Developer README](README.developer.md)
- **Hosted service docs:** [doc.freeinference.org](https://doc.freeinference.org/)
- **Developer docs:** [internaldoc.freeinference.org](https://internaldoc.freeinference.org/)

## What It Does

- Exposes an OpenAI-compatible API for chat/completions workflows.
- Routes traffic across local backends such as vLLM, SGLang, and Ollama.
- Connects to remote providers through provider-specific and OpenAI-compatible adapters.
- Supports weighted routing, health-aware fallback, circuit breaking, and per-model routing configuration.
- Includes a FastAPI backend, a Next.js dashboard, storage integrations, operational tooling, and documentation sites.

## Repository Map

```text
apps/
  backend/
    serving/      # FastAPI gateway, adapters, auth, storage, observability
    routing/      # Routing strategies, routers, health, circuit breaker
    benchmark/    # Benchmark utilities
  frontend/       # Next.js web UI
config/           # Model, routing, and alert configuration
  examples/       # Reference registries, including the OpenRouter quickstart
distributions/    # Per-distribution overlays: identity, content, config
services/         # status-monitor-worker, freeinference-harness
tests/            # Unit, API, integration, e2e, and external tests
ops/              # Deployment, setup, runtime, admin, perf, DB, Cloudflare tooling
deploy/           # Docker, systemd, observability manifests
docs/             # Developer docs, agent specs/plans, reviews
```

## Documentation

- [README.user.md](README.user.md) explains how to use FreeInference, connect OpenAI-compatible clients, choose models, and run a self-hosted gateway.
- [README.developer.md](README.developer.md) explains local setup, project structure, testing, formatting, architecture, configuration, and contribution workflow.
- `distributions/freeinference/content/docs/` contains the hosted user documentation source.
- `docs/developer/` contains deeper architecture, deployment, routing, configuration, and extension guides.

## License

This repository is licensed under the [MIT License](LICENSE).
