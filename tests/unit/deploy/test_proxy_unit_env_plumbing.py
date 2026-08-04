"""The idle proxies must read the gateway's key from .env, and nothing else.

Two failures took the DGX Spark proxy down together. Its systemd unit was never
installed, so a host reboot ended the process; and when it was restarted by hand
the operator lifted ``LOCAL_API_KEY`` out of ``.env`` with ``grep | cut -d=``,
which keeps the single quotes the file writes around the value, so the proxy
compared a quoted key against the gateway's unquoted one and 401'd every request
for the model it serves. Nothing alerted.

The unit could not have supplied the key either: it set no ``LOCAL_API_KEY`` at
all, so a systemd-managed proxy could only ever use the default hardcoded in the
proxy source -- correct until the day that key is rotated, then silently wrong.
So all three proxy units now read the repo ``.env``, the same file the gateway
signs its requests from, and systemd -- which does apply POSIX shell quoting --
strips the quotes the shell one-liner kept.

All three, not two: the four local routes (8001, 8002, and the two H200 nodes on
8003/8004) share one key, so a rotation path that reaches some of them converts a
uniform, loud failure into a partial, silent one -- and because the H200 model
carries a remote fallback, into paid traffic nobody ordered.

Reading a whole gateway .env into a proxy unit costs something, though, and this
is the part that is easy to get wrong later: ``EnvironmentFile=`` outranks
``Environment=`` unconditionally, so any key in .env silently wins over the
unit's own setting and no amount of reordering the directives changes that.
``MODELS_CONFIG`` is exactly such a key -- a legacy gateway alias naming a YAML
registry, and the proxies' name for their own JSON one. The two units that want
no value drop it with ``UnsetEnvironment=``, which systemd applies last of all;
the H200 unit, which must keep one, sets it through ``/usr/bin/env`` on its
``ExecStart`` line, which lands in the child after systemd is done. The rest of
what the units pin is safe only while .env never grows a key of the same name.

No systemd is required: the files are parsed directly.
"""

from __future__ import annotations

import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SYSTEMD = REPO / "deploy" / "systemd"
ENV_EXAMPLE = REPO / ".env.example"
KEY_HELPER = REPO / "ops" / "lib" / "systemd_local_api_key.sh"

sys.path.insert(0, str(REPO / "apps" / "backend"))

# Every unit that runs one of the idle proxies, and the installer that delivers a
# key to it on a box with no .env. All four proxy routes authenticate against the
# same LOCAL_API_KEY, so a unit missing from this list is a route a rotation
# silently skips.
PROXY_UNITS = {
    "local_deployment_proxy.service": REPO / "ops" / "local_deployment_proxy" / "install.sh",
    "spark_idle_proxy.service": REPO / "ops" / "spark_idle_proxy" / "install_service.sh",
    "h200_idle_proxy.service": REPO / "ops" / "h200_idle_proxy" / "install.sh",
}

# Assignments in .env.example, including the commented-out ones -- a documented
# knob an operator uncomments lands in a real .env just the same.
_ENV_ASSIGNMENT = re.compile(r"^\s*#?\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)

# A live (uncommented) assignment, split into name and value.
_LIVE_ASSIGNMENT = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)(?P<name>[A-Z][A-Z0-9_]*)=(?P<value>.*)$"
)


def _unit(name: str) -> str:
    return (SYSTEMD / name).read_text(encoding="utf-8")


