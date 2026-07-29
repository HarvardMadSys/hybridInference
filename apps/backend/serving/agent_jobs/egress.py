"""Egress policy for the sandbox: four tiers, two phases (issue #1041).

The design puts egress control at the network layer rather than inside the
agent process, and names the reason: Copilot's firewall covers the agent's Bash
process but not its MCP servers or setup steps, which its own documentation
concedes. A boundary the agent participates in is not a boundary.

Four tiers, from closed to open:

``platform_only``
    Our own gateway — models and event reporting — and nothing else. This is
    the tier the design calls our distinctive shape: an agent that can think
    and report but cannot reach the internet at all.
``trusted``
    Package registries and similar, through an allowlist. Enough to install
    dependencies, which is the one thing ``platform_only`` cannot do.
``custom``
    An operator-supplied network, for a deployment with its own proxy.
``full``
    Unrestricted. Never a default, and refused unless acknowledged.

**The phases get separate tiers.** Setup installs dependencies and needs a
registry; the agent afterwards does not. Codex cloud makes the same split (net
during setup, none during the agent turn), and it is strictly better than one
tier for a whole session — the phase that runs untrusted model output is the
phase that gets the least reach. The defaults here follow the design:
``setup=trusted``, ``agent=platform_only``.

An allowlist must never carry an agent vendor's telemetry domain: it would let
the sandbox phone home about a customer's private repository, past a boundary
the operator believes is closed. That is checked here rather than left to
review.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class EgressTier(str, Enum):
    """How far the sandbox may reach in one phase."""

    PLATFORM_ONLY = "platform_only"
    TRUSTED = "trusted"
    CUSTOM = "custom"
    FULL = "full"


class EgressPolicyError(Exception):
    """Raised when the configured egress policy is unusable or unsafe."""


# Telemetry endpoints belonging to agent vendors. An allowlist that reaches any
# of these turns a closed sandbox into one that reports on the repository it was
# given. Substring match, so subdomains are covered.
VENDOR_TELEMETRY_DOMAINS = (
    "statsig.com",
    "sentry.io",
    "segment.io",
    "segment.com",
    "amplitude.com",
    "posthog.com",
    "datadoghq.com",
    "bugsnag.com",
    "mixpanel.com",
    "google-analytics.com",
)

_PHASE_SETUP = "setup"
_PHASE_AGENT = "agent"
PHASES = (_PHASE_SETUP, _PHASE_AGENT)

# The design's external-beta defaults: install dependencies during setup, then
# take the network away before untrusted model output starts driving tools.
_DEFAULT_TIERS = {_PHASE_SETUP: EgressTier.TRUSTED, _PHASE_AGENT: EgressTier.PLATFORM_ONLY}

_TIER_NETWORK_VARS = {
    EgressTier.PLATFORM_ONLY: "AGENT_EGRESS_NETWORK_PLATFORM_ONLY",
    EgressTier.TRUSTED: "AGENT_EGRESS_NETWORK_TRUSTED",
    EgressTier.CUSTOM: "AGENT_EGRESS_NETWORK_CUSTOM",
    EgressTier.FULL: "AGENT_EGRESS_NETWORK_FULL",
}


@dataclass(frozen=True)
class EgressPolicy:
    """The tier each phase runs under, and the network each tier maps to."""

    tiers: dict[str, EgressTier]
    networks: dict[EgressTier, str]

    def tier_for(self, phase: str) -> EgressTier:
        """Return the tier configured for ``phase``."""
        if phase not in PHASES:
            raise EgressPolicyError(f"unknown phase {phase!r}; expected one of {PHASES}")
        return self.tiers[phase]

    def network_for(self, phase: str) -> str:
        """Return the docker network the given phase runs on."""
        tier = self.tier_for(phase)
        network = self.networks.get(tier, "")
        if not network:
            raise EgressPolicyError(
                f"the {tier.value!r} tier is selected for the {phase} phase but "
                f"{_TIER_NETWORK_VARS[tier]} names no network. Configure it, or choose "
                "a tier this deployment has a network for."
            )
        return network

    @property
    def agent_phase_is_closed(self) -> bool:
        """Whether the agent phase is the design's PlatformOnly shape."""
        return self.tiers[_PHASE_AGENT] is EgressTier.PLATFORM_ONLY


def check_allowlist(domains: list[str]) -> None:
    """Reject an allowlist that would let the sandbox phone home.

    Called on the configured Trusted-tier allowlist. A vendor telemetry domain
    here is not a policy nuance: it reopens the boundary for exactly the traffic
    the sandbox exists to prevent, and it does so invisibly, because the
    operator reads the tier name and believes the sandbox is closed.
    """
    offenders = sorted(
        {
            domain
            for domain in domains
            for vendor in VENDOR_TELEMETRY_DOMAINS
            if vendor in domain.lower()
        }
    )
    if offenders:
        raise EgressPolicyError(
            "the egress allowlist contains agent-vendor telemetry domains, which "
            "would let the sandbox report on the repository it was given: "
            f"{', '.join(offenders)}"
        )


def _tier_from(raw: str, *, phase: str) -> EgressTier:
    """Parse a configured tier name."""
    try:
        return EgressTier(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(tier.value for tier in EgressTier)
        raise EgressPolicyError(
            f"unknown egress tier {raw!r} for the {phase} phase; expected one of {allowed}"
        ) from exc


def build_policy_from_env(env: dict[str, str] | None = None) -> EgressPolicy:
    """Read the egress policy from configuration.

    ``AGENT_EGRESS_SETUP_TIER`` / ``AGENT_EGRESS_AGENT_TIER`` choose the tiers;
    ``AGENT_EGRESS_NETWORK_*`` map each tier to a docker network.
    ``AGENT_SANDBOX_NETWORK`` remains honoured as the PlatformOnly network so an
    existing deployment keeps working without being reconfigured.
    """
    source = env if env is not None else dict(os.environ)

    tiers = dict(_DEFAULT_TIERS)
    for phase, var in (
        (_PHASE_SETUP, "AGENT_EGRESS_SETUP_TIER"),
        (_PHASE_AGENT, "AGENT_EGRESS_AGENT_TIER"),
    ):
        if raw := source.get(var):
            tiers[phase] = _tier_from(raw, phase=phase)

    networks: dict[EgressTier, str] = {}
    for tier, var in _TIER_NETWORK_VARS.items():
        if value := source.get(var):
            networks[tier] = value.strip()
    # The pre-tier setting named the closed network; keep it meaning that.
    networks.setdefault(
        EgressTier.PLATFORM_ONLY, (source.get("AGENT_SANDBOX_NETWORK") or "").strip()
    )

    if allowlist := source.get("AGENT_EGRESS_ALLOWLIST"):
        check_allowlist([entry.strip() for entry in allowlist.split(",") if entry.strip()])

    if EgressTier.FULL in tiers.values() and source.get("AGENT_SANDBOX_ALLOW_OPEN_NETWORK", "") in (
        "",
        "0",
    ):
        selected = [phase for phase, tier in tiers.items() if tier is EgressTier.FULL]
        raise EgressPolicyError(
            f"the {', '.join(selected)} phase is configured for unrestricted egress. "
            "Set AGENT_SANDBOX_ALLOW_OPEN_NETWORK=1 to accept that deliberately, or "
            "choose a narrower tier."
        )

    return EgressPolicy(tiers=tiers, networks={k: v for k, v in networks.items() if v})


__all__ = [
    "PHASES",
    "VENDOR_TELEMETRY_DOMAINS",
    "EgressPolicy",
    "EgressPolicyError",
    "EgressTier",
    "build_policy_from_env",
    "check_allowlist",
]
