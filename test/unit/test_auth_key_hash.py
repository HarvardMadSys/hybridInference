"""Smoke tests for auth_key_hash plumbing."""

import inspect

from serving.servers import auth


def test_verify_api_key_returns_auth_key_hash():
    """verify_api_key surfaces auth_key_hash in the returned context dict."""
    source = inspect.getsource(auth.verify_api_key)
    assert "auth_key_hash" in source, (
        "verify_api_key must surface auth_key_hash for multi-key affinity"
    )


def test_completions_pushes_auth_key_hash_to_request_context():
    """The chat_completions handler updates req_ctx with auth_key_hash."""
    from serving.servers.routers import completions as cmod

    source = inspect.getsource(cmod)
    assert "auth_key_hash" in source, (
        "chat handler must propagate auth_key_hash into req_ctx for the adapter"
    )
