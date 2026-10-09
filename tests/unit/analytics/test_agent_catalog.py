"""Client identification for the public usage stats."""

from __future__ import annotations

import pytest

from serving.analytics import agent_catalog as catalog


@pytest.mark.parametrize(
    ("agent", "user_agent", "expected"),
    [
        # A name declared in the system prompt wins, whatever the header says.
        ("Claude", "node", ("Claude Code", catalog.CODING)),
        ("Hermes", "OpenAI/Python 2.24.0", ("Hermes Agent", catalog.GENERAL)),
        ("codex", "", ("Codex", catalog.CODING)),
        # Otherwise a catalog User-Agent.
        (None, "claude-cli/2.1.289 (external, cli)", ("Claude Code", catalog.CODING)),
        (None, "Codex Desktop/0.160.0 (Windows 10.0.26200; x86_64)", ("Codex", catalog.CODING)),
        (None, "codex_exec/0.153.2 (Ubuntu 26.4.0; x86_64)", ("Codex", catalog.CODING)),
        (None, "opencode/1.18.34 ai-sdk/provider-utils/4.0.23", ("OpenCode", catalog.CODING)),
        (None, "opencode-kilo-provider/1.0", ("Kilo Code", catalog.CODING)),
        (None, "pi (linux 6.17.0-35-generic; x64)", ("pi", catalog.CODING)),
        (
            None,
            "deepseek-harness/0.2.0-rc.2 (+https://example.test)",
            ("DeepSeek Harness", catalog.GENERAL),
        ),
        (None, "doc_assistant", ("Docs assistant", catalog.CHAT)),
        # A declared name outside the catalog, or a distinctive header: custom.
        ("Shiroko", "node", (None, catalog.CUSTOM)),
        (None, "paper-trade/1.0", (None, catalog.CUSTOM)),
        # Generic libraries with no declared name: direct API use.
        (None, "OpenAI/Python 3.26.0", (None, catalog.DIRECT)),
        (None, "curl/8.7.1", (None, catalog.DIRECT)),
        (None, "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", (None, catalog.DIRECT)),
        (None, None, (None, catalog.DIRECT)),
    ],
)
def test_classify(agent, user_agent, expected):
    assert catalog.classify(agent, user_agent) == expected


@pytest.mark.parametrize(
    ("user_agent", "name"),
    [
        ("codex-tui/0.160.0 (Pop!_OS 22.4.0; x86_64) gnome-terminal", "codex-tui"),
        ("Codex Desktop/0.159.0 (Windows 10.0.26200; x86_64)", "codex desktop"),
        ("pi (win32 10.0.26300; x64)", "pi"),
        ("Zed/1.16.1+stable.343 (windows; x86_64)", "zed"),
        ("zed/0.9", "zed"),
        (".atomcell/3.0.900", "atomcell"),
        ("UltimateResearchAgentV5", "ultimateresearchagent"),
        ("", ""),
        (None, ""),
    ],
)
def test_client_name_drops_version_and_platform(user_agent, name):
    assert catalog.client_name(user_agent) == name


@pytest.mark.parametrize(
    ("user_agent", "generic"),
    [
        ("python-requests/2.32.5", True),
        ("AsyncOpenAI/Python 2.41.1", True),
        ("Go-http-client/2.0", True),
        ("", True),
        (None, True),
        ("factory-cli/0.215.1", False),
        ("jcode/0.1.0", False),
    ],
)
def test_is_generic_client(user_agent, generic):
    assert catalog.is_generic_client(user_agent) is generic


def test_every_kind_is_listed_and_catalog_names_are_unique():
    assert {p.kind for p in catalog.CATALOG} <= set(catalog.KINDS)
    names = [p.name for p in catalog.CATALOG]
    assert len(names) == len(set(names))
