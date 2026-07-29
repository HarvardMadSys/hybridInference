"""The four-tier, two-phase egress policy.

The design puts egress control at the network layer and splits it by phase:
setup installs dependencies and needs a registry, the agent turn afterwards
does not — and the agent turn is the one driven by untrusted model output, so
it is the one that gets the least reach.
"""

from __future__ import annotations

import pytest

from serving.agent_jobs.egress import (
    VENDOR_TELEMETRY_DOMAINS,
    EgressPolicyError,
    EgressTier,
    build_policy_from_env,
    check_allowlist,
)


def test_the_defaults_follow_the_design():
    """setup=trusted, agent=platform_only — the external-beta shape."""
    policy = build_policy_from_env({"AGENT_SANDBOX_NETWORK": "agent-egress"})

    assert policy.tier_for("setup") is EgressTier.TRUSTED
    assert policy.tier_for("agent") is EgressTier.PLATFORM_ONLY
    assert policy.agent_phase_is_closed


def test_each_phase_resolves_to_its_own_network():
    """The whole point of two phases is that they are not the same network."""
    policy = build_policy_from_env(
        {
            "AGENT_EGRESS_NETWORK_PLATFORM_ONLY": "agent-egress",
            "AGENT_EGRESS_NETWORK_TRUSTED": "agent-setup",
        }
    )

    assert policy.network_for("setup") == "agent-setup"
    assert policy.network_for("agent") == "agent-egress"


def test_a_tier_with_no_network_is_an_error_not_a_fallback():
    """Falling back to a more open network would invert the policy."""
    policy = build_policy_from_env({"AGENT_EGRESS_NETWORK_PLATFORM_ONLY": "agent-egress"})

    with pytest.raises(EgressPolicyError, match="AGENT_EGRESS_NETWORK_TRUSTED"):
        policy.network_for("setup")


def test_unrestricted_egress_must_be_acknowledged():
    """`full` is never something a config typo can select silently."""
    with pytest.raises(EgressPolicyError, match="ALLOW_OPEN_NETWORK"):
        build_policy_from_env({"AGENT_EGRESS_AGENT_TIER": "full"})

    policy = build_policy_from_env(
        {
            "AGENT_EGRESS_AGENT_TIER": "full",
            "AGENT_EGRESS_NETWORK_FULL": "bridge",
            "AGENT_SANDBOX_ALLOW_OPEN_NETWORK": "1",
        }
    )
    assert policy.tier_for("agent") is EgressTier.FULL
    assert not policy.agent_phase_is_closed


def test_an_unknown_tier_name_is_refused():
    """A typo must not resolve to whatever tier sorts first."""
    with pytest.raises(EgressPolicyError, match="unknown egress tier"):
        build_policy_from_env({"AGENT_EGRESS_AGENT_TIER": "platformonly"})


@pytest.mark.parametrize("vendor", VENDOR_TELEMETRY_DOMAINS)
def test_an_allowlist_may_not_carry_vendor_telemetry(vendor: str):
    """A closed sandbox that can still phone home is not closed.

    The design says the allowlist must never contain an agent vendor's
    telemetry domain: it would let the sandbox report on a customer's private
    repository past a boundary the operator believes is shut, and the tier name
    would still read as closed.
    """
    with pytest.raises(EgressPolicyError, match="telemetry"):
        check_allowlist(["pypi.org", f"api.{vendor}"])


def test_an_ordinary_allowlist_passes():
    """Package registries are the point of the trusted tier."""
    check_allowlist(["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"])


def test_the_allowlist_is_checked_when_the_policy_is_built():
    """An operator must not have to remember to call the checker."""
    with pytest.raises(EgressPolicyError, match="telemetry"):
        build_policy_from_env(
            {
                "AGENT_SANDBOX_NETWORK": "agent-egress",
                "AGENT_EGRESS_ALLOWLIST": "pypi.org, telemetry.statsig.com",
            }
        )


def test_the_pre_tier_setting_still_names_the_closed_network():
    """An existing deployment keeps working without being reconfigured."""
    policy = build_policy_from_env({"AGENT_SANDBOX_NETWORK": "agent-egress"})
    assert policy.network_for("agent") == "agent-egress"
