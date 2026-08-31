"""Decision table for the X-Probe trust policy.

Every consumer of the marker (chat completions, embeddings, the rejection
log, the request-log middleware via req_ctx) goes through these predicates,
so the full caller matrix is pinned once, here.
"""

from __future__ import annotations

import pytest

from serving.utils.synthetic_probe import is_trusted_probe_caller


@pytest.mark.parametrize(
    ("user_ctx", "trusted"),
    [
        # The deployment's own monitors: authenticated internal/admin keys.
        pytest.param({"role": "internal", "authenticated": True}, True, id="internal-key"),
        pytest.param({"role": "admin", "authenticated": True}, True, id="admin-key"),
        # The auth-disabled anonymous context: verify_api_key hands it the
        # admin role, but nothing was authenticated — "auth is off" opens the
        # API, it does not mint a verifiable monitor identity, so the marker
        # is refused (fail-closed).
        pytest.param(
            {"role": "admin", "authenticated": False, "is_admin": True},
            False,
            id="auth-disabled-anonymous",
        ),
        # Role below internal never qualifies, authenticated or not.
        pytest.param({"role": "free", "authenticated": True}, False, id="free-key"),
        pytest.param({"role": "pro", "authenticated": True}, False, id="pro-key"),
        # Role without authentication (and without the auth-disabled marker)
        # is exactly the shape a forged/unauthenticated context would have.
        pytest.param({"role": "admin", "authenticated": False}, False, id="unauthenticated-admin"),
        pytest.param({"role": "admin"}, False, id="role-only"),
        # Grant contexts carry the owner's role while the requests are written
        # by sandboxed agent code — never trusted, whatever the role.
        pytest.param(
            {"role": "internal", "authenticated": True, "agent_grant_id": "g1"},
            False,
            id="grant-internal-owner",
        ),
        pytest.param(
            {"role": "admin", "authenticated": True, "agent_job_id": "j1"},
            False,
            id="job-admin-owner",
        ),
        # No resolved caller at all.
        pytest.param(None, False, id="none"),
        pytest.param({}, False, id="empty"),
        pytest.param({"authenticated": True}, False, id="authenticated-no-role"),
    ],
)
def test_is_trusted_probe_caller(user_ctx, trusted) -> None:
    assert is_trusted_probe_caller(user_ctx) is trusted
