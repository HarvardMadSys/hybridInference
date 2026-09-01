"""Tests for the versioned public branding document."""

from pathlib import Path

import pytest
import yaml

from serving.config.branding import BrandingConfigError, load_branding_config

_REPO_ROOT = Path(__file__).resolve().parents[3]
_EXAMPLE = _REPO_ROOT / "config" / "examples" / "branding.example.yaml"


def test_example_branding_is_valid_and_complete() -> None:
    config = load_branding_config(_EXAMPLE)

    assert config.schema_version == 1
    assert config.organization.name == "Example Organization"
    assert config.example.api_key_env_var == "EXAMPLE_INFERENCE_API_KEY"
    assert config.team == []
    assert config.sponsors == []


def test_public_payload_uses_snake_case_and_omits_absent_team_fields(tmp_path: Path) -> None:
    data = yaml.safe_load(_EXAMPLE.read_text())
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
    path = tmp_path / "branding.yaml"
    path.write_text(yaml.safe_dump(data))

    payload = load_branding_config(path).public_payload(docs_url="https://override.example/docs")

    assert payload["links"]["docs_url"] == "https://override.example/docs"
    assert payload["team"] == [{"name": "Example Person", "affiliations": ["Example Lab"]}]
    assert payload["sponsors"][0]["class_name"] == "h-8"


def test_public_payload_revalidates_the_operational_docs_override() -> None:
    config = load_branding_config(_EXAMPLE)

    with pytest.raises(ValueError, match="https"):
        config.public_payload(docs_url="http://insecure.example")


def test_loader_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BrandingConfigError, match="cannot read branding config"):
        load_branding_config(tmp_path / "missing.yaml")


def test_loader_rejects_malformed_yaml(tmp_path: Path) -> None:
    path = tmp_path / "branding.yaml"
    path.write_text("links: [")

    with pytest.raises(BrandingConfigError, match="cannot read branding config"):
        load_branding_config(path)


def test_loader_rejects_fields_outside_public_contract(tmp_path: Path) -> None:
    data = yaml.safe_load(_EXAMPLE.read_text())
    data["signup"]["turnstile_secret_key"] = "must-not-be-public"
    path = tmp_path / "branding.yaml"
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(BrandingConfigError, match="turnstile_secret_key"):
        load_branding_config(path)


@pytest.mark.parametrize(
    ("keys", "value"),
    [
        (("site_host",), ""),
        (("links", "docs_url"), "http://insecure.example"),
        (("links", "docs_url"), "https://example.com:bad"),
        (("example", "api_base"), "http://example.com:99999"),
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
def test_loader_rejects_values_the_runtime_client_cannot_consume(
    tmp_path: Path, keys: tuple[str, ...], value: str
) -> None:
    data = yaml.safe_load(_EXAMPLE.read_text())
    target = data
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    path = tmp_path / "branding.yaml"
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(BrandingConfigError):
        load_branding_config(path)


def test_loader_rejects_a_sponsor_class_the_neutral_css_does_not_compile(
    tmp_path: Path,
) -> None:
    data = yaml.safe_load(_EXAMPLE.read_text())
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
    path = tmp_path / "branding.yaml"
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(BrandingConfigError, match="class_name"):
        load_branding_config(path)
