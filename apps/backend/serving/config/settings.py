"""Application settings using Pydantic.

This module provides type-safe, validated configuration management.
All environment variables are centralized here for easy tracking and testing.
"""

import ipaddress
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)

# Resource models a provider route can be added under. ``quota`` and
# ``concurrency`` are the stateful kinds RouteWise accounts for; ``on_demand``
# is plain pay-as-you-go.
ROUTE_TYPE_ORDER: tuple[str, ...] = ("on_demand", "quota", "concurrency")
ROUTE_TYPES: frozenset[str] = frozenset(ROUTE_TYPE_ORDER)


def parse_provider_route_types(raw: str) -> dict[str, frozenset[str]]:
    """Parse ``PROVIDER_ROUTE_TYPES`` into ``{provider: allowed route types}``.

    The value is a comma-separated list of ``provider=type[|type]`` entries. A
    malformed entry or an unknown route type raises ``ValueError`` naming it,
    so a typo fails startup instead of silently leaving that provider
    unrestricted.
    """
    policy: dict[str, frozenset[str]] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        provider, sep, types = entry.partition("=")
        provider = provider.strip()
        allowed = frozenset(kind.strip() for kind in types.split("|") if kind.strip())
        if not sep or not provider or not allowed:
            raise ValueError(
                f"PROVIDER_ROUTE_TYPES entry {entry!r} must look like "
                "provider=type or provider=type|type"
            )
        unknown = sorted(allowed - ROUTE_TYPES)
        if unknown:
            raise ValueError(
                f"PROVIDER_ROUTE_TYPES entry {entry!r}: unknown route type "
                f"{', '.join(unknown)}; expected one of {', '.join(ROUTE_TYPE_ORDER)}"
            )
        policy[provider] = allowed
    return policy


