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
