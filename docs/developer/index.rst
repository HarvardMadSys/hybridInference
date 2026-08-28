HybridInference
===============

HybridInference is an open-source LLM inference gateway. It puts one
OpenAI-compatible HTTP API in front of a mix of inference servers you run
yourself — vLLM, SGLang, Ollama, or anything else that speaks the OpenAI API —
and hosted provider APIs, then decides per request which of them answers.

The idea the whole system is built around is that the *model id* a client asks
for is decoupled from the *endpoint* that serves it. One published id can be
backed by several routes at once, so traffic across them can be weighted,
failed over when an endpoint degrades, priced, and logged — without the client
changing a line.

This is the documentation for the gateway software itself: running it,
understanding it, extending it, and contributing to it.

What is in the box
------------------

- **An OpenAI-compatible surface** — ``POST /v1/chat/completions``,
  ``POST /v1/embeddings``, ``POST /v1/responses`` and ``GET /v1/models``, plus
  an Anthropic Messages surface at ``/v1/messages`` and
  ``/anthropic/v1/messages`` for clients that speak that protocol instead.
- **A routing engine** — a per-model choice of router, weighted selection among
  an id's routes, automatic fallback to the next route, and a per-endpoint
  circuit breaker that pulls a failing endpoint out of rotation.
- **Provider adapters** — a generic OpenAI-compatible adapter, which also
  serves your own local servers, alongside dedicated adapters for OpenRouter,
  Anthropic's direct API, Claude via Google Vertex, and Gemini.
- **A web and admin console** — a Next.js app for sign-up and login, API keys,
  usage and recent requests, a chat playground, and an admin area covering
  users, model visibility, provider keys, route weights and analytics.
- **Operational storage** — Postgres for accounts, API keys and a request log,
  with the schema created on startup rather than by a migration tool.

The backend is Python (3.10–3.13) under ``apps/backend/``, split into
``serving/`` (HTTP surface, auth, adapters, storage, observability) and
``routing/`` (route table, strategies, endpoint health, fallback). The console
is Next.js under ``apps/frontend/``. The repository is MIT-licensed.

Where to start
--------------

.. list-table::
   :header-rows: 1
   :widths: 45 55

   * - If you want to
     - Start here
   * - Watch a gateway serve a request, with no account, key or GPU
     - :doc:`router-tutorial`
   * - Run your own gateway against real providers
     - :doc:`installation`
   * - Follow a request from HTTP through to an upstream call
     - :doc:`architecture`
   * - Publish a new model id, or wire up a provider the gateway has never
       talked to
     - :doc:`adding-models`
   * - Change how an endpoint gets chosen, or write your own strategy
     - :doc:`routing`
   * - Send a patch
     - :doc:`contributing`

Scope of this site
------------------

These pages document the software, not any one installation of it. Which
models a given gateway serves, and how to get an account on it, are the
operator's to publish separately.

That separation is built into the repository: a deployment keeps its identity,
its configuration-file locations and its feature switches in a *distribution
overlay* under ``distributions/`` rather than in the code, which is why a fresh
clone comes up as nobody's gateway but your own. :doc:`configuration` explains
how those overlays resolve.

.. toctree::
   :maxdepth: 2
   :caption: Getting Started

   router-tutorial
   installation
   configuration

.. toctree::
   :maxdepth: 2
   :caption: Concepts

   architecture
   routing
   edge-and-console-routing

.. toctree::
   :maxdepth: 2
   :caption: Guides

   adding-models
   add-local-model
   openrouter
   hpc-model-host
   claude-code-setup
   rag-chat

.. toctree::
   :maxdepth: 2
   :caption: Operations

   deployment
   database
   staging
   trusted-proxies-and-client-ips
   automation-score
   codex-oncall

.. toctree::
   :maxdepth: 2
   :caption: Contributing

   contributing