def _parse_networks(
    raw: str, name: str
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse comma-separated CIDR ranges; an invalid entry raises, naming *name*."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=True))
        except ValueError as exc:
            raise ValueError(f"{name} entry {entry!r} is not a valid CIDR range: {exc}") from exc
    return tuple(networks)


class Settings(BaseSettings):
    """Application settings with validation and type safety."""

    # Database
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "hybridinference"
    db_user: str = "postgres"
    db_password: str = ""

    # Database privacy settings
    # Default False: prompt, response and request payload are not stored at all
    # (their api_logs columns are written as NULL). Operators can opt in to
    # full-content logging by setting DB_STORE_FULL_CONTENT=true after weighing
    # the privacy impact.
    db_store_full_content: bool = False

    # Admin
    admin_token: str = ""
    admin_emails: str = ""
    # Recipients for signup/registration approval notifications. Comma-separated.
    # When empty, falls back to admin_emails so existing deployments are
    # unaffected. Set this to notify a subset of admins (or a shared inbox)
    # without changing who holds the admin role.
    signup_notify_emails: str = ""
    user_auth_enabled: bool = True
    api_key_secret: str = ""

    # Erasure fence (issue #1421): dedicated, stable secret used to derive
    # non-reversible erasure-fence digests. MUST NOT be changed after
    # hard-delete operations have been performed: changing it invalidates
    # existing fences, which would allow a previously-erased account's logs
    # to be written again (a privacy regression). Set this to a long-term
    # stable value; if unset, API_KEY_SECRET is used as a fallback (with a
    # startup warning) so single-secret deployments still get fence coverage.
    erasure_fence_secret: str = ""
    # Keep hard-delete disabled until every api_logs writer in the deployment
    # participates in the fence protocol. This is intentionally opt-in for
    # the release that introduces the application-level fence: an older worker
    # can still insert directly and bypass an advisory-lock convention.
    erasure_fence_protocol_ready: bool = False

    # JWT (required in production)
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 30

    # Cookie
    cookie_secure: bool = True
    cookie_domain: str | None = None
    cookie_samesite: str = "lax"
    # Host-only cookies are still shared by every port on the same hostname.
    # A second local deployment can choose a distinct name without changing
    # the production-compatible default.
    refresh_token_cookie_name: str = "refresh_token"

    # Signup
    signup_enabled: bool = True
    # Deprecated: only used as fallback when no user_daily_quota_<role>
    # runtime setting is registered. Per-role quotas (user_daily_quota_free,
    # _pro, _internal, _admin) are the source of truth at signup.
    signup_default_daily_quota_usd: float = 100.00
    signup_require_email_verification: bool = True
    # Email the configured recipients when a new user registers and needs
    # approval. Set SIGNUP_ADMIN_NOTIFY_ENABLED=false to suppress these.
    signup_admin_notify_enabled: bool = True

    # Rate limiting
    signup_rate_limit_per_hour: int = 5
    signup_rate_limit_per_day: int = 10
    # Per-email window (default 5 attempts / 15 min) and per-IP window
    # (default 20 attempts / hour). The login limiter records attempts on
    # entry so probing varied passwords cannot bypass the limit.
    login_rate_limit_per_15min: int = 5
    login_rate_limit_per_hour_per_ip: int = 20
    # Auto-block a source IP at the API-key auth layer after repeated auth
    # failures. Once an IP (IPv6 bucketed to /64) reaches
    # auth_failure_block_threshold failures within auth_failure_block_window_sec,
    # it is refused for auth_failure_block_duration_sec. In-memory and
    # per-process, like the login/signup limiters above. Defaults: 200 failures
    # in a day → blocked for a day.
    auth_failure_block_enabled: bool = True
    auth_failure_block_threshold: int = 200
    auth_failure_block_window_sec: int = 86400
    auth_failure_block_duration_sec: int = 86400
    # Comma-separated IPs or CIDR ranges (e.g. "140.247.173.97,128.103.0.0/16")
    # that are exempt from auth-failure blocking: their failures are never
    # counted and an existing block never applies to them. For trusted shared
    # egress points (campus NAT, office gateways) where one client's stale key
    # would otherwise take every user behind the IP offline. Entries that fail
    # to parse are logged and skipped.
    auth_failure_block_exempt_ips: str = ""
    # Resolve who a rejected API key belongs to when auth fails, so the
    # ``auth_failure`` log record (and any alert built from it) can name the
    # account instead of only a count and an address. Worth having because the
    # keys that fail here are either nobody's -- a scanner's random token -- or a
    # deployment's own monitor, CI job or service account whose credential was
    # rotated, revoked or expired, and only the second is something to go and
    # fix. Costs one indexed lookup per failure, under the shared rejection
    # enrichment budget (REJECTED_ENRICHMENT_MAX_CONCURRENT), so a flood sheds it
    # instantly rather than queueing; the unbounded key lookup that already runs
    # on this path is the larger cost of the two. Set false to spend nothing.
    auth_failure_identify_caller: bool = True

    # Cloudflare Turnstile (signup captcha)
    turnstile_site_key: str = ""
    turnstile_secret_key: str = ""

    # Email (optional)
    smtp_host: str = "smtp.resend.com"
    smtp_port: int = 587
    smtp_user: str = "resend"
    smtp_password: str = ""
    smtp_from_email: str = "noreply@localhost"
    smtp_from_name: str = "HybridInference"

    sms_auth_enabled: bool = False
    sms_provider: Literal["none", "tencent"] = "none"
    sms_default_country_code: str = Field(default="", pattern=r"^(?:[1-9][0-9]{0,2})?$")
    sms_allowed_prefixes: str = ""
    sms_region: str = "ap-guangzhou"
    sms_sdk_app_id: str = ""
    sms_sign_name: str = ""
    sms_template_id: str = ""
    sms_otp_expire_seconds: int = Field(default=300, ge=60, le=600)
    sms_otp_send_cooldown_seconds: int = Field(default=60, ge=60)
    sms_otp_send_per_phone_hour: int = Field(default=5, ge=1)
    sms_otp_send_per_phone_day: int = Field(default=10, ge=1)
    sms_otp_send_per_ip_hour: int = Field(default=20, ge=1)
    sms_otp_send_per_ip_day: int = Field(default=100, ge=1)
    sms_otp_max_attempts: int = Field(default=5, ge=1, le=10)
    sms_otp_verify_per_phone_15min: int = Field(default=10, ge=1)
    sms_otp_verify_per_ip_hour: int = Field(default=30, ge=1)

    # Public base URL of this gateway. Empty by default: an unconfigured
    # deployment has none, and the two consumers already handle that —
    # auth derives it from the request, and alerts treat an unset value as a
    # local run rather than claiming an environment.
    base_url: str = ""

    # Frontend URL (for email links). Must be absolute to be clickable in an
    # email, so the default points at a local console rather than being empty.
    frontend_url: str = "http://localhost:3001"

    # Tencent Cloud credentials are server-only. Temporary CAM credentials may
    # additionally provide a token; no frontend receives any of these values.
    tencent_cloud_secret_id: str = ""
    tencent_cloud_secret_key: str = ""
    tencent_cloud_token: str = ""

    # Qdrant (shared vector database for codebase indexing)
    qdrant_base_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""

    # CORS
    cors_allowed_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3002",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:3002",
        "https://localhost:8443",
        "https://127.0.0.1:8443",
    ]

    # Trusted proxies (for real IP detection). Comma-separated CIDR ranges
    # (e.g. "172.16.0.0/12,10.0.0.0/8") authorized to assert forwarding
    # provenance via X-Forwarded-For / X-Real-IP. When empty (the default),
    # forwarding headers are never trusted. Invalid CIDRs fail startup.
    # NoDecode ensures pydantic-settings does NOT JSON-decode this value,
    # which would fail before our custom parser could split it.
    trusted_proxies: Annotated[str, NoDecode] = ""
    # Private client networks may be authorized separately for direct
    # connections. They are never accepted as forwarded client identities;
    # this opt-in only covers deployments where the socket peer is the client
    # on an RFC1918 or ULA network rather than a shared container proxy.
    trusted_direct_client_networks: Annotated[str, NoDecode] = ""
    # Master switch for forwarding-header interpretation. This is deliberately
    # a validated setting rather than a request-time environment lookup so the
    # trust decision has one immutable configuration source.
    trust_proxy_headers: bool = Field(
        default=False,
        validation_alias=AliasChoices("TRUST_PROXY_HEADERS", "trust_proxy_headers"),
    )
    # Networks explicitly authorized to assert Cloudflare-derived provenance
    # (CF-Connecting-IP). A generic trusted proxy does NOT automatically
    # make a client-supplied CF-Connecting-IP safe — only operators who
    # front this service with Cloudflare should populate this. When empty,
    # CF-Connecting-IP is never trusted regardless of TRUST_CLOUDFLARE_HEADERS.
    trusted_cloudflare_networks: Annotated[str, NoDecode] = ""
    # Additional authority for CF-Connecting-* headers. This never overrides
    # the master trust switch above.
    trust_cloudflare_headers: bool = Field(
        default=False,
        validation_alias=AliasChoices("TRUST_CLOUDFLARE_HEADERS", "trust_cloudflare_headers"),
    )
    # X-Real-IP is a separate, opt-in assertion scheme. X-Forwarded-For is
    # authoritative whenever it is present, even if its chain is unusable.
    trust_x_real_ip: bool = Field(
        default=False,
        validation_alias=AliasChoices("TRUST_X_REAL_IP", "trust_x_real_ip"),
    )
    # Parsed forms of the above, validated at startup. Consumers read these
    # rather than re-parsing on every request.
    trusted_proxies_parsed: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    trusted_direct_client_parsed: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    trusted_cloudflare_parsed: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    # Route types the admin console may add per provider, as comma-separated
    # "provider=type[|type]" entries, e.g.
    # "chutes=quota,featherless=concurrency,openrouter=concurrency|on_demand".
    # This is a deployment's contract with its vendors — a request-quota plan
    # here, a concurrency plan there — so it lives here rather than in the
    # code. A provider that is not listed, or an empty value (the default),
    # may be added as any route type.
    provider_route_types: str = ""

    # Enable RouteWise online routing subsystem (per-model opt-in via models.yaml)
    enable_routewise: bool = False

    # Adaptive cap on the gateway's own outbound concurrency against one remote
    # account (serving.adapters.upstream_limiter). One bucket per
    # (provider label, API key); local inference servers are never limited.
    # The limit starts at _initial, drops by one on every upstream 429, and
    # probes upward by one every _probe_success_interval *successful (HTTP 200)
    # responses*, staying within [1, _max]. Only a 200 advances that counter —
    # an error is not evidence the provider has headroom. A request that finds
    # its bucket full waits up to _acquire_timeout_sec for a slot before failing
    # over to another endpoint, so the value should stay well under the
    # client-facing request timeout.
    upstream_concurrency_enabled: bool = True
    upstream_concurrency_initial_limit: int = Field(default=8, ge=1)
    upstream_concurrency_max_limit: int = Field(default=64, ge=1)
    upstream_concurrency_probe_success_interval: int = Field(default=100, ge=1)
    upstream_concurrency_acquire_timeout_sec: float = Field(default=30.0, gt=0.0)

    # Slack alerting (optional). Empty SLACK_WEBHOOK_URL disables the feature
    # entirely — no scheduler job is registered and no errors are raised.
    slack_webhook_url: str = ""
    failed_request_alert_threshold: int = Field(default=200, ge=0)
    failed_request_alert_window_minutes: int = Field(default=5, ge=1)
    failed_request_alert_cooldown_minutes: int = Field(default=5, ge=0)
    # Failing fraction of a window that alerts on its own, independent of the
    # absolute threshold above. That threshold is ~57,600 failures/day at its
    # defaults, so a sustained low-rate defect never reaches it -- the routing
    # IndexError in #1361 ran at ~1% of all traffic for 18 days and never fired.
    # 0.0 disables the rate rule. The default is deliberately well above the
    # current production baseline so merging this pages nobody; tighten it (0.02
    # is a reasonable target) once the standing failure sources are cleared.
    failed_request_alert_rate: float = Field(default=0.10, ge=0.0, le=1.0)
    failed_request_alert_rate_min_count: int = Field(default=10, ge=0)

    # Config file paths. Canonical env names are MODELS_CONFIG_PATH /
    # ROUTING_CONFIG_PATH; the legacy MODELS_CONFIG / ROUTING_CONFIG names stay
    # honored, with the canonical name winning when both are set. The field
    # name itself is the last alias so Settings(models_config_path=...) works
    # in tests. Empty string means "not configured": callers warn when an
    # explicit override points at a missing file, but skip the config/*.yaml
    # defaults silently.
    models_config_path: str = Field(
        default="",
        validation_alias=AliasChoices("MODELS_CONFIG_PATH", "MODELS_CONFIG", "models_config_path"),
    )
    routing_config_path: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ROUTING_CONFIG_PATH", "ROUTING_CONFIG", "routing_config_path"
        ),
    )
    # Distribution manifest (serving.config.distribution). Empty path = pure
    # legacy behavior. Mode "dark" (the default) loads and validates the
    # manifest and logs what would change while current resolution stays
    # effective; applying manifest paths requires an explicit
    # DISTRIBUTION_CONFIG_MODE=active. Values are case-insensitive; anything
    # else degrades to "dark" with a warning — neither a typo nor a missing
    # mode can ever activate the manifest.
    distribution_config_path: str = Field(
        default="",
        validation_alias=AliasChoices("DISTRIBUTION_CONFIG_PATH", "distribution_config_path"),
    )
    distribution_config_mode: str = Field(
        default="dark",
        validation_alias=AliasChoices("DISTRIBUTION_CONFIG_MODE", "distribution_config_mode"),
    )

    # Alerting framework
    alerts_enabled: bool = Field(default=False, alias="ALERTS_ENABLED")
    slack_alerts_webhook_url: str = Field(default="", alias="SLACK_ALERTS_WEBHOOK_URL")
    alerts_config_path: str = Field(
        default="config/alerts.yaml",
        validation_alias=AliasChoices("ALERTS_CONFIG_PATH", "alerts_config_path"),
    )

    @field_validator(
        "trusted_proxies", "trusted_cloudflare_networks", "trusted_direct_client_networks"
    )
    @classmethod
    def _check_networks(cls, value: str, info: ValidationInfo) -> str:
        """Refuse an entry that is not a CIDR range, as an error of its own field.

        Each field accepts comma-separated CIDR strings; an empty string is
        valid (nothing trusted). An invalid entry raises so the operator is
        notified at startup rather than silently weakening identity resolution,
        and naming the field lets a valid stored value stand in for it (see
        :func:`get_settings`).
        """
        _parse_networks(value, info.field_name or "network")
        return value

    @model_validator(mode="after")
    def _parse_trusted_proxies(self) -> "Settings":
        """Parse trusted client, proxy, and Cloudflare network CIDRs.

        Every entry has passed :meth:`_check_networks`. The rules between the
        trust settings (:meth:`broken_rules`) apply here, except inside
        :func:`rules_deferred`: the database may still replace either side of
        a rule, so the configuration in force is judged once it is known.
        """
        proxy_networks = _parse_networks(self.trusted_proxies, "trusted_proxies")
        cf_networks = _parse_networks(
            self.trusted_cloudflare_networks, "trusted_cloudflare_networks"
        )
        direct_networks = _parse_networks(
            self.trusted_direct_client_networks, "trusted_direct_client_networks"
        )
        object.__setattr__(self, "trusted_proxies_parsed", proxy_networks)
        object.__setattr__(self, "trusted_direct_client_parsed", direct_networks)
        object.__setattr__(self, "trusted_cloudflare_parsed", cf_networks)
        broken = self.broken_rules()
        if broken and not _deferring_rules.get():
            raise ValueError(next(iter(broken)))
        return self

    def broken_rules(self) -> dict[str, tuple[str, ...]]:
        """Return each rule between settings these values break, with the fields it ties.

        Keyed by the rule's message. Empty when the values satisfy every rule.
        """
        broken: dict[str, tuple[str, ...]] = {}
        if self.trust_cloudflare_headers and not self.trust_proxy_headers:
            broken["TRUST_CLOUDFLARE_HEADERS requires TRUST_PROXY_HEADERS"] = (
                "trust_cloudflare_headers",
                "trust_proxy_headers",
            )
        if self.trust_cloudflare_headers and not self.trusted_cloudflare_parsed:
            broken["TRUST_CLOUDFLARE_HEADERS requires TRUSTED_CLOUDFLARE_NETWORKS"] = (
                "trust_cloudflare_headers",
                "trusted_cloudflare_networks",
            )
        if self.trust_x_real_ip and not self.trust_proxy_headers:
            broken["TRUST_X_REAL_IP requires TRUST_PROXY_HEADERS"] = (
                "trust_x_real_ip",
                "trust_proxy_headers",
            )
        return broken

    @model_validator(mode="after")
    def _alerts_webhook_fallback(self) -> "Settings":
        """Fall back to existing SLACK_WEBHOOK_URL when SLACK_ALERTS_WEBHOOK_URL unset."""
        if not self.slack_alerts_webhook_url and self.slack_webhook_url:
            object.__setattr__(self, "slack_alerts_webhook_url", self.slack_webhook_url)
        return self

    # Debug: when True, log full request payloads (including user prompts) at
    # DEBUG level in the OpenAI-compatible adapter. Defaults to False to avoid
    # leaking user content into logs in production.
    log_full_payload: bool = False

    class Config:
        """Pydantic configuration for Settings class."""

        env_file = ".env"
        case_sensitive = False
        # Allow extra fields for forward compatibility
        extra = "ignore"

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def parse_cors_allowed_origins(cls, value):
        """Accept either a list or a comma-separated env var for CORS origins."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("provider_route_types")
    @classmethod
    def validate_provider_route_types(cls, value: str) -> str:
        """Reject a route-type policy the admin console could not enforce."""
        parse_provider_route_types(value)
        return value

    def validate_auth_secrets(
        self, *, database_enabled: bool, user_auth_enabled: bool | None = None
    ) -> None:
        """Reject missing secrets before starting or enabling authentication.

        Database-backed deployments expose login and API-key management even
        when inference API-key authentication is disabled. Only a database-free,
        explicitly auth-disabled gateway can run without either secret.

        Raises:
            ValueError: If a required secret is empty or whitespace-only. The
                message names missing settings, never their values.
        """
        auth_enabled = self.user_auth_enabled if user_auth_enabled is None else user_auth_enabled
        if not database_enabled and not auth_enabled:
            return

        missing = [
            name
            for name, value in (
                ("JWT_SECRET_KEY", self.jwt_secret_key),
                ("API_KEY_SECRET", self.api_key_secret),
            )
            if not value.strip()
        ]
        if missing:
            raise ValueError(
                "Authentication configuration incomplete: set "
                + ", ".join(missing)
                + " to non-blank values before starting the gateway or enabling user authentication."
            )


def _declared_alias(field_name: str) -> str | None:
    """Return a field's ``alias``, or the first spelling of its validation alias."""
    info = Settings.model_fields[field_name]
    if info.alias:
        return info.alias
    alias = info.validation_alias
    if isinstance(alias, AliasChoices):
        first = alias.choices[0]
        return first if isinstance(first, str) else None
    return alias if isinstance(alias, str) else None