def _directive(text: str, key: str) -> list[str]:
    """Values of every ``key=`` directive in a unit, comments excluded."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            out.append(value.strip())
    return out


def _pinned_env_keys(text: str) -> set[str]:
    """Variable names the unit sets with ``Environment=``."""
    return {v.partition("=")[0].strip().strip('"') for v in _directive(text, "Environment")}


def _live_env_example_assignments() -> list[re.Match[str]]:
    """Uncommented ``NAME=value`` lines in .env.example."""
    out = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _LIVE_ASSIGNMENT.match(line)
        if match:
            out.append(match)
    return out


@pytest.mark.parametrize("unit", PROXY_UNITS)
def test_proxy_unit_reads_the_repo_env(unit: str) -> None:
    """Without this the key can only ever be the one hardcoded in the source."""
    files = _directive(_unit(unit), "EnvironmentFile")
    assert "-__REPO_ROOT__/.env" in files, (
        f"{unit} must read the repo .env so LOCAL_API_KEY matches what the gateway "
        f"sends; the leading '-' keeps a box without one startable. Found: {files}"
    )


@pytest.mark.parametrize("unit", PROXY_UNITS)
def test_proxy_unit_keeps_the_gateways_models_config_out(unit: str) -> None:
    """A gateway MODELS_CONFIG names YAML; these proxies json.load() the path.

    Two mechanisms are legitimate, because the units want different things. A
    unit that wants the proxy's own auto-detected value drops the variable with
    ``UnsetEnvironment=``. A unit that must pin a specific config sets it through
    ``/usr/bin/env`` on ``ExecStart``, in the child, after systemd has compiled
    the environment. What is *not* legitimate is a bare ``Environment=`` line: an
    ``EnvironmentFile=`` outranks it whatever the order, so it would read as
    though it decided the value while .env quietly replaced it.
    """
    text = _unit(unit)
    unset = {v.strip() for value in _directive(text, "UnsetEnvironment") for v in value.split()}
    exec_start = " ".join(_directive(text, "ExecStart"))
    pinned_on_argv = exec_start.startswith("/usr/bin/env ") and "MODELS_CONFIG=" in exec_start

    assert "MODELS_CONFIG" in unset or pinned_on_argv, (
        f"{unit} reads a gateway .env, where MODELS_CONFIG may name a YAML registry. "
        "Keep it out with UnsetEnvironment=, or pin the proxy's own value with a "
        "/usr/bin/env prefix on ExecStart -- both outrank EnvironmentFile=."
    )
    assert "MODELS_CONFIG" not in _pinned_env_keys(text), (
        f"{unit} sets MODELS_CONFIG with Environment=, which loses to the .env it "
        "reads. Use UnsetEnvironment= or the /usr/bin/env prefix on ExecStart."
    )


# Everywhere an operator is handed a way to pin MODELS_CONFIG on a unit that reads
# the gateway .env: the three units' own comments, and the two proxy READMEs.
MODELS_CONFIG_DOCS = [
    SYSTEMD / "local_deployment_proxy.service",
    SYSTEMD / "spark_idle_proxy.service",
    SYSTEMD / "h200_idle_proxy.service",
    REPO / "ops" / "local_deployment_proxy" / "README.md",
    REPO / "ops" / "spark_idle_proxy" / "README.md",
]

# ``Environment=MODELS_CONFIG=…`` in any form -- a directive, a fenced ini block, a
# comment. The lookbehind spares the ``UnsetEnvironment=MODELS_CONFIG`` the units
# really do use, of which it is otherwise a substring.
_ENVIRONMENT_PINS_MODELS_CONFIG = re.compile(r'(?<!Unset)Environment="?MODELS_CONFIG=')


@pytest.mark.parametrize(
    "doc", MODELS_CONFIG_DOCS, ids=[f"{p.parent.name}/{p.name}" for p in MODELS_CONFIG_DOCS]
)
def test_documented_models_config_override_is_one_that_actually_wins(doc: Path) -> None:
    """The escape hatch has to work in the one case it exists for.

    The obvious recipe -- a drop-in that resets ``UnsetEnvironment=`` and then
    writes ``Environment=MODELS_CONFIG=…`` -- is wrong, and wrong exactly when a
    .env does define MODELS_CONFIG, which is the collision it is offered for.
    Clearing the unset list only cancels the final deletion; the environment file
    then goes back to outranking ``Environment=`` and the proxy loads the gateway's
    YAML after all. An operator who follows it sees a proxy with no backends and a
    drop-in that reads as though it had decided the value.

    What survives is the child-process form the H200 unit already uses:
    ``/usr/bin/env MODELS_CONFIG=… <interpreter> <script>`` on ``ExecStart``, set
    after systemd has compiled the environment. So no file here may show
    ``Environment=MODELS_CONFIG=``, and each must still carry the form that works.
    """
    text = doc.read_text(encoding="utf-8")
    offenders = [
        line.strip() for line in text.splitlines() if _ENVIRONMENT_PINS_MODELS_CONFIG.search(line)
    ]
    assert not offenders, (
        f"{doc.name} sets MODELS_CONFIG with Environment=: {offenders}. The .env this "
        "unit reads outranks Environment= whatever the order, and resetting "
        "UnsetEnvironment= does not change that -- it only stops the deletion. Pin it "
        "with a /usr/bin/env prefix on ExecStart instead."
    )
    assert "/usr/bin/env MODELS_CONFIG=" in text, (
        f"{doc.name} no longer shows how to pin MODELS_CONFIG in the child process. "
        "Without that recipe the next operator reaches for Environment=, which loses "
        "to the .env silently."
    )


def test_models_config_is_still_a_gateway_alias() -> None:
    """The premise for all of the above: drop the alias and the units can stop.

    Asserted behaviourally. ``"MODELS_CONFIG" in settings.py`` would be satisfied
    by the canonical ``MODELS_CONFIG_PATH``, of which it is a substring, so that
    spelling of the test passes even with the alias deleted.
    """
    from serving.config.settings import Settings

    settings = Settings(_env_file=None, MODELS_CONFIG="/only-reachable-via-the-alias.yaml")
    assert settings.models_config_path == "/only-reachable-via-the-alias.yaml", (
        "the proxy units go out of their way to keep MODELS_CONFIG out of the proxy "
        "environment because the gateway still accepts it as a legacy alias of "
        "MODELS_CONFIG_PATH. Once the gateway ignores it, that collision is gone "
        "and the UnsetEnvironment= lines and the /usr/bin/env prefix can go too."
    )


def test_env_example_declares_the_shared_proxy_key() -> None:
    """The whole arrangement rests on .env carrying LOCAL_API_KEY.

    On a deploy whose .env grew from this template -- which is every deploy --
    an undeclared key means ``EnvironmentFile=`` supplies nothing, the proxy
    keeps its hardcoded default, and the gateway's ``${LOCAL_API_KEY}``
    interpolation in models.yaml resolves to empty. That is the outage's own
    shape, reached with every other test here green.
    """
    values = {m.group("name"): m.group("value") for m in _live_env_example_assignments()}
    assert "LOCAL_API_KEY" in values, (
        f"{ENV_EXAMPLE.name} must declare LOCAL_API_KEY: the proxy units read this "
        "file for it, and models.yaml interpolates it into every local route's "
        "api_keys. Undeclared, the two ends silently disagree."
    )
    assert values["LOCAL_API_KEY"].strip(), (
        "LOCAL_API_KEY must carry a non-empty value. Unlike the provider keys, "
        "blank is not 'feature off' here -- it is a 100% 401 rate on every local "
        "model, and it used to disable proxy auth outright."
    )


def test_env_example_avoids_syntax_systemd_reads_differently() -> None:
    """.env is now parsed by two parsers that do not agree.

    Compose feeds this file to the gateway through its dotenv reader; the proxy
    units read the same file with systemd's. Per systemd.exec(5), systemd ignores
    only lines *starting* with '#' and keeps "interior whitespace within the line
    ... verbatim", so a trailing ``# comment`` becomes part of the value -- the
    gateway would sign with ``key`` and the proxy compare against
    ``key   # comment``. An ``export`` prefix diverges the other way: honored by
    Compose, rejected by systemd as an invalid variable name.
    """
    offenders = []
    for match in _live_env_example_assignments():
        if match.group("prefix").strip():
            offenders.append(f"{match.group('name')}: 'export' prefix")
        if "#" in match.group("value"):
            offenders.append(f"{match.group('name')}: trailing '#' comment")
    assert not offenders, (
        "systemd and Compose read these lines differently, which is the silent "
        f"key-mismatch the proxy units exist to prevent: {offenders}. Put the "
        "comment on its own line above the assignment, and drop 'export'."
    )


def test_env_example_declares_nothing_a_proxy_unit_pins() -> None:
    """.env.example is the template every real .env grows from.

    A key added there that a proxy unit also sets with ``Environment=`` would
    take over the moment the unit reads the file, because environment files
    outrank ``Environment=`` unconditionally. The unit would still read as
    though it decided the value. Nothing in the unit can defend against this, so
    the defence is here: keep the two namespaces disjoint.
    """
    declared = set(_ENV_ASSIGNMENT.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))
    collisions = {
        unit: sorted(_pinned_env_keys(_unit(unit)) & declared)
        for unit in PROXY_UNITS
        if _pinned_env_keys(_unit(unit)) & declared
    }
    assert not collisions, (
        "these .env.example keys would override the proxy unit setting of the same "
        f"name, silently: {collisions}. Rename the gateway variable, or move the "
        "unit's value out of the environment."
    )


@pytest.mark.parametrize(("unit", "installer"), PROXY_UNITS.items(), ids=list(PROXY_UNITS))
def test_installer_delivers_the_key_and_bounces_the_proxy(unit: str, installer: Path) -> None:
    """A key on disk that the running process never read is the same outage.

    ``systemctl enable --now`` only *starts* a unit, and start is a no-op on an
    already-active one -- so on the rotation re-run the installers advertise, the
    new key would land in /etc/systemd/system while the live proxy kept
    authenticating with the old one.
    """
    text = installer.read_text(encoding="utf-8")
    assert "write_local_api_key_dropin" in text, (
        f"{installer.name} must deliver LOCAL_API_KEY to {unit}; boxes that run only "
        "a proxy and its tunnel have no .env for the unit to read."
    )
    assert re.search(r'systemctl restart "\$\{(PROXY_UNIT|SERVICE_NAME|SERVICE_UNIT)\}"', text), (
        f"{installer.name} must restart {unit}: systemd does not re-read a drop-in "
        "on its own, and `enable --now` does nothing to an already-running unit."
    )


def _installer_key_call(installer: Path) -> str:
    """The single line where an installer hands LOCAL_API_KEY to the helper."""
    calls = [
        line.strip()
        for line in installer.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("write_local_api_key_dropin ")
    ]
    assert len(calls) == 1, f"{installer.name}: expected one call, found {calls}"
    return calls[0]


@pytest.mark.parametrize(("unit", "installer"), PROXY_UNITS.items(), ids=list(PROXY_UNITS))
@pytest.mark.parametrize(
    ("env", "expected_argc", "why"),
    [
        ({}, "2", "LOCAL_API_KEY unset must reach the helper as *no* key argument"),
        ({"LOCAL_API_KEY": ""}, "3", "an explicit empty key must still ask for a clear"),
        ({"LOCAL_API_KEY": "k"}, "3", "a real key must be passed through"),
    ],
    ids=["unset", "empty", "set"],
)
def test_installer_does_not_turn_a_key_less_re_run_into_a_clear(
    unit: str, installer: Path, env: dict[str, str], expected_argc: str, why: str
) -> None:
    """``"${LOCAL_API_KEY:-}"`` would delete the key on every ordinary re-run.

    The helper reads an empty third argument as "remove the drop-in", so an
    installer that expands an unset LOCAL_API_KEY to an empty string erases the
    key whenever it is re-run for something else entirely -- a tunnel host, a
    port -- and then restarts the proxy onto its hardcoded default. Run the real
    call line against a shim, because the difference is invisible to a reader:
    ``${VAR+"$VAR"}`` and ``"${VAR:-}"`` differ only in whether an argument
    exists at all.
    """
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            'SYSTEMD_DST="/nonexistent"; PROXY_UNIT="u.service"; SERVICE_UNIT="u.service"\n'
            'write_local_api_key_dropin() { printf "%s" "$#"; }\n' + _installer_key_call(installer),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", **env},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected_argc, f"{installer.name} ({unit}): {why}"


@pytest.mark.parametrize(
    "uninstaller",
    [
        REPO / "ops" / "local_deployment_proxy" / "uninstall.sh",
        REPO / "ops" / "h200_idle_proxy" / "uninstall.sh",
        REPO / "ops" / "spark_idle_proxy" / "install_service.sh",  # --uninstall
    ],
    ids=["local_deployment_proxy", "h200_idle_proxy", "spark_idle_proxy"],
)
def test_uninstaller_removes_the_key_dropin(uninstaller: Path) -> None:
    """The drop-in is a credential; a 'clean' box must not still be holding it."""
    text = uninstaller.read_text(encoding="utf-8")
    dropin_dir = re.search(r'"\$\{SYSTEMD_DST\}/\$\{(PROXY_UNIT|SERVICE_UNIT)\}\.d"', text)
    assert dropin_dir, (
        f"{uninstaller.name} names no proxy .d directory, so it cannot be removing "
        "one. install.sh writes the LOCAL_API_KEY drop-in there."
    )
    assert re.search(r"rm -rf", text), (
        f"{uninstaller.name} removes the unit file but not the proxy's .d directory, "
        "so a mode-0600 file in /etc/systemd/system keeps the live LOCAL_API_KEY on "
        "a box the operator believes is clean -- and a later reinstall inherits it."
    )


# ── The drop-in writer, run for real ───────────────────────────────────────
#
# ops/lib/systemd_local_api_key.sh is a pure function over a directory, so these
# exercise it instead of grepping it. No systemd and no root: the drop-in is an
# ordinary file, and what matters is exactly what systemd would later read out of
# it.

UNIT = "local_deployment_proxy.service"


def _write_dropin(systemd_dir: Path, key: str | None) -> subprocess.CompletedProcess[str]:
    """Run the helper for real. ``key=None`` omits the argument entirely.

    The distinction is the contract: an empty key clears the drop-in, an absent
    one leaves it alone.
    """
    args = [str(systemd_dir), UNIT] if key is None else [str(systemd_dir), UNIT, key]
    return subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail; source "{KEY_HELPER}"; write_local_api_key_dropin "$@"',
            "bash",
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _dropin(systemd_dir: Path) -> Path:
    return systemd_dir / f"{UNIT}.d" / "local-api-key.conf"


def test_key_dropin_is_written_unreadable_to_others(tmp_path: Path) -> None:
    """It holds the shared secret for every local model."""
    result = _write_dropin(tmp_path, "sekrit-key")
    assert result.returncode == 0, result.stderr

    dropin = _dropin(tmp_path)
    assert dropin.read_text() == '[Service]\nEnvironment="LOCAL_API_KEY=sekrit-key"\n'
    assert stat.S_IMODE(dropin.stat().st_mode) == 0o600


def test_key_dropin_doubles_a_percent_so_systemd_does_not_rewrite_it(tmp_path: Path) -> None:
    """Environment= is specifier-expanded, so a bare '%' does not survive.

    ``%h`` would be substituted with a path and ``%z`` -- no such specifier --
    would invalidate the assignment and drop it, leaving the proxy on its
    hardcoded default and 401ing every request.
    """
    result = _write_dropin(tmp_path, "ab%hc%%d%ze")
    assert result.returncode == 0, result.stderr

    written = _dropin(tmp_path).read_text()
    assert written == '[Service]\nEnvironment="LOCAL_API_KEY=ab%%hc%%%%d%%ze"\n'
    # What systemd expands that back to is the key the operator passed.
    value = written.splitlines()[1].split("LOCAL_API_KEY=", 1)[1].rstrip('"')
    assert value.replace("%%", "%") == "ab%hc%%d%ze"


@pytest.mark.parametrize(
    ("key", "why"),
    [
        ('has"quote', "systemd unquotes the value, so the quote would not survive"),
        ("has\\backslash", "same -- systemd.syntax(7) unescaping eats it"),
        ("has\nnewline", "the newline would end the directive and start a second one"),
    ],
    ids=["quote", "backslash", "newline"],
)
def test_key_dropin_refuses_a_key_systemd_would_mangle(tmp_path: Path, key: str, why: str) -> None:
    """Refusing is the point: a mis-set key is the failure this prevents."""
    result = _write_dropin(tmp_path, key)
    assert result.returncode != 0, f"{why}; the helper accepted it instead"
    assert "LOCAL_API_KEY" in result.stderr
    assert not _dropin(tmp_path).exists(), "a refused key must leave nothing behind"


def test_an_explicitly_empty_key_clears_a_dropin_an_earlier_run_left(tmp_path: Path) -> None:
    """Taking a key away has to be possible, and this is how it is asked for."""
    assert _write_dropin(tmp_path, "old-key").returncode == 0
    assert _dropin(tmp_path).exists()

    result = _write_dropin(tmp_path, "")
    assert result.returncode == 0, result.stderr
    assert not _dropin(tmp_path).exists()


def test_an_absent_key_leaves_an_installed_one_alone(tmp_path: Path) -> None:
    """The installers are re-run for reasons that have nothing to do with the key.

    A new tunnel host, a moved port, a fresh checkout: none of those runs carries
    LOCAL_API_KEY, and on the boxes this drop-in exists for -- the ones with no
    repo .env for the unit to read -- deleting it reverts the proxy to the default
    hardcoded in its source while the gateway keeps signing with the rotated key.
    The installers restart the unit right afterwards, so that lands in the live
    process: a 100% 401 rate on that route, and for deepseek-v4-flash a quiet
    move to a paid provider. Absence is therefore not a request to clear.
    """
    assert _write_dropin(tmp_path, "live-key").returncode == 0
    before = _dropin(tmp_path).read_text()

    result = _write_dropin(tmp_path, None)
    assert result.returncode == 0, result.stderr
    assert _dropin(tmp_path).exists(), (
        "a key-less re-run deleted the drop-in; on a box with no .env that is the "
        "silent 401 these units exist to prevent"
    )
    assert _dropin(tmp_path).read_text() == before
    assert "Keeping" in result.stdout, "the operator has to be told the key was kept"


def test_an_absent_key_is_quiet_when_there_is_nothing_installed(tmp_path: Path) -> None:
    """The common case -- a box whose .env supplies the key -- says nothing."""
    result = _write_dropin(tmp_path, None)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", result.stdout
    assert not _dropin(tmp_path).exists()


def test_writing_a_key_is_idempotent(tmp_path: Path) -> None:
    """The installers re-run to rotate; a re-run must converge, not accumulate."""
    assert _write_dropin(tmp_path, "same-key").returncode == 0
    first = _dropin(tmp_path).read_text()
    assert _write_dropin(tmp_path, "same-key").returncode == 0

    assert _dropin(tmp_path).read_text() == first
    assert sorted(p.name for p in (tmp_path / f"{UNIT}.d").iterdir()) == ["local-api-key.conf"]
