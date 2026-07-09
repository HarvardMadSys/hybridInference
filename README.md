# HybridInference

HybridInference is an open-source LLM inference gateway for routing requests across local inference servers and remote OpenAI-compatible providers. It powers FreeInference, but it can also be self-hosted as a standalone gateway for teams that need provider fallback, local/remote routing, observability, and a familiar API surface.

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
config/           # Model, routing, routewise, and alert configuration
services/         # status-monitor-worker, freeinference-harness
tests/            # Unit, API, integration, e2e, and external tests
ops/              # Deployment, setup, runtime, admin, perf, DB, Cloudflare tooling
deploy/           # Docker, systemd, observability manifests
docs/             # User docs, developer docs, agent specs/plans, reviews
```

## Documentation

- [README.user.md](README.user.md) explains how to use FreeInference, connect OpenAI-compatible clients, choose models, and run a self-hosted gateway.
- [README.developer.md](README.developer.md) explains local setup, project structure, testing, formatting, architecture, configuration, and contribution workflow.
- `docs/free_inference/` contains the hosted user documentation source.
- `docs/developer/` contains deeper architecture, deployment, routing, configuration, and extension guides.

## License

This repository is licensed under the [MIT License](LICENSE).