def settings_env_name(field_name: str) -> str:
    """Return the canonical environment-variable name of a ``Settings`` field."""
    return _declared_alias(field_name) or field_name.upper()


def settings_init_name(field_name: str) -> str:
    """Return the keyword ``Settings(...)`` accepts for a field.

    A field declared with ``alias=`` takes only its alias, and an ``AliasChoices``
    field its canonical spelling; every other field takes its own name, because
    the environment's case-insensitive matching does not extend to init kwargs.
    """
    return _declared_alias(field_name) or field_name


def _field_named(location: object) -> str | None:
    """Return the field a validation error's location names, by name or by alias."""
    if not isinstance(location, str):
        return None
    if location in Settings.model_fields:
        return location
    folded = location.lower()
    for name, info in Settings.model_fields.items():
        spellings: list[object] = [name, info.alias]
        alias = info.validation_alias
        spellings.extend(alias.choices if isinstance(alias, AliasChoices) else [alias])
        if any(isinstance(spelling, str) and spelling.lower() == folded for spelling in spellings):
            return name
    return None


def invalid_fields(error: ValidationError) -> dict[str, str]:
    """Return each field *error* blames, with its message.

    Empty when any part of the error names no single field: a rule between
    settings, which no one value can be blamed for.
    """
    blamed: dict[str, str] = {}
    for item in error.errors():
        location = item.get("loc") or ()
        name = _field_named(location[0]) if location else None
        if name is None:
            return {}
        message = str(item.get("msg") or "invalid value").removeprefix("Value error, ")
        blamed.setdefault(name, message)
    return blamed


