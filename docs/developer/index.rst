HybridInference
===============

HybridInference is an open-source LLM inference gateway. It puts one
OpenAI-compatible HTTP API in front of inference servers you run yourself
(vLLM, SGLang, Ollama or any other OpenAI-compatible server) and hosted
provider APIs, and picks for each request which of them answers.

A client asks for a *model id*, and the gateway decides which *endpoint* serves
it. One id can have several routes, so the gateway can split traffic by weight,
fail over when an endpoint degrades, and price and log every request while the
client keeps sending the same id.

This site documents the gateway software, not any one deployment of it. Which
models a gateway serves, and how to get an account on it, are up to its
operator.

What is in the box
------------------

- **OpenAI-compatible API**: ``/v1/chat/completions``, ``/v1/embeddings``,
  ``/v1/responses``, ``/v1/models`` and the Batch API (``/v1/files``,
  ``/v1/batches``), plus Anthropic's Messages API at ``/v1/messages`` and
  ``/anthropic/v1/messages``.
- **Routing**: a router per model, weighted selection, fallback to the next
  route and a circuit breaker per endpoint.
- **Adapters**: a generic OpenAI-compatible adapter, which also serves local
  servers, and dedicated adapters for OpenRouter, Anthropic, Claude on Google
  Vertex and Gemini.
- **Console**: a Next.js app for sign-up, API keys, usage, request history and
  a chat playground, with an admin area for users, providers, models, routing
  and settings.
- **Postgres**: accounts, API keys, the request log and the gateway's
  configuration. The schema comes only from the migrations in
  ``apps/backend/migrations/``; the service issues no DDL.

The backend is Python 3.11–3.13 under ``apps/backend/``: ``serving/`` holds the
HTTP API, authentication, adapters, storage and observability, and ``routing/``
the route table, strategies, endpoint health and fallback. The console is
Next.js under ``apps/frontend/``.

A deployment keeps its identity, branding and model catalog in its own
database, and its code and assets in a *distribution overlay* under
``distributions/``. A fresh clone is therefore nobody's gateway but your own;
see :doc:`distribution-customization`.

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
   * - Choose what your distribution can configure or customize
     - :doc:`distribution-customization`
   * - Follow a request from HTTP through to an upstream call
     - :doc:`architecture`
   * - Serve another model from a provider the gateway already supports
     - :doc:`adding-models`
   * - Support a provider whose API the gateway cannot speak yet
     - :doc:`provider-adapters`
   * - Add a provider, a key or a model from the admin console, without a
       restart
     - :doc:`configuration`, under *Runtime configuration from the admin
       console*
   * - Change how an endpoint gets chosen, or write your own strategy
     - :doc:`routing`
   * - Let an organization's members sign in with its own accounts
     - :doc:`sso`
   * - Send a patch
     - :doc:`contributing`
   * - Identify a version or plan an upgrade
     - :doc:`releases`
   * - Look up a term such as *route*, *endpoint* or *distribution*
     - :doc:`glossary`

About the project
-----------------

HybridInference is developed by the `Harvard MadSys Lab
<https://juncheng.seas.harvard.edu/>`_ at the Harvard John A. Paulson School of
Engineering and Applied Sciences and published under the `MIT license
<https://github.com/HarvardMadSys/hybridInference/blob/main/LICENSE>`_. Its
RouteWise router is the subject of the lab's EuroSys '27 paper and ships
separately as the MIT-licensed `llm-routewise
<https://github.com/HarvardMadSys/RouteWise>`_ library; this gateway is its
production reference integration.

The source is at `github.com/HarvardMadSys/hybridInference
<https://github.com/HarvardMadSys/hybridInference>`_, and its `contributors page
<https://github.com/HarvardMadSys/hybridInference/graphs/contributors>`_ lists
everyone who has landed a change. Report bugs and ask questions on the `issue
tracker <https://github.com/HarvardMadSys/hybridInference/issues>`_.

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
   public-path-table
   glossary

.. toctree::
   :maxdepth: 2
   :caption: Guides

   adding-models
   add-local-model
   provider-adapters
   claude-code-setup
   batches
   rag-chat
   sso

.. toctree::
   :maxdepth: 2
   :caption: Customizing a Deployment

   distribution-customization
   site-ui-modules
   backend-extensions

.. toctree::
   :maxdepth: 2
   :caption: Operations

   deployment
   releases
   database
   alert-history
   openinfra
   accounting
   trusted-proxies-and-client-ips
   automation-score
   user-tags

.. toctree::
   :maxdepth: 2
   :caption: Contributing

   contributing
   translations
   console-translations
