"""Validated public branding loaded from a distribution-owned YAML file."""

from __future__ import annotations

import posixpath
import re
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import unquote, urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError, field_validator

if TYPE_CHECKING:
    from pathlib import Path


class _BrandingModel(BaseModel):
    """Strict base for the versioned public branding contract."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


NonEmptyString = Annotated[str, Field(min_length=1)]
EnvironmentVariable = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
Digits = Annotated[str, Field(pattern=r"^\d*$")]
PublicToken = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]*$")]
StorageKeyPrefix = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:-]+$")]
SponsorClassName = Annotated[
    str,
    Field(pattern=r"^(?:h-(?:8|10|12|14|16))(?: sm:h-(?:8|10|12|14|16))?$"),
]

_SITE_ASSET_PREFIX = "/site-assets/"
_SITE_ASSET_EXTENSIONS = {".avif", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _absolute_url_or_empty(value: str, *, schemes: set[str]) -> str:
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        # Accessing port performs the range/numeric validation that urlsplit
        # deliberately defers. Use hostname rather than netloc so malformed
        # authority-only values cannot pass the public URL contract either.
        _ = parsed.port
        valid = parsed.scheme in schemes and bool(parsed.hostname)
    except ValueError:
        valid = False
    if not valid or any(char.isspace() for char in value) or "\\" in value:
        expected = " or ".join(sorted(schemes))
        raise ValueError(f"must be empty or an absolute {expected} URL")
    return value


def validate_public_https_url(value: str) -> str:
    """Return an empty or public HTTPS URL, raising for unsafe values."""
    return _absolute_url_or_empty(value, schemes={"https"})


def _asset_url(value: str) -> str:
    if not value:
        return value
    if not value.startswith(_SITE_ASSET_PREFIX):
        return validate_public_https_url(value)

    parsed = urlsplit(value)
    raw_relative = parsed.path.removeprefix(_SITE_ASSET_PREFIX)
    if _INVALID_PERCENT_ESCAPE.search(parsed.path):
        raise ValueError("contains an invalid percent escape")
    try:
        relative = unquote(raw_relative, errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("contains invalid UTF-8 escaping") from exc

    parts = relative.split("/")
    if (
        not relative
        or "\0" in relative
        or "\\" in relative
        or any(not part or part in {".", ".."} or part.startswith(".") for part in parts)
        or posixpath.splitext(relative)[1].lower() not in _SITE_ASSET_EXTENSIONS
    ):
        raise ValueError("must name an image served by /site-assets")
    return value


class BrandingOrganization(_BrandingModel):
    """Organization named by a distribution."""

    name: str
    url: str
    tagline: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """Allow no organization link or a public HTTPS link."""
        return validate_public_https_url(value)


class BrandingLinks(_BrandingModel):
    """Public external links rendered by the console."""

    docs_url: str
    status_url: str
    github_url: str

    @field_validator("docs_url", "status_url", "github_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """Allow hidden links or public HTTPS links."""
        return validate_public_https_url(value)


class BrandingExample(_BrandingModel):
    """Copy-paste example shown on the public landing page."""

    api_base: str
    api_key_env_var: EnvironmentVariable
    model: NonEmptyString

    @field_validator("api_base")
    @classmethod
    def validate_api_base(cls, value: str) -> str:
        """Allow a hidden example or an absolute HTTP(S) API base."""
        return _absolute_url_or_empty(value, schemes={"http", "https"})


class BrandingAnalytics(_BrandingModel):
    """Public identifiers for optional client-side analytics."""

    statcounter_project_id: Digits
    statcounter_security_key: PublicToken


class BrandingSignup(_BrandingModel):
    """Public signup widget and affiliated-domain presentation values."""

    turnstile_site_key: PublicToken
    fast_track_domain: str
    fast_track_org: str


class BrandingAssets(_BrandingModel):
    """Same-origin or public URLs for distribution-owned site assets."""

    logo_url: str
    favicon_url: str

    @field_validator("logo_url", "favicon_url")
    @classmethod
    def validate_asset_url(cls, value: str) -> str:
        """Keep runtime assets same-origin or on HTTPS origins."""
        return _asset_url(value)


class BrandingTeamMember(_BrandingModel):
    """One public team profile."""

    name: NonEmptyString
    affiliations: list[NonEmptyString]
    badge: NonEmptyString | None = None
    image: NonEmptyString | None = None
    website: str | None = None

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str | None) -> str | None:
        """Validate an optional public team image."""
        return _asset_url(value) if value is not None else None

    @field_validator("website")
    @classmethod
    def validate_website(cls, value: str | None) -> str | None:
        """Validate an optional public team link."""
        return validate_public_https_url(value) if value is not None else None


class BrandingSponsor(_BrandingModel):
    """One public sponsor image and its presentation metadata."""

    name: NonEmptyString
    alt: NonEmptyString
    src: NonEmptyString
    class_name: SponsorClassName
    width: PositiveInt
    height: PositiveInt

    @field_validator("src")
    @classmethod
    def validate_src(cls, value: str) -> str:
        """Keep sponsor images same-origin or on HTTPS origins."""
        return _asset_url(value)


class BrandingConfig(_BrandingModel):
    """Version 1 distribution-owned public branding document."""

    schema_version: Literal[1]
    app_description: str
    site_host: NonEmptyString
    organization: BrandingOrganization
    links: BrandingLinks
    example: BrandingExample
    analytics: BrandingAnalytics
    signup: BrandingSignup
    storage_key_prefix: StorageKeyPrefix
    data_policy_notice: str
    assets: BrandingAssets
    team: list[BrandingTeamMember]
    sponsors: list[BrandingSponsor]

    def public_payload(self, *, docs_url: str) -> dict[str, Any]:
        """Return the browser-safe contract, applying resolved docs identity."""
        payload = self.model_dump(exclude={"schema_version"}, exclude_none=True)
        payload["links"] = {
            **self.links.model_dump(),
            "docs_url": validate_public_https_url(docs_url),
        }
        return payload


class BrandingConfigError(Exception):
    """Raised when a declared branding document cannot be loaded."""


def load_branding_config(path: Path) -> BrandingConfig:
    """Read and validate a versioned public branding YAML document.

    Raises:
        BrandingConfigError: On unreadable files, YAML errors, or schema
            validation failures.
    """
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BrandingConfigError(f"cannot read branding config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BrandingConfigError(f"branding config {path} must be a YAML mapping")
    try:
        return BrandingConfig.model_validate(data)
    except ValidationError as exc:
        raise BrandingConfigError(f"invalid branding config {path}: {exc}") from exc