#: The settings that reach the database. A valid value for any other setting
#: may be stored there; these can never be replaced from it, so an invalid one
#: always stops whoever reads the settings.
CONNECTION_FIELDS = frozenset({"db_host", "db_port", "db_name", "db_user", "db_password"})

#: What :func:`get_settings` set aside: field name -> why its value is not valid.
_invalid_environment: dict[str, str] = {}

#: True inside :func:`rules_deferred`: rules between settings wait.
_deferring_rules: ContextVar[bool] = ContextVar("_deferring_rules", default=False)


@contextmanager
def rules_deferred() -> Iterator[None]:
    """Build ``Settings`` without judging the rules between settings.

    For a caller that judges them itself, with :meth:`Settings.broken_rules`,
    once it knows the configuration in force: the database may replace either
    side of a rule. Every other build applies them.
    """
    deferring = _deferring_rules.set(True)
    try:
        yield
    finally:
        _deferring_rules.reset(deferring)


@lru_cache
def get_settings() -> Settings:
    """Get settings instance.

    This function is cached to return the same instance across the application.
    For testing, use pytest's monkeypatch or clear the cache with get_settings.cache_clear().

    A setting whose environment value is not valid on its own -- a CIDR range
    that does not parse, a number that is not one -- is left at its default and
    logged, not raised: the database-backed configuration may hold a valid
    value for it, and only the gateway's startup can tell, once it has read the
    database. Startup refuses to continue unless it does
    (``app_config.require_valid_environment``); :func:`invalid_environment`
    lists what was set aside. The database connection settings are never set
    aside.

    A rule between settings (``Settings.broken_rules``) is not judged here
    either: the database may replace either side of it, so startup judges it
    against the configuration in force once the database has been read, and
    refuses to continue if that still breaks it. The environment alone breaking
    one says nothing until then.

    Returns:
        Settings: Validated settings object.

    Raises:
        ValidationError: If the environment does not validate even with the
            invalid settings at their defaults.
    """
    _invalid_environment.clear()
    with rules_deferred():
        return _settings_without_invalid_environment()


