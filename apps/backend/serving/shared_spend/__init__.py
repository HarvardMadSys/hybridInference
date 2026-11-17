"""Shared spend accounting: budget reservations and settlements kept in PostgreSQL.

Off unless an operator activates it (``python -m serving.shared_spend``);
see :mod:`serving.shared_spend.mode` for how a process picks its mode, and
``docs/agents/plans/2026-11-14-shared-spend-admission-and-settlement.md`` for
the protocol.
"""
