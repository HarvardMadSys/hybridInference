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

**How the allowlist is enforced.** The tiers above the closed one are networks
with no route of their own; the only thing on them with a second leg is an
allowlist proxy (see :mod:`serving.agent_jobs.egress_proxy`). The sandbox is
pointed at it with the conventional ``http_proxy``/``https_proxy`` environment,
which every package manager already honours. That is a convenience, not the
boundary: a phase that ignored the variables would find a network that cannot
route anywhere, so "bypass the proxy" is not a thing the sandbox can decide to
do. It also means we never terminate TLS — a CONNECT request names its target
host in the clear, which is all an allowlist needs.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse


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

# What the Trusted tier reaches unless an operator adds to it: the package
# registries a dependency install actually needs, and the hosts git fetches a
# dependency from. Curated rather than borrowed wholesale — a vendor's own list
# includes its telemetry, which `check_allowlist` refuses on principle.
#
# The selection rule is exfiltration, not download safety. The sandbox is
# disposable and unprivileged, so "it could fetch something malicious" is
# already priced in; what matters is whether a host can *receive* a private
# repository. These are read-only distribution endpoints. `api.github.com` is
# deliberately absent for exactly that reason: an authenticated agent could
# push a repository's contents into an issue body.
DEFAULT_TRUSTED_DOMAINS: tuple[str, ...] = (
    # Python
    "pypi.org",
    "files.pythonhosted.org",
    # JavaScript
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    # Rust
    "crates.io",
    "static.crates.io",
    "index.crates.io",
    # Go
    "proxy.golang.org",
    "sum.golang.org",
    # Ruby
    "rubygems.org",
    "index.rubygems.org",
    # JVM
    "repo.maven.apache.org",
    "repo1.maven.org",
    # Dependencies fetched as source, and release tarballs.
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
)

_TIER_PROXY_VARS = {
    EgressTier.TRUSTED: "AGENT_EGRESS_PROXY_URL_TRUSTED",
    EgressTier.CUSTOM: "AGENT_EGRESS_PROXY_URL_CUSTOM",
}

# A domain is interpolated verbatim into the proxy's configuration file, so its
# syntax is a security boundary rather than a nicety: a value carrying a newline
# would append directives of the attacker's choosing to the config that decides
# what the sandbox may reach. Anything outside this alphabet is refused here,
# before it can reach a renderer.
_DOMAIN_PATTERN = re.compile(r"\.?[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?")

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


def normalize_domains(raw: str | list[str]) -> tuple[str, ...]:
    """Parse an allowlist into validated, deduplicated domains.

    ``*.example.com`` and ``.example.com`` both mean "this domain and its
    subdomains" and normalize to the leading-dot form the proxy understands;
    a bare ``example.com`` matches that host alone. Order is preserved so a
    rendered config reads the way the operator wrote it.
    """
    entries = raw.split(",") if isinstance(raw, str) else list(raw)
    seen: dict[str, None] = {}
    for entry in entries:
        domain = entry.strip().lower()
        if not domain:
            continue
        if domain.startswith("*."):
            domain = domain[1:]
        if not _DOMAIN_PATTERN.fullmatch(domain):
            raise EgressPolicyError(
                f"{entry.strip()!r} is not a domain. The allowlist is rendered into the "
                "proxy's configuration, so only letters, digits, dots and hyphens are "
                "accepted (optionally led by '*.' or '.' for subdomains)."
            )
        seen[domain] = None
    return tuple(seen)