def _settings_without_invalid_environment() -> Settings:
    """Build ``Settings``, setting aside each environment value that fails on its own."""
    try:
        return Settings()
    except ValidationError as exc:
        error = exc
    set_aside: dict[str, str] = {}
    current = error
    while True:
        blamed = invalid_fields(current)
        if not blamed or not blamed.keys().isdisjoint(set_aside.keys() | CONNECTION_FIELDS):
            raise error
        if any(Settings.model_fields[name].is_required() for name in blamed):
            raise error
        set_aside.update(blamed)
        defaults = {
            settings_init_name(name): Settings.model_fields[name].get_default(
                call_default_factory=True
            )
            for name in set_aside
        }
        try:
            settings = Settings(**defaults)
        except ValidationError as again:
            current = again
            continue
        break
    # The defaults stand in for values nobody chose; they were not explicitly set.
    object.__setattr__(
        settings, "__pydantic_fields_set__", set(settings.model_fields_set) - set(set_aside)
    )
    _invalid_environment.update(set_aside)
    for name, problem in set_aside.items():
        logger.warning(
            "%s in the environment is not valid (%s); its default applies unless the "
            "database holds a valid value",
            settings_env_name(name),
            problem,
        )
    return settings


def invalid_environment() -> dict[str, str]:
    """Return the settings :func:`get_settings` left at their default, with the reason.

    Keyed by field name; empty when the environment validated as it is.
    """
    get_settings()
    return dict(_invalid_environment)


