"""Batch inference: submit many chat requests at once, collect the results later.

``POST /v1/batches`` stores the requests (``serving.storage.batch_store``); an
in-process worker (:mod:`serving.batch.scheduler`) drains them through the
ordinary chat-completions path, a few at a time per model, re-authenticating the
owner's key before every item. Who may use it, and how much of each model it may
occupy, is configured at runtime (:mod:`serving.batch.config`,
:mod:`serving.batch.model_limits`). Design record:
``docs/agents/specs/2026-10-25-batch-inference-design.md``.
"""
