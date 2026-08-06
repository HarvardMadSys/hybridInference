"""Every local model route reaches its gateway over a supervised reverse tunnel.

The proxies in ``ops/*_idle_proxy/`` and ``ops/local_deployment_proxy/`` listen on
a GPU box the gateway cannot address. What makes them reachable is a reverse SSH
tunnel that binds the same port on the gateway host, where the gateway's backend
container finds it as ``host.docker.internal:<port>``. That tunnel is therefore
not an optimisation around the route -- it *is* the route, and it is the part with
no health check pointing at it.

On 2026-08-06 the Spark tunnel was still the bare ``ssh -N -R`` that
``spark_idle_service.sh`` starts by hand. It exited and stayed dead. Nothing
restarted it, nothing alerted on it, and the shape of the failure is why it is
worth a test: ``spark_idle_proxy`` kept answering ``/v1/models`` with 200 and its
container stayed warm, so every signal *on the Spark* said healthy while every
diffusiongemma request failed to connect until the breaker opened. It read as a
dead model server and was not one.

So each tunnel here must be supervised (autossh, ``Restart=always``), must survive
a reboot (``WantedBy=multi-user.target``), must fail loudly rather than forward
nothing (``ExitOnForwardFailure``), and must not outlive the listener it
advertises. That last one is h200a's separate incident: its proxy was inactive for
hours while its tunnels stayed up, so the gateway host accepted connections and
forwarded them into a box with nothing listening -- which the breaker and the
paid-API fallback turned into silent spend instead of a page.

Both directions of that binding are checked, because ``BindsTo=`` alone looks
sufficient and is worse than nothing: dependencies propagate stops and never
starts, and the proxies restart themselves on failure. So a crash stops the tunnel,
systemd brings the proxy back, and the route stays dark with every local signal
green -- this outage, reintroduced by the guard against the other one. ``Upholds=``
on the proxy is the missing direction, and the installers write it because only they
know the per-host instance name.

No systemd is required: the unit files are parsed directly. The behaviour they
encode was measured on spark2 (systemd 255) with throwaway units rather than
inferred from the manual, which is how the ``PartOf=`` reading -- plausible, and
wrong -- was caught before it shipped.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SYSTEMD = REPO / "deploy" / "systemd"

# Tunnel template → (the proxy unit it advertises, the installer that enables it).
#
# A local route missing from this table is a route whose reachability nothing here
# defends. The Spark entry is the one this file was written for: it had no tunnel
# unit at all, so there was nothing to assert against.
TUNNELS = {
    "local_deployment_tunnel@.service": (
        "local_deployment_proxy.service",
        REPO / "ops" / "local_deployment_proxy" / "install.sh",
    ),
    "spark_idle_tunnel@.service": (
        "spark_idle_proxy.service",
        REPO / "ops" / "spark_idle_proxy" / "install_service.sh",
    ),
    "h200_idle_tunnel@.service": (
        "h200_idle_proxy.service",
        REPO / "ops" / "h200_idle_proxy" / "install.sh",
    ),
    "h200_idle_tunnel_b@.service": (
        "h200_idle_proxy_b.service",
        REPO / "ops" / "h200_idle_proxy" / "install.sh",
    ),
}


def _unit(name: str) -> str:
    return (SYSTEMD / name).read_text(encoding="utf-8")


def _directive(text: str, key: str) -> list[str]:
    """Values of every ``key=`` directive in a unit, comments excluded.

    Continuation lines matter here: ``ExecStart=`` spans several with trailing
    backslashes, so they are joined before anything is matched.
    """
    joined = re.sub(r"\\\n\s*", " ", text)
    out = []
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            out.append(value.strip())
    return out


def _one(text: str, key: str) -> str:
    values = _directive(text, key)
    assert len(values) == 1, f"expected exactly one {key}=, found {values}"
    return values[0]


@pytest.mark.parametrize("tunnel", TUNNELS)
def test_every_local_route_has_a_tunnel_unit(tunnel: str) -> None:
    """A hand-started ``ssh -N -R`` is the failure, so the unit must exist."""
    assert (SYSTEMD / tunnel).is_file(), (
        f"{tunnel} is missing, so this route's tunnel can only be started by hand. "
        "That is what took diffusiongemma down on 2026-08-06: the process exited and "
        "nothing owned bringing it back."
    )


@pytest.mark.parametrize("tunnel", TUNNELS)
def test_tunnel_is_respawned_rather_than_dialled_once(tunnel: str) -> None:
    """``ssh`` exits on a dropped link and stays exited; autossh redials."""
    text = _unit(tunnel)
    exec_start = _one(text, "ExecStart")

    assert "/usr/bin/autossh" in exec_start, (
        f"{tunnel} must run autossh, not ssh: a plain `ssh -N -R` that loses its link "
        f"exits and never returns. Found: {exec_start}"
    )
    assert "-M 0" in exec_start, (
        f"{tunnel} must pass `-M 0` to disable autossh's legacy monitoring port and "
        "rely on ServerAlive* plus ExitOnForwardFailure instead."
    )
    assert "AUTOSSH_GATETIME=0" in " ".join(_directive(text, "Environment")), (
        f"{tunnel} must set AUTOSSH_GATETIME=0, or autossh reads an early first-dial "
        "failure as 'this will never work' and gives up -- which on a box that boots "
        "before its network is routable is a tunnel that never appears at all."
    )
    assert _directive(text, "Restart") == ["always"], (
        f"{tunnel} must set Restart=always so systemd restarts autossh itself, not "
        f"only the ssh underneath it. Found: {_directive(text, 'Restart')}"
    )


@pytest.mark.parametrize("tunnel", TUNNELS)
def test_tunnel_fails_loudly_instead_of_forwarding_nothing(tunnel: str) -> None:
    """A connected ssh that forwards nothing looks healthy from the GPU box.

    Without ``ExitOnForwardFailure`` a remote bind refused because the port is
    already taken -- by a leftover tunnel from an earlier by-hand start, say --
    leaves ssh happily connected while the gateway gets nothing. With it, the
    refusal is a non-zero exit and ``Restart=always`` retries.
    """
    exec_start = _one(_unit(tunnel), "ExecStart")
    assert "ExitOnForwardFailure=yes" in exec_start, (
        f"{tunnel} must pass -o ExitOnForwardFailure=yes. Found: {exec_start}"
    )
    assert "ServerAliveInterval=30" in exec_start and "ServerAliveCountMax=3" in exec_start, (
        f"{tunnel} must keep the ServerAlive* probes: they are what turns a silently "
        f"dead link into an exit autossh can act on. Found: {exec_start}"
    )


@pytest.mark.parametrize("tunnel", TUNNELS)
def test_tunnel_forwards_the_ports_its_environment_names(tunnel: str) -> None:
    """The installers tune these through a drop-in, so they must stay variables.

    A literal port here would be silently un-overridable: the drop-in would set
    ``REMOTE_PORT`` and the unit would go on forwarding the number baked into
    ``ExecStart``.
    """
    text = _unit(tunnel)
    exec_start = _one(text, "ExecStart")
    assert "-R ${REMOTE_BIND}:${REMOTE_PORT}:localhost:${LISTEN_PORT}" in exec_start, (
        f"{tunnel} must build its -R from REMOTE_BIND/REMOTE_PORT/LISTEN_PORT so an "
        f"installer drop-in can retarget it. Found: {exec_start}"
    )
    pinned = {v.partition("=")[0].strip() for v in _directive(text, "Environment")}
    assert {"LISTEN_PORT", "REMOTE_PORT", "REMOTE_BIND"} <= pinned, (
        f"{tunnel} references ports it does not default; an unset one expands to "
        f"empty and the -R becomes unparseable. Pinned: {sorted(pinned)}"
    )


@pytest.mark.parametrize("tunnel", TUNNELS)
def test_tunnel_comes_back_after_a_reboot(tunnel: str) -> None:
    """Restart=always covers a crash, not a boot; only the symlink does that."""
    assert _directive(_unit(tunnel), "WantedBy") == ["multi-user.target"], (
        f"{tunnel} must be WantedBy=multi-user.target, or `systemctl enable` has "
        "nothing to hook and the route stays dark until someone notices."
    )


@pytest.mark.parametrize(("tunnel", "proxy"), [(t, v[0]) for t, v in TUNNELS.items()])
def test_tunnel_does_not_outlive_the_listener_it_advertises(tunnel: str, proxy: str) -> None:
    """h200a's incident: tunnels up, proxy down, connections forwarded into a void.

    ``Wants=`` is the wrong strength and reads as though it were right. It orders
    startup and nothing else, so a stopped or crashed proxy leaves the tunnel
    advertising a port that accepts and then fails -- and because the gateway falls
    back to a paid provider, the bill is the only symptom.
    """
    text = _unit(tunnel)
    assert _directive(text, "BindsTo") == [proxy], (
        f"{tunnel} must BindsTo={proxy} so it stops when the proxy stops, fails or "
        f"disappears. Found: {_directive(text, 'BindsTo')}"
    )
    assert proxy not in " ".join(_directive(text, "Wants")), (
        f"{tunnel} still lists {proxy} in Wants=, which only orders startup. BindsTo= "
        "is what stops the tunnel with it; leaving both is a contradiction a reader "
        "resolves in the weaker direction."
    )


@pytest.mark.parametrize(
    ("tunnel", "installer"),
    [(t, v[1]) for t, v in TUNNELS.items()],
    ids=list(TUNNELS),
)
def test_something_starts_the_tunnel_again_after_its_proxy_crashes(
    tunnel: str, installer: Path
) -> None:
    """The gap ``BindsTo=`` opens, which is this outage's own shape.

    systemd propagates stops along these dependencies and never starts, and the
    proxies carry ``Restart=on-failure``. So on a proxy crash ``BindsTo=`` stops the
    tunnel, systemd brings the proxy straight back, and nothing brings the tunnel
    back: a healthy proxy behind a dead route -- which is precisely what opened the
    breaker on 2026-08-06, reachable again through the directive added to prevent the
    *other* incident.

    ``Upholds=`` is the start-propagating direction, and it is asserted on the
    installer rather than the unit because the instance name is per-host: the
    template cannot name what it will be instantiated as.

    Measured on spark2 (systemd 255), because guessing gets both halves wrong.
    ``kill -9`` of the proxy leaves the tunnel ``inactive`` with the proxy ``active``
    again; adding the ``Upholds=`` drop-in leaves both active. In the other
    direction ``systemctl restart <proxy>`` does *not* propagate through
    ``BindsTo=`` at all -- the tunnel stays up -- so ``PartOf=``, the obvious
    reading, fixes nothing here.
    """
    text = installer.read_text(encoding="utf-8")
    base = tunnel.removesuffix("@.service")

    assert re.search(
        rf"Upholds=(\$\{{TUNNEL_BASE\}}|{re.escape(base)})@\$\{{?host\}}?\.service", text
    ), (
        f"{installer.name} must write an `Upholds=` drop-in on the proxy naming each "
        f"{base}@<host> instance. Without it, BindsTo= makes a proxy crash take the "
        "tunnel down for good while systemd restarts the proxy -- the failure that "
        "took diffusiongemma out, caused by the fix for a different one."
    )
    assert "upholds-tunnels.conf" in text, (
        f"{installer.name} must keep the Upholds= list in its own drop-in file: the "
        "override.conf beside it is rewritten from the ports, and merging the two "
        "makes each rewrite silently depend on the other's inputs."
    )
    assert re.search(r"^\s*echo \"Upholds=\"\s*$", text, re.MULTILINE), (
        f"{installer.name} must emit a bare `Upholds=` before the list. These "
        "directives accumulate across drop-ins, so without the reset a gateway host "
        "removed from SSH_HOST keeps being upheld from the previous run's file."
    )


@pytest.mark.parametrize(
    ("tunnel", "installer"),
    [(t, v[1]) for t, v in TUNNELS.items()],
    ids=list(TUNNELS),
)
def test_installer_enables_the_tunnel_it_installs(tunnel: str, installer: Path) -> None:
    """Installing a supervised unit and not enabling it buys nothing.

    ``enable`` for the reboot, ``restart`` for the re-run: ``enable --now`` starts a
    stopped unit but is a no-op on a running one, so a re-run that changes the
    tunnel drop-in (a new TUNNEL_USER, a moved port) would leave the new value on
    disk and the old one live in the process.
    """
    text = installer.read_text(encoding="utf-8")
    base = tunnel.removesuffix("@.service")

    assert base in text, (
        f"{installer.name} never names {base}, so {tunnel} is installed by hand or not at all."
    )

    # The instance argument, however the installer spells it: a literal base name or
    # the TUNNEL_BASE variable the templated installers use.
    instance = r'"(?:\$\{TUNNEL_BASE\}|' + re.escape(base) + r')@\$\{?host\}?"'
    assert re.search(rf"systemctl enable {instance}", text), (
        f"{installer.name} must `systemctl enable` each tunnel instance; without the "
        "symlink the unit is present and dead after the next reboot."
    )
    assert re.search(rf"systemctl restart {instance}", text), (
        f"{installer.name} must `systemctl restart` each tunnel instance: `enable "
        "--now` does nothing to an already-running one, so a changed drop-in would "
        "never reach the live process."
    )
