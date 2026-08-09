"""The cloud-agent contract variables are discoverable before a cutover.

The gateway's half of the split is five environment variables. All five read
from `os.environ` in code, and none of them appeared in `.env.example` — so
the only place an operator could learn they exist was a plan document under
`docs/agents/plans/`. An unset `GATEWAY_GRANT_DISPATCH_TOKEN` makes the whole
internal API answer 404, which is correct and indistinguishable from "this
build has no internal API": exactly the shape that costs an afternoon during a
cutover window.

The names are imported rather than spelled out here. Copying the strings would
make this a test that `.env.example` contains five particular strings, which
stays green through a rename in the code it is supposed to be tracking.

Presence only. What a good value looks like belongs in the comments beside
each entry, and asserting on those would break every time someone improves the
prose.
"""

from __future__ import annotations

from pathlib import Path

from serving.servers.routers.internal_auth import ENV_DISPATCH_TOKEN
from serving.utils.identity_keys import ENV_PRIVATE_KEY, ENV_RETIRING_PUBLIC_KEYS
from serving.utils.identity_tokens import ENV_ALLOWED_REDIRECTS, ENV_ISSUER

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"

CONTRACT_VARS = (
    ENV_PRIVATE_KEY,
    ENV_RETIRING_PUBLIC_KEYS,
    ENV_ISSUER,
    ENV_ALLOWED_REDIRECTS,
    ENV_DISPATCH_TOKEN,
)


def test_every_contract_variable_is_in_env_example() -> None:
    """An operator provisioning a cutover can find all five in one place."""
    text = ENV_EXAMPLE.read_text()
    missing = sorted(name for name in CONTRACT_VARS if f"\n{name}=" not in text)
    assert not missing, (
        f"add these to .env.example, with a comment saying what a value is and "
        f"how to generate it: {missing}"
    )


def test_no_contract_variable_ships_a_value() -> None:
    """The example file carries placeholders, never credentials.

    Two of these are secrets and one is a private key. A value committed here
    would be published in every clone of a repository that is going open
    source, and would read as a working default rather than an accident.
    """
    populated = []
    for line in ENV_EXAMPLE.read_text().splitlines():
        name, _, value = line.partition("=")
        if name in CONTRACT_VARS and value.strip():
            populated.append(name)
    assert not populated, f"these ship a value in .env.example: {populated}"


def test_the_moved_half_is_not_still_offered() -> None:
    """No `AGENT_*` entry survives here — nothing in this repository reads one.

    H4 removed the cloud agent from the gateway, and with it every reader of
    the dispatcher token, the egress tiers, and the GitHub App and GitLab
    OAuth credentials. Leaving those entries in `.env.example` would have an
    operator provisioning a GitHub App for a service this repository no longer
    runs, and reading the silence afterwards as a broken integration.

    Assignments only. The section that replaced them names the variables in
    prose, so that an upgrading deployment knows what to carry across rather
    than concluding the feature was withdrawn.
    """
    offered = sorted(
        line.partition("=")[0]
        for line in ENV_EXAMPLE.read_text().splitlines()
        if line.startswith("AGENT_") and "=" in line
    )
    assert not offered, (
        "these are still offered in .env.example, but nothing in this "
        f"repository reads them after H4: {offered}"
    )