# The shared ``Settings`` instance, as ``from serving.config.settings import
# settings``. Created on first use rather than when this module is imported, so
# importing it never validates the environment: the command-line recovery tools
# (``serving.auth.reset_password``, ``serving.config.manage``) import it and must
# still reach the database when an unrelated setting in the environment is
# invalid. Otherwise it is what it was: the ``get_settings()`` instance, created
# once and kept for the life of the process, and an invalid environment raises
# for whoever reads it first.
settings: Settings


def _shared_settings() -> Settings:
    """Return ``settings``, creating it on first use."""
    shared = globals().get("settings")
    if shared is None:
        shared = globals()["settings"] = get_settings()
    return shared


def __getattr__(name: str) -> Settings:
    if name == "settings":
        return _shared_settings()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class DatabaseSettings(BaseSettings):
    """The database connection settings alone, read the way ``Settings`` reads them.

    For the command-line recovery tools: they connect with these and nothing
    else, so a setting that keeps the gateway from validating its configuration
    cannot keep them from repairing it.
    """

    db_host: str = Settings.model_fields["db_host"].default
    db_port: int = Settings.model_fields["db_port"].default
    db_name: str = Settings.model_fields["db_name"].default
    db_user: str = Settings.model_fields["db_user"].default
    db_password: str = Settings.model_fields["db_password"].default

    # Read like ``Settings``: the same ``.env`` file and case rules.
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


