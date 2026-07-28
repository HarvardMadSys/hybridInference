"""Unit tests for per-attempt agent worker capability tokens."""

from __future__ import annotations

import pytest

from serving.agent_jobs.tokens import (
    InvalidAgentToken,
    mint_worker_token,
    parse_worker_token,
)


@pytest.fixture(autouse=True)
def _api_key_secret(monkeypatch):
    """Provide the signing secret the tokens are derived from."""
    monkeypatch.setenv("API_KEY_SECRET", "unit-test-secret")
    from serving.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_round_trip_preserves_the_fencing_triple():
    """A minted token parses back to exactly the triple it was bound to."""
    token = mint_worker_token(job_id="ajob_abc", attempt_id=7, lease_generation=3)
    assert parse_worker_token(token) == {
        "job_id": "ajob_abc",
        "attempt_id": 7,
        "lease_generation": 3,
        "scope": "full",
    }


def test_tokens_are_domain_prefixed():
    """Tokens carry the agent prefix so they can't be confused with API keys."""
    token = mint_worker_token(job_id="ajob_abc", attempt_id=1, lease_generation=1)
    assert token.startswith("ajt.")


def test_tampering_with_claims_is_rejected():
    """Swapping the payload without re-signing fails verification."""
    token = mint_worker_token(job_id="ajob_abc", attempt_id=1, lease_generation=1)
    other = mint_worker_token(job_id="ajob_xyz", attempt_id=9, lease_generation=4)
    forged = ".".join([token.split(".")[0], other.split(".")[1], token.split(".")[2]])
    with pytest.raises(InvalidAgentToken):
        parse_worker_token(forged)


def test_signature_from_a_different_secret_is_rejected(monkeypatch):
    """A token minted under another secret does not verify."""
    token = mint_worker_token(job_id="ajob_abc", attempt_id=1, lease_generation=1)
    from serving.config.settings import get_settings

    monkeypatch.setenv("API_KEY_SECRET", "a-different-secret")
    get_settings.cache_clear()
    with pytest.raises(InvalidAgentToken):
        parse_worker_token(token)


@pytest.mark.parametrize(
    "bad",
    ["", "not-a-token", "ajt.only-two-parts", "xxx.aaa.bbb", "ajt.!!!.???"],
)
def test_malformed_tokens_raise(bad):
    """Structurally invalid tokens raise rather than returning junk claims."""
    with pytest.raises(InvalidAgentToken):
        parse_worker_token(bad)


def test_scopes_round_trip():
    """A minted token reports the scope it was minted with."""
    from serving.agent_jobs.tokens import SCOPE_FULL, SCOPE_MODEL

    full = mint_worker_token(job_id="j", attempt_id=1, lease_generation=1)
    model = mint_worker_token(job_id="j", attempt_id=1, lease_generation=1, scope=SCOPE_MODEL)
    assert parse_worker_token(full)["scope"] == SCOPE_FULL
    assert parse_worker_token(model)["scope"] == SCOPE_MODEL
    # Different scopes are different tokens: one cannot be swapped for the other.
    assert full != model


def test_scope_is_signed_not_advisory():
    """Editing the scope claim invalidates the signature."""
    from serving.agent_jobs.tokens import SCOPE_MODEL

    model = mint_worker_token(job_id="j", attempt_id=1, lease_generation=1, scope=SCOPE_MODEL)
    full = mint_worker_token(job_id="j", attempt_id=1, lease_generation=1)
    # Splice the full token's payload onto the model token's signature.
    forged = ".".join([model.split(".")[0], full.split(".")[1], model.split(".")[2]])
    with pytest.raises(InvalidAgentToken):
        parse_worker_token(forged)


def test_unknown_scope_is_rejected():
    """A signed token naming a scope we do not implement is refused."""
    with pytest.raises(ValueError):
        mint_worker_token(job_id="j", attempt_id=1, lease_generation=1, scope="admin")
