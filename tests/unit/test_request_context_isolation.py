"""Each test starts with an empty request context (``tests/conftest.py``).

The request context is a ``ContextVar``, and a synchronous test writes straight
into its worker's main context. pytest runs these two in file order on one
worker: the first leaves an ``affinity_key`` behind, as a test calling
``req_ctx.set`` without cleaning up does, and the second fails if it can still
see it — deterministically, rather than only when an unlucky ``--dist
loadfile`` assignment puts such a writer ahead of a test that routes.
"""

from __future__ import annotations

from serving.utils import context as req_ctx


def test_a_sync_test_leaves_an_affinity_key_behind():
    req_ctx.set({"request_id": "r-leak", "affinity_key": "userA"})

    assert req_ctx.get()["affinity_key"] == "userA"


def test_the_next_test_does_not_inherit_it():
    assert req_ctx.get() == {}
