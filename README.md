# HybridInference

HybridInference is an open-source multi-provider LLM gateway, and the production reference integration of [RouteWise](#routewise), the cost--latency routing system from our EuroSys '27 paper. It routes requests across local inference servers and remote OpenAI-compatible providers, and turns RouteWise's per-request decision into a deployable system: provider adapters, an OpenAI-compatible API, health-aware fallback, observability, configuration, and a control plane. It powers FreeInference, and can be self-hosted as a standalone gateway. HybridInference is developed by the [Harvard MadSys Lab](https://juncheng.seas.harvard.edu/) at Harvard SEAS.

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

The developer documentation is published at
**[doc.hybridinference.org](https://doc.hybridinference.org/)**
(also in [Simplified Chinese](https://doc.hybridinference.org/zh_CN/)),
and its sources are in [`docs/developer/`](docs/developer/). Start at whichever
row describes you:

| If you want to | Start here |
|---|---|
| Watch a gateway serve a request, with no account, key or GPU | [Quickstart](docs/developer/router-tutorial.md) |
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

## What It Does

- Exposes an OpenAI-compatible API for chat/completions workflows.
- Routes traffic across local backends such as vLLM, SGLang, and Ollama.
- Connects to remote providers through provider-specific and OpenAI-compatible adapters.
- Runs [RouteWise](#routewise), a cost- and latency-aware router that picks
  between providers serving the same model under an explicit cost budget.
- Supports weighted routing, health-aware fallback, circuit breaking, and per-model routing configuration.
- Includes a FastAPI backend, a Next.js dashboard, storage integrations, operational tooling, and documentation sites.

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

RouteWise is developed by the [Harvard MadSys Lab](https://juncheng.seas.harvard.edu/)
and published separately as the MIT-licensed
[`llm-routewise`](https://github.com/HarvardMadSys/RouteWise) library, which
this gateway takes as a required dependency. The library is deliberately
gateway-agnostic: it performs no network I/O and reads no credentials, so any
application can use it to choose a provider and report the outcome back.
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
  uv run uvicorn serving.servers.app:app --port 8080
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
services/         # Generic protocol-conformance harness and testkit
tests/            # Unit, API, integration, e2e, and external tests
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
