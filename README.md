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
  ROUTING_CONFIG_PATH=config/examples/routing.minimal.yaml \
  DB_ENABLED=false USER_AUTH_ENABLED=false \
  uv run uvicorn serving.servers.app:app --port 8080
```

```bash
curl localhost:8080/v1/models

curl localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "llama-3.1-8b", "messages": [{"role": "user", "content": "hi"}]}'
```

For a deterministic Docker path that needs no provider key, run the public
[router distribution example](distributions/example/README.md). The
[Router Tutorial](docs/developer/router-tutorial.md) follows one deployment
from its first routed request into the Web/Admin Console, accounts, API keys,
request history, and finally a local vLLM/SGLang/Ollama server. CI executes the
same Stage 1 → Stage 2 transition and verifies the Stage 3 override against a
local deterministic fixture. The user journey begins:

```bash
make up DISTRIBUTION=example
make smoke DISTRIBUTION=example
make demo DISTRIBUTION=example

# Sign up as admin@local.dev with a demo-only password, then reuse it here:
EXAMPLE_DEMO_ADMIN_PASSWORD='<the same password>' \
make demo-smoke DISTRIBUTION=example

make demo-down DISTRIBUTION=example
```

Stage 1 uses `DB_ENABLED=false USER_AUTH_ENABLED=false` so the first request
has only two moving parts. Stage 2 continues the same Compose project with
Postgres and authentication enabled; it is not a separate deployment mode.

The example pins its own model registry and minimal routing config. In Stage 3,
the public model remains `example-chat` while three documented environment
variables point its route at your local OpenAI-compatible server.

## Start Here

- **Run and use a gateway:** [User README](README.user.md)
- **Develop, operate, or contribute:** [Developer README](README.developer.md)
- **See one in production:** [FreeInference](https://freeinference.org/) is a
  public HybridInference gateway run at Harvard SEAS; its
  [user documentation](https://doc.freeinference.org/) is a worked example of
  what a deployment publishes.

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
                  # (including example/, the public runnable router example)
services/         # Generic protocol-conformance harness and testkit
tests/            # Unit, API, integration, e2e, and external tests
ops/              # Deployment, setup, runtime, admin, perf, DB, Cloudflare tooling
deploy/           # Docker, systemd, observability manifests
docs/             # Developer docs, agent specs/plans, reviews
```

## Documentation

- [README.user.md](README.user.md) explains how to run your own gateway, connect OpenAI-compatible clients, choose models, and call a hosted one.
- [README.developer.md](README.developer.md) explains local setup, project structure, testing, formatting, architecture, configuration, and contribution workflow.
- `docs/developer/` contains deeper architecture, deployment, routing, configuration, and extension guides.
- `distributions/` holds per-deployment overlays. A deployment's identity, content and documentation live in its own overlay rather than in the code, which is why a fresh clone comes up as nobody's gateway but your own.

## License

This repository is licensed under the [MIT License](LICENSE).