def _split_email_list(raw: str) -> list[str]:
    """Split a comma-separated email string, trimming whitespace and blanks.

    Case is preserved: email local-parts may be case-sensitive, so callers that
    deliver mail must not lowercase. Membership checks should lowercase
    separately via :func:`_parse_admin_emails`.
    """
    if not raw:
        return []
    return [e.strip() for e in raw.split(",") if e.strip()]


def _parse_admin_emails(raw: str) -> list[str]:
    """Parse comma-separated admin emails string into a lowercase list."""
    return [e.lower() for e in _split_email_list(raw)]


def is_admin_email(email: str | None) -> bool:
    """Check if the given email is in the admin list.

    An account without an email address (the first-run setup administrator
    signs in with a login name) is never on it.
    """
    if not email or not email.strip():
        return False
    return email.strip().lower() in _parse_admin_emails(_shared_settings().admin_emails)


def get_signup_notify_emails() -> list[str]:
    """Return recipients for signup approval notifications.

    Uses ``signup_notify_emails`` when set, otherwise falls back to
    ``admin_emails`` so that recipients can be narrowed without altering who
    holds the admin role. Recipient casing is preserved for SMTP delivery.
    """
    current = _shared_settings()
    notify = _split_email_list(current.signup_notify_emails)
    if notify:
        return notify
    return _split_email_list(current.admin_emails)


ROLE_RANK: dict[str, int] = {"free": 0, "pro": 1, "internal": 2, "admin": 3}

VALID_ROLES = frozenset(ROLE_RANK)


def has_role(user_role: str, required: str) -> bool:
    """Check if user_role meets or exceeds the required role level.

    Fail-closed: unknown *required* role is treated as rank infinity (never passes).
    Unknown *user_role* is treated as rank 0 (free).
    """
    if required not in ROLE_RANK:
        return False
    return ROLE_RANK.get(user_role, 0) >= ROLE_RANK[required]
