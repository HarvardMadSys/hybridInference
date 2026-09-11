# HybridInference

HybridInference is an open-source, self-hosted LLM gateway for serving local
models and external model APIs to a team. It is developed by the
[Harvard MadSys Lab](https://juncheng.seas.harvard.edu/) at Harvard SEAS and powers
[FreeInference](https://freeinference.org/).

## Who It's For

Labs and teams serving LLMs to their members from their own GPUs, external
APIs, or both.

- [RouteWise](#routewise): provider selection for the same model by price and
  measured time-to-first-token, under a routing cost budget you set.
- One OpenAI-compatible endpoint for vLLM, SGLang, Ollama, and remote providers,
  with multiple routes per model, weighted traffic splits, and health-aware
  fallback.
- User dashboards for API keys and usage, and an [admin console](#admin-console)
  for operating the gateway.

## Admin Console

Admins can approve or suspend accounts, set daily quotas and per-user
concurrency limits, and restrict model access. Provider credentials, model
routes, routing weights, and RouteWise settings are editable in the console.

Provider pages show availability, latency, and generation speed, with endpoint
probes for troubleshooting. Request logs include the user, provider, errors,
token usage, cost, first-token latency, and cached tokens. RouteWise requests
also show whether a backup request was sent and whether it won.

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
  uv run uvicorn serving.servers.app:app --no-proxy-headers --port 8080
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

## RouteWise

**RouteWise** is the router that decides which provider serves each request:
given a cost budget you set, it picks a point on the cost--latency Pareto
frontier across the providers that can serve the model.

`router: fixed` splits traffic by weights you choose. `router: routewise`
instead solves a small cost-budgeted linear program per request over every
provider that can serve the model, using their prices and the time-to-first-token
it has measured from each one, samples that solution to pick one, and can
dispatch a hedged backup when the primary looks unlikely to meet the latency
target. One knob, `budget_alpha`, moves the policy from "never spend more than
the cheapest provider" to "spend up to the dearest one if it buys latency".

RouteWise is published separately as the MIT-licensed
[`llm-routewise`](https://github.com/HarvardMadSys/RouteWise) library, with its
own [documentation](https://harvardmadsys.github.io/RouteWise/); this gateway
takes it as a required dependency. The library is deliberately gateway-agnostic:
it performs no network I/O and reads no credentials, so any application can use
it to choose a provider and report the outcome back.
Everything needed to run that decision against real providers — adapters,
credentials, dispatch, health, hedged execution, accounting — is what this
repository adds.

### Try it

Two copies of the bundled example fixture stand in for two providers serving
one model: premium answers immediately and costs more, budget is cheap and
400 ms slower. No account, no database.

```bash
F=distributions/example/fixtures/fake-openai-provider/server.py
uv run python $F --port 18351 --response-text ROUTED_TO_PREMIUM &
uv run python $F --port 18352 --response-text ROUTED_TO_BUDGET --ttft-delay-ms 400 &

PYTHONPATH=apps/backend \
  MODELS_CONFIG_PATH=config/examples/models.routewise.yaml \
  ROUTING_CONFIG_PATH=config/examples/routing.minimal.yaml \
  DB_ENABLED=false USER_AUTH_ENABLED=false \
  uv run uvicorn serving.servers.app:app --no-proxy-headers --port 8080
```

Give it about ten seconds to measure both endpoints, then ask for a completion.
The reply text names the provider RouteWise picked:

```bash
curl localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "routewise-demo", "messages": [{"role": "user", "content": "hi"}]}'
```

When you are done, stop the two fixtures — they hold 18351 and 18352, and a
second run of the block above would fail to bind:

```bash
pkill -f fake-openai-provider/server.py
```

The example ships `budget_alpha: 0.0`, so every reply is `ROUTED_TO_BUDGET`:
the LP may not spend more than the cheapest eligible provider. Set it to `1.0`
in `config/examples/models.routewise.yaml`, restart, and — after another ten
seconds, since the restart drops the measurements with the process — every
reply becomes `ROUTED_TO_PREMIUM`: the wider cost budget lets the policy buy
the 400 ms. That one edit is the cost/latency tradeoff the paper is about.

That registry doubles as the annotated reference for every RouteWise option;
the [routing guide](docs/developer/routing.md#routewise)
explains the configuration contract.

### Citation

The design is described in *RouteWise: Latency--Cost Optimization for
Multi-Provider LLM Routing*, to appear at
[EuroSys '27](https://2027.eurosys.org/):

```bibtex
@inproceedings{tian2027routewise,
  title     = {{RouteWise}: Latency--Cost Optimization for Multi-Provider LLM Routing},
  author    = {Muxin Tian and Haoran Ni and Yiyan Zhai and Yangsun Park and Juncheng Yang},
  booktitle = {Proceedings of the 22nd European Conference on Computer Systems (EuroSys '27)},
  year      = {2027}
}
```

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
