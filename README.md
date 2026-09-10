# HybridInference

HybridInference is an open-source, self-hosted LLM gateway for serving local
models and external model APIs to a team. It is developed by the
[Harvard MadSys Lab](https://juncheng.seas.harvard.edu/) at Harvard SEAS and powers
[FreeInference](https://freeinference.org/).

## Who It's For

Labs and teams serving LLMs to their members from their own GPUs, external
APIs, or both.

- One OpenAI-compatible endpoint in front of local servers such as vLLM, SGLang,
  and Ollama, and remote providers: any OpenAI-compatible API, Anthropic,
  Gemini, and OpenRouter.
- Several routes per model, weighted traffic splits, and health-aware fallback;
  [RouteWise](#routing) adds cost- and latency-aware route selection.
- User dashboards for API keys and usage, and an [admin console](#admin-console)
  for operating the gateway.

## Admin Console

Admins can approve or suspend accounts, set daily quotas and per-user
concurrency limits, and restrict model access. Provider credentials, model
routes, and routing settings are editable in the console.

Provider pages show availability, latency, and generation speed, with endpoint
probes for troubleshooting. Request logs include the user, provider, errors,
token usage, cost, first-token latency, and cached tokens.

## Quickstart

### Gateway with the Web and Admin Consoles

Requires Git, Make, Python 3.10–3.13, and a running Docker daemon with Compose.
The local example runs the gateway, Postgres, and both consoles with a
simulated model provider. No GPU or provider API key is needed.

```bash
git clone https://github.com/HarvardMadSys/hybridInference.git hybridinference
cd hybridinference

make up DISTRIBUTION=example
make smoke DISTRIBUTION=example
make demo DISTRIBUTION=example
```

Open [localhost:13001/signup](http://localhost:13001/signup). Sign up as
`admin@local.dev` with a demo-only password (at least eight characters,
including uppercase, lowercase, and a number), then sign in. From the
dashboard, create an API key, open the API Playground, or enter the Admin
Console. The example model returns the fixed reply `RUNNABLE_EXAMPLE_OK`.

When you are done, stop the stack. Its database volume is kept for the next
run:

```bash
make demo-down DISTRIBUTION=example
```

To connect real models, add a provider, credentials, and model routes through
the [admin console](docs/developer/configuration.md#runtime-configuration-from-the-admin-console),
or follow the [local server setup](docs/developer/router-tutorial.md#stage-3-replace-the-fake-provider-with-local-inference).
The [Router Tutorial](docs/developer/router-tutorial.md) covers prerequisites,
API calls, request-history checks, and how to resume or reset the example.

### Backend with an OpenRouter Key

For a backend-only setup against real models, install
[Python and uv](docs/developer/installation.md#development-checkout-no-docker)
and run the following from the repository root. The reference registry includes
two OpenRouter-served models and an optional local route.

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

## Start Here

The developer documentation is published at
**[doc.hybridinference.org](https://doc.hybridinference.org/)**
(also in [Simplified Chinese](https://doc.hybridinference.org/zh_CN/)),
and its sources are in [`docs/developer/`](docs/developer/). Start at whichever
row describes you:

| If you want to | Start here |
|---|---|
| Follow the tutorial through to a local vLLM, SGLang, or Ollama server | [Router Tutorial](docs/developer/router-tutorial.md) |
| Run your own gateway against real providers | [Installation](docs/developer/installation.md) |
| Understand how a request becomes a routing decision | [Architecture](docs/developer/architecture.md) |
| Add a model, a local server, or a new provider | [Adding a New Model](docs/developer/adding-models.md) |
| Operate one in production | [Deployment Guide](docs/developer/deployment.md) |
| Send a change | [Contributing](docs/developer/contributing.md) |

Bugs and questions go to the
[issue tracker](https://github.com/HarvardMadSys/hybridInference/issues).
Security reports have their own channel — see [SECURITY.md](SECURITY.md).

**See one in production:** [FreeInference](https://freeinference.org/) is a
public HybridInference gateway run at Harvard SEAS; its
[user documentation](https://doc.freeinference.org/) is a worked example of
what a deployment publishes.

## Routing

A model in the registry can list several routes. `router: fixed` splits traffic
across them by the weights you set and falls back to another route when one
fails; the [routing guide](docs/developer/routing.md) covers endpoint health,
the circuit breaker, and fallback.

`router: routewise` instead picks the route per request from provider prices
and the time-to-first-token it has measured, within a cost budget you set
(`budget_alpha`). It is the MIT-licensed
[`llm-routewise`](https://github.com/HarvardMadSys/RouteWise) library, published
separately and described in *RouteWise: Latency--Cost Optimization for
Multi-Provider LLM Routing* (to appear at EuroSys '27); that repository carries
the citation. The annotated
[`models.routewise.yaml`](config/examples/models.routewise.yaml) runs it against
two bundled fixtures with no provider account, and the
[RouteWise section](docs/developer/routing.md#routewise) of the routing guide
documents the options.

## Repository Map

```text
apps/
  backend/
    serving/      # FastAPI gateway, adapters, auth, storage, observability
    routing/      # Routing strategies, routers, health, circuit breaker
  frontend/       # Next.js web UI
benchmark/        # Benchmark utilities
config/
  examples/       # Reference registries and routing config; also the built-in
                  # fallback a checkout with no overlay resolves to. A
                  # deployment's own config lives in distributions/<name>/.
distributions/    # Per-distribution overlays: identity, content, config
                  # (including example/, the public runnable router example)
tests/            # Unit, API, integration, and external tests
ops/              # CI classifiers, admin sweeps, release/deploy scripts, backend-coupled DB analysis
deploy/           # Docker, systemd, observability manifests
docs/             # Developer docs, agent specs/plans, reviews
```

## Documentation

- The developer documentation site — [doc.hybridinference.org](https://doc.hybridinference.org/), sources in [`docs/developer/`](docs/developer/) — is the single place this project documents itself: setup, architecture, routing, configuration, deployment, extension, and the contribution workflow. Edit the sources, not a copy.
- `distributions/` holds per-deployment overlays. A deployment's identity, content and documentation live in its own overlay rather than in the code, which is why a fresh clone comes up as nobody's gateway but your own.
- A deployment's *user*-facing documentation — which models it serves, how to get an account — is the operator's to publish, not this repository's.

## Provider Terms

You connect providers with your own credentials — this project ships none — so
each provider's terms bind you, not the gateway. Read them before adding a
route.

Some plans, in particular the subscription and coding-plan tiers that several
providers offer, are licensed for individual personal use and do not permit
reselling, sharing or otherwise redistributing the capacity they grant. The
gateway will let you configure such a route; that is not the same as being
permitted to. If a plan is licensed to you personally, route it only for your
own personal, non-commercial or research use.

## License

This repository is licensed under the [MIT License](LICENSE). That covers its
source code alone; it grants no rights to any third-party model, API or
subscription you route to.