@dataclass(frozen=True)
class EgressPolicy:
    """The tier each phase runs under, and the network each tier maps to."""

    tiers: dict[str, EgressTier]
    networks: dict[EgressTier, str]
    # Where the sandbox is told to send traffic for tiers that are fronted by
    # an allowlist proxy. Empty for PlatformOnly, which reaches no proxy, and
    # for Full, which needs none.
    proxies: dict[EgressTier, str] = field(default_factory=dict)
    # Operator additions to the Trusted tier's built-in domains, and the whole
    # of a Custom tier's list.
    allowlist: tuple[str, ...] = ()
    # Hosts the sandbox must reach *directly* rather than through the proxy:
    # our own gateway, which is on the sandbox's network already and would
    # otherwise need a proxy rule to talk to itself.
    no_proxy: tuple[str, ...] = ()

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

    def proxy_for(self, phase: str) -> str:
        """Return the proxy URL this phase should use, or ``""`` for none."""
        return self.proxies.get(self.tier_for(phase), "")

    def domains_for(self, tier: EgressTier) -> tuple[str, ...]:
        """Return the domains a tier may reach.

        Trusted is the built-in registry list *plus* whatever the operator
        added; Custom is only what they configured, because a custom tier
        exists precisely to replace our judgement with theirs.
        """
        if tier is EgressTier.TRUSTED:
            return normalize_domains([*DEFAULT_TRUSTED_DOMAINS, *self.allowlist])
        if tier is EgressTier.CUSTOM:
            return self.allowlist
        return ()

    def sandbox_env(self, phase: str) -> dict[str, str]:
        """The proxy environment to hand a sandbox running this phase.

        Both cases are set on purpose. curl deliberately ignores uppercase
        ``HTTP_PROXY`` (it is attacker-settable in a CGI context), while other
        toolchains read only the uppercase form — setting one case installs a
        proxy for some of a repository's tooling and not the rest, which
        presents as "the install half worked".
        """
        tier = self.tier_for(phase)
        proxy = self.proxies.get(tier, "")
        if not proxy:
            # Trusted *is* an allowlist proxy. Selecting it without naming one
            # leaves the sandbox on a network with no route and nothing to ask,
            # so every install dies at DNS — fail-closed, but for a reason
            # nothing in the operator's configuration points at. Raised here
            # rather than when the policy is built, for the same reason
            # `network_for` does: a deployment that never runs this phase must
            # not be refused a startup over a capability it does not use.
            if tier is EgressTier.TRUSTED:
                raise EgressPolicyError(
                    f"the {phase} phase runs under the 'trusted' tier but "
                    f"{_TIER_PROXY_VARS[tier]} names no proxy. That tier is an allowlist "
                    "proxy; without one the sandbox has no route out at all."
                )
            return {}
        no_proxy = ",".join(self.no_proxy)
        env = {
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "http_proxy": proxy,
            "https_proxy": proxy,
        }
        if no_proxy:
            env["NO_PROXY"] = no_proxy
            env["no_proxy"] = no_proxy
        return env

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

    allowlist = normalize_domains(source.get("AGENT_EGRESS_ALLOWLIST") or "")
    check_allowlist(list(allowlist))

    proxies: dict[EgressTier, str] = {}
    for tier, var in _TIER_PROXY_VARS.items():
        if value := (source.get(var) or "").strip():
            proxies[tier] = _validated_proxy_url(value, var=var)

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

    return EgressPolicy(
        tiers=tiers,
        networks={k: v for k, v in networks.items() if v},
        proxies=proxies,
        allowlist=allowlist,
        no_proxy=_no_proxy_hosts(source),
    )


def _validated_proxy_url(value: str, *, var: str) -> str:
    """Reject a proxy URL the sandbox could not use, or that is not a proxy."""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise EgressPolicyError(
            f"{var}={value!r} is not a proxy URL; expected something like "
            "http://agent-egress-proxy:3128"
        )
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise EgressPolicyError(
            f"{var}={value!r} carries a path or query. A proxy is named by host and port "
            "only, and the extra part would be silently ignored by every client."
        )
    return value


def _no_proxy_hosts(source: dict[str, str]) -> tuple[str, ...]:
    """Hosts a proxied phase must still reach directly.

    The gateway sits on the sandbox's own network. Routing it through the proxy
    would demand an allowlist entry for our own service and put every model
    call through a hop that exists to police the *internet*.
    """
    base_url = source.get("AGENT_GATEWAY_URL") or source.get("FREEINFERENCE_BASE_URL") or ""
    hosts = ["localhost", "127.0.0.1"]
    if host := (urlparse(base_url).hostname or "").strip():
        hosts.insert(0, host)
    return tuple(dict.fromkeys(hosts))


__all__ = [
    "DEFAULT_TRUSTED_DOMAINS",
    "PHASES",
    "VENDOR_TELEMETRY_DOMAINS",
    "EgressPolicy",
    "EgressPolicyError",
    "EgressTier",
    "build_policy_from_env",
    "check_allowlist",
    "normalize_domains",
]
