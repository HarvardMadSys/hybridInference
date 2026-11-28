"""OpenInfra: live GPU, host and inference-engine metrics, published at ``/openinfra``.

A reporter on each GPU host (``ops/openinfra/reporter.py``) scrapes
``nvidia-smi``, DCGM, ``/proc`` and its SGLang or vLLM engines, and posts one
snapshot every few seconds to ``POST /internal/openinfra/ingest``, which takes
the shared ``OPENINFRA_INGEST_TOKEN``. The snapshots land in the ``openinfra_*``
tables (:mod:`serving.openinfra.store`); the scheduler rolls them up every five
minutes (:mod:`serving.openinfra.maintenance`).

A deployment publishes them with ``features.openinfra: true``: the console's
``/openinfra`` page then reads ``GET /public-openinfra`` (the current snapshot,
served totals and recent requests) and ``GET /public-openinfra/history``
(time series from two hours to seven days). Collecting needs only the token;
publishing needs the flag, so a deployment can collect before it publishes.

Modules:

- :mod:`~serving.openinfra.fields`: the metric names every layer shares;
- :mod:`~serving.openinfra.wire`: the ingest body, parsed leniently;
- :mod:`~serving.openinfra.prometheus`: engine metrics from Prometheus text;
- :mod:`~serving.openinfra.store`: the SQL;
- :mod:`~serving.openinfra.dashboard`: the documents the routes serve;
- :mod:`~serving.openinfra.config`: the settings;
- :mod:`~serving.openinfra.maintenance`: the rollup job;
- :mod:`~serving.openinfra.import_d1`: a one-off import of the Cloudflare D1
  database the dashboard used before it moved into the gateway.
"""
