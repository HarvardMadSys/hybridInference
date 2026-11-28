"""The cloud-agent contract settings are discoverable before a cutover.

The gateway's half of the split is five settings. None of them used to appear
in `.env.example`, so the only place an operator could learn they exist was a
plan document. An unset
`GATEWAY_GRANT_DISPATCH_TOKEN` makes the whole internal API answer 404, which
is correct and indistinguishable from "this build has no internal API":
exactly the shape that costs an afternoon during a cutover window.

They are database-backed settings now, so the one place to find all five is
the admin console's Configuration tab, which lists the registry's
`integrations` entries with a description of each. `.env.example` keeps only
the environment, and no longer offers them.

The names are imported rather than spelled out here. Copying the strings would
make this a test that the registry contains five particular strings, which
stays green through a rename in the code it is supposed to be tracking.
"""

from __future__ import annotations

from pathlib import Path

from serving.config.app_config_registry import static_entry
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


def test_every_contract_variable_is_a_described_integration_setting() -> None:
    """An operator provisioning a cutover finds all five in one place."""
    entries = {name: static_entry(name) for name in CONTRACT_VARS}
    unregistered = sorted(name for name, entry in entries.items() if entry is None)
    assert not unregistered, (
        f"register these in app_config_registry, with a description saying what "
        f"a value is: {unregistered}"
    )
    misplaced = sorted(
        name
        for name, entry in entries.items()
        if entry is not None and (entry.category != "integrations" or not entry.description)
    )
    assert not misplaced, f"these are not described under Integrations: {misplaced}"
    # The signing key and the dispatch token never leave the admin API.
    assert entries[ENV_PRIVATE_KEY].secret
    assert entries[ENV_DISPATCH_TOKEN].secret


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
