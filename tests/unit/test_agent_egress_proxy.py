"""The allowlist proxy configuration the Trusted egress tier is made of.

This file is the enforcement point for what an untrusted sandbox may reach off
the host, and it is generated from operator input. Both halves of that are
tested here: that the rendered policy denies by default, and that a hostile
value cannot become policy of its own.
"""

from __future__ import annotations

import pytest

from serving.agent_jobs.egress import DEFAULT_TRUSTED_DOMAINS, EgressPolicyError
from serving.agent_jobs.egress_proxy import (
    CANARY_HOST,
    DEFAULT_PROXY_PORT,
    build_config_from_env,
    main,
    render_squid_config,
)


def test_the_last_word_is_deny_all():
    """Squid stops at the first matching rule, so the fallback must be a denial.

    Every allowance above this line is qualified by destination and method. If
    the file ended any other way, a request that matched nothing would be
    served, and the tier would be an open proxy that reads like an allowlist.
    """
    config = render_squid_config(["pypi.org"])

    rules = [line for line in config.splitlines() if line.startswith("http_access")]
    assert rules[-1] == "http_access deny all"
    assert "http_access deny !allowed_domains" in rules


def test_a_listed_domain_becomes_a_rule_and_an_unlisted_one_does_not():
    """The allowlist is the whole policy; nothing else grants reach."""
    config = render_squid_config(["pypi.org", "*.example.com"])

    assert "acl allowed_domains dstdomain pypi.org" in config
    # `*.example.com` is the form operators write; Squid's is the leading dot.
    assert "acl allowed_domains dstdomain .example.com" in config
    assert "evil.test" not in config


def test_tunnels_are_restricted_to_443():
    """CONNECT to an arbitrary port would relay anything to an allowlisted name.

    A registry's name plus a port of the caller's choosing is a general-purpose
    tunnel — the allowlist would still read as "package registries only" while
    carrying an SSH session.
    """
    config = render_squid_config(["pypi.org"])
    assert "http_access deny CONNECT !SSL_ports" in config
    assert "acl SSL_ports port 443" in config


def test_plain_http_is_not_allowed_even_to_a_listed_host():
    """The only allow rule names CONNECT, so cleartext falls through to deny."""
    config = render_squid_config(["pypi.org"])
    allows = [line for line in config.splitlines() if line.startswith("http_access allow")]
    assert allows == ["http_access allow CONNECT allowed_domains"]


def test_tls_is_never_intercepted():
    """A CONNECT line names its host in the clear, which is all an allowlist needs.

    Terminating TLS would put the proxy in a position to read a private
    repository's traffic — a strictly worse thing to have on the network than
    the sandbox it exists to police.
    """
    config = render_squid_config(["pypi.org"])
    for directive in ("ssl_bump", "sslcrtd_program", "tls_outgoing_options"):
        assert directive not in config


def test_nothing_is_cached_because_the_cache_would_be_shared():
    """One cache serving every job is a channel between tenants."""
    assert "cache deny all" in render_squid_config(["pypi.org"])


def test_denied_requests_are_logged_where_something_can_read_them():
    """`internal: true` refuses a connection silently; the proxy is the only witness.

    This is the observation point the design's "record refused egress attempts"
    needs — without it a blocked request exists only as a failure inside the
    sandbox, in the agent's own words.
    """
    config = render_squid_config(["pypi.org"])
    assert "access_log stdio:/dev/stdout agent" in config
    assert "logformat agent " in config
    assert config.index("logformat agent ") < config.index("access_log stdio:")


