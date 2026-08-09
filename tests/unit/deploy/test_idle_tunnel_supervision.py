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
encode was measured on the Spark (systemd 255) with throwaway units rather than
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


# The installers above, deduplicated: the h200_idle_proxy one serves both of its
# replicas, so a table
# keyed on the tunnel would run every installer-level check on it twice.
INSTALLERS = {str(p.relative_to(REPO)): p for _, p in TUNNELS.values()}

# Installers that write a `User=` into the tunnel drop-in. h200a is why the directive
# exists at all -- root there has no SSH key for the routers -- and it is why the
# value must survive a re-run that does not mention it.
INSTALLERS_WITH_TUNNEL_USER = {
    name: path for name, path in INSTALLERS.items() if "TUNNEL_USER" in path.read_text("utf-8")
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


_OPENERS = (("if ", "fi"), ("for ", "done"), ("while ", "done"), ("case ", "esac"))


def _nesting_depth_at(lines: list[str], index: int) -> int:
    """How many shell blocks enclose ``lines[index]``.

    Indentation would be the easy proxy and is not one: bash is happy to nest an
    unindented statement inside an ``if``, so a check on leading whitespace would
    pass on exactly the regression it is meant to catch. This counts openers against
    their terminators instead, skipping comments and heredoc bodies -- a ``[Service]``
    stanza has no shell keywords in it, but the next drop-in written here might.
    """
    depth = 0
    heredoc: str | None = None
    for line in lines[:index]:
        stripped = line.strip()
        if heredoc is not None:
            if stripped == heredoc:
                heredoc = None
            continue
        if stripped.startswith("#"):
            continue
        match = re.search(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?\s*$", stripped)
        if match:
            heredoc = match.group(1)
            continue
        for opener, closer in _OPENERS:
            if stripped.startswith(opener):
                depth += 1
                break
            if stripped == closer or stripped.startswith(f"{closer} "):
                depth -= 1
                break
    return depth


def _line_index(lines: list[str], needle: str, installer: str) -> int:
    hits = [i for i, line in enumerate(lines) if needle in line]
    assert len(hits) == 1, f"expected exactly one {needle!r} in {installer}, found {len(hits)}"
    return hits[0]


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

    Measured on the Spark (systemd 255), because guessing gets both halves wrong.
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


@pytest.mark.parametrize("installer", INSTALLERS.values(), ids=list(INSTALLERS))
def test_tunnel_override_is_rewritten_even_when_it_restores_the_defaults(
    installer: Path,
) -> None:
    """A drop-in written only when something differs is a drop-in that never leaves.

    All three installers started out writing ``<tunnel>.d/override.conf`` inside an
    ``if`` that compared the ports and bind against the unit's own defaults, on the
    reasoning that the common case should stay untouched. It inverts: the run that
    *needs* the file removed is exactly the one the guard skips. Move a route to
    8004, then move it back, and the second run writes nothing while the stale
    drop-in still outranks the unit -- so the tunnel goes on forwarding 8004 to a
    proxy that has returned to 8003, and the installer's output says it did its job.

    Rewriting unconditionally makes the file a function of this run's inputs alone,
    which is the only version of it an operator can reason about from the command
    they typed.
    """
    lines = installer.read_text(encoding="utf-8").splitlines()

    # The tunnel drop-in identified by what it contains rather than where it is
    # written: the path is spelled differently in each installer, the heredoc body is
    # not.
    body = _line_index(lines, "Environment=REMOTE_PORT=${REMOTE_PORT}", installer.name)
    write = max(i for i in range(body) if lines[i].lstrip().startswith("cat > "))

    depth = _nesting_depth_at(lines, write)
    assert depth == 0, (
        f"{installer.name} writes the tunnel override inside {depth} enclosing "
        "block(s). It must be written on every run, including the all-defaults one: a "
        "conditional write leaves the previous run's ports in place when this run "
        "restores the unit's, and the tunnel keeps forwarding a port the proxy no "
        "longer listens on."
    )


@pytest.mark.parametrize("installer", INSTALLERS.values(), ids=list(INSTALLERS))
def test_installer_retires_tunnels_dropped_from_ssh_host(installer: Path) -> None:
    """Rewriting ``Upholds=`` unlinks a dropped host; it does not stop its tunnel.

    The tunnels carry ``Restart=always`` and are enabled into
    ``multi-user.target``, so an instance an earlier run created outlives both its
    removal from ``SSH_HOST`` and the next reboot. It keeps a port bound on a gateway
    that no longer routes to this box -- and because the bind succeeds, the gateway
    that *does* route here can be refused the port by a tunnel nobody meant to keep.

    The installer is the only place that can notice: it is the one that knows both
    what is installed and what was asked for. Discovery has to come from systemd,
    not from SSH_HOST, for the same reason the uninstall path already discovers --
    what the caller names now says nothing about what an earlier caller left behind.
    """
    text = installer.read_text(encoding="utf-8")

    assert re.search(r"_installed_instances\(\)\s*\{", text), (
        f"{installer.name} must enumerate the tunnel instances systemd actually has "
        "(`list-units` for the live ones, `list-unit-files` for enabled-but-stopped) "
        "before it can tell which of them SSH_HOST no longer names."
    )
    assert "list-unit-files" in text, (
        f"{installer.name} must consult `systemctl list-unit-files` as well as "
        "`list-units`: an instance that is enabled but currently stopped is still "
        "wired into multi-user.target and comes back at the next reboot."
    )
    assert "< <(_installed_instances)" in text, (
        f"{installer.name} defines _installed_instances but never reads it, so nothing "
        "compares what is installed against what SSH_HOST names."
    )
    assert re.search(r'echo "Retiring \$\{unit\}', text), (
        f"{installer.name} must announce each retired instance. A tunnel disappearing "
        "silently is the same class of problem as one surviving silently: the operator "
        "learns what this box advertises from this script's output or not at all."
    )
    assert re.search(r'systemctl disable --now "\$unit"', text), (
        f"{installer.name} must `systemctl disable --now` the instances SSH_HOST no "
        "longer names. `stop` alone leaves the multi-user.target symlink, so the "
        "retired tunnel returns at the next reboot."
    )


@pytest.mark.parametrize(
    "installer", INSTALLERS_WITH_TUNNEL_USER.values(), ids=list(INSTALLERS_WITH_TUNNEL_USER)
)
def test_a_run_that_omits_tunnel_user_keeps_the_installed_one(installer: Path) -> None:
    """The one value in that drop-in that must *not* be a function of this run alone.

    ``User=`` shares override.conf with the ports, and the unconditional rewrite
    above would otherwise reset it to the unit's ``root`` default on every run that
    does not pass ``TUNNEL_USER``. On h200a that is an outage: root there has no SSH
    key for the routers, so the tunnels would fail host key verification and autossh
    would restart forever behind a proxy reporting itself healthy.

    So the same contract ``write_local_api_key_dropin`` uses for the API key --
    unset means keep, explicitly empty means clear -- which needs the variable left
    undefaulted until the installed value has been read back out of the file.
    """
    text = installer.read_text(encoding="utf-8")
    lines = text.splitlines()

    assert "${TUNNEL_USER+set}" in text, (
        f"{installer.name} must distinguish an unset TUNNEL_USER from an empty one "
        "(`${TUNNEL_USER+set}`, not `${TUNNEL_USER:-}`). Without it there is no way to "
        "ask for root back, and no way to re-run without asking for it by accident."
    )
    assert re.search(r"sed -n 's/\^User=//p'", text), (
        f"{installer.name} must read the installed User= back out of the existing "
        "override.conf. Nothing else remembers it: the drop-in is the only record that "
        "these tunnels do not run as root."
    )

    keep = _line_index(lines, "${TUNNEL_USER+set}", installer.name)
    default = _line_index(lines, 'TUNNEL_USER="${TUNNEL_USER:-}"', installer.name)
    assert keep < default, (
        f"{installer.name} collapses TUNNEL_USER to empty on line {default + 1}, before "
        f"reading the installed value on line {keep + 1}. Defaulting it first destroys "
        "the unset/empty distinction the lines below depend on, which is the exact "
        "shape of the bug: every re-run that omits TUNNEL_USER silently hands h200a's "
        "tunnels back to root."
    )


def test_h200_retires_only_the_replica_it_is_installing() -> None:
    """A 4xH200 box runs two replicas, and installing one must not disturb the other.

    Replica A and B have separate unit names precisely so they can be managed
    independently. A retirement pass that enumerated ``h200_idle_tunnel*`` rather
    than this replica's own base would make ``REPLICA=a`` disable every tunnel
    replica B installed -- taking half the box's capacity off both gateways as a side
    effect of a routine reinstall of the other half.

    The ``@`` is what does the scoping: ``h200_idle_tunnel@`` cannot match
    ``h200_idle_tunnel_b@``, so the pattern is only safe as long as it is anchored on
    the variable rather than typed out.
    """
    text = (REPO / "ops" / "h200_idle_proxy" / "install.sh").read_text(encoding="utf-8")

    enumeration = re.search(r"_installed_instances\(\)\s*\{(.+?)\n\}", text, re.S)
    assert enumeration, "ops/h200_idle_proxy/install.sh must define _installed_instances()"
    block = enumeration.group(1)

    assert '"${TUNNEL_BASE}@*.service"' in block, (
        "ops/h200_idle_proxy/install.sh must enumerate instances of ${TUNNEL_BASE} -- the base this "
        "REPLICA selected -- not a literal or a wider glob, or installing replica A "
        "retires replica B's tunnels."
    )
    assert not re.search(r"h200_idle_tunnel(_b)?@", block), (
        "ops/h200_idle_proxy/install.sh hardcodes a tunnel base in its enumeration. Both replicas run "
        "this same code path with different TUNNEL_BASE values, so a literal makes one "
        "of them enumerate -- and retire -- the other's instances."
    )
    assert re.search(r'"\$\{TUNNEL_BASE\}@\$\{host\}\.service"', text), (
        "ops/h200_idle_proxy/install.sh must compare each installed instance against "
        "${TUNNEL_BASE}@${host}.service. Matching on the host alone would spare "
        "replica B's tunnel for a host in this run's SSH_HOST and retire it otherwise, "
        "which is the same cross-replica damage by a slower route."
    )
