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
    normalize_domains,
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


# ── the proxy that makes a tier above `platform_only` mean anything ────


def _trusted_setup(**extra: str):
    """A policy whose setup phase is trusted and fully configured."""
    return build_policy_from_env(
        {
            "AGENT_EGRESS_NETWORK_PLATFORM_ONLY": "agent-egress",
            "AGENT_EGRESS_NETWORK_TRUSTED": "agent-egress-trusted",
            "AGENT_EGRESS_PROXY_URL_TRUSTED": "http://agent-egress-proxy:3128",
            **extra,
        }
    )


def test_only_the_proxied_phase_is_handed_a_proxy():
    """The closed phase gets no proxy variables at all.

    Setting them anyway would be harmless in a working deployment and
    misleading in a broken one: an agent turn that could name a proxy would
    look like it had somewhere to go.
    """
    policy = _trusted_setup()

    assert policy.sandbox_env("agent") == {}
    assert policy.sandbox_env("setup")["https_proxy"] == "http://agent-egress-proxy:3128"


def test_both_spellings_of_the_proxy_variables_are_set():
    """curl reads the lowercase pair; other toolchains read only the uppercase.

    Setting one case installs a proxy for some of a repository's tooling and
    not the rest, which presents as an install that half worked.
    """
    env = _trusted_setup().sandbox_env("setup")

    assert env["http_proxy"] == env["HTTP_PROXY"]
    assert env["https_proxy"] == env["HTTPS_PROXY"]


def test_the_gateway_is_reached_directly_rather_than_through_the_proxy():
    """Our own service is on the sandbox's network already.

    Routing it through the proxy would need an allowlist entry for ourselves,
    and would put every model call through a hop that exists to police the
    internet.
    """
    env = _trusted_setup(AGENT_GATEWAY_URL="http://backend:8080").sandbox_env("setup")

    assert env["NO_PROXY"].split(",")[0] == "backend"
    assert env["no_proxy"] == env["NO_PROXY"]
    assert "localhost" in env["NO_PROXY"]


def test_a_trusted_tier_with_no_proxy_is_refused_at_the_point_of_use():
    """That tier *is* an allowlist proxy; without one it reaches nothing.

    Raised when the phase runs rather than when the policy is built, for the
    same reason `network_for` does: a deployment that never runs a setup script
    must not be refused a startup over a capability it does not use.
    """
    policy = build_policy_from_env(
        {
            "AGENT_EGRESS_NETWORK_PLATFORM_ONLY": "agent-egress",
            "AGENT_EGRESS_NETWORK_TRUSTED": "agent-egress-trusted",
        }
    )

    assert policy.sandbox_env("agent") == {}  # the closed phase still works
    with pytest.raises(EgressPolicyError, match="AGENT_EGRESS_PROXY_URL_TRUSTED"):
        policy.sandbox_env("setup")


def test_a_job_cannot_unset_the_proxy_its_phase_runs_under():
    """Policy is applied after the job's own environment, never before."""
    from serving.agent_jobs.sandbox import ContainerBackend, SandboxSpec

    backend = ContainerBackend(image="img:1", egress=_trusted_setup())
    command = backend.build_command(
        SandboxSpec(
            argv=["sh"],
            workdir="/w",
            phase="setup",
            env={"https_proxy": "http://attacker.example:8080"},
        )
    )

    assert "https_proxy=http://agent-egress-proxy:3128" in command
    assert "https_proxy=http://attacker.example:8080" not in command


@pytest.mark.parametrize(
    "url",
    ["ftp://proxy:3128", "proxy:3128", "http://", "http://proxy:3128/path", "http://p:3128?x=1"],
)
def test_a_proxy_url_that_is_not_one_is_refused(url: str):
    """A client would ignore the extra part in silence and dial the host anyway."""
    with pytest.raises(EgressPolicyError, match="AGENT_EGRESS_PROXY_URL_TRUSTED"):
        build_policy_from_env({"AGENT_EGRESS_PROXY_URL_TRUSTED": url})


def test_a_custom_tier_may_be_fronted_transparently():
    """`custom` exists to replace our judgement with the operator's.

    A deployment whose network gateway filters transparently needs no proxy
    variables, so the requirement that applies to `trusted` must not apply
    here — otherwise the escape hatch only fits our own shape.
    """
    policy = build_policy_from_env(
        {
            "AGENT_EGRESS_AGENT_TIER": "custom",
            "AGENT_EGRESS_SETUP_TIER": "custom",
            "AGENT_EGRESS_NETWORK_CUSTOM": "operator-net",
        }
    )

    assert policy.sandbox_env("agent") == {}


def test_the_trusted_list_is_ours_plus_theirs_but_a_custom_list_is_only_theirs():
    """Adding a mirror must not silently drop PyPI; choosing `custom` must."""
    policy = _trusted_setup(AGENT_EGRESS_ALLOWLIST="mirror.internal.example")

    trusted = policy.domains_for(EgressTier.TRUSTED)
    assert "pypi.org" in trusted and "mirror.internal.example" in trusted
    assert policy.domains_for(EgressTier.CUSTOM) == ("mirror.internal.example",)
    assert policy.domains_for(EgressTier.PLATFORM_ONLY) == ()


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("*.example.com", (".example.com",)),
        (".example.com", (".example.com",)),
        ("Example.COM", ("example.com",)),
        ("a.com, a.com , b.com", ("a.com", "b.com")),
        ("", ()),
    ],
)
def test_an_allowlist_is_normalized_the_way_an_operator_writes_it(written, expected):
    """Both wildcard spellings mean the same thing and reach the proxy as one."""
    assert normalize_domains(written) == expected