@pytest.mark.parametrize(
    "hostile",
    [
        "pypi.org\nhttp_access allow all",
        "pypi.org http_access allow all",
        "pypi.org\thttp_access allow all",
        'pypi.org"',
        "pypi.org # comment",
        "http://pypi.org",
        "pypi.org:443",
        "../etc/passwd",
    ],
)
def test_a_domain_that_is_not_a_domain_is_refused(hostile: str):
    """The allowlist is interpolated into the file that decides what is reachable.

    A newline is the one that matters most: it appends a directive of the
    caller's choosing to a config nobody re-reads, and `http_access allow all`
    on its own line turns the tier into an open proxy while every name in it
    still looks like a package registry.
    """
    with pytest.raises(EgressPolicyError, match="not a domain"):
        render_squid_config([hostile])


def test_vendor_telemetry_cannot_be_rendered_even_if_it_reaches_here():
    """Checked again at the renderer, not only where the allowlist is parsed.

    Defence in depth on purpose: this function is what a future caller will
    reach for, and the rule it enforces (a closed sandbox must not phone home
    about a private repository) must not depend on that caller remembering.
    """
    with pytest.raises(EgressPolicyError, match="telemetry"):
        render_squid_config(["pypi.org", "events.statsig.com"])


def test_an_empty_allowlist_is_refused_rather_than_rendered():
    """A proxy that denies everything is the closed tier with extra machinery.

    Rendering it would produce a deployment that looks like it can install
    dependencies and cannot — the exact confusion the tier exists to remove.
    """
    with pytest.raises(EgressPolicyError, match="deny every request"):
        render_squid_config([])


def test_the_environment_adds_to_the_built_in_registries_rather_than_replacing_them():
    """An operator adding one internal mirror must not lose PyPI in the process."""
    config = build_config_from_env({"AGENT_EGRESS_ALLOWLIST": "mirror.internal.example"})

    assert "acl allowed_domains dstdomain mirror.internal.example" in config
    for domain in DEFAULT_TRUSTED_DOMAINS:
        assert f"acl allowed_domains dstdomain {domain}" in config


def test_the_default_list_carries_no_write_capable_endpoint():
    """The selection rule is exfiltration, not download safety.

    The sandbox is disposable and unprivileged, so fetching something hostile
    is already priced in. What matters is whether a listed host can *receive* a
    private repository — `api.github.com` can (an issue body), and is therefore
    absent while `github.com` is present.
    """
    assert "api.github.com" not in DEFAULT_TRUSTED_DOMAINS
    assert "github.com" in DEFAULT_TRUSTED_DOMAINS


def test_the_generated_config_listens_where_the_sandbox_is_told_to_dial():
    """The port is a constant precisely so it cannot drift from the proxy URL."""
    assert f"http_port {DEFAULT_PROXY_PORT}" in render_squid_config(["pypi.org"])


def test_the_canary_host_can_never_resolve():
    """Preflight probes with it, so it must not be able to reach a real service.

    RFC 2606 reserves `.invalid`. A canary on a registrable domain could be
    bought, and a probe that expects a refusal would then be proving something
    about someone else's server.
    """
    assert CANARY_HOST.endswith(".invalid")
    assert CANARY_HOST not in DEFAULT_TRUSTED_DOMAINS


def test_the_renderer_writes_the_file_the_proxy_starts_from(tmp_path):
    """The whole point of the one-shot service: a file, or a non-zero exit."""
    out = tmp_path / "generated" / "squid.conf"

    assert main(["--out", str(out)]) == 0
    assert "http_access deny all" in out.read_text()


def test_a_refused_allowlist_stops_the_deploy_instead_of_starting_a_proxy(
    tmp_path, monkeypatch, capsys
):
    """A bad allowlist must not leave the previous config in place and start.

    Exiting non-zero is what makes compose hold the proxy back: the proxy waits
    on this service completing successfully, so a refusal here means no proxy
    rather than a proxy enforcing something nobody chose.
    """
    monkeypatch.setenv("AGENT_EGRESS_ALLOWLIST", "pypi.org\nhttp_access allow all")
    out = tmp_path / "squid.conf"

    assert main(["--out", str(out)]) == 2
    assert not out.exists()
    assert "refused" in capsys.readouterr().err
