"""Tests for the versioned public branding document."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from serving.config.branding import BrandingConfig

_REPO_ROOT = Path(__file__).resolve().parents[3]
_EXAMPLE = _REPO_ROOT / "config" / "examples" / "branding.example.json"


def _example() -> dict[str, Any]:
    return json.loads(_EXAMPLE.read_text())


def test_example_branding_is_valid_and_complete() -> None:
    config = BrandingConfig.model_validate(_example())

    assert config.schema_version == 1
    assert config.organization.name == "Example Organization"
    assert config.example.api_key_env_var == "EXAMPLE_INFERENCE_API_KEY"
    assert [(link.label, link.url) for link in config.links.nav] == [
        ("Example Project", "https://project.example.com")
    ]
    assert config.team == []
    assert config.sponsors == []


def test_branding_written_before_nav_links_existed_still_loads() -> None:
    data = _example()
    del data["links"]["nav"]

    config = BrandingConfig.model_validate(data)

    # The strict model must not turn an added optional field into a validation
    # failure for every distribution that has not adopted it yet.
    assert config.links.nav == []
    assert config.public_payload(docs_url="https://docs.example.com")["links"]["nav"] == []


def test_public_payload_uses_snake_case_and_omits_absent_team_fields() -> None:
    data = _example()
    data["team"] = [{"name": "Example Person", "affiliations": ["Example Lab"]}]
    data["sponsors"] = [
        {
            "name": "Example Sponsor",
            "alt": "Example Sponsor logo",
            "src": "/site-assets/sponsor.svg",
            "class_name": "h-8",
            "width": 160,
            "height": 40,
        }
    ]

    payload = BrandingConfig.model_validate(data).public_payload(
        docs_url="https://override.example/docs"
    )

    assert payload["links"]["docs_url"] == "https://override.example/docs"
    assert payload["team"] == [{"name": "Example Person", "affiliations": ["Example Lab"]}]
    assert payload["sponsors"][0]["class_name"] == "h-8"


@pytest.mark.parametrize(
    "docs_url",
    [
        "http://insecure.example",
        "https://docs.example?language=en",
        "https://docs.example#install",
    ],
)
def test_public_payload_revalidates_the_operational_docs_override(docs_url: str) -> None:
    config = BrandingConfig.model_validate(_example())

    with pytest.raises(ValueError):
        config.public_payload(docs_url=docs_url)


def test_fields_outside_the_public_contract_are_refused() -> None:
    data = _example()
    data["signup"]["turnstile_secret_key"] = "must-not-be-public"

    with pytest.raises(ValidationError, match="turnstile_secret_key"):
        BrandingConfig.model_validate(data)


@pytest.mark.parametrize(
    ("keys", "value"),
    [
        (("site_host",), ""),
        (("links", "docs_url"), "http://insecure.example"),
        (("links", "docs_url"), "https://example.com:bad"),
        (("links", "docs_url"), "https://exa%mple.com"),
        (("links", "docs_url"), "https://999.999.999.999"),
        (("links", "docs_url"), "https://[v1.zz]"),
        (("links", "docs_url"), "https://docs.example.com?language=en"),
        (("links", "docs_url"), "https://docs.example.com#"),
        (("links", "github_url"), "https://github.com/example/repo?tab=readme"),
        (("links", "github_url"), "https://github.com/example/repo#readme"),
        (("example", "api_base"), "http://example.com:99999"),
        (("example", "api_base"), "https://api.example.com?tenant=example"),
        (("example", "api_base"), "https://api.example.com?"),
        (("example", "api_base"), "https://api.example.com#completion"),
        (("example", "api_key_env_var"), "not-a-shell-name"),
        (("analytics", "statcounter_project_id"), "not-digits"),
        (("storage_key_prefix",), "contains spaces"),
        (("assets", "logo_url"), "/public/logo.svg"),
        (("assets", "logo_url"), "/site-assets//logo.svg"),
        (("assets", "logo_url"), "/site-assets/%2e%2e/logo.svg"),
        (("assets", "logo_url"), "/site-assets/.private/logo.svg"),
        (("assets", "logo_url"), "/site-assets/logo.txt"),
        (("assets", "logo_url"), "/site-assets/logo%ZZ.svg"),
    ],
)
def test_values_the_runtime_client_cannot_consume_are_refused(
    keys: tuple[str, ...], value: str
) -> None:
    data = _example()
    target = data
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value

    with pytest.raises(ValidationError):
        BrandingConfig.model_validate(data)


@pytest.mark.parametrize(
    "nav",
    [
        [{"label": "Example Project", "url": "http://insecure.example.com"}],
        [{"label": "Example Project", "url": "javascript:alert(1)"}],
        [{"label": "Example Project", "url": ""}],
        [{"label": "", "url": "https://project.example.com"}],
        [{"url": "https://project.example.com"}],
        [{"label": "Example Project", "url": "https://project.example.com", "target": "_self"}],
    ],
)
def test_a_nav_link_the_console_cannot_render_is_refused(nav: list[dict[str, str]]) -> None:
    data = _example()
    data["links"]["nav"] = nav

    with pytest.raises(ValidationError):
        BrandingConfig.model_validate(data)


def test_a_sponsor_class_the_neutral_css_does_not_compile_is_refused() -> None:
    data = _example()
    data["sponsors"] = [
        {
            "name": "Unsafe Sponsor",
            "alt": "Unsafe Sponsor logo",
            "src": "/site-assets/sponsor.svg",
            "class_name": "absolute bg-[url(javascript:alert(1))]",
            "width": 160,
            "height": 40,
        }
    ]

    with pytest.raises(ValidationError, match="class_name"):
        BrandingConfig.model_validate(data)


def test_unsupported_layout_keys_are_named() -> None:
    """Unsupported branding keys fail with an actionable field name.

    Runtime branding does not select public-page layouts. A module supplies its
    own presentation at build time, and the branding schema rejects fields it
    does not support instead of silently accepting unused settings.
    """
    data = _example()
    data["theme"] = {"accent": "#0052D9"}

    with pytest.raises(ValidationError, match="theme"):
        BrandingConfig.model_validate(data)
