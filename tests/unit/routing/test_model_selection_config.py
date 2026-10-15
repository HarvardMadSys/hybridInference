"""The ``auto:`` section of ``models.yaml`` (``routing.model_selection.config``).

A mistake in the section turns auto off with every problem listed; a candidate
the route table cannot serve as written is only a warning, because the route
table changes after startup.
"""

from __future__ import annotations

import pytest
import yaml

from routing.model_selection import (
    AutoConfigError,
    Candidate,
    CandidateInfo,
    check_against_catalog,
    parse_auto_config,
)

SECTION = """
auto:
  default_profile: balance
  session:
    idle_timeout_seconds: 900
    max_sessions: 500
  profiles:
    intelligence:
      candidates:
        - {model: frontier-a, reasoning_effort: high}
        - {model: mid-b, reasoning_effort: high}
    balance:
      candidates:
        - {model: mid-b}
        - {model: frontier-a}
    fast:
      candidates:
        - {model: small-c}
        - {model: mid-b, reasoning_effort: low}
"""


def _raw() -> dict:
    return yaml.safe_load(SECTION)["auto"]


def _problems(raw) -> list[str]:
    with pytest.raises(AutoConfigError) as excinfo:
        parse_auto_config(raw)
    return excinfo.value.problems


def test_a_section_parses_into_ordered_tiers():
    config = parse_auto_config(_raw())

    assert config.default_profile == "balance"
    assert config.idle_timeout_seconds == 900
    assert config.max_sessions == 500
    assert config.profiles["intelligence"].candidates == (
        Candidate("frontier-a", "high"),
        Candidate("mid-b", "high"),
    )
    assert config.virtual_model_ids == {"auto", "auto-intelligence", "auto-balance", "auto-fast"}


def test_session_settings_have_defaults():
    raw = _raw()
    del raw["session"], raw["default_profile"]

    config = parse_auto_config(raw)

    assert config.default_profile == "balance"
    assert config.idle_timeout_seconds == 1800
    assert config.max_sessions == 10_000


def test_auto_means_the_default_tier_and_auto_dash_names_one():
    config = parse_auto_config(_raw())

    assert config.resolve("auto").name == "balance"
    assert config.resolve("auto-fast").name == "fast"
    assert config.resolve("auto-missing") is None
    assert config.resolve("mid-b") is None


def test_the_version_follows_what_decides_the_ranking():
    versions = {name: p.version for name, p in parse_auto_config(_raw()).profiles.items()}
    again = {name: p.version for name, p in parse_auto_config(_raw()).profiles.items()}
    reordered = _raw()
    reordered["profiles"]["balance"]["candidates"].reverse()
    session_only = _raw()
    session_only["session"]["idle_timeout_seconds"] = 60

    assert versions == again
    assert len(set(versions.values())) == 3
    assert parse_auto_config(reordered).profiles["balance"].version != versions["balance"]
    assert parse_auto_config(session_only).profiles["balance"].version == versions["balance"]


def test_every_schema_problem_is_reported_at_once():
    raw = _raw()
    raw["sesion"] = {}
    raw["session"]["max_sessions"] = 0
    raw["profiles"]["fast"]["candidates"] = []
    raw["profiles"]["balance"]["candidates"][0]["weight"] = 2

    problems = _problems(raw)

    assert len(problems) == 4
    assert any(p.startswith("auto.sesion:") for p in problems)
    assert any(p.startswith("auto.session.max_sessions:") for p in problems)
    assert any(p.startswith("auto.profiles.fast.candidates:") for p in problems)
    assert any(p.startswith("auto.profiles.balance.candidates.0.weight:") for p in problems)


def test_every_semantic_problem_is_reported_at_once():
    raw = _raw()
    raw["default_profile"] = "cheap"
    raw["profiles"]["Fast!"] = raw["profiles"].pop("fast")
    raw["profiles"]["balance"]["candidates"].append({"model": "mid-b"})
    raw["profiles"]["intelligence"]["candidates"].append({"model": "auto-balance"})
    raw["profiles"]["intelligence"]["candidates"].append({"model": "auto"})

    problems = _problems(raw)

    assert problems == [
        "auto.default_profile: 'cheap' is not one of the profiles "
        "['Fast!', 'balance', 'intelligence']",
        "auto.profiles.intelligence.candidates.2: 'auto-balance' is an auto model, "
        "not a model to choose",
        "auto.profiles.intelligence.candidates.3: 'auto' is an auto model, not a model to choose",
        "auto.profiles.balance.candidates.2: mid-b is listed twice",
        "auto.profiles.Fast!: a profile name is lowercase letters, digits and single hyphens",
    ]


def test_the_same_model_at_two_efforts_is_two_candidates():
    raw = {
        "profiles": {
            "balance": {
                "candidates": [{"model": "m", "reasoning_effort": "high"}, {"model": "m"}],
            }
        }
    }

    assert len(parse_auto_config(raw).profiles["balance"].candidates) == 2


@pytest.mark.parametrize("raw", [None, [], "balance"])
def test_a_section_that_is_not_a_mapping_is_refused(raw):
    assert _problems(raw) == [f"auto must be a mapping, got {type(raw).__name__}"]


def test_problems_are_in_the_message_too():
    with pytest.raises(AutoConfigError, match="default_profile"):
        parse_auto_config({**_raw(), "default_profile": "cheap"})


def _catalog(**overrides) -> dict[str, CandidateInfo]:
    efforts = frozenset({"low", "high"})
    return {
        "frontier-a": CandidateInfo(200_000, 32_000, reasoning_efforts=efforts),
        "mid-b": CandidateInfo(128_000, 16_000, reasoning_efforts=efforts),
        "small-c": CandidateInfo(32_000, 8_000),
        **overrides,
    }


def test_a_section_that_fits_the_catalog_has_nothing_to_report():
    check = check_against_catalog(parse_auto_config(_raw()), _catalog())

    assert check.conflicts == ()
    assert check.warnings == ()


def test_a_tier_named_like_a_registered_model_is_a_conflict():
    """``auto-fast`` already names a model or alias; auto would hide it from its callers."""
    catalog = _catalog(**{"auto-fast": CandidateInfo(8_000, 1_000)})

    check = check_against_catalog(parse_auto_config(_raw()), catalog)

    assert check.conflicts == ("'auto-fast' is a registered model name; auto would hide it",)


def test_a_candidate_the_catalog_cannot_serve_is_only_a_warning():
    """Unregistered (skipped at boot, or yet to be added) or at an effort its model lacks."""
    catalog = _catalog(
        **{"mid-b": CandidateInfo(128_000, 16_000, reasoning_efforts=frozenset({"high"}))}
    )
    del catalog["small-c"]

    check = check_against_catalog(parse_auto_config(_raw()), catalog)

    assert check.conflicts == ()
    assert check.warnings == (
        "auto.profiles.fast: small-c: model is not registered; skipped until it is",
        "auto.profiles.fast: mid-b@low: model accepts ['high']; skipped",
    )
